# from __future__ import annotations
import time
from typing import Union, TYPE_CHECKING, Optional, List

import discord
from discord.commands import Option, SlashCommandGroup, slash_command
from discord.commands import context
from discord.ext import commands
from loguru import logger

from .utilities import generate_tag_tree, respond_paginated, abbreviate_message
from discord_hvz.config import config

if TYPE_CHECKING:
    from main import HVZBot
    from discord_hvz.chatbot import ChatBotManager


def dump(obj):
    """Prints the passed object in a very detailed form for debugging"""
    for attr in dir(obj):
        print("obj.%s = %r" % (attr, getattr(obj, attr)))


DISCORD_MESSAGE_MAX_LENGTH = 2000

guild_id_list = [config.server_id]

CONFIG_CHOICES = ['registration', 'tag_logging', 'silent_oz', 'google_sheet_export']


def setup(bot):  # this is called by Pycord to setup the cog
    bot.add_cog(AdminCommandsCog(bot))  # add the cog to the bot


class AdminCommandsCog(commands.Cog, guild_ids=guild_id_list):
    bot: "HVZBot"
    config_download_messages: List[int]

    def __init__(self, bot: "HVZBot"):
        self.bot = bot
        self.config_download_messages = []

    member_group = SlashCommandGroup("member", "Commands for dealing with members.", guild_ids=guild_id_list)
    tag_group = SlashCommandGroup("tag", "Commands for dealing with tags.", guild_ids=guild_id_list)

    @member_group.command(name='delete')
    async def member_delete(
            self,
            ctx,
            member: Option(discord.Member, 'Member to delete from the database.', default=None),
            id: Option(str, 'Delete member by ID instead.', default=None)
    ):
        """
        Removes the specified member from the game: stays on the server.

        member_string must be a @mentioned member in the channel, an ID, a Discord_Name,
        a Nickname, or a Name.
        After deletion, the member still remains on the server and in tag records.
        If they are still in the tag records, there could be unknown side effects down the road.
        Deletion works even on players who have left the server.
        """
        bot = self.bot

        msg = ''
        if member:
            member_id = member.id
            if id:
                msg += 'Both member and ID supplied. Ignoring ID.\n'
        elif id:
            try:
                int(id)
            except ValueError:
                await ctx.respond(f'"{id}" is not a number. Example of a Discord member ID: 293465952895631360')
                return
            member_id = id
        else:
            await ctx.respond('No member or ID provided: nothing deleted.')
            return
        try:
            member_row = bot.db.get_member(member_id)
        except ValueError:
            await ctx.respond('That member is not in the database as a player, and so there is nothing to delete.')
            return
        bot.db.delete_row('members', 'id', str(member_id))

        if member:
            await member.remove_roles(bot.roles.human)
            await member.remove_roles(bot.roles.zombie)
            await member.remove_roles(bot.roles.player)
            msg += f'<@{member_id}> deleted from the game. Roles revoked, expunged from the database.'
        else:
            msg += f'{member_row.name} deleted from the game and expunged from the database.'

        msg += ' Any tags will still exist.'

        await ctx.respond(msg)

    @member_group.command(name='edit')
    async def member_edit(
            self,
            ctx,
            member: Option(discord.Member, 'The member to edit.'),
            attribute: Option(str, 'Database column to change. Exact match required.'),
            value: Option(str, 'Value to change to. Does not check validity!')
    ):
        """
        Edits one attribute of a member in the database. Reference the Google Sheet.

        Any arguments with spaces must be "surrounded in quotes"
        member_string must be an @mentioned member in the channel, an ID, a Discord_Name,
        a Nickname, or a Name.
        Valid attributes are the column names in the database, which can be found in exported Google Sheets.
        Case-sensitive, exact matches only!
        There is no validation to check if the value you provide will work, so be careful!
        """
        bot = self.bot
        try:
            member_row = bot.db.get_member(member)
        except ValueError as e:
            await ctx.respond('This user is not in the database. They probably aren\'t registered.')
            logger.warning(e)
            return

        original_value = member_row[attribute]
        bot.db.edit_row('members', 'id', member_row.id, attribute, value)
        await ctx.respond(
            f'The value of {attribute} for <@{member_row.id}> was changed from \"{original_value}\"" to \"{value}\"')
        # bot.sheets_interface.export_to_sheet('members')

    @member_group.command(name='list')
    async def member_list(self, ctx):
        """
        Lists all members. The Google Sheet is probably better.

        """

        members = self.bot.db.get_table('members')
        if not members:
            await ctx.respond(f'No members found.')
            return
        message = ''
        for member in members:
            name = ('\t' + member.name) if getattr(member, 'name') else ''
            email = ('\t' + member.email) if getattr(member, 'email') else ''
            nickname = ('\t' + member.nickname) if getattr(member, 'nickname') else ''
            sub_string = f'<@!{member.id}>{name}{nickname}{email}\n'
            message += sub_string

        await respond_paginated(ctx, message)

    @member_group.command(name='register')
    async def member_register(
            self,
            ctx: discord.ApplicationContext,
            member: Option(discord.Member, 'The Discord user to register as a member of the game.')
    ):
        """
        Starts a registration chatbot on behalf of another Discord user.

        member_string must be an @mentioned member in the channel, or an ID
        A registration chatbot will be started with the sender of this command,
        but the discord user registered will be the one specified.
        """
        bot = self.bot
        try:
            bot.db.get_member(member)
            await ctx.respond(f'<@{member.id}> is already registered.')
            return
        except ValueError:
            pass

        chatbotmanager: Union[ChatBotManager, None] = bot.get_cog('ChatBotManager')
        if not chatbotmanager:
            await ctx.respond('ChatBotManager not loaded. Command failed.')
            return

        await chatbotmanager.start_chatbot(ctx.interaction, "registration", member, override_config=True)
        await ctx.respond('Registration chatbot started in a DM', ephemeral=True)

    @member_group.command(name='remove_roles')
    async def member_remove_roles(
            self,
            ctx: discord.ApplicationContext,
            role: Option(
                str,
                'Role to remove from all users on the server.',
                choices=['zombie', 'human', 'player']),
            are_you_sure: Option(bool, 'Do you really want to remove all game roles from players?')
    ):
        """
        Removes a game-related role from all users on the server.

        Removes as many roles as able, even if some return an error. The changed members, or those who errored,
        are all listed elegantly.
        """

        role_map: dict[str, discord.Role] = {
            'zombie': self.bot.roles.zombie,
            'human': self.bot.roles.human,
            'player': self.bot.roles.player
        }
        selected_role = role_map[role]

        if not are_you_sure:
            await ctx.respond(
                'Since this is a powerful command, you must submit it with the argument "are_you_sure" set to True.')
            return
        if not selected_role.members:
            await ctx.respond('No members have this role.')
            return
        await ctx.response.defer()

        complete_members = []
        forbidden_members = []
        HTTP_members = []

        for member in selected_role.members:
            try:
                await member.remove_roles(selected_role, reason='Role removed in bulk by a command.')
                complete_members.append(member)
            except discord.Forbidden as e:
                forbidden_members.append(member)
                logger.debug(e)
            except discord.HTTPException as e:
                HTTP_members.append(member)
                logger.debug(e)

        msg = ''
        debug_msg = '>>>---Action log for the remove_roles command---<<<'
        categories = {
            "Removed": {"members": complete_members, "message": "Removed"},
            "Denied Permission": {"members": forbidden_members, "message": "The bot was denied permission to remove"},
            "HTTP Error": {"members": HTTP_members, "message": "There was an HTTP error while removing"}
        }

        for category, data in categories.items():
            members = data["members"]
            if members:
                msg += f"\n**{data['message']} <@&{selected_role.id}> from these members:**\n"
                debug_msg += f"\n{data['message']} role called '{selected_role.name} from the following members.\n'"
                for member in members[:5]:
                    msg += f"<@{member.id}> "
                if len(members) > 5:
                    msg += f" + {len(members) - 5} others. Full list available in the bot's log file."
                for member in members:
                    debug_msg += f"{member.name}, "

        logger.debug(debug_msg)
        await ctx.respond(msg, ephemeral=True)

    @tag_group.command(name='create')
    async def tag_create(self, ctx, member: discord.Member):
        """
        Starts a tag log chatbot on behalf of another member.

        member_string must be an @mentioned member in the channel, an ID, a Discord_Name,
        a Nickname, or a Name.
        A tag logging chatbot will be started with the sender of this command,
        but the discord user actually making the tag will be the one specified.
        Does not check the faction membership of the tagger or if tag logging is on.
        """
        bot = self.bot
        chatbotmanager: Union[ChatBotManager, None] = bot.get_cog('ChatBotManager')
        if not chatbotmanager:
            await ctx.respond('ChatBotManager not loaded. Command failed.')
            return

        await chatbotmanager.start_chatbot(ctx.interaction, "tag_logging", member, override_config=True)
        await ctx.respond('Tag logging chatbot started in a DM', ephemeral=True)

    @tag_group.command(name='delete')
    async def tag_delete(
            self,
            ctx,
            tag_id: Option(int, 'Tag ID from the Google Sheet')
    ):
        """
        Removes the tag by its ID, changing tagged member to human.

        Takes a tag ID, which you can get from the Google sheet.
        Removes the tag from the database. Also changes the tagged member back to
        human if there aren't any remaining tags on them.
        """
        bot = self.bot
        tag_row = bot.db.get_tag(tag_id)
        try:
            bot.db.delete_row('tags', 'tag_id', tag_id)
        except ValueError as e:
            print("Got value error from delete_row")
            raise ValueError(f"There is no tag with an id of {tag_id}") from e
        else:
            self.bot.dispatch('tag_changed')

        msg = ''
        tagged_member = bot.guild.get_member(int(tag_row.tagged_id))
        if tagged_member is None:
            msg += f'Roles not changed since <@{tag_row.tagged_id}> ({tag_row.tagged_name}) is no longer on the server.'
        else:
            try:
                existing_tag = bot.db.get_tag(tag_row.tagged_id, column='tagged_id', filter_revoked=True)
                # Change to human if there are no previous tags on the tagged member
                msg += (f'Left <@{tagged_member.id}> as zombie because <@{existing_tag.tagger_id}> '
                        f'({existing_tag.tagger_name}) still tagged them in tag {existing_tag.tag_id}')
            except ValueError:
                await tagged_member.add_roles(bot.roles.human)
                await tagged_member.remove_roles(bot.roles.zombie)
                msg += f'Changed <@{tagged_member.id}> to human.'

        msg = f'Tag {tag_id} deleted. ' + msg
        await ctx.respond(msg)

    @tag_group.command(name='edit')
    async def tag_edit(
            self,
            ctx,
            tag_id: Option(str, 'ID from Google Sheet of the tag.'),
            attribute: Option(str, 'Column in the database to edit. Exact only.'),
            value: Option(str, 'Value to change to. Does not verify validity!')
    ):
        """
        Edits one attribute of a tag.

        Takes a tag ID, which you can get from the Google sheet.
        Valid attributes are the column names in the database, which can be found in exported Google Sheets.
        Case-sensitive, exact matches only!
        There is no validation to check if the value you provide will work, so be careful!
        """
        bot = self.bot
        tag_row = bot.db.get_tag(tag_id)

        original_value = tag_row[attribute]
        bot.db.edit_row('tags', 'tag_id', tag_row.tag_id, attribute, value)
        self.bot.dispatch('tag_changed')
        await ctx.respond(
            f'The value of {attribute} for tag {tag_row.tag_id} was changed from \"{original_value}\"" to \"{value}\"')

    @tag_group.command(name='revoke')
    async def tag_revoke(
            self,
            ctx,
            tag_id: Option(int, 'Tag ID from Google sheet to revoke.')
    ):
        """
        Sets Tag_Revoked for a tag to True and leaves it in the database. Changes tagged member to human.

        Takes a tag ID, which you can get from the Google sheet.
        Sets the tag to Revoked, but leaves it in the database.
        Restores the tagged member to human if there isn't another
        tag that makes them a zombie.
        """
        bot = self.bot
        tag_row = bot.db.get_tag(tag_id)

        bot.db.edit_row('tags', 'tag_id', tag_id, 'revoked_tag', True)
        self.bot.dispatch('tag_changed')

        msg = ''

        tagged_member = bot.guild.get_member(int(tag_row.tagged_id))
        if tagged_member is None:
            msg += f'Roles not changed since <@{tag_row.tagged_id}> ({tag_row.tagged_name}) is no longer on the server.'
        else:
            try:
                existing_tag = bot.db.get_tag(tag_row.tagged_id, column='tagged_id', filter_revoked=True)
                # Change to human if there are no previous tags on the tagged member
                msg += (f'Left <@{tagged_member.id}> as zombie because <@{existing_tag.tagger_id}> '
                        f'({existing_tag.tagger_name}) still tagged them in tag {existing_tag.tag_id}')
            except ValueError:
                await tagged_member.add_roles(bot.roles.human)
                await tagged_member.remove_roles(bot.roles.zombie)
                msg += f'Changed <@{tagged_member.id}> to human.'

        msg = f'Tag {tag_id} revoked. ' + msg
        await ctx.respond(msg)

    @tag_group.command(name='restore')
    async def tag_restore(
            self,
            ctx,
            tag_id: Option(int, 'Tag ID from Google Sheet to restore.')
    ):
        """
        Sets Tag_Revoked for a tag to False. Changes roles.

        Takes a tag ID, which you can get from the Google sheet.
        Restores a revoked tag in the database.
        Restores the tagged member to zombie.
        """
        bot = self.bot
        tag_row = bot.db.get_tag(tag_id)

        bot.db.edit_row('tags', 'tag_id', tag_id, 'revoked_tag', False)
        self.bot.dispatch('tag_changed')

        msg = ''

        tagged_member = bot.guild.get_member(int(tag_row.tagged_id))
        if tagged_member is None:
            msg += f'Roles not changed since <@{tag_row.tagged_id}> ({tag_row.tagged_name}) is no longer on the server.'
        else:
            await tagged_member.add_roles(bot.roles.zombie)
            await tagged_member.remove_roles(bot.roles.human)
            msg += f'Changed <@{tagged_member.id}> to zombie.'

        msg = f'Tag {tag_id} restored. ' + msg
        await ctx.respond(msg)

    @tag_group.command(name='list')
    async def tag_list(self, ctx):
        """
        Lists all tags. The Google Sheet is probably better.

        """

        tags = self.bot.db.get_table('tags')
        if not tags:
            await ctx.respond(f'No tags found.')
            return

        message = ''

        for tag in tags:
            time = tag.tag_time.strftime('at about %I:%M %p on %b %d')
            revoked = tag.revoked_tag
            sub_string = f"{'REVOKED ' if revoked else ''}Tag {tag.tag_id}, <@{tag.tagger_id}> tagged <@{tag.tagged_id}>" \
                         f" {time}\n"
            message += sub_string

        await respond_paginated(ctx, message)

    @slash_command(name='config')
    async def config_command(
            self,
            ctx,
            setting: Option(
                str,
                'Config setting to change or view.',
                choices=CONFIG_CHOICES
            ),
            choice: Option(bool, 'What to change the setting to.', required=False)
    ):
        """
        Views or edits a few configuration settings.

        If only 'setting' is provided, prints the current setting.
        If 'choice' is True or False, the config setting is set.
        Current 'setting' options:
            'registration' Is the registration button enabled? Default: True
            'tag_logging' Is the tag log button enabled? Default: True
            'silent_oz' Are OZ names omitted from tag announcements? Default: False
        """

        found_setting = getattr(config, setting, None)

        if found_setting is None:
            await ctx.respond(f'\"{setting}\" did not match any configuration settings. Case-sensitive.')
            return

        if choice is None:
            await ctx.respond(f'The config setting \"{setting}\" is currently set to \"{found_setting}\"')
            return

        setattr(config, setting, choice)
        await ctx.respond(f'Set \"{setting}\" to \"{choice}\"')

    @slash_command()
    async def code(self, ctx):
        """
        Gives you your tag code in a private reply. Keep it secret, keep it safe.

        """
        bot = self.bot
        try:
            tag_code = bot.db.get_member(ctx.author).tag_code
            await ctx.respond(f'Your tag code is: {tag_code}\nHave this ready to give to a zombie who tags you.',
                              ephemeral=True)
        except ValueError:
            await ctx.respond('You aren\'t registered for the game.', ephemeral=True)
        except Exception as e:
            await ctx.author.send('Sorry, something went wrong with that command. Derp.')
            logger.exception(e)

    @slash_command(name='tag_tree')
    async def tag_tree(self, ctx: context.ApplicationContext):
        """
        Responds with a family tree of the zombies in the game.

        """
        bot = self.bot
        await ctx.response.defer()
        tree = generate_tag_tree(bot.db, bot)
        if tree == '':
            tree = 'There are no tags yet! Try again when there are.'
        tree = '**THE ZOMBIE FAMILY TREE\n**' + tree

        await respond_paginated(ctx, tree)

    @slash_command(name='shutdown', description='Shuts down the bot.')
    async def shutdown(
            self,
            ctx,
            force: Option(bool, 'Shuts down regardless of active chatbots.', required=False, default=False)
    ):
        """
        Shuts down bot. If there are active chats, list them and don't shut down.

        """
        bot = self.bot
        chatbot_cog: Optional[ChatBotManager] = bot.get_cog('ChatBotManager')
        if chatbot_cog:
            chatbot_list = chatbot_cog.list_active_chatbots()
            if len(chatbot_list) > 0:
                if force:
                    await chatbot_cog.shutdown()
                    await ctx.respond(
                        'These chatbots are being destroyed: \n' + '\n'.join(chatbot_list)
                    )

                else:
                    await ctx.respond(
                        'The bot did not shut down due to the following active chatbots. Use the "force" option to override. \n' + '\n'.join(
                            chatbot_list)
                    )
                    return

        await ctx.respond('Shutting Down')
        logger.critical('Shutting Down\n. . .\n\n')
        await bot.close()
        time.sleep(1)

    @slash_command(name='oz')
    async def oz(
            self,
            ctx,
            member: Option(discord.Member, 'Member to query or set.'),
            setting: Option(bool, 'OZ setting.', default=None, required=False)
    ):
        """
        Checks or sets OZ on member. OZs can access zombie channels when human, including the tag channel.

        member_string must be a @mentioned member in the channel, an ID, a Discord_Name,
        a Nickname, or a Name.
        If 'setting' is not provided, the commands prints the member's OZ state.
        If 'setting' is True or False, the member's OZ status is set accordingly.
        When OZ goes True, the member can access the tag & chat channels even when human.
        Make sure to give the OZs the zombie role after the secret is out.
        """
        bot = self.bot
        try:
            member_row = bot.db.get_member(member)
        except ValueError as e:
            await ctx.respond('This user is not in the database. They probably aren\'t registered.')
            logger.warning(e)
            return

        if setting is None:
            await ctx.respond(
                f'{member_row.name}\'s OZ status is {member_row.oz}. Give a True or False argument to change their setting.')
            return
        bot.db.edit_row('members', 'id', member_row.id, 'oz', setting)

        await ctx.respond(f'Changed <@{member_row.id}>\'s OZ status to {setting}')

        member = bot.guild.get_member(int(member_row.id))
        t_channel = bot.channels.report_tags
        c_channel = bot.channels.zombie_chat
        try:
            if setting:
                await t_channel.set_permissions(member, read_messages=True)
                await c_channel.set_permissions(member, read_messages=True)
            else:
                await t_channel.set_permissions(member, overwrite=None)
                await c_channel.set_permissions(member, overwrite=None)
        except Exception as e:
            await ctx.respond('Could not change permissions in the channels. Please give the bot permission to.')
            logger.warning(e)

    # A command called hide_oz that does the opposite of the reveal_oz command
    @slash_command(name='hide_oz', description='OZ identities are concealed from the humans.')
    async def hide_oz(self, ctx: discord.ApplicationContext):
        bot = self.bot
        try:
            ozs = bot.db.get_rows('members', 'oz', True)
        except ValueError as e:
            await ctx.respond('There are no OZs. Use the /oz command to set some.')
            return
        for oz in ozs:
            member = bot.guild.get_member(int(oz["id"]))
            if member:
                await member.remove_roles(bot.roles.zombie)
                await member.add_roles(bot.roles.human)

                try:
                    await bot.channels.report_tags.set_permissions(member, read_messages=True)
                    await bot.channels.zombie_chat.set_permissions(member, read_messages=True)
                except discord.Forbidden as e:
                    await ctx.respond(
                        'Could not change permissions in the report_tags and/or zombie_chat channels. Please give the bot permission to.')
                    logger.debug(e)
                except Exception as e:
                    await ctx.respond(f'Could not change permissions for member: <@{member.id}>')
                    logger.warning(e)
        config.silent_oz = True
        await ctx.respond(
            'The OZs have been hidden. They have the human role and not the zombie role. '
            f'They can now access the <#{bot.channels.report_tags.id}> and <#{bot.channels.zombie_chat.id}> channels despite being human because of *special* permissions.'
            'The configuration option silent_oz is now True, which means OZs won\'t show up in tag announcements and other displays.'
        )

    @slash_command(name='reveal_oz', description='OZ identities are no longer concealed from the humans.')
    async def reveal_oz(self, ctx: discord.ApplicationContext):
        bot = self.bot
        try:
            ozs = bot.db.get_rows('members', 'oz', True)
        except ValueError as e:
            await ctx.respond('There are no OZs. Use the /oz command to set some.')
            return
        for oz in ozs:
            member = bot.guild.get_member(int(oz["id"]))
            if member:
                await member.add_roles(bot.roles.zombie)
                await member.remove_roles(bot.roles.human)

                try:
                    await bot.channels.report_tags.set_permissions(member, overwrite=None)
                    await bot.channels.zombie_chat.set_permissions(member, overwrite=None)
                except discord.Forbidden as e:
                    await ctx.respond(
                        'Could not change permissions in the report_tags and/or zombie_chat channels. Please give the bot permission to.')
                    logger.debug(e)
                except Exception as e:
                    await ctx.respond(f'Could not change permissions for member: <@{member.id}>')
                    logger.warning(e)
        config.silent_oz = False
        await ctx.respond(
            'The OZs have been revealed. They have the zombie role and not the human role. '
            f'They no longer have *special* permissions for the <#{bot.channels.report_tags.id}> and <#{bot.channels.zombie_chat.id}> channels.'
            'The configuration option silent_oz is now False, which means OZs will show up in tag announcements and other displays.'
        )

    @slash_command(name='download_config', description='Sends you the config files, which you can edit and re-upload.')
    async def download_config(self, ctx: discord.ApplicationContext):

        file_obj = [discord.File(config.script_path), discord.File(config.filepath)]

        # Send a message with the file attached
        message = await ctx.author.send(
            files=file_obj,
            content='Attached are the editable configuration files. Edit any of them, then *reply* to this message '
                    'with the edited files attached to update the bot\'s actual files.'
        )
        self.config_download_messages.append(message.id)
        await ctx.respond("Replied in a Direct Message", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        A listener function that will receive direct messages from users and check if they are replying to a message
            created by the /download_config command
        """
        valid_paths = {"config.yml": config.filepath, "scripts.yml": config.script_path}
        if message.channel.type != discord.ChannelType.private or message.author.bot:
            return
        reference = message.reference
        if not reference: return
        if not reference.message_id in self.config_download_messages: return
        filenames = [a.filename for a in message.attachments]
        for attachment in message.attachments:
            target_path = valid_paths.get(attachment.filename)
            if not target_path:
                await message.reply(f"The file {attachment.filename} does not match a file that can be written to.")
                continue
            bytes = await attachment.read()
            with open(target_path, 'wb') as file:
                file.write(bytes)
