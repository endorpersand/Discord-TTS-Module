import discord
from discord.ext import commands

import asyncio
from asyncio import sleep
from io import BytesIO, StringIO
from typing import Optional

async def temporary_reaction(ctx: commands.Context, emoji, secs):
    """
    Reaction that shows up for a temporary amount of time
    """
    await ctx.message.add_reaction(emoji)
    await sleep(secs)
    await ctx.message.remove_reaction(emoji, ctx.bot.user)

async def multireaction(bot: commands.Bot, msg: discord.Message, emojis: "list[discord.Emoji | discord.PartialEmoji | str]", *, allowed_users: "list[int]" = None, check = None, timeout = None) -> "tuple[Optional[discord.Reaction], Optional[discord.User]]":
    """
    Multiple reactions that record the first clicked reaction of the reactions (This is based off which reaction a user adds to)
    """

    def c(rxn, user):
        return rxn not in emojis \
           and user != bot.user \
           and (user.id in allowed_users if allowed_users else True) \
           and rxn.message == msg \
           and (check(rxn, user) if callable(check) else True)

    already_emoji: "list[discord.Emoji]" = [r.emoji for r in msg.reactions]
    for emoji in emojis:
        if emoji not in already_emoji:
            await msg.add_reaction(emoji)

    try:
        result = await bot.wait_for("reaction_add", check=c, timeout=timeout)
        return result
    except asyncio.TimeoutError:
        return (None, None)

async def send_long(ctx: commands.Context, msg: str, warn=""):
    """
    Sends a message, or a file if msg is too long
    """
    if len(msg) <= 2000:
        return await ctx.send(msg)
        
    outfile = discord.File(StringIO(msg), 'output.txt')
    await ctx.send(warn, file=outfile)

async def send_multi(ctx: commands.Context, msg: str):
    """
    Sends a message, or multiple if msg is too long
    """
    if len(msg) <= 2000:
        return await ctx.send(msg)
        
    lines = msg.splitlines()

    if any(len(l) > 2000 for l in lines):
        return await send_long(ctx, msg)
    
    while len(lines) > 0:
        outlines = []
        while len(lines) > 0:
            outlen = sum(len(l) for l in (*outlines, lines[0])) # length of lines
            outlen += len(outlines) # n of new lines
            if outlen <= 2000:
                outlines.append(lines.pop(0))
            else:
                break
        await ctx.send("\n".join(outlines))