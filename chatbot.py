from __future__ import annotations
import cProfile
from dataclasses import dataclass, field, InitVar
import yaml
import copy
import regex
import discord
from discord.commands import slash_command
from discord.ext import commands
from loguru import logger
from typing import List, Union, Dict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discord_hvz import Bot
    from hvzdb import HvzDb
    from datetime import datetime

from config import config

log = logger

guild_id_list = [config['available_servers'][config['active_server']]]

@dataclass
class Question:
    question_data: InitVar[Dict]
    name: str = None
    display_name: str = None
    query: str = None
    valid_regex: Union[str, None] = None
    rejection_response: Union[str, None] = None

    required_attributes = ['name', 'display_name', 'query']
    coupled_attributes = [
        ('valid_regex', 'rejection_response'), ]  # Attributes where if one appears, the other must also

    def __post_init__(self, question_data: Dict):
        for a in self.required_attributes:
            x = question_data.get(a)
            if x is None:
                raise ValueError(f'Question missing required attribute "{a}". Check scripts.yml')

        for pair in self.coupled_attributes:
            for i in range(0, 1):
                if question_data.get(i) is not None:
                    other = int(not i)  # Invert
                    if question_data.get(pair[other]) is None:
                        raise ValueError(f'Missing coupled attribute')

        for key, content in question_data.items():
            if not isinstance(content, str):
                raise ValueError(f'Attribute "{key}" of question does not evaluate to a string.')
            if hasattr(self, key):
                self.__setattr__(key, content)
            else:
                log.warning(f'"{key}" is not a valid question attribute. Ignoring it.')
        log.info(f'Loaded question called {self.name}')




@dataclass
class ChatBotScript:
    """
    A prototype object meant to be created at bot launch for every script in the scripts.yml file,
    then deep-copied for each ChatBot launched.
    """
    kind: str
    script: InitVar[Dict]
    _questions: List[Question] = field(default_factory=list, init=False)
    beginning: str = field(init=False)
    ending: str = field(init=False)
    table: str = field(init=False)

    def __post_init__(self,script: Dict):
        self.beginning = script['beginning']
        self.ending = script['ending']
        self.table = script['table']

        for q in script['questions']:
            try:
                self._questions.append(Question(q))
            except ValueError as e:
                # e.args
                raise e

    @property
    def length(self):
        return len(self._questions)

    @property
    def kind(self):
        return

    @property
    def response_dict(self) -> Dict[str, None]:
        output = {}
        for q in self._questions:
            output[q.name] = None
        return output


    def get_question(self, question_number: int):
        return self._questions[question_number]



@dataclass
class ChatBot:
    script: ChatBotScript
    database: HvzDb
    chat_member: discord.Member
    target_member: discord.Member = None
    processing: bool = field(default=False, init=False)
    reviewing: bool = field(default=False, init=False)
    last_asked_question: int = field(default=0, init=False)
    responses: dict[str, any] = field(default=None, init=False)

    def __post_init__(self,):
        self.responses = self.script.response_dict

        if self.target_member is None:
            self.target_member = self.chat_member

    async def start(self, existing_chatbot_kind: str = None):
        await self.ask_question(0, starting=True, existing_chatbot_kind=existing_chatbot_kind)

    async def ask_question(self, question_number: int, starting: bool = False, existing_chatbot_kind: str = None):
        question = self.script.get_question(question_number)
        msg = ''
        if existing_chatbot_kind is not None:
            msg += f'Cancelled the previous {existing_chatbot_kind} conversation.\n'
        if starting:
            msg += (self.script.beginning + '\n\n')
        msg += question.query
        await self.chat_member.send(msg)
        self.last_asked_question = question_number

    async def receive(self, message: discord.Message):
        question = self.script.get_question(self.last_asked_question)
        response: str = message.clean_content
        if question.valid_regex is not None:
            match = regex.fullmatch(r'{}'.format(question.valid_regex), message.content)
            if match is None:
                await message.reply(
                    question.rejection_response + '\nPlease answer again.')  # An error message for failing the regex test, configurable per-question
                return

        self.responses[question.name] = response
        if self.last_asked_question + 1 >= self.script.length:
            await self.review()
        else:
            await self.ask_question(self.last_asked_question + 1)

    async def review(self):
        self.reviewing = True
        msg = ('**Type "yes" to submit.**'
               '\nOr type the name of what you want to change, such as "%s".\n\n' % (
                   self.script.get_question(1).display_name))
        for q in self.script.questions:  # Build a list of the questions and their responses
            msg += (q.display_name + ': ' + self.responses[q.name] + '\n')
        await self.chat_member.send(msg)

    async def end(self):
        self.database.add_member()


class ChatBotManager(commands.Cog):
    bot: Bot
    active_chatbots: Dict[int, ChatBot] = {}
    loaded_scripts: Dict[str, ChatBotScript] = {}

    def __init__(self, bot: Bot):
        print('Started ChatBotManager')
        self.bot = bot

        file = open('scripts.yml', mode='r')
        scripts_data = yaml.safe_load(file)
        file.close()

        for kind, script in scripts_data.items():
            self.loaded_scripts[kind] = (ChatBotScript(kind, script))
        log.info(self.loaded_scripts['registration'].questions)

        log.info('ChatBotManager Initialized')

    async def start_chatbot(
            self,
            chatbot_kind: str,
            chat_member: discord.Member,
            target_member: discord.Member = None
    ):
        existing = self.active_chatbots.get(chat_member.id)
        if existing is not None:
            existing = existing.kind

        new_script_instance = copy.deepcopy(self.loaded_scripts[chatbot_kind])
        self.active_chatbots[chat_member.id] = ChatBot(
            new_script_instance,
            self.bot.db,
            chat_member,
            target_member


        )
        await self.active_chatbots[chat_member.id].start(existing)

    @slash_command(guild_ids=guild_id_list)
    async def chatbots(self, ctx):
        pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.channel.type == discord.ChannelType.private:

            chatbot = self.active_chatbots.get(message.author.id)

            if chatbot is None or chatbot.processing is True:
                return
            try:
                chatbot.processing = True
                completed = await chatbot.receive(message)
            except Exception as e:
                await chatbot.chat_member.send(
                    f'The chatbot had a critical error. You will need to retry from the beginning.')
                self.active_chatbots.pop(message.author.id)
                log.exception(e)
                return

            if completed:
                self.active_chatbots.pop(message.author.id)
            else:
                chatbot.processing = False
