import functools
import inspect
import json
import textwrap
from types import FunctionType
import typing
import discord
from discord.ext import commands, tasks

import dataclasses
import datetime as dt
import gtts.lang
import re
import requests
import sox

from bot import Bot  # ONLY FOR PYLANCE LINTER PURPOSES
from collections import deque
from collections.abc import Iterable
from gtts import gTTS
from pathlib import Path
from typing import Any, Callable, ClassVar, NewType, Optional, TypeVar
from core.sql import Database, UserTable
from utils import multireaction, send_multi

# discord.VoiceState = state of member voice, used to query data about other members' vc
# discord.VoiceClient = bot voice channel, 1 per guild where bot is in vc, used to do stuff to bot voice

DEFAULT_VC_TEXT = ("voice-context", "vc", "vc-text")
"""
Channels that are automatically included as voice context channels if found.
"""

CACHE_FOLDER = Path("cache")
"""
Audio files have to be created to temporarily store sent messages. They are saved to this folder.
"""

def in_vc(ctx: commands.Context):
    """
    Command check: Checks that user is currently in VC.

    This check assumes the command is NOT run in a DM channel.
    """
    author = typing.cast(discord.Member, ctx.author)
    vchan = author.voice and author.voice.channel # author.voice?.channel
    
    if vchan is None:
        raise commands.CheckFailure("You are not in VC!")
    return True

T = TypeVar("T")
def any_from(st: "Iterable[T]") -> T:
    """
    Utility function to get any element from an iterable 
    (though, in particular, this function is aimed at `Set`s).
    """
    return next(iter(st))

# For whatever reason, the encoder seems to have a strange encoding order. 
# (Could be because `SoxFilter` is a dataclass? Not sure)

# Because of this, the "__sox_filter__" property may or may not exist 
# and is not a reliable method of searching for JSON objects encoding `SoxFilter`s.

# So, if the decoder finds "__sox_filter__", it'll assume the object is a `SoxFilter`.
# Otherwise, it'll assume it is a `SoxFilter` IFF both "fun" and "args" are present.

# Here's the TypeScript type for `SoxFilter`'s JSON object.
# interface SFObject {
#     "__sox_filter__"?: true,
#     "fun": string,
#     "args": { [s: string]: any }
# }

class SFEncoder(json.JSONEncoder):
    """
    Encodes `SoxFilter`s into JSON.
    
    Usage: `json.dump(data, fp, cls=BoardEncoder)`
    """
    def default(self, obj):
        if isinstance(obj, FunctionType):
            return obj.__name__
        if isinstance(obj, SoxFilter):
            return {
                "__sox_filter__": True,
                "fun": obj.fun.__name__,
                "args": obj.args
            }
        return json.JSONEncoder.default(self, obj)

def sf_from_json(o: "dict[str, Any]") -> "SoxFilter | dict[str, Any]":
    """
    Converts a JSON object into a `SoxFilter`.

    Usage: `json.load(fp, object_hook=sf_from_json)`
    """

    if o.get("__sox_filter__", None) is not None:
        return SoxFilter(o["fun"], o["args"].values())
    elif (
        (fun := o.get("fun", None)) is not None and
        (args := o.get("args", None)) is not None
        ):
        return SoxFilter(fun, args.values())
    return o

