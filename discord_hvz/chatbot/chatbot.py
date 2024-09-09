from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Any
from typing import TYPE_CHECKING

import discord
import regex
from discord.ext import commands
from loguru import logger

from discord_hvz.config import config, ConfigError, ConfigChecker
from discord_hvz.buttons import HVZButton

from . import modal
from .chatbot_utilities import Response, ResponseError, ChatbotState, disable_previous_buttons
from .script_models import load_model

if TYPE_CHECKING:
    from discord_hvz.main import HVZBot
    from .script_models import ScriptDatas, QuestionDatas

# Used for creating commands
guild_id_list = [config.server_id]


@dataclass
class ChatBot:
    script: ScriptDatas
    bot: HVZBot
    chat_member: discord.Member
    chatbot_manager: ChatBotManager
    thread: discord.Thread | None
    target_member: discord.Member = None,
    processing: bool = field(default=False, init=False)
    next_question: int = field(init=False, default=0)
    responses: dict[int, Response] = field(init=False, default_factory=dict)
    state: ChatbotState = ChatbotState.BEGINNING

    def __post_init__(self, ):
        if self.target_member is None:
            self.target_member = self.chat_member

    def __str__(self) -> str:
        return f'<@{self.chat_member.id}>, Script: {str(self.script.kind)}'

    def __int__(self) -> int:
        return self.chat_member.id

    async def remove(self) -> None:
        await self.chatbot_manager.remove_chatbot(self)

    async def ask_question(self, existing_chatbot: ChatBot = None, interaction: discord.Interaction = None):
        logger.debug(f'Asking question: next_question is {self.next_question}. State: {self.state.name}')
        msg = ''
        view = None
        if self.state is ChatbotState.BEGINNING:
            if self.script.starting_processor:
                # Should return None to continue, and raise an Error if there's a problem.
                await self.script.starting_processor(self.target_member, self.bot)
            if existing_chatbot is not None:
                msg += f'Cancelled the previous {existing_chatbot.script.kind} conversation.\n'
            if self.target_member != self.chat_member:
                msg += f'The following is for <@{self.target_member.id}>.\n'
            msg += f'{self.script.beginning} \n\n'
            self.state = ChatbotState.QUESTIONING

        if self.state in (ChatbotState.QUESTIONING, ChatbotState.MODIFYING):
            logger.debug(f'QUESTIONING or MODIFYING')
            question = self.script.questions[self.next_question]
            msg += question.query

            if question.button_options:
                view = discord.ui.View(timeout=None)
                for button in question.get_option_buttons(callback=self.chatbot_manager.receive_interaction):
                    view.add_item(button)

        elif self.state is ChatbotState.REVIEWING:
            logger.debug('Entered reviewing mode')
            view = discord.ui.View(timeout=None)

            view.add_item(HVZButton(
                self.chatbot_manager.receive_interaction,
                custom_id='submit',
                label='Submit',
                color='green',
                unique=True
            ))
            view.add_item(HVZButton(
                self.chatbot_manager.receive_interaction,
                custom_id='modify',
                label='Edit Answers',
                color='blurple',
                unique=True
            ))
            view.add_item(HVZButton(
                self.chatbot_manager.receive_interaction,
                custom_id='cancel',
                label='Cancel',
                color='gray',
                unique=True
            ))
            msg = self.create_review_string(self.responses, self.script)

        elif self.state is ChatbotState.MODIFYING_SELECTION:
            view = discord.ui.View(timeout=None)
            for button in self.script.get_selection_buttons(self.chatbot_manager.receive_interaction):
                view.add_item(button)
            msg = 'Select answer to modify:'

        if self.script.modal:
            await modal.send_modal(interaction, self)
        else:
            await self.thread.send(msg, view=view)

    async def receive(self, message: str, interaction: discord.Interaction = None) -> bool:
        """Receives user responses into the chatbot. Returns True if the chatbot is complete."""

        message = message.strip()
        processed_response = message

        if self.script.modal and message == 'modify' and interaction:
            await modal.send_modal(interaction, self, disable_buttons=True)
            self.state = ChatbotState.MODIFYING
            return False

        if message.casefold() == 'cancel':
            msg = f'Chat "{self.script.kind}" cancelled.'
            if interaction:
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await self.thread.send(msg)
            logger.info(
                f'Chatbot "{self.script.kind}" cancelled by {self.chat_member.name} (Nickname: {self.chat_member.nick})')
            return True

        elif self.state in (ChatbotState.QUESTIONING, ChatbotState.MODIFYING):
            logger.debug(f'receive method got "{message}" as a message, and is processing the question.')
            question = self.script.questions[self.next_question]
            if question.valid_regex:
                logger.debug('Processing regex...')
                match = regex.fullmatch(r'{}'.format(question.valid_regex), message)
                if match is None:
                    msg = f'{question.rejection_response} Please try again.'
                    await self.thread.send(msg)
                    return False

            if question.processor:
                logger.debug('Using processor function...')
                try:
                    processed_response = question.processor(input_text=message, bot=self.bot)
                except ValueError as e:
                    await self.thread.send(str(e))
                    return False

            self.responses[self.next_question] = Response(message, processed_response)
            self.next_question += 1

            if (self.next_question > len(self.script.questions) - 1) or (self.state is ChatbotState.MODIFYING):
                self.state = ChatbotState.REVIEWING


        elif self.state is ChatbotState.REVIEWING:
            choice = message.casefold()
            if choice == 'submit':
                try:
                    await self.save()
                except ResponseError as e:
                    await self.thread.send(str(e))
                else:
                    await self.thread.send(self.script.ending)
                    logger.info(
                        f'Chatbot "{self.script.kind}" with {self.chat_member.name} (Nickname: {self.chat_member.nick}) completed successfully.'
                    )
                finally:
                    return True

            elif choice == 'modify':
                self.state = ChatbotState.MODIFYING_SELECTION
            else:
                await self.thread.send(
                    'That is an invalid response. Please use the buttons to select, or type "cancel"')
                return False

        elif self.state is ChatbotState.MODIFYING_SELECTION:
            selection = message.casefold()
            for i, q in enumerate(self.script.questions):
                if selection == (q.column.casefold() or q.display_name.casefold()):
                    self.next_question = i
                    self.state = ChatbotState.MODIFYING
                    break
            else:
                await self.thread.send(
                    'That is an invalid response. Please use the buttons to select, or type "cancel"')
                return False

        await self.ask_question()
        return False

    async def save(self):
        """Attempts to save a chatbot's data to the database"""
        response_map: dict[str, Any] = {}

        # Convert the dictionary of indexed response objects to a map of question names to response values
        for i, response in self.responses.items():
            column = self.script.questions[i].column
            response_map[column] = response.processed_response
        if self.script.ending_processor:
            try:
                response_map = await self.script.ending_processor(
                    responses=response_map,
                    bot=self.bot,
                    target_member=self.target_member
                )
            except ValueError as e:
                raise ResponseError(e)

        self.bot.db.add_row(self.script.table, response_map)

    @classmethod
    def create_review_string(cls, responses: dict[int, Response], script: ScriptDatas) -> str:
        # Return a string list of questions and responses, useful for reviewing answers
        # TODO: Rework responses to not require this sort of int indexing
        output = ''
        for i, q in enumerate(script.questions):
            response = responses[i].raw_response
            output += f"**{q.display_name}**: {response}\n"
        return output


