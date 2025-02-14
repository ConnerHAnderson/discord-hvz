#!/bin/python3
from __future__ import annotations

import dataclasses
import functools
import asyncio
import logging
import sys
import time
from datetime import datetime
from os import getenv
from typing import Dict, Union, Any, Type

import discord
import loguru
from discord import Guild
from discord.ext import commands
from dotenv import load_dotenv
from loguru import logger
from sqlalchemy.exc import NoSuchColumnError

from discord_hvz.config import config, ConfigError, ConfigChecker
from discord_hvz import utilities
from discord_hvz.chatbot import script_models

# The below imports are commented to prevent double-importing.
# These modules need to exist in "hiddenimports" in discord_hvz.spec for the sake of pyinstaller
# from discord_hvz.commands import AdminCommandsCog
# from discord_hvz.buttons import HVZButtonCog
# from discord_hvz.chatbot import ChatBotManager
# from discord_hvz.display import DisplayCog
# from discord_hvz.item_tracker import ItemTrackerCog


from discord_hvz.database import HvzDb

# The latest Discord HvZ release this code is, or is based on.
VERSION = "0.4.0"


def dump(obj):
    """Prints the passed object in a very detailed form for debugging"""
    for attr in dir(obj):
        print("obj.%s = %r" % (attr, getattr(obj, attr)))


load_dotenv()  # Load the Discord token from the .env file
TOKEN = getenv("TOKEN")

log = logger
logger.remove()
logger.add(sys.stderr, level="INFO")
log_path = config.path_root / 'logs/discord-hvz_{time}.log'
logger.add(log_path, rotation='1 week', level='DEBUG', mode='a', backtrace=True, diagnose=True)

discord_handler = logging.getLogger('discord')


class InterceptHandler(logging.Handler):
    def emit(self, record):
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


class StartupError(Exception):
    def __init__(self, message=None):
        if message is not None:
            super().__init__(message)

class DiscordSink:
    def __init__(self, channel: discord.channel, bot: HVZBot):
        self.channel = channel
        self.bot = bot

    def write(self, message):
        # Send log messages to the Discord channel
        msg = utilities.abbreviate_message(message, 2000)
        self.bot.loop.create_task(self.channel.send(msg))

@dataclasses.dataclass
class BotChannels:
    tag_announcements: discord.TextChannel
    report_tags: discord.TextChannel
    zombie_chat: discord.TextChannel
    bot_output: discord.TextChannel = None

@dataclasses.dataclass
class BotRoles:
    zombie: discord.Role
    human: discord.Role
    player: discord.Role