class ParseEffects:
    """
    Parser that reads arguments to `tts effects`.

    This class does not verify that arguments provided match the argument types of the sox filter;
    it only converts a string into a list of Python objects.

    However, the sox filter itself verifies the arguments match, so woohoo.

    The grammar of the parser in EBNF-inspired form:
    ```ebnf
    list  = "[" (unit),* "]"
    dict  = "{" (int ":" unit),* "}" # note that only dict[int, *] is valid
    bool  = "true" | "false"
    none  = "none"
    pair  = "(" float "," float ")"
    int   = ? anything parseable as an int ?
    float = ? anything parseable as a float ?
    str   = ? anything else ?

    unit  = ? any token above ?
    ```

    Usage: `[*ParseEffects(argument_string)]`
    """

    # You: Wait. Did you manually write a whole parser JUST to read effect arguments?
    # Me: Yes. And?

    # HACK: Pylance does not support sentinels (i.e. object()), so:
    NoMatchType = NewType("NO_MATCH", object)
    NO_MATCH = NoMatchType(object())
    """
    Sentinel object designating that the next token could not be matched to the given type.
    """

    def __init__(self, arg_str: str):
        norm = "".join(c for c in arg_str.lower() if c.isascii and c.isprintable)

        # the list of tokens consists of "{", "}", "(", ")", "[", "]", ",", ":", 
        # and any words (miscellaneous text separated by spaces)

        # whitespace is ignored.

        self.tokens: "list[str]" = [
            t for s in re.findall(r"\S+", norm) for t in re.split(r"([{}()\[\],:])", s) if t != ""
        ]
        self.cursor = 0
    
    def peek(self):
        """
        Look forward to the next token (but do not advance the cursor).

        If the parser reaches the end of the argument list, return `None`.
        """
        if self.cursor >= len(self.tokens): return None
        return self.tokens[self.cursor]

    def next(self):
        """
        Look forward to the next token (and advance the cursor).

        If the parser reaches the end of the argument list, return `None`.
        """
        val = self.peek()
        self.cursor += 1
        return val

    def matches(self, string: str) -> bool:
        """
        Check if the next token matches a specified string.

        If it does, advance the cursor and return `True`. Otherwise, do nothing and return `False`.
        """
        hit = self.peek() == string
        if hit: self.cursor += 1

        return hit

    def err_at_cursor(self, msg: str):
        """
        Throw a `ValueError` with a given message at the current cursor point.
        """

        if self.cursor >= len(self.tokens):
            l, m = " ".join(self.tokens), "[EOL]"
            raise ValueError(f"{msg}:\n{l}**{m}**")
        else:
            l, m = " ".join(self.tokens[:self.cursor]), self.tokens[self.cursor]

            if self.cursor + 1 == len(self.tokens):
                raise ValueError(f"{msg}:\n{l}**{m}**")

            raise ValueError(f"{msg}:\n{l}**{m}**...")

    def require(self, match_fn: Callable[[], "T | NoMatchType"], expected: str = "") -> T:
        """
        Require that the next token is matched by the specified match function.

        Optionally provide a parameter to note in the error message what token was expected.
        """

        hit = match_fn()

        if isinstance(hit, self.NoMatchType):
            if len(expected) == 0: self.err_at_cursor("Unexpected token")
            self.err_at_cursor(f"Expected {expected} here")
        
        return hit

    def require_str(self, match_str: str):
        """
        Require that the next token matches a specified string.
        """
        hit = self.matches(match_str)

        if not hit:
            self.err_at_cursor(f"Expected `{match_str}` here")
        
        return match_str

    def match_trool(self):
        """
        Match the next token to `true`, `false`, or `none`.
        If the next token does not match, return `NO_MATCH`.
        """
        if self.matches("true"): return True
        elif self.matches("false"): return False
        elif self.matches("none"): return None
        return self.NO_MATCH

    def match_int(self):
        """
        Match the next token as a `int`.
        If the next token cannot be parsed as an `int`, return `NO_MATCH`.
        """
        try:
            v = int(self.peek()) # type: ignore
        except (ValueError, TypeError):
            return self.NO_MATCH
        
        self.next()
        return v
    
    def match_str(self):
        """
        Match the next token as an alphabetic string.
        If the next token cannot be parsed as an alphabetic string, return `NO_MATCH`.
        """
        pk = self.peek()
        if pk is not None and pk.isalpha():
            return self.next()
        return self.NO_MATCH

    def match_float(self):
        """
        Match the next token as a `float`.
        If the next token cannot be parsed as a `float`, return `NO_MATCH`.
        """
        try:
            v = float(self.peek()) # type: ignore
        except (ValueError, TypeError):
            return self.NO_MATCH
        
        self.next()
        return v

    def match_pair(self):
        """
        Match the next few tokens a pair of floats: `(float, float)`.

        If the next token is not `(`, these tokens do not represent a pair. Return `NO_MATCH`.
        If the parameters of the pairs are not floats, the parser will error.
        """
        if self.matches("("):
            f1 = self.require(self.match_float, expected="float")
            self.require_str(",")
            f2 = self.require(self.match_float, expected="float")
            self.require_str(")")

            return typing.cast("tuple[float, float]", (f1, f2))
        return self.NO_MATCH
    
    def match_list(self):
        """
        Match the next few tokens to a list.

        If the next token is not `[`, these tokens do not represent a list. Return `NO_MATCH`.
        """
        if self.matches("["):
            out = []

            unit = self.match_unit()
            if not isinstance(unit, self.NoMatchType): 
                out.append(unit)
                while self.matches(","):
                    unit = self.match_unit()
                    if not isinstance(unit, self.NoMatchType):
                        out.append(unit)
                    else:
                        break
            self.require_str("]")
            return out
        return self.NO_MATCH
    
    def match_entry(self):
        """
        Match the next few tokens to an entry. `int: unit`.

        This match function can return `NO_MATCH` if the next object is not an entry 
        or error if it is partially through parsing an entry and cannot recognize the tokens as an entry.
        """
        key = self.match_int()
        if not isinstance(key, self.NoMatchType):
            key = typing.cast(int, key)

            self.require_str(":")
            value = self.require(self.match_unit, expected="value")
            return (key, value)
        return self.NO_MATCH

    def match_dict(self):
        """
        Matches the next few tokens to a dict.
        """
        if self.matches("{"):
            out = {}

            entry = self.match_entry()
            if not isinstance(entry, self.NoMatchType):
                k, v = entry
                out[k] = v
                while self.matches(","):
                    entry = self.match_entry()
                    if not isinstance(entry, self.NoMatchType):
                        k, v = entry
                        out[k] = v
                    else:
                        break

            self.require_str("}")
            return out
        return self.NO_MATCH
    
    def match_unit(self):
        """
        Matches most types. If it cannot find any match using any of the match functions, 
        then return `NO_MATCH`.
        """
        match_fns = (
            self.match_trool,
            self.match_int,
            self.match_float,
            self.match_str,
            self.match_pair,
            self.match_list,
            self.match_dict
        )

        return next(
            (res for f in match_fns if not isinstance(res := f(), self.NoMatchType)),
            self.NO_MATCH
        )

    def __iter__(self):
        while self.peek() is not None:
            yield self.require(self.match_unit, expected="value")

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
    
