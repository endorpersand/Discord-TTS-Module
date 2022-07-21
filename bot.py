import core.sql # ONLY FOR PYLANCE LINTER PURPOSES
import traceback
import logging
import os
import sys

import discord

from discord.ext import commands
from ruamel.yaml import YAML
from pathlib import Path


class Bot(commands.Bot):
    Database: "type[core.sql.Database]"
    
    def __init__(self, command_prefix='?', *args, **kwargs):
        logging.basicConfig(level=logging.INFO, format='[%(name)s %(levelname)s] %(message)s')
        self.logger = logging.getLogger('bot')

        self.yaml = YAML(typ='safe')
        with open('config/config.yml') as conf_file:
            self.config = self.yaml.load(conf_file)

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

    async def on_ready(self):
        self.logger.info(f'Connected to Discord')
        self.logger.info(f'Guilds  : {len(self.guilds)}')
        self.logger.info(f'Users   : {len(set(self.get_all_members()))}')
        self.logger.info(f'Channels: {len(list(self.get_all_channels()))}')

    def load_module(self, module: str, force_load: bool = False):
        """
        Loads a module
        """
        if not force_load:
            # possible names for this module
            aliases = {module}
            if module.startswith('cogs.'): aliases.add(module[len('cogs.'):])

        try:
            self.load_extension(module)
        except Exception as e:
            self.logger.exception(f'Failed to load module {module}:')
            print()
            self.logger.exception(e)
            print()
        else:
            self.logger.info(f'Loaded module {module}.')

    def load_dir(self, directory: str, force_load: bool = False):
        """
        Loads all modules in a directory
        """
        path = Path(directory)
        if not path.is_dir(): 
            self.logger.info(f"Directory {directory} does not exist, skipping")
            return

        modules = [f"{directory}.{p.stem}" for p in path.iterdir() if p.suffix == ".py"]
        for m in modules:
            self.load_module(m, force_load=force_load)

    def get_cog_logger(self, cog: commands.Cog):
        """
        Gets the logger for a cog.
        This can be used during cog initialization as `bot.get_cog_logger(self)` to obtain a logger before the cog is registered.
        """
        return self.logger.getChild(cog.qualified_name.lower())

    def add_cog(self, cog: commands.Cog, *, override: bool = False) -> None:
        cog.logger = self.get_cog_logger(cog)
        super().add_cog(cog, override=override)

    def run(self, token):
        self.load_dir("core", True)
        self.load_dir("cogs", False)

        self.logger.info(f'Loaded {len(self.cogs)} cogs')
        super().run(token)

if __name__ == '__main__':
    bot = Bot()
    token = open(bot.config['token_file'], 'r').read().split('\n')[0]
    bot.run(token)
