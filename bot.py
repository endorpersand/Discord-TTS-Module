import io

import yaml
import traceback
import logging
import sys

import discord

from discord.ext import commands
from pathlib import Path


class Bot(commands.Bot):
    def __init__(self, command_prefix='?', *args, **kwargs):
        logging.basicConfig(level=logging.INFO, format='[%(name)s %(levelname)s] %(message)s')
        self.logger = logging.getLogger('bot')

        self.config = yaml.safe_load(open('config/config.yml'))

        if 'command_prefix' in self.config:
            command_prefix = self.config['command_prefix']

        am = discord.AllowedMentions(everyone=False, replied_user=False, roles=False)
        intents = discord.Intents.all()
        super().__init__(
            command_prefix=command_prefix,
            help_command=commands.MinimalHelpCommand(sort_commands=False),
            allowed_mentions=am, 
            intents=intents, 
            *args, 
            **kwargs
        )

    async def on_error(self, event_method, *args, **kwargs):
        info = sys.exc_info()
        info = traceback.format_exception(*info, chain=False)
        self.logger.error('Unhandled exception - {}'.format(''.join(info)))

    async def on_command_error(self, ctx: commands.Context, exception: Exception):
        bot = ctx.bot

        info = traceback.format_exception(type(exception), exception, exception.__traceback__, chain=False)
        bot.logger.error('Unhandled command exception - {}'.format(''.join(info)))
        errorfile = discord.File(io.StringIO(''.join(info)), 'traceback.txt')

        await ctx.send(f'{type(exception).__name__}: {exception}', file=errorfile)

    async def on_ready(self):
        self.logger.info(f'Connected to Discord')
        self.logger.info(f'Guilds  : {len(self.guilds)}')
        self.logger.info(f'Users   : {len(set(self.get_all_members()))}')
        self.logger.info(f'Channels: {len(list(self.get_all_channels()))}')

    async def load_module(self, module: str):
        """
        Loads a module
        """
        try:
            await self.load_extension(module)
        except Exception as e:
            self.logger.exception(f'Failed to load module {module}:')
            print()
            self.logger.exception(e)
            print()
        else:
            self.logger.info(f'Loaded module {module}.')

    async def start(self, token: str, *, reconnect: bool = True) -> None:
        await self.load_module("tts")

        self.logger.info(f'Loaded {len(self.cogs)} cogs')
        return await super().start(token, reconnect=reconnect)

if __name__ == '__main__':
    bot = Bot()
    token = open(bot.config['token_file'], 'r').read().split('\n')[0]
    bot.run(token)
