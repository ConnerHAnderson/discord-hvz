import sys
from pathlib import Path
import requests
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from inspect import getmembers, isclass
from typing import TYPE_CHECKING, Dict, List, Union, Set

import discord
import pandas as pd
from quickchart import QuickChart
import sqlalchemy
from discord.commands import slash_command, Option
from loguru import logger

from .utilities import pool_function, have_lists_changed, generate_tag_tree
from .config import config

if TYPE_CHECKING:
    from database import HvzDb
    from main import HVZBot

guild_id_list = [config.server_id]
LAST_GAME_PLOT_HASH = None
LAST_GAME_PLOT_URL = ""



def create_quickchart(filepath: Path) -> discord.File:
    global LAST_GAME_PLOT_URL

    image_folder = config.path_root / "plots"
    if not image_folder.exists():
        image_folder.mkdir()
    image_path = image_folder / "latest_gameplot.png"


    qc = QuickChart()
    qc.width = 1000
    qc.height = 600
    qc.config = {
        "type": "line",
        "stacked": "false",
        "data": dict(),
        "options": {
            "title": {
                "display": "true",
                "text": "Players over Time" + (" (OZs counted as humans)" if config.silent_oz else ""),
                "fontSize": 18,
            },
            "scales": {
                "xAxes": [{
                    "type": "time",
                    "time": {
                        # "unit": "day",
                        "minUnit": "hour",
                        "displayFormats": {
                            "hour": "ddd, H:mm",
                            "day": "ddd, MMM DD"
                        }
                    }
                }]
            }
        }
    }

    # TODO: Access the database in a more sustainable way
    engine = sqlalchemy.create_engine(f"sqlite+pysqlite:///{str(filepath)}")

    with engine.connect() as conn:
        tags_df = pd.read_sql(
            sql="SELECT tag_time, revoked_tag FROM tags",
            con=conn
        )
        members_df = pd.read_sql(
            sql="SELECT registration_time, oz FROM members",
            con=conn
        )
        tags_df = pd.read_sql(
            sql="SELECT tag_time, revoked_tag FROM tags",
            con=conn
        )

    if len(tags_df.index) == 0:  # If there are no tags
        qc.config['options']['title']['text'] = "No Tags Yet"
    else:

        def total_players(x):
            total = (members_df.registration_time <= x.tag_time)
            return total.sum()

        def total_zombies(x):
            '''
            To be given a pandas series which is a single tag. Compares the tag to the dataframe of all tags,
            counting how many precede it (including it). Excludes revoked tags
            '''
            total = (tags_df.tag_time <= x.tag_time) & (tags_df.revoked_tag == False)
            return total.sum()

        def format_datapoint(timestamp, y):
            iso_timestamp = datetime.fromisoformat(timestamp).isoformat()
            return {
                "x": iso_timestamp,
                "y": y
            }

        oz_count = members_df['oz'].sum()

        player_count_sr = tags_df.apply(total_players, axis=1)
        tags_df = tags_df.assign(Player_Count=player_count_sr)
        zombie_count_sr = tags_df.apply(total_zombies, axis=1) + oz_count
        tags_df = tags_df.assign(Zombie_Count=zombie_count_sr)
        tags_df['Human_Count'] = tags_df['Player_Count'] - tags_df['Zombie_Count']
        tags_df.sort_values(by='tag_time', inplace=True)

        zombie_series = [format_datapoint(x, y) for x, y in zip(tags_df['tag_time'], tags_df['Zombie_Count'])]
        human_series = [format_datapoint(x, y) for x, y in zip(tags_df['tag_time'], tags_df['Human_Count'])]
        # player_series = [format_datapoint(x, y) for x, y in zip(tags_df['tag_time'], tags_df['Player_Count'])]

        qc.config['data'] = {
            "datasets": [
                {
                    "label": "Zombie Count",
                    "fill": "false",
                    "data": zombie_series,
                    "borderColor": "#13ad20",
                    "backgroundColor": "#13ad20",
                },
                {
                    "label": "Human Count",
                    "fill": "false",
                    "data": human_series,
                    "borderColor": "#dbce14",
                    "backgroundColor": "#dbce14",
                },
            ]
        }

    url = qc.get_url()
    # Download the image from QuickPlot and save it. If the url hasn't changed, re-use the previous image.
    if url != LAST_GAME_PLOT_URL or not image_path.exists():
        with open(image_path, 'wb') as handle:
            response = requests.get(url, stream=True)

            if not response.ok:
                logger.warning(response)

            for block in response.iter_content(1024):
                if not block:
                    break
                handle.write(block)

    file = discord.File(image_path)
    LAST_GAME_PLOT_URL = url
    return file