@dataclasses.dataclass
class Voice:
    """
    Dataclass holding information about a voice. This includes:
    - `lang` & `tld`: the language type and from which `translate.google.[tld]` (both determine voice accent)
    
    - `use_effects`: 
        - If `True`, use `effects` and ignore `pitch`.
        - If `False`, use `pitch` and ignore `effects`.
    - `pitch`: How pitch-shifted this voice is
    - `effects`: Sox filter effects applied onto this voice
    """

    lang: str
    tld: str
    pitch: float
    effects: "tuple[SoxFilter, ...]"
    use_effects: bool

    valid_accents: "ClassVar[dict[tuple[str, str], tuple[str, ...]]]" = {
        # filter Chinese (Mandarin) in favor for Mandarin (*) below
        **{(k, "com"): (v,) for k, v in gtts.lang.tts_langs().items() if v not in ("Chinese (Mandarin)", )},

        # from documentation
        # some are removed if they match another pair's accent
        # tuple = (main name, *aliases)
        ("en", "com.au"): ("English (Australia)",),
        ("en", "co.uk"):  ("English (UK)",),
        ("en", "com"):    ("English (US)", "English"),
        ("en", "co.in"):  ("English (India)",),
        ("fr", "ca"):     ("French (Canada)",),
        ("fr", "com"):    ("French (France)", "French"),
        ("zh-CN", "com"): ("Mandarin (China Mainland)", "Mandarin"),
        ("zh-TW", "com"): ("Mandarin (Taiwan)",),
        ("pt", "com"):    ("Portuguese (Brazil)", "Portuguese"),
        ("pt", "pt"):     ("Portuguese (Portugal)",),
        ("es", "com"):    ("Spanish (Mexico)", "Spanish"),
        ("es", "es"):     ("Spanish (Spain)",),
    }
    """
    A mapping from `(lang, tld)` pair to that accent's aliases
    """

    # This request of TLDs from Google is given as a list of domains in the format: `.google.tld`
    # Thus, to get the valid *TLDs*, remove the `.google.`
    __resp = requests.get("https://www.google.com/supported_domains")
    allowed_tlds = [tld[8:] for tld in __resp.content.decode('utf-8').splitlines()]
    """
    All `tld`s accepted by Google Translate.
    """

    @classmethod
    def all_accent_aliases(cls):
        for name in cls.valid_accents.values():
            yield from name

    def __init__(
        self, 
        lang: str = "en", 
        tld: str = "com", 
        pitch: float = 0, 
        effects: "Iterable[SoxFilter] | None" = None,
        use_effects: bool = False
    ):
        langs = tuple(k for k, _ in self.valid_accents)
        if lang not in langs:
            raise ValueError(f"Invalid language {lang}")

        if tld not in self.allowed_tlds:
            raise ValueError(f"Invalid TLD {tld}")

        self.lang = lang
        self.tld = tld
        self.pitch = min(max(-12, pitch), 12)
        self.effects = tuple(effects) if effects is not None else ()
        self.use_effects = bool(use_effects)

    @classmethod
    def from_name(cls, name: str, pitch=0):
        """
        Parses a name into a voice with a matching `lang, tld` pair.

        A name can either be an alias or of the form `lang@tld`.
        """

        if "@" in name:
            lang, _, tld = name.partition("@")
            return cls(lang, tld, pitch)

        try:
            pair = next(p for p, n in cls.valid_accents.items() if 
                (isinstance(n, str) and name == n) or
                (isinstance(n, tuple) and name in n)
        )
        except StopIteration:
            raise ValueError("Invalid accent")
        
        return cls(*pair, pitch)

    def say(self, text: str):
        """
        Create an `AudibleText` object that can be read, using this Voice as the voice.
        """
        return AudibleText(text, self)
    
    @property
    def accent_name(self):
        """
        Get this voice's name.

        This is either the first in the list of aliases OR a placeholder text `??? (lang@tld)`.
        """
        acname = self.valid_accents.get((self.lang, self.tld), f"??? ({self.lang}@{self.tld})")
        
        if isinstance(acname, tuple): return acname[0]
        return acname
    
    @functools.cached_property
    def transformer(self) -> sox.Transformer:
        """
        Compute the `sox.Transformer` this voice has after applying all the effects.

        This function assumes this `Voice` was not modified ever (which it SHOULD not be).
        Use `copy` if you want to change a `Voice`.
        """
        # add effects & pitch
        # if effects are present (& enabled), pitch is ignored
        tfm = sox.Transformer()

        if self.use_effects:
            for f in self.effects:
                f.apply(tfm)
        elif self.pitch != 0:
            tfm.pitch(self.pitch)

        return tfm


    def copy(self, **kwargs):
        """
        Copy this `Voice` but modify some parameters.
        """
        dct = dataclasses.asdict(self)
        dct.update(kwargs)

        return Voice(**dct)

