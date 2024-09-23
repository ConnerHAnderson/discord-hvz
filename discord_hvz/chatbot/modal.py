from __future__ import annotations

from typing import TYPE_CHECKING

import discord
import regex
from datetime import datetime, timedelta

from .chatbot_utilities import Response, ResponseError, ChatbotState, disable_previous_buttons
from discord_hvz.buttons import HVZButton
from discord_hvz.config import config

if TYPE_CHECKING:
    from discord_hvz.chatbot import ChatBot
    from discord_hvz.chatbot.script_models import QuestionDatas, ScriptDatas

from loguru import logger


class ChatbotModal(discord.ui.Modal):
    chatbot: ChatBot
    original_interaction: discord.Interaction
    disable_buttons: bool

    def __init__(self, chatbot: ChatBot, interaction: discord.Interaction, disable_buttons=False, *args,
                 **kwargs, ) -> None:
        self.chatbot = chatbot
        self.original_interaction = interaction
        self.disable_buttons = disable_buttons
        super().__init__(*args, timeout=800.0, **kwargs)

    async def on_timeout(self) -> None:
        """When the modal times-out, this shuts down the chatbot and lets the user know."""

        await self.chatbot.remove()
        # await self.original_interaction.followup.send("Chatbot timed out.", ephemeral=True)
        # The timeout seems bothersome. Removing it for now.
    '''Method is called when a user submits the modal'''

    async def callback(self, interaction: discord.Interaction):
        """
        A function that is called when the modal is submitted.
        Much of this code is duplicated from the non-modal chatbot code
        TODO: Unify this code with chatbot
        """

        raw_responses = [x.value.strip() for x in self.children]
        errors = []
        any_error = False

        self.chatbot.state = ChatbotState.REVIEWING

        for i, question in enumerate(self.chatbot.script.questions):
            question: QuestionDatas
            self.chatbot.responses[i] = Response(raw_responses[i], raw_responses[i])
            if question.valid_regex:
                match = regex.fullmatch(r'{}'.format(question.valid_regex), raw_responses[i])
                if match is None:
                    errors.append(question.rejection_response)
                    any_error = True
                    continue

            if question.processor:
                try:
                    self.chatbot.responses[i].processed_response = question.processor(input_text=raw_responses[i],
                                                                                      bot=self.chatbot.bot)
                except ValueError as e:
                    errors.append(str(e))
                    any_error = True
                    continue

            errors.append(None)

        if any_error:
            error_msg = '**Your response had some errors:**'
            for i, error in enumerate(errors):
                if not error:
                    continue
                query = self.chatbot.script.questions[i].query
                error_msg += f'\n\n{query}\n`{raw_responses[i]}`\n*{error}*'

            view = discord.ui.View(timeout=None)
            view.add_item(HVZButton(
                self.chatbot.chatbot_manager.receive_interaction,
                custom_id='modify',
                label='Edit Answers',
                color='blurple',
                unique=True
            ))
            view.add_item(HVZButton(
                self.chatbot.chatbot_manager.receive_interaction,
                custom_id='cancel',
                label='Cancel',
                color='gray',
                unique=True
            ))
            # TODO: Ephemeral messages can't have persistent views. Thus these buttons can't be modifed
            # You can edit the message only through the original interaction.response. Can I save it?
            await interaction.response.send_message(error_msg, ephemeral=True, view=view)
        else:
            try:
                await self.chatbot.save()
            except ResponseError as e:
                msg = str(e)
            else:
                ending = self.chatbot.script.ending
                msg = ending if ending else "Complete!"
                logger.info(
                    f'Chatbot "{self.chatbot.script.kind}" with {self.chatbot.chat_member.name} (Nickname: {self.chatbot.chat_member.nick}) completed successfully.'
                )
            finally:
                await self.chatbot.remove()

            await interaction.response.send_message(msg, ephemeral=True)

        if self.disable_buttons:
            try:
                await disable_previous_buttons(self.original_interaction)
            except Exception as e:
                logger.exception(e)

async def send_modal(interaction: discord.Interaction, chatbot: ChatBot,
                     existing_chatbot: ChatBot = None, disable_buttons=False):
    """Responds to an interaction with the chatbot's modal version."""
    script = chatbot.script
    modal = ChatbotModal(
        title=script.modal_title[:45] if script.modal_title else script.kind,
        chatbot=chatbot,
        interaction=interaction,
        disable_buttons=disable_buttons
    )

    for i, question in enumerate(script.questions):
        prefilled_value = chatbot.responses.get(i)
        if prefilled_value: prefilled_value = prefilled_value.raw_response
        try:
            modal.add_item(
                create_modal_input_text(question, prefilled_value)
            )
        except ValueError as e:
            logger.warning(
                f'There was an error building a modal for a chatbot. Script name: {script.kind} Error: {e}')
            break

    await interaction.response.send_modal(modal)

def create_modal_input_text(question: QuestionDatas, prefilled_value=None) -> discord.ui.InputText:
    """Creates an InputText object, which is an element of a modal dialogue."""
    style = discord.InputTextStyle.long if question.modal_long else discord.InputTextStyle.short
    prefilled_value = prefilled_value or question.modal_default

    # TODO: Find a more robust and flexible way to have keyword values
    if isinstance(prefilled_value, str) and prefilled_value.strip().lower() == ('current_time' or 'current time'):
        now = datetime.now(tz=config.timezone) - timedelta(minutes=1)
        prefilled_value = now.strftime('%I:%M %p')
    return discord.ui.InputText(
        style=style,
        label=question.query[:45],
        value=prefilled_value
    )