class PanelElement(ABC):
    @property
    @abstractmethod
    def refresh_event(self) -> str:
        ...

    @abstractmethod
    def add(self, embed: discord.Embed, panel: "HVZPanel") -> Union[discord.File, None]:
        ...


class HumanElement(PanelElement):
    @property
    def refresh_event(self):
        return 'on_role_change'

    def add(self, embed: discord.Embed, panel: "HVZPanel") -> None:
        human_count = len(panel.bot.roles.human.members)
        embed.add_field(name='Humans', value=str(human_count))


class ZombieElement(PanelElement):
    @property
    def refresh_event(self):
        return 'on_role_change'

    def add(self, embed: discord.Embed, panel: "HVZPanel") -> None:
        count = len(panel.bot.roles.zombie.members)
        value = str(count) + (" (no OZ)" if config.silent_oz else "")
        embed.add_field(name='Zombies', value=value)


class PlayerElement(PanelElement):
    @property
    def refresh_event(self):
        return 'on_role_change'

    def add(self, embed: discord.Embed, panel: "HVZPanel") -> None:
        count = len(panel.bot.roles.player.members)
        embed.add_field(name='Players', value=str(count))


class PlayersTodayElement(PanelElement):
    @property
    def refresh_event(self):
        return 'on_role_change'

    def add(self, embed: discord.Embed, panel: "HVZPanel") -> None:
        try:
            rows = panel.bot.db.get_rows(
                table='members',
                search_column_name='registration_time',
                lower_value=datetime.now(tz=config.timezone) - timedelta(days=1),
                upper_value=datetime.now(tz=config.timezone)

            )
            count = len(rows)
        except ValueError:
            count = 0

        extra = ''
        if not config.registration:
            extra = ' (Registration Closed)'
        embed.add_field(name='New Players, Last 24h', value=str(count) + extra)


class TagsTodayElement(PanelElement):
    @property
    def refresh_event(self):
        return 'on_role_change'

    def add(self, embed: discord.Embed, panel: "HVZPanel") -> None:
        try:
            rows = panel.bot.db.get_rows(
                table='tags',
                search_column_name='tag_time',
                lower_value=datetime.now(tz=config.timezone) - timedelta(days=1),
                upper_value=datetime.now(tz=config.timezone)
            )
            count = len(rows)
        except ValueError:
            count = 0

        embed.add_field(name='Tags Today', value=str(count))


class GamePlotElement(PanelElement):
    @property
    def refresh_event(self):
        return 'on_tag_changed'

    def add(self, embed: discord.Embed, panel: "HVZPanel") -> discord.File:
        file = create_quickchart(panel.bot.db.filepath)
        embed.set_image(url=f'attachment://{file.filename}')
        return file


class TagTreeElement(PanelElement):
    @property
    def refresh_event(self) -> str:
        return 'on_tag_changed'

    def add(self, embed: discord.Embed, panel: "HVZPanel") -> None:
        tree_str = generate_tag_tree(panel.bot.db, panel.bot)
        char_limit = 4096
        if len(tree_str) > char_limit:
            too_long_msg = "\nThe tag tree was trimmed for being too long."
            while len(tree_str) > (char_limit - len(too_long_msg)):
                tree_str = tree_str.rsplit('\n', 1)[0]
            tree_str += too_long_msg

        embed.description = tree_str[:char_limit]


