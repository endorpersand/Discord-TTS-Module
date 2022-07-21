import discord
from discord.ext import commands, tasks

import dataclasses
import datetime as dt
import inspect
import json
import re
import textwrap
import typing

from bot import Bot  # ONLY FOR PYLANCE LINTER PURPOSES
from collections import deque
from importlib import reload
from typing import Any, Callable, Optional

# allow hotloading
import helpers.sql
reload(helpers.sql)
import helpers.discord
reload(helpers.discord)
import helpers.voice
reload(helpers.voice)
import helpers.json
reload(helpers.json)

from helpers.sql import Database, UserTable
from helpers.discord import in_vc, multireaction, send_multi
from helpers.voice import AudibleText, ParseEffects, SoxFilter, Voice
from helpers.json import SFEncoder, sf_from_json

# discord.VoiceState = state of member voice, used to query data about other members' vc
# discord.VoiceClient = bot voice channel, 1 per guild where bot is in vc, used to do stuff to bot voice

DEFAULT_VC_TEXT = ("voice-context", "vc", "vc-text")
"""
Channels that are automatically included as voice context channels if found.
"""

@dataclasses.dataclass
class GuildHandler:
    """
    This class handles the bot's `VoiceClient` for a specific guild
    """

    bot: Bot
    db: Database
    guild_id: int

    @property
    def guild(self):
        """
        The guild this guild handler deals with
        """
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            raise ValueError("Bot cannot access this guild")
        
        return guild

    @property
    def voice_client(self) -> Optional[discord.VoiceClient]:
        """
        The voice client of the guild this guild handler deals with
        """
        return self.guild.voice_client  # type: ignore
    
    @property
    def cog(self) -> "TTS": 
        """
        Reference to the TTS cog
        """
        return self.bot.get_cog("TTS") # type: ignore

    @property
    def bot_member(self) -> discord.Member:
        """
        Get the bot as a `discord.Member`
        """
        return self.guild.get_member(self.bot.user.id) # type: ignore

    def __post_init__(self):
        self.tracked_users = self.db.TwoKeyView("tracked_users", (
            "guild_id int",
            "user_id int",
            "timeout datetime"
        ))[self.guild_id]

        self.vc_text = self.db.SetView("vc_text")[self.guild_id]
        if len(self.vc_text) == 0:
            for c in self.guild.channels:
                if isinstance(c, discord.TextChannel) and c.name in DEFAULT_VC_TEXT:
                    self.vc_text.add(c.id)

        self.misc_guild_data = self.db.GuildTable("misc_guild_data", (
            "output_channel int",
        )).get_row(self.guild_id)

        self.queue: "deque[AudibleText]" = deque()

    ### ORIGINAL PROPERTIES ###
    def output_channel(self) -> "discord.VoiceChannel | discord.StageChannel | None":
        """
        Get the current channel the bot is outputting to, or `None` if not outputting to any channel
        """
        n_users = len(self.tracked_users)

        if n_users <= 0:
            self.misc_guild_data["output_channel"] = None
        else:
            chan_ids = ((m := self.guild.get_member(uid)) 
                        and m.voice 
                        and m.voice.channel 
                        and m.voice.channel.id 
                        for uid in self.tracked_users) # m?.voice?.channel?.id
            chan_ids = filter(lambda e: e is not None, chan_ids)
            self.misc_guild_data["output_channel"] = next(chan_ids, None)

        return self.bot.get_channel(self.misc_guild_data["output_channel"])

    ###

    def is_output_channel(self, channel: "discord.VoiceChannel | discord.StageChannel | None") -> bool:
        """
        Returns whether the specified channel is the tracked channel
        If specified channel is `None`, this will always return `False`
        """
        return channel is not None and channel == self.output_channel()

    ### VC MOVEMENT ###
    async def join_channel(self, channel: "discord.VoiceChannel | discord.StageChannel | None"):
        """
        Join the specified voice channel (or disconnect if `None`)
        """
        vc = self.voice_client

        # verify bot can actually join this channel
        if channel is not None:
            perms = channel.permissions_for(self.bot_member)
            if not perms.connect:
                raise commands.UserInputError("I can't connect to VC!")

        # if bot not in VC, connect
        # otherwise, move to the channel
        if vc is None: 
            if channel is not None:
                await channel.connect()
        else:
            if channel is not None:
                await vc.move_to(channel)
            else:
                await self.disconnect()

    async def disconnect(self):
        """
        Disconnect from all voice channels
        """
        vc = self.voice_client
        if vc is not None:
            await vc.disconnect()

    async def join_output_channel(self):
        """
        Join the tracked voice channel.
        """
        try:
            await self.join_channel(self.output_channel())
        except commands.UserInputError:
            # If the code lands here, the bot was not able to join the tracked channel
            # This implies the bot does not have the permissions to

            # Remove everyone in channels the bot cannot access:
            for uid in self.tracked_users.keys():
                member = self.guild.get_member(uid)

                member_vchan = member \
                               and member.voice \
                               and member.voice.channel # member?.voice?.channel
                if member_vchan is None: continue

                if not member_vchan.permissions_for(self.bot_member).connect:
                    self.remove_member(member)

            await self.join_output_channel()
            raise commands.UserInputError("Could not connect to VC, removed tracked users in hidden channels")
    ###

    ### PLAY TEXT ###
    def play_text(self, text, voice=None, extra_phondict: "dict[str, tuple]" = {}):
        """
        Enqueue some text to the audio queue.

        Voice and text substitutions can be specified.
        """
        if voice is None: voice = Voice()
        q = self.queue

        text = self.process_text(text, extra_phondict=extra_phondict)
        if text is None: return

        try:
            q.append(voice.say(text))
        except ValueError as e:
            raise commands.UserInputError(str(e))

        vc = self.voice_client
        if vc is not None and not vc.is_playing(): 
            q.popleft().play_in(vc, after=lambda e: self.advance_queue())

    def play_text_by(self, text, *, by: discord.abc.Snowflake):
        """
        Play text with the specified user's voice and text substitution settings.
        """
        return self.play_text(text, self.cog.lang_prefs[by.id], self.cog.user_phondict[by.id])

    def advance_queue(self):
        """
        When text is finished being read in the text queue, 
        this function is called to advance onto the next text to be read.
        """

        q = self.queue
        vc = self.voice_client

        if q and vc: 
            q.popleft().play_in(vc, after=lambda e: self.advance_queue())

    @staticmethod
    def _sub(fn: Callable[[int], "Any | None"]) -> Callable[[re.Match[str]], str]:
        """
        Function that converts a `discord.Object` getter function into a match substitution 
        that takes a match to the name of the `discord.Object`.
        """
        def cb(m: re.Match[str]):
            oid = int(m[1])
            dobj = fn(oid)

            if dobj: return f"{dobj.name}"
            return m.string
        return cb
    
    def process_text(self, text: str, *, extra_phondict: "dict[str, tuple]" = {}):
        """
        This function cleans up input text so it can be read out in voice.
        """

        # remove escaping backslashes
        text = re.sub(r"\\([^A-Za-z0-9])", lambda m: m[1], text)

        # remove links
        # https://www.urlregex.com
        text = re.sub(r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+", "", text)

        # replace object mentions with their name 
        text = re.sub(r"<#(\d+)>",   self._sub(self.bot.get_channel), text)
        text = re.sub(r"<@!?(\d+)>", self._sub(self.bot.get_user),    text)
        text = re.sub(r"<@&(\d+)>",  self._sub(self.guild.get_role),  text)
        text = re.sub(r"<a?:(\w+?):\d+>", lambda m: m[1], text)

        # replace spoilers with SPOILER

        # not perfect, but w/e
        # anything in ||bars||, failing for backslashes
        text = re.sub(r"\|\|[\S\s]+?\|\|", "SPOILER", text)

        # apply phondict
        phondict = {
            **self.cog.phondict,
            **extra_phondict
        }

        for k, [v, w] in phondict.items():
            if w:
                text = re.sub(r"(?<!\w){0}(?!\w)".format(re.escape(k)), v, text, flags=re.IGNORECASE)
            else:
                text = re.sub(r"((?<=\W){0}(?=\W)|^{0}|{0}$)".format(re.escape(k)), v, text, flags=re.IGNORECASE)

        return text

    ###

    ### PURELY GUILD DATA MODIF ###

    def add_member(self, user: discord.abc.Snowflake):
        """
        Add a member to the list of tracked members
        """

        member = self.guild.get_member(user.id)
        if member is None: raise ValueError(f"Member {user.id} could not be found")

        vchan_id = member \
                   and member.voice \
                   and member.voice.channel \
                   and member.voice.channel.id # member?.voice?.channel?.id

        # fail if user not in vc
        if vchan_id is None:
            raise commands.CheckFailure("You are not in VC!")

        # fail if user not in same vc
        out = self.output_channel()
        if out is not None and vchan_id != out.id:
            raise commands.CheckFailure(f"Cannot bind TTS. You are not in the same VC as {self.bot.user.name}!")
        
        self.clear_timeout(member)

    def remove_member(self, user: Optional[discord.abc.Snowflake]):
        """
        Remove a member from the list of tracked members
        """

        if user is not None: 
            self.tracked_users.pop(user.id, None)

    def set_timeout(self, user: discord.abc.Snowflake, secs: Optional[float]):
        """
        Add a timeout to a user before they are disconnected from TTS.
        """

        timeout = None
        if secs is not None:
            timeout = dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(seconds=secs)

        self.tracked_users[user.id] = (timeout, )
    
    def wipe_phantom_users(self):
        """
        Remove users that have no timeout but aren't in VC
        """

        for uid, *_ in self.db.execute("""
            SELECT user_id 
            FROM tracked_users 
            WHERE guild_id = ? 
            AND timeout IS NULL
        """, (self.guild_id,)).fetchall():
            m = self.guild.get_member(uid)
            if m is not None and not self.is_output_channel(m.voice and m.voice.channel):
                self.remove_member(m)

    def clear_timeout(self, user: discord.abc.Snowflake):
        """
        Remove a member's timeout.
        """
        self.set_timeout(user, None)
    ###

class TTS(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.prepare_data()
        self.check_inactives.start()

    async def cog_check(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    @commands.Cog.listener('on_voice_state_update')
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # check that we're tracking this member & that they did a channel change
        guild: discord.Guild = member.guild
        gh = self.guild_handler(guild)
        
        if member.id not in gh.tracked_users: return # only tracked users
        if before.channel == after.channel: return # only on channel changes

        if len(gh.tracked_users) == 1:
            try:
                await gh.join_channel(after.channel)
            except commands.UserInputError:
                # if user joins inaccessible channel & that channel would be the tracked vc, send an err
                vc_text_chan = next(
                    (c for cid in gh.vc_text if (c := guild.get_channel(cid)) is not None),
                    None
                )
                m = next(
                    (m for uid in gh.tracked_users if (m := guild.get_member(uid)) is not None),
                    None
                )

                gh.remove_member(m)
                await gh.join_output_channel()
                await vc_text_chan.send(f"{m.mention}, I cannot join your VC, so \U0001F44B")
                return

        # cancel dc tasks when user joins tracked channel
        if gh.is_output_channel(after.channel):
            gh.clear_timeout(member)
        else:
            # add task if user leaves tracked channel
            gh.set_timeout(member, 60)

    @tasks.loop(seconds=60)
    async def check_inactives(self):
        if self.db.is_closed(): return

        if self.db.table_exists("tracked_users"):
            self.db.execute("""
                DELETE FROM tracked_users 
                WHERE strftime('%s', timeout) < strftime('%s', 'now')
            """)

        for g in self.bot.guilds:
            gh = self.guild_handler(g)

            await gh.join_output_channel()
            gh.wipe_phantom_users()

    @commands.Cog.listener('on_message')
    async def on_message(self, message: discord.Message):
        # remove filtered text
        content = message.content
        prefixes = self.bot.command_prefix
        if isinstance(prefixes, str):
            if content.startswith(prefixes): return
        elif any(content.startswith(p) for p in prefixes): return

        if content.startswith("#"): return

        author_guild = self.find_tracked_guild(message.author) # find the guild of this user, None if not being tracked

        if author_guild is not None:
            author = author_guild.get_member(message.author.id)
            
            if author.voice is not None: # affirm that they are in VC
                uchan = author.voice.channel
                gh = self.guild_handler(author_guild)

                if not gh.is_output_channel(uchan): return # and are also in the tracked channel

                # accept message if:
                accept_message = (
                    isinstance(message.channel, discord.DMChannel) or # in dms
                    (uchan == message.channel) or # in the VC's text channel
                    message.channel.id in gh.vc_text # in a tracked #voice-context channel
                )

                if accept_message: gh.play_text_by(content, by=message.author)

    def update_sql_tables(self):
        """
        If a table update is necessary, this function should be included to perform the update.
        Otherwise, leave it empty.
        """
        # self.db is accessible here
        pass

    def prepare_data(self):
        """
        Initialize tables.
        """
        self.db = db = Database("tts.db")
        self.update_sql_tables()

        # create tables if missing
        self.lang_prefs: UserTable[Voice] = db.UserTable("lang_pref", (
            'lang text DEFAULT "en"',
            'tld text DEFAULT "com"',
            'pitch real DEFAULT 0',
            'effects text DEFAULT "[]"',
            'use_effects int DEFAULT 0',
            ))\
        .map_values(
            from_sql = lambda t: Voice(*t[:3], json.loads(t[3], object_hook=sf_from_json), t[4]),
            to_sql = lambda v: (v.lang, v.tld, v.pitch, json.dumps(v.effects, cls=SFEncoder), v.use_effects)
        )

        self.guild_handlers = {}

        self.phondict = db.RowView("phondict",  (
            "old text PRIMARY KEY",
            'sub text',
            'ignore_conjs int'
            ))\
        .map_values(
            from_sql = lambda t: (t[0], bool(t[1]))
        )

        self.user_phondict = db.TwoKeyView("user_phondict", (
            "user_id int",
            "old text",
            'sub text',
            'ignore_conjs int'
            ))\
        .map_values(
            from_sql = lambda t: (t[0], bool(t[1]))
        )

    ### INTERFACE WITH GUILD DATA ###

    def guild_handler(self, guild: discord.Guild) -> GuildHandler:
        """
        Get this guild's guild handler
        """
        return self.guild_handlers.setdefault(guild.id, GuildHandler(self.bot, self.db, guild.id))

    async def track_member(self, member: discord.Member):
        """
        Add a user to the list of tracked members
        """
        guild = member.guild
        gh = self.guild_handler(guild)
        gh.add_member(member)

        chan = ((bm := gh.bot_member)
               and bm.voice
               and bm.voice.channel) # bm.voice?.channel
        if chan is None:
            await gh.join_output_channel()

    async def untrack_member(self, member: discord.Member):
        """
        Remove a user from the list of tracked members
        """
        gh = self.guild_handler(member.guild)
        gh.remove_member(member)

        await gh.join_output_channel()

    def find_tracked_guild(self, user: discord.User) -> Optional[discord.Guild]:
        """
        Find guild of a tracked user. If user is not tracked, return None.
        """
        row = self.db.execute("SELECT guild_id FROM tracked_users WHERE user_id = ?", (user.id,)).fetchone()
        if row is None: return None
        return self.bot.get_guild(row[0])
    ###

    def greet(self, ctx: commands.Context):
        """
        Send a greet. "X says hello!"
        """

        guild = typing.cast(discord.Guild, ctx.guild) # cannot be None, since TTS commands should not be called in DMs
        gh = self.guild_handler(guild)

        author = typing.cast(discord.Member, ctx.author)
        author_vchan = author.voice and author.voice.channel # ctx.author.voice?.channel
        if gh.is_output_channel(author_vchan):
            gh.play_text_by(f"{ctx.author.name} says hello!", by=ctx.author)

    async def tts(self, ctx: commands.Context):
        await ctx.send_help("tts")

    tts.__doc__ = """
        When activated (with `tts on`), the bot will repeat anything you type in a voice context
         channel (any channel under `tts channels`, your DMs, or the channel's integrated chat)
        into your current VC.

        If you want the bot to ignore a message you send in a voice context channel, start your message
        with `#`. Example: ```
        # hello! this message is ignored!
        ```

        **# General**
        `]tts on`, `]tts off` to toggle TTS
        `]tts status` to see the guild's current TTS status
        `]tts channels` to query/edit the current voice context channels.
        `]tts skip` to skip the current messsage (if it is going on for too long or is glitched)

        **# User Settings**
        `]tts accent` to query/edit your TTS accent
        `]tts pitch` to adjust your TTS pitch
        `]tts effects` to add effects to your TTS voice

        **# Text Substitutions**
        `]tts subs add/remove` to add text substitutions to your TTS
        `]tts subs global add/remove` to add text substitutions to all TTS
    """
    tts = commands.group(invoke_without_command=True)(tts) # type: ignore

    @tts.command(name="help")
    async def tts_help(self, ctx: commands.Context):
        """
        Sends the help message.
        """
        await self.tts(ctx)

    @tts.command(name="on", aliases=["connect", "join"])
    @commands.check(in_vc)
    async def tts_on(self, ctx):
        """
        Enable TTS.
        """
        await self.track_member(ctx.author)
        await ctx.send(f"Bound TTS to **{ctx.author.mention}**!", allowed_mentions=discord.AllowedMentions.none())
    
    @tts.command(name="off", aliases=["disconnect", "dc", "fuckoff", "leave"])
    async def tts_off(self, ctx):
        """
        Disable TTS.
        """
        await self.untrack_member(ctx.author)
        return await ctx.send(f"Unbound TTS from **{ctx.author.mention}**!", allowed_mentions=discord.AllowedMentions.none())
        
    @tts.command(name="skip")
    async def tts_skip(self, ctx):
        """
        Stop playing the current message. 
        
        This is useful for if the current spoken message is too annoying or long.
        """
        vc = ctx.voice_client
        if vc.is_playing():
            vc.stop()

    @tts.group(name="voice", invoke_without_command=True)
    async def tts_voice(self, ctx: commands.Context):
        """
        Get your current saved voice settings.

        `]tts voice`: Print your voice.
        `]tts accent [accent]`: Get or set your accent.
        `]tts pitch [pitch]`: Get or set your pitch.
        """

        pref = self.lang_prefs[ctx.author.id]

        accent_name = pref.accent_name
        pitch = pref.pitch
        use_effects = pref.use_effects

        if use_effects:
            pitch_text = f"`0.0` (disable effects to change pitch)"
        elif pitch > 0:
            pitch_text = f"`+{pitch:.1f}`"
        else:
            pitch_text = f"`{pitch:.1f}`"

        lines = [
            f"Accent: `{accent_name}`",
            f"Pitch: {pitch_text}",
        ]

        if use_effects:
            lines.append("")
            lines.append("Effects:")
            lines.append(self.display_effects(pref, False))
        
        await send_multi(ctx, "\n".join(lines))
    
    @tts_voice.command(name="users")
    async def tts_voice_users(self, ctx):
        """
        DEBUG.
        """
        await ctx.send("\n".join(f"{k}: {v}" for k, v in self.lang_prefs.items()))

    def display_phondict_item(self, old, new, ignore_conj):
        return f"""`{old}` => `{new}`{"" if ignore_conj else " (incl. conjugations)"}"""
    def display_phondict(self, phondict):
        return "\n".join(self.display_phondict_item(k,v,w) for k, [v, w] in phondict.items())

    async def modify_phondict_prompt(self, ctx, phondict: dict, old: str, new: str):
        voice: Voice = self.lang_prefs[ctx.author.id]
        voice.say(f"{new}").build_audio()
        with open(AudibleText.AUDIO_PATH2, "rb") as f:
            msg = await ctx.send(f"Is this good? (`{old}` => `{new}`)", file=discord.File(f, filename="sample.mp3"))
        
        YES, NO = "\u2705", "\u274C"
        rxn, _ = await multireaction(self.bot, msg, [YES, NO], allowed_users=[ctx.author.id], timeout=60)
        if rxn is None:
            return await ctx.send("Cancelled prompt")
        elif rxn.emoji == YES:
            msg = await ctx.send(f"Include conjugations? ({old}er, {old}ing, {old}ed, etc.)")
            rxn, _ = await multireaction(self.bot, msg, [YES, NO], allowed_users=[ctx.author.id], timeout=60)
            if rxn is None:
                return await ctx.send("Cancelled prompt")
            else:
                phondict[old] = [new, rxn.emoji != YES]
                await ctx.send(f"Registered `{old}` => `{new}`!")
        else:
            return await ctx.send("Cancelled prompt")

    @tts.group(name="accent", aliases=["lang"], invoke_without_command=True)
    async def tts_accent(self, ctx, *, new_accent=None):
        """
        Get or set your current accent.
        The accent determines, well, *the accent*, as well as the way words are spoken. 
        No certainty about whether accents not labeled `English` can speak English well.

        `]tts accent`: Print your accent.
        `]tts accent <accent>`: Set your accent.
        """

        if new_accent == None:
            return await self.tts_voice(ctx)
        else:
            return await self.tts_accent_set(ctx, new_accent=new_accent)
    
    @tts_accent.command(name="set")
    async def tts_accent_set(self, ctx, *, new_accent):
        """
        Longhand for setting your accent

        All valid accents are in `]tts accent list`.
        """
        acc = None
        try:
            acc = Voice.from_name(new_accent)
        except ValueError as e:
            raise commands.BadArgument(str(e))

        self.lang_prefs[ctx.author.id] = acc

        self.greet(ctx)
        return await self.tts_voice(ctx)
    
    @tts_accent.command(name="list")
    async def tts_accent_list(self, ctx):
        """
        Get the list of valid accent aliases. (warning: slightly spammy)
        """
        acc_list = sorted(Voice.all_accent_aliases(), key=lambda v: (not v.startswith("English"), v))
        await ctx.send("```\n" + "\n".join(acc_list) + "```")

    @tts.group(name="pitch", invoke_without_command=True)
    async def tts_pitch(self, ctx, new_pitch: float = None):
        """
        Get or set your voice's pitch.

        `]tts pitch`: Print your pitch.
        `]tts pitch <pitch>`: Set your pitch.
        """

        if new_pitch == None:
            return await self.tts_voice(ctx)
        else:
            return await self.tts_pitch_set(ctx, new_pitch=new_pitch)
    
    def edit_voice(self, ctx: commands.Context, greet=True, **kwargs):
        prefs: Voice = self.lang_prefs[ctx.author.id]
        self.lang_prefs[ctx.author.id] = prefs.copy(**kwargs)
        if greet: self.greet(ctx)

    def edit_voice_method(self, ctx: commands.Context, greet=True, *, modify: "Callable[[Voice], dict[str, Any]]"):
        prefs: Voice = self.lang_prefs[ctx.author.id]
        self.lang_prefs[ctx.author.id] = prefs.copy(**modify(prefs))
        if greet: self.greet(ctx)

    @tts_pitch.command(name="set")
    async def tts_pitch_set(self, ctx, new_pitch: float):
        """
        Set your TTS voice's pitch
        """
        self.edit_voice(ctx, pitch=new_pitch)
        return await self.tts_voice(ctx)

    @staticmethod
    def display_effects(voice: Voice, show_status: bool = True):
        enabled_str = "enabled" if voice.use_effects else "disabled"
        filters = voice.effects

        filter_lines = [f"{i}: `{f}`" for i, f in enumerate(filters, start=1)]
        filter_str = "\n".join(filter_lines)

        if show_status:
            return "\n".join((
                f"Voice effects: **{enabled_str}**",
                filter_str,
            ))
        else:
            return filter_str

    @staticmethod
    def make_filter(effect: str, args: str):
        try:
            parsed_args = [*ParseEffects(args)]
        except ValueError as e:
            raise commands.BadArgument(f"{e}")

        try:
            sf = SoxFilter(effect, parsed_args)
        except Exception as e:
            raise commands.BadArgument(f"{e}")

        try:
            sf.test()
        except Exception as e:
            raise commands.BadArgument(f"Invalid filter `{sf}`. {e}")
        
        return sf

    @tts.group(name="effects", invoke_without_command=True, aliases=["effect", "filter", "filters"])
    async def tts_effects(self, ctx: commands.Context, effect: str = None):
        """
        Customize filters for your voice.

        `]tts effects`: Display currently enabled effects.
        `]tts effects <effect>`: Get information on an effect.
        """
        if effect is None:
            prefs: Voice = self.lang_prefs[ctx.author.id]
            await send_multi(ctx, self.display_effects(prefs))
        else:
            await self.tts_effects_help(ctx, effect)

    @tts_effects.command(name="on", aliases=["enable"])
    async def tts_effects_on(self, ctx: commands.Context):
        """
        Enable effects.
        """
        self.edit_voice(ctx, False, use_effects=True)

        await self.tts_effects(ctx)

    @tts_effects.command(name="off", aliases=["disable"])
    async def tts_effects_off(self, ctx: commands.Context):
        """
        Disable effects.
        """
        self.edit_voice(ctx, False, use_effects=False)

        await self.tts_effects(ctx)

    @tts_effects.command(name="toggle")
    async def tts_effects_toggle(self, ctx: commands.Context):
        """
        Toggle effects.
        """
        self.edit_voice_method(ctx, False, modify=lambda prefs: {"use_effects": not prefs.use_effects})

        await self.tts_effects(ctx)

    @tts_effects.command(name="list")
    async def tts_effects_list(self, ctx: commands.Context):
        """
        Get a list of allowed filters.
        """
        lst = ", ".join(f"`{s}`" for s in SoxFilter.valid_filters)
        await ctx.send(textwrap.dedent(f"""
            **Allowed filters**: {lst}

            To get a description of a filter, `{ctx.prefix}{ctx.command.parent} help [effect]`
        """))
        
    @tts_effects.command(name="help", aliases=["info", "what"])
    async def tts_effects_help(self, ctx: commands.Context, effect: str):
        """
        Get information on a filter.
        """
        try:
            tf = SoxFilter.get_filter(effect)
        except ValueError:
            return
        
        sig = inspect.signature(tf)
        sig = sig.replace(
            parameters = [*sig.parameters.values()][1:]
        )
        doc = inspect.cleandoc(tf.__doc__)

        await send_multi(ctx, (
            f"**{effect}**{sig}\n"
            f"    {doc}"
        ))

    @tts_effects.command(name="add")
    async def tts_effects_add(self, ctx: commands.Context, effect: str, *, args = ""):
        """
        Add a filter.

        A filter may accept (or require) parameters, check `]tts effects [effect]` to see the filter's parameters.
        Parameters are separated by spaces.

        Examples:
        `]tts effects add echo 0.8 0.9 4 [100, 200, 300, 400] [1, 0.8, 0.6, 0.4]` (4 echos)
        `]tts effects add bass 20 10000` (**warning**: don't do this one unless you want your friends to hate you)
        """

        sf = self.make_filter(effect, args)
        self.edit_voice_method(ctx, False, modify=lambda v: {
            "effects": [*v.effects, sf],
            "use_effects": True
        })

        await self.tts_effects(ctx)
        
    @tts_effects.command(name="rm", aliases=["remove"])
    async def tts_effects_rm(self, ctx: commands.Context, index: int):
        """
        Remove a filter.

        This command takes an index and removes the filter at that index.
        Check `]tts effects` for effect indexes.
        """
        prefs: Voice = self.lang_prefs[ctx.author.id]

        if 0 <= index - 1 < len(prefs.effects):
            f = prefs.effects.pop(index - 1)
        else:
            raise commands.BadArgument(f"No effect at index {index}")
        
        self.edit_voice(
            ctx, False, 
            effects = prefs.effects,
            use_effects = bool(len(prefs.effects))
        )

        await send_multi(ctx, "\n".join((
            f"Removed `{f}`",
            "",
            "Voice effects:",
            self.display_effects(self.lang_prefs[ctx.author.id], False)
        )))
        
    @tts_effects.command(name="replace")
    async def tts_effects_replace(self, ctx: commands.Context, index: int, effect: str, *, args):
        """
        Replace the filter at index with a new effect.

        Check `]tts effects` for effect indexes.
        """
        prefs: Voice = self.lang_prefs[ctx.author.id]
        
        if 0 <= index - 1 < len(prefs.effects):
            prefs.effects[index - 1] = self.make_filter(effect, args)
        else:
            raise commands.BadArgument(f"No effect at index {index}")

        self.edit_voice(
            ctx, False, 
            effects = prefs.effects,
            use_effects = True
        )
        
        await self.tts_effects(ctx)
        
    @tts_effects.command(name="insert")
    async def tts_effects_insert(self, ctx: commands.Context, index: int, effect: str, *, args):
        """
        Insert the filter at the index, shifting everything down.

        Check `]tts effects` for effect indexes.
        """
        prefs: Voice = self.lang_prefs[ctx.author.id]
        
        if 0 <= index - 1 < len(prefs.effects):
            prefs.effects.insert(index - 1, self.make_filter(effect, args))
        else:
            raise commands.BadArgument(f"Cannot insert at index {index}")

        self.edit_voice(
            ctx, False, 
            effects = prefs.effects,
            use_effects = True
        )
        
        await self.tts_effects(ctx)

    @tts_effects.command(name="swap")
    async def tts_effects_swap(self, ctx: commands.Context, index1: int, index2: int):
        """
        Swap the filters at the two specified indexes.

        Check `]tts effects` for effect indexes.
        """
        prefs: Voice = self.lang_prefs[ctx.author.id]
        
        effects = prefs.effects
        length = len(effects)

        if not (0 <= index1 - 1 < length): raise commands.BadArgument(f"No effect at index {index1}")
        if not (0 <= index2 - 1 < length): raise commands.BadArgument(f"No effect at index {index2}")

        [effects[index1 - 1], effects[index2 - 1]] = [effects[index2 - 1], effects[index1 - 1]]

        self.edit_voice(
            ctx, False, 
            effects = effects,
            use_effects = True
        )
        
        await self.tts_effects(ctx)

    @tts_effects.command(name="clear")
    async def tts_effects_clear(self, ctx: commands.Context):
        """
        Clear all added filters.
        """
        self.edit_voice(ctx, False, effects=[], use_effects=False)
        await ctx.send("Cleared all filters")

    async def remove_phondict_prompt(self, ctx, phondict, key):
        try:
            phondict.pop(key)
        except KeyError:
            await ctx.send(f"No substitution for `{key}` to remove")
        else:
            await ctx.send(f"Removed substitution for `{key}`")

    @tts.group(name="subs", aliases=["sub", "substitutions", "phondict"], invoke_without_command=True)
    async def tts_sub(self, ctx):
        """
        Configure your current text substitutions.

        This allows you to change how specific phrases are pronounced 
        (by replacing them with another phrase that matches the sound you want.)
        """
        phondict = self.user_phondict[ctx.author.id]

        await ctx.send("**User Substitutions**:\n" + self.display_phondict(phondict))
    
    @tts_sub.command(name="add", aliases=["modify", "edit"])
    async def tts_sub_add(self, ctx, old: str, new: str):
        """
        Add or modify a substitution.
        """
        phondict = self.user_phondict[ctx.author.id]

        await self.modify_phondict_prompt(ctx, phondict, old, new)
        
    @tts_sub.command(name="remove", aliases=["rm"])
    async def tts_sub_rm(self, ctx, key: str):
        """
        Remove a substitution.
        """
        phondict = self.user_phondict[ctx.author.id]

        await self.remove_phondict_prompt(ctx, phondict, key)
        

    @tts_sub.group(name="global", invoke_without_command=True)
    async def tts_sub_global(self, ctx):
        """
        View or modify global substitutions.
        """
        phondict = self.phondict

        await ctx.send("**Global Substitutions**:\n" + self.display_phondict(phondict))

        
    @tts_sub_global.command(name="add", aliases=["modify", "edit"])
    async def tts_sub_global_add(self, ctx, old: str, new: str):
        """
        Add or modify a substitution.
        """
        phondict = self.phondict

        await self.modify_phondict_prompt(ctx, phondict, old, new)

    @tts_sub_global.command(name="remove", aliases=["rm"])
    async def tts_sub_global_rm(self, ctx, key: str):
        """
        Remove a substitution.
        """
        phondict = self.phondict

        await self.remove_phondict_prompt(ctx, phondict, key)

    @tts.command(name="status", aliases=["guild"])
    async def tts_status(self, ctx):
        """
        Get the TTS status on the current guild.
        """
        gh = self.guild_handler(ctx.guild)

        string = f"VC bound: {gh.output_channel()}\n" \
                 f"Members bound: {', '.join(f'`{self.bot.get_user(u)}`' for u in gh.tracked_users)}"
        
        await ctx.send(string)

    @tts.group(name="channels", aliases=["context"], invoke_without_command=True)
    async def tts_channels(self, ctx):
        """
        Check which channels are being listened to for messages.
        """
        gh = self.guild_handler(ctx.guild)
        chans = ", ".join(f"<#{c}>" for c in gh.vc_text)
        if chans.strip() == "": chans = None

        await ctx.send(f"VC Text Channels: {chans}")

    @tts_channels.command(name="add")
    async def tts_chan_add(self, ctx, chan: discord.TextChannel):
        """
        Add a channel to the list of voice context channels.
        """
        gh = self.guild_handler(ctx.guild)
        gh.vc_text.add(chan.id)
        await self.tts_channels(ctx)
        
    @tts_channels.command(name="remove", aliases=["rm"])
    async def tts_chan_rm(self, ctx, chan: discord.TextChannel):
        """
        Remove a channel to the list of voice context channels.
        """
        gh = self.guild_handler(ctx.guild)
        gh.vc_text.discard(chan.id)
        await self.tts_channels(ctx)

    @tts.command(name="play")
    async def tts_play(self, ctx, lang, tld, pitch: float, *, text):
        """
        DEBUG. Play text with some language and TLD.
        """
        gh = self.guild_handler(ctx.guild)
        gh.play_text(text, Voice(lang, tld, pitch))

    @tts.command(name="queue")
    async def tts_queue(self, ctx):
        """
        DEBUG. Check what is on queue to be sent.
        """
        gh = self.guild_handler(ctx.guild)
        await ctx.send(f"Queue: {', '.join(str(i) for i in gh.queue)}")

    def cog_unload(self):
        self.db.close()

def setup(bot):
    bot.add_cog(TTS(bot))