from __future__ import annotations

import discord
from discord.ext import commands

from loguru import logger

from typing import List, Dict, Any
from typing import TYPE_CHECKING




if TYPE_CHECKING:
    from discord_hvz.main import HVZBot
    from .script_models import ScriptDatas, QuestionDatas

TABLE_NAME = 'threads'


class ThreadManager:
    bot: HVZBot
    threads: Dict[int, int] = {}
    def __init__(self, bot: HVZBot):
        self.bot: HVZBot = bot

        bot.db.prepare_table(TABLE_NAME, columns={
            'thread_id': 'integer',
            'member_id': 'integer',
            'chatbot_type': 'string'
        })

        self.bot.add_listener(self.on_ready, 'on_ready')



    async def create_thread(self, channel: discord.TextChannel, member: discord.Member, chatbot_type: str) -> discord.Thread:
        db = self.bot.db

        try:
            thread: discord.Thread = await channel.create_thread(
                name=f"{chatbot_type} for {member.name}",
                auto_archive_duration=60,  # Can only be 60, 1440, 4320, or 10080
                slowmode_delay=0,
                invitable=False,
                reason=f"Created by a {chatbot_type} chatbot for {member.name} ({member.id})."
            )
        except discord.Forbidden as e:
            logger.error(f"The bot is not allowed to create private threads in the channel '{channel.name}', and so a chatbot failed.")
            raise ValueError(f"The bot is not allowed to create private threads in the channel '{channel.name}'.") from e

        try:
            await thread.add_user(member)
        except discord.Forbidden as e:
            logger.error(f"The bot is not allowed to add {member.name} to the thread '{thread.name}'.")
            raise ValueError(f"The bot is not allowed to add {member.name} to the thread '{thread.name}'.") from e


        db.add_row(
            table_selection = TABLE_NAME,
            input_row = {
                "thread_id": thread.id,
                "member_id": member.id,
                "chatbot_type": chatbot_type
            }
        )

        self.threads[thread.id] = member.id
        return thread

    async def on_ready(self):
        logger.success(f"ThreadManager is ready.")