# Create a list of PanelElement classes available in the module
# Needs to be here for the sake of the slash_command decorator.
this_module = sys.modules[__name__]
AVAILABLE_PANEL_ELEMENTS = [cls[1] for cls in getmembers(this_module, isclass) if
                            issubclass(cls[1], PanelElement) and cls[1] is not PanelElement]
AVAILABLE_PANEL_ELEMENTS_STR = [element.__name__ for element in AVAILABLE_PANEL_ELEMENTS]


@dataclass
class HVZPanel:
    cog: "DisplayCog"
    live: bool = field(init=False, default=True)
    channel: discord.TextChannel = field(init=False, default=None)
    message: discord.Message = field(init=False, default=None)
    elements: List[PanelElement] = field(init=False, default_factory=list)
    bot: "HVZBot" = field(init=False)
    listener_events: Set[str] = field(init=False, default_factory=set)

    def __post_init__(self):
        self.bot = self.cog.bot

    async def send(self, channel: discord.TextChannel, element_names: List[Union[str, PanelElement]], live=True):
        self.live = live
        self.load_elements(element_names)
        self.channel = channel

        embed, file = self.create_embed()
        kwargs = {'embed': embed}
        if file:
            kwargs.update({'file': file})
        message = await self.channel.send(**kwargs)
        self.message = message
        if self.live:
            self.cog.add_panel(self)
            self.setup_listeners()
            self.save()

    def load_elements(self, element_names: List[Union[str, PanelElement]]) -> None:
        for name in element_names:
            for element in AVAILABLE_PANEL_ELEMENTS:
                if name == element.__name__ or name == element:
                    self.elements.append(element())

    async def refresh(self):
        pool_function(self._refresh, 6.0)

    async def _refresh(self):
        embed, file = self.create_embed()
        kwargs = {'embed': embed}
        if file:
            kwargs.update({'file': file})
        await self.message.edit(**kwargs)

    def create_embed(self) -> (discord.Embed, discord.File):
        embed = discord.Embed(title='Game Status')
        output_file = None

        for element in self.elements:
            try:
                file = element.add(embed=embed, panel=self)
            except Exception as e:
                logger.exception(e)
                raise e
            if file:
                output_file = file

        time_string = datetime.now(tz=config.timezone).strftime('%B %d, %I:%M %p')
        if self.live:
            embed.set_footer(text=f'Live updating. Updated: {time_string}')
        else:
            embed.set_footer(text=f'Created: {time_string}')

        return embed, output_file

    def save(self):
        row_data = {
            'channel_id': self.channel.id,
            'message_id': self.message.id
        }
        # Converts elements into a string of their class names separated by commas and no spaces
        elements_string = ','.join([type(element).__name__ for element in self.elements])
        row_data.update({'elements': elements_string})

        self.bot.db.add_row('persistent_panels', row_data)

    async def load(self, row: sqlalchemy.engine.Row) -> Union["HVZPanel", None]:
        """
        Accepts a database row originally created by save() and creates populates the Panel's data so it can
        continue to be updated. Almost exclusively used to keep panels active through reboots
        """
        self.channel = self.bot.guild.get_channel(row['channel_id'])
        if not self.channel:
            logger.warning(
                'Tried to load a persistent panel, but could not find the channel it was in. Removing it from the database.')
            self.bot.db.delete_row('persistent_panels', 'message_id', row['message_id'])
            return None
        try:
            self.message = await self.channel.fetch_message(row['message_id'])
        except discord.NotFound:
            logger.warning('Could not find panel message. Removing it from the database.')
            self.bot.db.delete_row('persistent_panels', 'message_id', row['message_id'])
            return None

        self.load_elements(row['elements'].split(','))
        self.setup_listeners()
        return self

    def setup_listeners(self):
        event_names = [element.refresh_event for element in self.elements]
        self.listener_events.update(event_names)
        for event_name in self.listener_events:
            self.bot.add_listener(self.refresh, name=event_name)

    def remove_listeners(self):
        for event_name in self.listener_events:
            self.bot.remove_listener(self.refresh, event_name)