class ChatBotManager(commands.Cog, guild_ids=guild_id_list):
    """
    The cog that the main bot imports to run the chatbot system.
    """
    bot: HVZBot
    active_chatbots: Dict[int, ChatBot] = {}  # Maps member ids to ChatBots
    loaded_scripts: Dict[str, ScriptDatas] = {}
    config_checkers: Dict[str, ConfigChecker] = {}
    _postable_buttons: List[HVZButton] = []

    def __init__(self, bot: HVZBot):
        self.bot = bot

        script_file_model = self.bot.get_cog_startup_data(self)['script_file_model']
        self.loaded_scripts = {s.kind: s for s in script_file_model.scripts}
        self.config_checkers = {k: self.get_config_checker(k, self.bot) for k, s in self.loaded_scripts.items()}
        for kind, script in self.loaded_scripts.items():
            self._postable_buttons.append(
                HVZButton(
                    function=self.start_chatbot,
                    custom_id=kind,
                    label=getattr(script, 'postable_button_label', kind),
                    color=getattr(script, 'postable_button_color', 'green'),
                    postable_bot=self.bot)
            )
        # TODO: Make the bot adapt to new critical chatbot names
        if not self.loaded_scripts.get("registration"):
            logger.warning(
                f'There is no script in scripts.yml named "registration", so the /member register command will not function.')
        if not self.loaded_scripts.get("tag_logging"):
            logger.warning(
                f'There is no script in scripts.yml named "tag_logging", so the /tag create command will not function.')

        logger.debug('ChatBotManager Initialized')

    async def start_chatbot(
            self,
            interaction: discord.Interaction,
            chatbot_kind: str = None,
            target_member: discord.Member = None,
            override_config: bool = False
    ) -> None:
        # chatbot_kind only needed if the custom_id of the interaction isn't a valid script type
        response_msg = ''
        error = False
        try:
            script = self.loaded_scripts.get(interaction.custom_id) or self.loaded_scripts.get(chatbot_kind)
            if not script:
                script = self.loaded_scripts.get(interaction.custom_id)
                if not script:
                    raise ConfigError(f'There is no chatbot called "{script}", so this command doesn\'t work.')
            script: ScriptDatas

            member = interaction.user
            config_checker = self.config_checkers[script.kind]
            if config_checker and not config_checker.get_state() and not override_config:
                raise ConfigError(f'The chatbot {script.kind} is disabled in the bot\'s configuration. ')

            existing = self.active_chatbots.get(member.id)

            # Create private thread here for non-modals
            if not script.modal:
                thread: discord.Thread | None = await interaction.channel.create_thread(
                    name=f"Registration for {interaction.user.name}",
                    auto_archive_duration=10,
                    slowmode_delay=0,
                    invitable=False,
                    reason="Created to register a user for the game."
                )
            else:
                thread = None

            new_chatbot = ChatBot(
                script = script,
                bot = self.bot,
                chat_member = interaction.user,
                chatbot_manager = self,
                thread = thread,
                target_member = target_member,
            )

            await new_chatbot.ask_question(existing, interaction=interaction)

            self.active_chatbots[member.id] = new_chatbot

            logger.info(f'Chatbot "{script.kind}" started with {member.name} (Nickname: {member.nick})')

        except (ValueError, ConfigError) as e:
            response_msg = e
            error = True
        except discord.Forbidden:
            response_msg = 'The bot is not allowed to create private threads.'
            error = True
        except Exception as e:
            response_msg = f'The chatbot failed unexpectedly. Here is the error you can give to an admin: "{e}"'
            logger.exception(e)
            error = True
        else:
            if new_chatbot.thread:
                response_msg = f'The bot will talk to you in this thread: {new_chatbot.thread.jump_url}.'
            else:
                response_msg = f'Starting chatbot.'
        finally:
            # Assume that if there was an error, the interaction was not responded to.
            # Assume that if there was no error and the interaction has been responded to, there is nothing to send.
            if error or not interaction.response.is_done():
                await interaction.response.send_message(response_msg, ephemeral=True)

    async def remove_chatbot(self, chatbot: int | ChatBot, instant_delete = False):

        removed = self.active_chatbots.pop(int(chatbot))
        if removed.thread:
            try:
                await removed.thread.delete()
            except discord.Forbidden:
                logger.warning(f"This bot requires the 'manage_threads' permission to delete threads.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        A listener function that will receive messages in private threads.
        The happy path will call receive_response()
        """
        if message.channel.type != discord.ChannelType.private_thread or message.author.bot:
            return
        author_id = message.author.id
        response_text = str(message.clean_content)
        await self.receive_response(author_id, response_text, thread = message.channel)

    async def receive_interaction(self, interaction: discord.Interaction):
        """
        A function to pass to a component (button, etc.) to be called on activation.
        This is the sole method that non-modal chatbots respond to buttons.
        The happy path will call receive_response()
        """
        if interaction.type != discord.InteractionType.component:
            logger.warning('receive_interaction got something other than a component')
            return
        if interaction.channel.type in (discord.ChannelType.text, discord.ChannelType.private_thread):
            user_id = interaction.user.id

            custom_id = interaction.data['custom_id']
            response_text = slice_custom_id(custom_id)

            try:
                await self.receive_response(user_id, response_text, interaction=interaction, thread=interaction.channel)
            finally:
                try:
                    await disable_previous_buttons(interaction)
                except Exception as e:
                    logger.exception(e)

    async def receive_response(
            self, author_id: int,
            response_text: str,
            interaction: discord.Interaction = None,
            thread: discord.Thread = None
    ):
        """
        Receives all responses to a chatbot: direct messages, buttons, modals, etc.
        """

        chatbot = self.active_chatbots.get(author_id)

        if chatbot is None or chatbot.processing is True:
            return
        if chatbot.thread and thread != chatbot.thread:
            return
        try:
            chatbot.processing = True
            completed = await chatbot.receive(response_text, interaction=interaction)
        except Exception as e:
            await chatbot.thread.send(
                f'The chatbot had a critical error. You will need to retry from the beginning.')
            await self.remove_chatbot(chatbot)
            logger.exception(e)
            return

        if completed:
            await self.remove_chatbot(chatbot)
        else:
            chatbot.processing = False

    def list_active_chatbots(self) -> List[str]:
        output_list = []
        for i, chatbot in self.active_chatbots.items():
            output_list.append(str(chatbot))
        return output_list

    async def shutdown(self):
        """Sends a shutdown message to all members in a chatbot"""
        for i, chatbot in self.active_chatbots.items():
            await chatbot.thread.send(
                'Unfortunately, the bot has shut down. You will need to restart this chatbot when it comes back online.'
            )
            await self.remove_chatbot(chatbot)

    # TODO: Check if this is the best way to do this
    def get_config_checker(self, key: str, bot: HVZBot) -> ConfigChecker | None:
        try:
            return bot.get_cog_startup_data(self)['config_checkers'][key]
        except KeyError:
            return None


def slice_custom_id(text: str):
    """
    A custom id is a string tied to a Discord element such as a button or modal that identifies that particular one
    to the bot. To make them unique, the bot may add a colon followed by a unique number.
    This function removes the colon and unique number. If there is none, it returns the original text.
    This type of string is generated within a HVZButton
    """
    return text[:text.find(':')]


def setup(bot):
    """Called by Pycord to setup the cog"""
    bot.add_cog(ChatBotManager(bot))  # add the cog to the bot