class HVZBot(discord.ext.commands.Bot):
    guild: Guild | None
    db: HvzDb
    roles: BotRoles
    channels: BotChannels
    discord_handler: loguru.Logger
    _cog_startup_data: Dict[str, Dict[str, Any]]
    readied: bool

    def check_event(self, func):
        """
        A decorator that aborts events/listeners if they are from the wrong guild
        If you add an event of a type not used before, make sure the ctx here works with it
        """

        @functools.wraps(func)
        async def inner(ctx, *args, **kwargs):
            my_guild_id = self.guild.id
            if isinstance(ctx, discord.Interaction):
                guild_id = ctx.guild_id
            elif isinstance(ctx, discord.message.Message):
                if ctx.channel.type == discord.ChannelType.private:
                    guild_id = my_guild_id  # Treat private messages as if they are part of this guild
                else:
                    guild_id = self.guild.id
            elif isinstance(ctx, discord.Member):
                guild_id = ctx.guild.id
            elif isinstance(ctx, commands.Context):
                guild_id = my_guild_id
            if guild_id != my_guild_id:
                return
            result = await func(ctx, *args, **kwargs)

            return result

        return inner

    def __init__(self):
        self.guild: Union[discord.Guild, None] = None
        script_file_model = script_models.load_model(config.script_path)
        self.db = HvzDb(database_config = script_file_model.get_database_schema())
        self.readied = False

        intents = discord.Intents.all()
        super().__init__(
            description='Discord HvZ bot!',
            intents=intents
        )

        # cog_startup_data holds data that can be fetched by cogs during startup
        self._cog_startup_data = {
            'ChatBotManager': {
                'script_file_model':script_file_model,
                'config_checkers': {
                    'registration': ConfigChecker('registration'),
                    'tag_logging': ConfigChecker('tag_logging')
                }
            }
        }

        @self.listen()
        async def on_connect():
            logger.debug('Received the on_connect event')

        @self.listen()
        async def on_disconnect():
            logger.debug('Received the on_disconnect event')


        @self.listen()  # Always using listen() because it allows multiple events to respond to one thing
        async def on_ready():
            if self.readied:
                log.info('The bot encountered the on_ready event again, which usually means it had to reconnect to Discord. Everything is probably fine.')
            self.readied = True
            try:

                for guild in self.guilds:
                    if guild.id == config.server_id:
                        self.guild = guild
                        break
                else:
                    raise ConfigError(f'This bot is not on any server matching the "server_id" set in config.yml. Either the ID is set wrong, or the bot account has not joined the server.')

                # Updates the cache with all members and channels and roles
                await self.guild.fetch_members(limit=500).flatten()
                await self.guild.fetch_channels()
                await self.guild.fetch_roles()



                self.roles = BotRoles(
                    zombie=self.str_to_role(config.role_names.zombie),
                    human=self.str_to_role(config.role_names.human),
                    player=self.str_to_role(config.role_names.player),
                )
                self.channels = BotChannels(
                    tag_announcements=self.str_to_channel(config.channel_names.tag_announcements),
                    report_tags=self.str_to_channel(config.channel_names.report_tags),
                    zombie_chat=self.str_to_channel(config.channel_names.zombie_chat),
                )

                if config.channel_names.bot_output:
                    logger_channel = discord.utils.find(lambda c: c.name.lower() == config.channel_names.bot_output, self.guild.channels)
                    if logger_channel and isinstance(logger_channel, discord.TextChannel):
                        logger.add(DiscordSink(channel=logger_channel, bot=self), level="INFO")
                        self.channels.bot_output = logger_channel
                    else:
                        logger.warning(f"A bot output channel was specified in {config.filepath.name}" 
                                       f"as '{config.channel_names.bot_output}' but there is no text channel by that name."
                                       )

                log.success(
                    f'Discord-HvZ Bot launched correctly! Logged in as: {self.user.name} ------------------------------------------')
            except StartupError as e:
                logger.error(f'The bot failed to start because of this error: \n{e}')
                await self.close()
                time.sleep(1)
            except Exception as e:
                log.error('Bot startup failed.')
                log.exception(e)
                await self.close()
                time.sleep(1)

        @self.event
        async def on_error(event: str, *args, **kwargs):
            # exception = sys.exc_info()[1]
            logger.info(f'Args: {args}, kwargs: {kwargs}')
            logger.exception(f'The event "{event}" had an exception, which is being ignored. \n These are the arguments passed to the event: \n Positional Arguments: {args} \n Keyword Arguments: {kwargs}')

        @self.listen()
        async def on_application_command_error(ctx, error):
            logger.opt(exception=True).debug(error)
            error = getattr(error, 'original', error)
            log_level = None
            trace = False

            if isinstance(error, NoSuchColumnError):
                log_level = 'warning'
            elif isinstance(error, ValueError):
                log_level = 'warning'
            else:
                log_level = 'error'
                trace = True

            if log_level is not None:
                if trace:
                    trace = error

                # log_function(f'{error.__class__.__name__} exception in command {ctx.command}: {error}', exc_info=trace)

                getattr(log.opt(exception=trace), log_level)(
                    f'{error.__class__.__name__} exception in command {ctx.command}: {error}')
                print(type(error))


            await ctx.respond(f'The command at least partly failed: {error}')

        @self.listen()
        @self.check_event
        async def on_member_update(before, after):
            # When roles or nicknames change, update the database and sheet.
            try:
                self.db.get_member(before.id)
            except ValueError:
                return
            if not before.roles == after.roles:
                zombie = self.roles.zombie in after.roles
                human = self.roles.human in after.roles
                if zombie and not human:
                    self.db.edit_row('members', 'id', after.id, 'faction', 'zombie')
                elif human and not zombie:
                    self.db.edit_row('members', 'id', after.id, 'faction', 'human')
            if not before.nick == after.nick:
                self.db.edit_row('members', 'id', after.id, 'nickname', after.nick)
                log.debug(f'{after.name} changed their nickname.')

    def get_member(self, user_id: int):
        user_id = int(user_id)
        member = self.guild.get_member(user_id)
        return member

    def str_to_channel(self, string: str) -> discord.TextChannel:
        result = discord.utils.find(lambda c: c.name.lower() == string.lower(), self.guild.channels)
        if not result:
            raise ValueError(f"Could not find channel '{string}' on the server.")
        if not isinstance(result, discord.TextChannel):
            raise ValueError(f"The channel '{string}' was found on the server, but it was not a text channel. Found channel type: {type(result)}")
        return result

    def str_to_role(self, string: str) -> discord.Role:
        result = discord.utils.find(lambda c: c.name.lower() == string.lower(), self.guild.roles)
        if not result:
            raise ValueError(f"Could not find role '{string}' on the server.")
        return result

    async def announce_tag(self, tagged_member: discord.Member, tagger_member: discord.Member, tag_time: datetime):

        new_h = len(self.roles.human.members)
        new_z = len(self.roles.zombie.members)

        msg = f'<@{tagged_member.id}> has turned zombie!'
        if not config.silent_oz:
            msg += f'\nTagged by <@{tagger_member.id}>'
            msg += tag_time.strftime(' at about %I:%M %p')
            msg += f"\nThere are now {new_h} humans and {new_z} zombie{'s' if new_z > 1 else ''}."
        else:
            msg += f"\nThere {'are' if new_z > 1 else 'is'} now {new_z} zombie{'s' if new_z > 1 else ''}, apart from any OZs."

        await self.channels.tag_announcements.send(msg)

    def get_cog_startup_data(self, cog: commands.Cog | Type[commands.Cog]) -> Dict:
        # Fetches the startup_data dictionary from the bot when given a cog
        try:
            return self._cog_startup_data[cog.__class__.__name__]
        except KeyError:
            pass
        try:
            return self._cog_startup_data[cog.__name__]
        except KeyError:
            logger.warning(f'get_startup_data() called in an HVZBot, but no startup data found for this cog: {cog}')
            return {}