class DisplayCog(discord.Cog, guild_ids=guild_id_list):
    bot: 'HVZBot'
    panels: Dict[int, "HVZPanel"]
    roles_to_watch: List[discord.Role]
    readied: bool

    def __init__(self, bot: "HVZBot"):
        self.bot = bot
        self.panels = {}
        self.roles_to_watch = []
        self.readied = False

        bot.db.prepare_table('persistent_panels', columns={
            'channel_id': 'integer',
            'message_id': 'integer',
            'elements': 'string'
        })

    def add_panel(self, panel: "HVZPanel"):
        if self.panels.get(panel.message.id):
            raise ValueError(f'Panel with id {panel.message.id} already exists.')
        self.panels[panel.message.id] = panel

    def delete_panel(self, message_id: int):
        panel = self.panels.pop(message_id, None)
        if panel:
            panel.remove_listeners()
        try:
            self.bot.db.delete_row('persistent_panels', 'message_id', message_id)
        except ValueError:
            pass

    @slash_command(description='Post a message with various live-updating game statistics.')
    async def post_panel(
            self,
            ctx: discord.ApplicationContext,
            element1: Option(str, required=True, choices=AVAILABLE_PANEL_ELEMENTS_STR, description='Element to add.'),
            element2: Option(str, required=False, choices=AVAILABLE_PANEL_ELEMENTS_STR, default=None,
                             description='Element to add.'),
            element3: Option(str, required=False, choices=AVAILABLE_PANEL_ELEMENTS_STR, default=None,
                             description='Element to add.'),
            element4: Option(str, required=False, choices=AVAILABLE_PANEL_ELEMENTS_STR, default=None,
                             description='Element to add.'),
            element5: Option(str, required=False, choices=AVAILABLE_PANEL_ELEMENTS_STR, default=None,
                             description='Element to add.'),
            element6: Option(str, required=False, choices=AVAILABLE_PANEL_ELEMENTS_STR, default=None,
                             description='Element to add.'),
            static: Option(bool, required=False, default=False, description='The data will never update if static.')
    ):
        selections = {element1, element2, element3, element4, element5, element6}
        panel = HVZPanel(self)
        await ctx.response.defer(ephemeral=True)
        await panel.send(ctx.channel, selections, live=not static)
        await ctx.respond('Embed posted', ephemeral=True)

    @slash_command(description='Post a message with a graph of zombie and human populations over time.')
    async def game_plot(
            self,
            ctx: discord.ApplicationContext,
            static: Option(bool, required=False, default=False, description='The plot will never update if static.')
    ):
        panel = HVZPanel(self)
        await ctx.response.defer(ephemeral=True)
        await panel.send(ctx.channel, [GamePlotElement], live=not static)
        await ctx.respond('Game Plot posted', ephemeral=True)

    @discord.Cog.listener()
    async def on_ready(self):
        if self.readied:
            return  # Don't do this on_ready event more than once
        self.readied = True
        # Load persistent panels from the database.
        rows = self.bot.db.get_table('persistent_panels')
        for row in rows:
            loaded_panel = await HVZPanel(self).load(row)
            if not loaded_panel:
                continue
            self.add_panel(loaded_panel)

    @discord.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        for name, role in self.bot.roles.__dict__.items():
            if not role: continue
            self.roles_to_watch.append(role)
        changed = have_lists_changed(before.roles, after.roles, self.roles_to_watch)
        if not changed:
            return
        self.bot.dispatch('role_change')
        return

    @discord.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        panel = self.panels.get(payload.message_id)
        self.delete_panel(payload.message_id)
        logger.debug(f'Removed panel with id: {payload.message_id}')


def setup(bot):  # this is called by Pycord to setup the cog
    bot.add_cog(DisplayCog(bot))  # add the cog to the bot


"""
Possible content:
Player count, Zombie Count, Human Count: On role change
Population plot: Change on role change
Most dangerous zombie: On role change or on tag
Tags today: On tag or role change
New players today: On registration

"""

