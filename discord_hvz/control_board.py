import discord
from discord.ext import commands
from discord.ui import Button, View
from typing import Optional

class ControlBoardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.slash_command(description="Post a control board with command buttons")
    async def post_control_board(self, ctx: discord.ApplicationContext):
        view = ControlBoardView(self.bot)
        await ctx.send("Control Board", view=view)

class ControlBoardView(View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.add_buttons()

    def add_buttons(self):
        for command in self.bot.commands:
            if not command.requires_arguments:
                button = ControlBoardButton(command)
                self.add_item(button)

class ControlBoardButton(Button):
    def __init__(self, command: commands.Command):
        super().__init__(label=command.name, style=discord.ButtonStyle.primary)
        self.command = command

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.command(interaction)

def setup(bot: commands.Bot):
    bot.add_cog(ControlBoardCog(bot))