def main():
    try:
        logger.info(f'Launching Discord-HvZ version {VERSION}  ...')
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        if not TOKEN:
            logger.error("You need your Discord bot token to be in a file called '.env' "
                         "Find this on your Application's general information page on your Discord Developer Console. "
                         "The entire file should contain only this: TOKEN='replace_me_with_your_token'")
            return

        bot = HVZBot()

        bot.load_extension('.buttons', package = 'discord_hvz')
        bot.load_extension('.chatbot', package = 'discord_hvz')
        bot.load_extension('.commands', package = 'discord_hvz')
        bot.load_extension('.display', package = 'discord_hvz')
        bot.load_extension('.item_tracker', package = 'discord_hvz')
        bot.load_extension('.control_board', package = 'discord_hvz')

        bot.run(TOKEN)

    except discord.errors.LoginFailure as e:
        logger.error(f'Discord failed to log in: {e}')
    except KeyboardInterrupt:
        logger.info('Keyboard Interrupt!')
    except ConfigError as e:
        logger.error(e)
    except discord.errors.ExtensionFailed as e:
        context = e.__context__
        if isinstance(context, ConfigError):
            logger.error(context)
        else:
            logger.exception(e)
    except Exception as e:
        logger.exception(e)
    else:
        logger.success('The bot has shut down normally.')
    finally:
        logger.info('Press Enter to close.')
        input()

if __name__ == "__main__":
    main()