@dataclasses.dataclass
class SoxFilter:
    """
    A filter (using Sox) that can be applied to a voice
    """
    
    fun: Callable
    args: "dict[str, Any]"

    valid_filters = (
        "allpass", "bandpass", "bandreject", "bass", "bend", "biquad", "chorus", "compand", 
        "contrast", "dcshift", "deemph", "delay", "downsample", "echo", "echos", "equalizer", 
        "fade", "fir", "flanger", "gain", "highpass", "hilbert", "loudness", "lowpass", 
        "mcompand", "norm", "oops", "overdrive", "pad", "phaser", "pitch", "rate", "remix", 
        "repeat", "reverb", "reverse", "silence", "sinc", "speed", "swap", "tempo", "treble", 
        "tremolo", "trim", "upsample", "vad", "vol"
    )
    """
    A tuple of all supported Sox filters.
    """

    def __init__(self, fun: str, args: list):
        # ex: sox.Transformer.upsample(self, factor: int)
        self.fun: Callable = self.get_filter(fun)

        # verify the function fits the parameters
        # get the parameters (ignoring the self parameter)
        bound_args = inspect.signature(self.fun).bind(None, *args)
        bound_args.apply_defaults()
        self.args = dict(bound_args.arguments)
        self.args.pop("self", None)
    
    @classmethod
    def get_filter(cls, fil: str) -> Callable:
        """
        Get the function version of a filter given its name
        """
        if fil not in cls.valid_filters: raise ValueError(f"Unrecognized filter `{fil}`")

        return getattr(sox.Transformer, fil)

    def test(self):
        """
        Check if this filter is valid (i.e. does not have argument type errors)
        """
        self.apply(sox.Transformer())

    def apply(self, tf: sox.Transformer):
        """
        Apply filter to a given `Transformer`.
        """
        self.fun(tf, **self.args)
    
    def __str__(self):
        params = ", ".join(f"{p}={v!r}" for p, v in self.args.items())
        return f"{self.fun.__name__}({params})"

class AudibleText(gTTS):
    """
    Class that converts text into output audio
    """

    AUDIO_PATH1 = CACHE_FOLDER / 'gtts_out.mp3'
    AUDIO_PATH2 = CACHE_FOLDER / 'sox_out.mp3'

    def __init__(self, text, voice=Voice()):
        self.voice = voice
        super().__init__(text, lang=voice.lang, tld=voice.tld)
    
    def __repr__(self):
        return f"AudibleText({repr(self.text)}, voice={self.voice})"
    
    def build_audio(self):
        self.save(self.AUDIO_PATH1)
        self.voice.transformer.build(
            input_filepath=str(self.AUDIO_PATH1), 
            output_filepath=str(self.AUDIO_PATH2)
        )

    def play_in(self, vc: discord.VoiceClient, *, after: Callable = None):  # type: ignore
        self.build_audio()

        audio = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(str(self.AUDIO_PATH2)))
        vc.play(audio, after=after)

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

                await vc_text_chan.send(f"{m.mention}, I cannot join your VC, so bye")
                gh.remove_member(m)
                await gh.join_output_channel()

        # cancel dc tasks when user joins tracked channel
        if gh.is_output_channel(after.channel):
            gh.clear_timeout(member)
        else:
            # add task if user leaves tracked channel
            gh.set_timeout(member, 60)

    @tasks.loop(seconds=60)
    async def check_inactives(self):
        if self.db.is_closed(): return

        self.db.execute("""DELETE FROM tracked_users WHERE strftime('%s', timeout) < strftime('%s', 'now')""")
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
        bot = self.bot

        self.db = db = bot.Database("tts.db")
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