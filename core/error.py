import discord
from discord.ext import commands

import io
import traceback
import utils

async def on_command_error(ctx: commands.Context, exception: Exception):
    if isinstance(exception, commands.CommandInvokeError):
        if isinstance(exception.original, discord.Forbidden):
            try: await ctx.send(f'Permissions error: `{exception}`')
            except discord.Forbidden: pass
            return
        await fallback_error(ctx, exception.original)
    elif isinstance(exception, commands.CheckFailure):
        await ctx.send("You can't do that. " + str(exception))
    elif isinstance(exception, commands.CommandNotFound):
        pass
    elif isinstance(exception, commands.ConversionError):
        await ctx.send(f"Expected a {exception.converter.__name__}. {str(exception.original)}")
    elif isinstance(exception, commands.BadArgument):
        await ctx.send(''.join(exception.args) or 'Bad argument. No further information was specified.')
    elif isinstance(exception, commands.UserInputError):
        if hasattr(exception, "message") and exception.message:
            await ctx.send(exception.message)
        else:
            await ctx.send('Error: {}'.format(' '.join(exception.args)))
    elif isinstance(exception, commands.CommandOnCooldown):
        await utils.temporary_reaction(ctx, '🛑', exception.retry_after)
    else:
        await fallback_error(ctx, exception)

async def fallback_error(ctx: commands.Context, exception):
    bot = ctx.bot

    info = traceback.format_exception(type(exception), exception, exception.__traceback__, chain=False)
    bot.logger.error('Unhandled command exception - {}'.format(''.join(info)))
    errorfile = discord.File(io.StringIO(''.join(info)), 'traceback.txt')

    await ctx.send(f'{exception}', file=errorfile)


def setup(bot):
    bot.add_listener(on_command_error)