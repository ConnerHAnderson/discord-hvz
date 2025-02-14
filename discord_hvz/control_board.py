from __future__ import annotations
import discord
from discord.ext import commands
from discord.ui import Button, View
from typing import Optional, Any
from loguru import logger

from discord_hvz.config import config

GUILD_ID_LIST = [config.server_id]

# Valid Control Board commands: No arguments, and no posting of messages to the same channel as the control board

# Dictionary mapping command names to keyword arguments for the command
COMMANDS: dict[str, dict[str, Any]] = {
    'tag_tree': {},
    'tag list': {},
    'shutdown': {'force': True},
    'hide_oz': {},
    'reveal_oz': {},
}


class ControlBoardCog(commands.Cog, guild_ids=GUILD_ID_LIST):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.slash_command(description="Post a control board with command buttons")
    async def post_control_board(self, ctx: discord.ApplicationContext):
        view = ControlBoardView(self.bot)
        await ctx.send("Control Board", view=view)
        await ctx.respond("Posted control board.", ephemeral=True)

class ControlBoardView(View):
    def __init__(self, bot: commands.Bot):
        logger.info('ControlBoardView initializing')
        super().__init__(timeout=None)
        self.bot = bot
        self.add_buttons()

    def add_buttons(self):
        logger.info(f"Commands: {self.bot.all_commands}")
        for command_name, kwargs in COMMANDS.items():
            command = self.bot.get_application_command(command_name, GUILD_ID_LIST)
            if not command:
                logger.warning(f"Command {command_name} not found")
                continue

            button = ControlBoardButton(command, kwargs)
            self.add_item(button)
            logger.info(f"Added button for {command.name}")

class ControlBoardButton(Button):
    def __init__(self, command: discord.ApplicationCommand, kwargs: dict[str, Any]):
        super().__init__(label=command.name, style=discord.ButtonStyle.primary)
        self.command = command
        self.kwargs = kwargs

    async def callback(self, interaction: discord.Interaction):
        #await interaction.response.defer()
        await self.command(interaction, **self.kwargs)

def setup(bot: commands.Bot):
    bot.add_cog(ControlBoardCog(bot))