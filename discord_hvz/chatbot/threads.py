from __future__ import annotations

import discord
import asyncio
from discord.ext import commands

from discord_hvz.utilities import do_after_wait

from loguru import logger

from typing import List, Dict, Any
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from discord_hvz.main import HVZBot

TABLE_NAME = 'threads'

class ThreadManager:
    bot: HVZBot
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

        return thread

    def delayed_delete(self, thread_id: int, delay_sec: float = 10.0):
        # Delete a thread after a delay. This is useful for when a chatbot is done with a thread, but the user
        # still needs to see the last message.

        asyncio.create_task(do_after_wait(self.delete_thread, delay=delay_sec, thread_id=thread_id))
        #logger.info(f"Thread {thread_id} will be deleted in 5 seconds.")


    async def delete_thread(self, thread_id: int):
        # Delete a thread and remove its database entry. Made for use in delayed_delete.
        thread = self.bot.guild.get_thread(thread_id)
        if not thread: return
        try:
            await thread.delete()
        except discord.Forbidden:
            logger.error(f"The bot is not allowed to delete the thread '{thread.name}'. It needs the 'manage_threads' permission.")
        except Exception as e:
            logger.exception(f"Failed to delete the thread '{thread.name}' for an unusual reason. This should be reported to the developer.", e)

        rows = self.bot.db.get_rows(TABLE_NAME, 'thread_id', thread_id)
        if len(rows) != 0:
            try:
                self.bot.db.delete_row(TABLE_NAME, 'thread_id', thread_id)
            except Exception as e:
                logger.exception(f"Failed to delete the database entry for thread {thread_id}. This should be reported to the developer.", e)

    async def on_ready(self):
        # After bot startup, check if there are any threads that need to be deleted.
        db = self.bot.db
        table = db.get_table(TABLE_NAME)

        if len(table) == 0:
            return

        # Delete all created threads, since chatbots do not recover from restarts.
        # If the thread does not exist, delete the database entry instead.
        for row in table:
            thread_id = row['thread_id']
            thread = self.bot.guild.get_thread(thread_id)
            if not thread:
                db.delete_row(TABLE_NAME, 'thread_id', thread_id)
            else:
                try:
                    await thread.delete()
                except discord.Forbidden:
                    logger.error(f"The bot is not allowed to delete the thread '{thread.name}'. It needs the 'manage_threads' permission.")