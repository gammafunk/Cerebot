"""Creating and managing the Discord connection."""

import asyncio
import discord
import enum
import logging
import os
import random
import re
import signal
import sys
import time
import traceback

from beem.botdb import BotDB
from beem.chat import ChatWatcher, BotCommandException, toggle_arg, nick_regexp

from .version import __version__

_log = logging.getLogger(__name__)

class AccessLevel(enum.IntEnum):
    BANNED       = enum.auto()
    NORMAL       = enum.auto()
    SERVER_MOD   = enum.auto()
    SERVER_ADMIN = enum.auto()
    BOT_ADMIN    = enum.auto()

# Used to split URLs in discord messages.
_url_regexp = re.compile(
r'(https?://(?:\S+(?::\S*)?@)?(?:(?:[1-9]\d?|1\d\d|2[01]\d|22'
r'[0-3])(?:\.(?:1?\d{1,2}|2[0-4]\d|25[0-5])){2}(?:\.(?:[1-9]\d?'
r'|1\d\d|2[0-4]\d|25[0-4]))|(?:(?:[a-z\u00a1-\uffff0-9]+-?)*'
r'[a-z\u00a1-\uffff0-9]+)(?:\.(?:[a-z\u00a1-\uffff0-9]+-?)*'
r'[a-z\u00a1-\uffff0-9]+)*(?:\.(?:[a-z\u00a1-\uffff]{2,})))'
r'(?::\d{2,5})?(?:/[^\s]*)?)')

# For parsing out IRC color codes and replacing them with ANSI color escape
# codes in a Discord markdown code block. See the following:
# https://modern.ircdocs.horse/formatting
# https://gist.github.com/kkrypt0nn/a02506f3712ff2d1c8ca7c9e0aed7c06
_irc_color_regexp = re.compile('\N{ETX}' + r'(\d{2})')
_irc_color_map = {0 : (1, 37), 1 : (1, 30), 2 : (0, 34), 3 : (0, 32), 4 : (1, 31),
                  5 : (0, 31), 6 : (0, 35), 7 : (0, 33), 8 : (1, 33),
                  9 : (1, 32), 10 : (0, 36), 11 : (1, 36), 12 : (1, 34),
                  13 : (1, 35), 14 : (0, 30), 15 : (0, 37) }

_emote_regexp = re.compile(r'^<:[^>]+:([0-9]+)>$')
_channel_regexp = re.compile(r'^<#([0-9]+)>$')
_list_markdown_regexp = re.compile(r'^([0-9]+)\.')

# String that faction roles end with.
_faction_suffix = " Faction"

# How long we allow inactivity in a channel before we remove its channel source
# object from the cache.
_channel_idle_timeout = 90 * 60

# How long we allow a chatter in a channel to be idle before we no longer
# include them in the $chat variable.
_chatter_idle_timeout = 60 * 60

# How long to wait after a connection failure before reattempting the
# connection.
_reconnect_timeout = 10

class DiscordSource(ChatWatcher):
    """The channel source object that handles chat for any kind of discord
    channel. These objects are created as needed by the discord manager when
    activity is seen in a new discord channel and cached based on message
    activity."""

    def __init__(self, manager, channel, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.manager = manager
        self.channel = channel

        # Time since any message was last seen in the channel, used for the
        # Discord manager cache of these objects.
        self.time_last_message = None

        # Dict of recent chatters with users as keys and timestamp as values.
        self.chatters = {}

    @property
    def parent_channel(self):
        """The parent Discord channel of this source. If this source is a
        thread, this gives the parent channel of the thread, otherwise it's
        simply the source's channel."""

        if isinstance(self.channel, discord.Thread):
            return self.manager.get_channel(self.channel.parent_id)

        else:
            return self.channel

    @property
    def dcss_manager(self):
        return self.manager.dcss_manager

    @property
    def bot_user(self):
        return self.manager.user

    @property
    def is_private(self):
        """"True if the channel is a private channel, false otherwise."""

        return isinstance(self.parent_channel, discord.abc.PrivateChannel)

    @property
    def description(self):
        name = self.parent_channel.id
        if self.is_private:
            if self.parent_channel.recipient:
                name = self.channel.recipient.name
            return f'DM:{name}'
        else:
            name = self.parent_channel.name
            return f'{self.parent_channel.guild.name}:#{name}'

    @property
    def bot_commands(self):
        return bot_commands

    @property
    def command_period(self):
        return self.manager.conf['command_period']

    @property
    def command_limit(self):
        return self.manager.conf['command_limit']

    @property
    def help_text(self):
        return self.manager.conf['help_text']

    def log_info(self, message, debug=False):
        """Log an informational message."""

        self.manager.log_info(f"{self.description}: {message}", debug=debug)

    def log_error(self, message, trace=False):
        """Log an error message."""

        self.manager.log_error(f"{self.description}: {message}", trace=trace)

    def should_limit_sequell_lines(self, sender):
        """We allow unlimited response lines for a single command in private
        channels."""
        return not self.is_private

    def allow_sequell_edits(self, sender):
        """Should we allow messages from this source and sender to make Sequell
        edits? Messages without this permission will be relayed to Sequell in
        read-only mode. Messages in read-only mode result in an error message
        as their response from Sequell."""

        # Private channels never allow edits.
        if self.is_private:
            return False

        channel_data = self.manager.get_channel_data(self.parent_channel,
                                                     default=True)
        return channel_data['allow_edits']

    def get_chat_name(self, user, sanitize=False):
        return super().get_chat_name(user.name, sanitize)

    def get_dcss_nick(self, user):
        """Return the nick we have mapped for a given user. If the user's name
        has no valid characters, use their discord id instead."""

        user_data = self.manager.get_user_data(user)
        if user_data['dcss_nick']:
            nick = user_data['dcss_nick']
        else:
            nick = self.get_chat_name(user, True)

        # Users can't use the nick of an irc nick unless they're the
        # corresponding discord user for that nick.
        if (nick in self.manager.irc_nicks
                and self.manager.irc_nicks[nick] != user.id):
            nick = None

        if not nick:
            nick = str(user.id)

        return nick

    def get_chat_dcss_nicks(self, requester):
        """Return a set of dcss nicks for users where we have a nick
        mapping."""

        nicks = set()
        for user in self.chatters:
            nick = self.get_dcss_nick(user)
            if nick:
                nicks.add(nick)

        return nicks

    def get_managed_roles(self):
        """Find which bot-managed roles are available on this guild for use
        with the !addrole bot command."""

        # We must be associated with a guild.
        if self.is_private:
            return

        guild = self.channel.guild
        bot_role = None
        for r in guild.roles:
            if r.name == "Bot" and r in guild.me.roles:
                bot_role = r
                break

        if not bot_role:
            return

        roles = []
        default = guild.default_role
        for r in guild.roles:
            if (r.position < bot_role.position
                    # Don't want the everyone role
                    and r is not default
                    # Don't want e.g. Twitch subscriber roles
                    and not r.managed
                    # We want only roles that have default role permissions,
                    # since these are meant for users.
                    and r.permissions.is_subset(default.permissions)):
                roles.append(r)

        # Remove any roles that are faction roles. These would end in
        # _faction_suffix and have a corresponding role without that suffix.
        role_names = [r.name for r in roles]
        for r in guild.roles:
            if (r in roles
                    and r.name.endswith(_faction_suffix)
                    and r.name[:-len(_faction_suffix)] in role_names):
                roles.remove(r)

        return roles

    def get_faction_roles(self):
        """Find which faction roles are available on this guild for use with
        the !addfaction bot command."""

        # We must be associated with a guild.
        if self.is_private:
            return

        guild = self.channel.guild
        bot_role = None
        for r in guild.roles:
            if r.name == "Bot" + _faction_suffix and r in guild.me.roles:
                bot_role = r
                break

        if not bot_role:
            return

        roles = self.get_managed_roles()
        factions = []
        role_names = [r.name for r in roles]
        for r in guild.roles:
            if (r.position < bot_role.position
                    and r.name.endswith(_faction_suffix)
                    and r.name[:-len(_faction_suffix)] in role_names):
                factions.append(r)

        return factions

    def get_source_ident(self):
        """Get a unique identifier hash of the discord channel."""

        return {'protocol' : self.manager.service, 'id' : self.channel.id }

    def filter_text(self, message):
        """Censor any text according to our list of text filters, replacing
        each match with the text [censored]."""

        # Messages in private channels are unfiltered.
        if self.is_private:
            return message

        if self.channel.guild.id not in self.manager.text_filters:
            return message

        result = message
        for f in self.manager.text_filters[self.channel.guild.id]:
            result = f.sub('[censored]', result)
        return result

    def filter_markdown(self, message):
        """Escape markdown from message output, being careful not to mangle any
        URLs."""

        result = ""
        for i, p in enumerate(_url_regexp.split(message)):
            # URLs parts are at odd indices. Place angle brackes around these
            # to disable discord preview.
            if i % 2:
                p = '<' + p + '>'
            # Remove markdown characters from non-url parts.
            else:
                p = discord.utils.escape_markdown(p)
            result += p

        # Escape output interpreted as a markdown ordered list.
        result = _list_markdown_regexp.sub(r'\1\.', result)

        return result

    def find_discord(self, iterable, search, *, fail_error=True):
        """Find a discord object from an iterable by id, name string exact
        match, or name substring match, in order of preferance."""

        allow_substring = True
        search_id = None
        if search.isdigit():
            search_id = int(search)
            allow_substring = False

        if isinstance(iterable[0], discord.Guild):
            desc = "server"
        elif isinstance(iterable[0], discord.TextChannel):
            desc = "channel"
            # These have straightforward names and there can be a lot of them.
            allow_substring = False

            if not search_id:
                match = _channel_regexp.match(search)
                if match:
                    search_id = int(match.group(1))

        elif isinstance(iterable[0], discord.Role):
            desc = "role"
        else:
            raise Exception("Unknown discord object type search: {iterable[0]}")

        matches = []
        lsearch = search.lower()
        for i in iterable:
            # Lookup by id
            if search_id:
                if i.id == search_id:
                    return i
                else:
                    continue

            # Give exact matches priority
            if lsearch == i.name.lower():
                return i

            if (allow_substring and len(search) >= 4
                    and lsearch in i.name.lower()):
                matches.append(i)

        if matches:
            if len(matches) > 1:
                raise BotCommandException(f"{desc} search '{search}' has "
                        "multiple matches: {}".format(
                            ', '.join([m.name for m in matches])))
            else:
                return matches.pop()
        else:
            if fail_error:
                raise BotCommandException(f"Can't find {desc} match for "
                        f"{search}")
            else:
                return None

    async def finalize_bot_command_args(self, user, args):
        """Set the target details of a bot command given its arguments. Find
        the guild and channel objects for the respective 'server' and
        'channel' arguments."""

        def ambiguous_search(search, desc):
            raise BotCommandException("Current channel has no server, so "
                    f"{desc} '{search}' is ambiguous")

        dest_server = None
        if not self.is_private:
            dest_server = self.channel.guild

        vargs = vars(args)
        if 'server' in vargs:
            if args.server:
                dest_server = self.find_discord(self.manager.guilds,
                        args.server)
            args.server = dest_server

            # If the 'server' option is present, we must be able to either
            # resolve the option argument to a server or be in a server
            # channel.
            if not args.server:
                raise BotCommandException("Can't determine a target server.")

        if 'user' in vargs:
            if args.user:
                match = await self.manager.get_user(args.user, dest_server)
                if match:
                    args.user = match
                else:
                    raise BotCommandException(
                        f"Can't find a user match for {args.user}")
            # The default value for 'user' is the user running the bot command.
            else:
                args.user = user

        if 'channel' in vargs:
            if args.channel:
                if dest_server:
                    match = self.find_discord(dest_server.channels,
                            args.channel)
                elif args.channel.isdigit():
                    match = self.manager.get_channel(int(args.channel))
                else:
                    ambiguous_search("channel name", args.channel)

                if (not match
                        or not isinstance(match, discord.TextChannel)):
                    raise BotCommandException("Can't find a text channel match"
                                              f" for '{args.channel}'")
                else:
                    args.channel = match
            elif isinstance(self.parent_channel, discord.TextChannel):
                args.channel = self.parent_channel
            # If the 'channel' option is present, we must be able to either
            # resolve the option argument to a channel or be in a server text
            # channel.
            else:
                raise BotCommandException(
                        "Can't determine a target server channel.")

        if 'role' in vargs and args.role:
            if not dest_server:
                ambiguous_search("role search", args.role)

            args.role = self.find_discord(dest_server.roles, args.role)

        if 'managed_role' in vargs and args.managed_role:
            if not dest_server:
                ambiguous_search("role search", args.managed_role)

            args.managed_role = self.find_discord(self.get_managed_roles(),
                    args.managed_role)

        if 'faction_role' in vargs and args.faction_role:
            if not dest_server:
                ambiguous_search("role search", args.faction_role)

            match = self.find_discord(self.get_managed_roles(),
                    args.faction_role, fail_error=False)
            if match:
                args.faction_role = args.faction_role + _faction_suffix
            args.faction_role = self.find_discord(self.get_faction_roles(),
                    args.faction_role)

    def user_access_level(self, user):
        return self.manager.user_access_level(user, self.parent_channel)

    def user_can_run_commands(self, user):
        """Return whether the user is allowed to run bot commands."""

        return (not user.bot
                and self.user_access_level(user) > AccessLevel.BANNED)

    def user_is_rate_limited(self, user):
        return self.user_access_level(user) < AccessLevel.SERVER_MOD

    def check_bot_command(self, user, entry):
        """Check overall bot command restrictions for this user."""

        super().check_bot_command(user, entry)

        if entry.get('require_guild') and self.is_private:
            raise BotCommandException(
                    "This command must be run in a server channel.")

    def expire_idle_chatters(self, current_time):
        """Remove any chatters from the list maintained for the $chat variable
        if they've been idle in the channel too long."""

        for c in list(self.chatters):
            if current_time - self.chatters[c] >= _chatter_idle_timeout:
                del self.chatters[c]

    async def send_chat(self, message, message_type='normal'):
        """Clean up message output before sending it to chat."""

        message = self.filter_text(message)

        # Clean up any markdown we don't want.
        if message_type == 'monster':
            # For code blocks, ``` is the only thing we need to escape.
            message = message.replace('```', r'\`\`\`')
        else:
            message = self.filter_markdown(message)
            message = discord.utils.escape_mentions(message)

        if message_type == 'action':
            message = '_' + message + '_'
        # Put monster output in a code block for readability of the tightly
        # spaced info.
        elif message_type == 'monster':
            # Re-encode the reset formatting character to the ANSI equivalent.
            message = message.replace('\N{SI}', '\N{ESC}[0m')

            parts = _irc_color_regexp.split(message)
            message = ""
            for i, part in enumerate(parts):
                if i % 2:
                    irc_color = int(part)
                    if irc_color in _irc_color_map:
                        fmt, color = _irc_color_map[irc_color]
                        part = f"\N{ESC}[{fmt};{color}m"
                    # If we don't recognize a color code, remove it.
                    else:
                        part = ""

                message += part

            message = f"```ansi\n{message}\n```"
        elif self.message_needs_escape(message):
            message = f"]{message}"

        await self.channel.send(message)

    async def read_chat(self, sender, content):
        current_time = time.time()
        if self.user_can_run_commands(sender):
            self.chatters[sender] = current_time

        self.expire_idle_chatters(current_time)

        await super().read_chat(sender, content)


class DiscordManager(discord.Client):
    """Manages the discord client, recieving discord events and handling them
    or passing them to the appropriate channel source object."""

    service = 'Discord'

    def __init__(self, conf, db_file, dcss_manager, *args, **kwargs):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(*args, intents=intents, **kwargs)

        self.conf = conf

        self.admins = set()
        if self.conf.get('admins'):
            self.admins.update(self.conf['admins'])

        self.bot_db = BotDB(db_file, db_tables)

        self.wait_task = None
        self.stopping = False
        self.sources = set()
        self.allowed_servers = set()
        self.allowed_dm = {}

        self.dcss_manager = dcss_manager
        dcss_manager.managers[self.service] = self

    def log_info(self, message, debug=False):
        """Log an informational message."""

        message = f"{self.service}: {message}"
        if debug:
            _log.debug(message)
        else:
            _log.info(message)

    def log_error(self, message, trace=False):
        """Log an error, possibly with the traceback of an associated
        exception."""

        _log.error(f"{self.service}: Error: {message}")
        if trace:
            exc_type, exc_value, exc_tb = sys.exc_info()
            _log.error("".join(traceback.format_exception(
                exc_type, exc_value, exc_tb)))

    def get_server_data(self, server, default=True):
        """Get the server's data as a dict. If no server is found and `default`
        is `True`, return a dict of default values. Otherwise return `None`
        when the user isn't found."""

        return self.bot_db.get_row('discord_servers', { "id" : server.id },
                                   default)

    def set_server_field(self, server, field, value, create=True):
        """Set a server data field. If the server doesn't exist and create is
        True, create the user first, otherwise missing users generate an
        exception."""

        update = { 'id' : server.id, field : value }
        return self.bot_db.update_row('discord_servers', update, create)

    def update_allowed_servers(self):
        self.allowed_servers = set()
        self.allowed_dm = {}
        for g in self.guilds:
            server_data = self.get_server_data(g)
            if server_data and server_data['allowed']:
                self.allowed_servers.add(g.id)

    def get_channel_data(self, channel, default=True):
        """Get the channel's data as a dict. If no channel is found and
        `default` is `True`, return a dict of default values. Otherwise return
        `None` when the channel isn't found."""

        return self.bot_db.get_row('discord_channels', { "id" : channel.id },
                                   default)

    def set_channel_field(self, channel, field, value, create=True):
        """Set a channel data field. If the channel doesn't exist and create is
        True, create the channel first, otherwise missing channels generate an
        exception."""

        update = { 'id' : channel.id, field : value }
        return self.bot_db.update_row('discord_channels', update, create)

    def get_user_data(self, user, default=True):
        """Get the user's data as a dict. If no user is found and `default` is
        `True`, return a dict of default values. Otherwise return `None` when
        the user isn't found."""

        return self.bot_db.get_row('discord_users', { "id" : user.id },
                                   default)

    def set_user_field(self, user, field, value, create=True):
        """Set a user data field. If the user doesn't exist and create is True,
        create the user first, otherwise missing users generate an
        exception."""

        update = { 'id' : user.id, field : value }
        return self.bot_db.update_row('discord_users', update, create)

    def register_irc_nick(self, nick, user):
        """Register an IRC nick. A registered nick with a corresponding discord
        ID can't be used by anyone other than this user for Sequell commands. A
        nick registered with a user of None can't be used by anyone at all. A
        user that tries to use a registered nick they're not authorized for
        will have their Sequell nick be their discord ID."""

        if user:
            discord_id = user.id
        else:
            discord_id = None
        update = { 'nick' : nick, 'discord_id' : discord_id }
        self.bot_db.update_row('irc_nicks', update, True)
        self.update_irc_nicks()

    def remove_irc_nick(self, nick):
        """Remove the registration of an IRC nick."""

        self.bot_db.remove_row('irc_nicks', { 'nick' : nick })
        self.update_irc_nicks()

    def update_irc_nicks(self):
        self.irc_nicks = {}
        rows = self.bot_db.get_rows("irc_nicks")
        for row in rows:
            self.irc_nicks[row['nick']] = row['discord_id']

    def update_text_filters(self):
        """Update the per-server list of text filter compiled regular
        expressions so we can use them for text filtering."""

        self.text_filters = {}
        for g in self.guilds:
            if g.id not in self.allowed_servers:
                continue

            server_data = self.get_server_data(g)
            if not server_data['text_filter']:
                continue

            filters = []
            terms = server_data['text_filter'].split(',')
            for t in terms:
                filters.append(re.compile(re.escape(t.strip()), re.IGNORECASE))
            self.text_filters[g.id] = filters

    def get_source(self, channel_id):
        """Get the DiscordSource object of the given discord channel id."""

        for s in self.sources:
            if s.channel.id == channel_id:
                return s

        return

    def expire_idle_sources(self, current_time):
        """Remove the cached source object for any sources that have been idle
        for too long."""

        for c in list(self.sources):
            if not c.time_last_message:
                continue

            if current_time - c.time_last_message >= _channel_idle_timeout:
                self.sources.remove(c)

    async def on_error(self, message, *args, **kwargs):
        self.log_error(f"Failed to process discord event {message}",
                       trace=True)

    async def on_ready(self):
        """Handle anything that needs to be done only after Discord is fully
        connected and ready."""

        try:
            self.bot_db.open_db()

        except Exception as e:
            self.log_error(f"Error opening DB file {self.bot_db.db_file}: {e}")
            os.kill(os.getpid(), signal.SIGTERM)

        # Make sure the application owner is always a bot admin.
        appinfo = await self.application_info()
        if appinfo.owner:
            self.admins.add(appinfo.owner.id)

        self.update_allowed_servers()
        self.update_text_filters()
        self.update_irc_nicks()

    def user_access_level(self, user, channel=None):
        """Returns the AccessLevel of the given user considering the channel
        of this source."""

        if user.id in self.admins:
            return AccessLevel.BOT_ADMIN

        user_data = self.get_user_data(user)
        if user_data['is_banned']:
            return AccessLevel.BANNED

        if (not channel or not isinstance(channel, discord.abc.GuildChannel)):
            return AccessLevel.NORMAL

        if channel.permissions_for(user).administrator:
            return AccessLevel.SERVER_ADMIN

        mod_role = self.get_server_data(channel.guild)['moderator_role']
        if (mod_role and channel.guild.get_role(mod_role) in user.roles):
            return AccessLevel.SERVER_MOD

        return AccessLevel.NORMAL

    async def get_user(self, user_search, server=None):
        user_id = None
        if isinstance(user_search, int):
            user_id = user_search
        elif (isinstance(user_search, str)
              and user_search.isdigit()
              and 18 <= len(user_search) <= 20):
            user_id = int(user_search)

        if server:
            guilds = [server]
        else:
            guilds = self.guilds
        for g in guilds:
            if g.id not in self.allowed_servers:
                continue

            if user_id:
                try:
                    user = await g.fetch_member(user_id)
                except discord.NotFound:
                    return
                else:
                    return user

            users = await g.query_members(user_search)
            for u in users:
                # query_members() uses a prefix search, and we want an
                # exact match.
                if u.display_name == user_search:
                    return u

    async def dm_is_allowed(self, message):
        """Users are allowed to DM the bot if they're a bot admin or in an
        allowed server. This is cached for future messages."""

        if message.author.id in self.allowed_dm:
            return self.allowed_dm[message.author.id]

        access_level = self.user_access_level(message.author, message.channel)
        if access_level <= AccessLevel.BANNED:
            self.allowed_dm[message.author.id] = False
            return False

        # Bot admins are always allowed to DM.
        if access_level >= AccessLevel.BOT_ADMIN:
            self.allowed_dm[message.author.id] = True
            return True

        # This will only get us back the user if they're in an allowed server.
        user = await self.get_user(message.author.id)
        if user:
            self.allowed_dm[user.id] = True
            return True

        self.allowed_dm[message.author.id] = False
        return False

    async def on_message(self, message):
        """Handle a Discord chat message."""

        if not self.is_ready():
            return

        if isinstance(message.channel, discord.abc.GuildChannel):
            if not message.channel.guild.id in self.allowed_servers:
                return
        elif isinstance(message.channel, discord.abc.PrivateChannel):
            if not await self.dm_is_allowed(message):
                return
        # Some unknown type of message source.
        else:
            return

        current_time = time.time()
        self.expire_idle_sources(current_time)

        source = self.get_source(message.channel.id)
        if not source:
            source = DiscordSource(self, message.channel)
            self.sources.add(source)

        source.time_last_message = current_time
        await source.read_chat(message.author, message.content)

    def get_source_by_ident(self, source_ident):
        """Given an 'identity' key tuple identifying a source, return the
        source channel."""

        return self.get_source(source_ident['id'])

    async def start(self):
        """Set the discord login token an connect, processing discord events
        indefinitely."""

        self.log_info("Starting manager.")

        need_wait = False
        while True:

            if self.stopping:
                return

            if need_wait:
                self.wait_task = asyncio.ensure_future(
                        asyncio.sleep(_reconnect_timeout))
                await self.wait_task

            self.log_info("Starting Discord connection.")

            try:
                await super().start(self.conf['token'])

            except discord.LoginFailure:
                self.log_error(
                        "Login failure: Token not accepted. Shutting down.")
                os.kill(os.getpid(), signal.SIGTERM)

            except discord.HTTPException as e:
                if e.text:
                    msg = e.text
                else:
                    msg = f"status: {e.status}, Discord code: {e.code}"
                self.log_error(f"Login failure: HTTP error: {msg}")
                need_wait = True

            except asyncio.CancelledError:
                raise

            except Exception as e:
                self.log_error("Failure starting Discord connection:"
                        f" {e.args[0]}", trace=True)

    async def stop(self):
        """Disconnect from Discord and stop the manager."""

        if self.wait_task and not self.wait_task.done():
            self.wait_task.cancel()

        if self.conf.get('fake_connect') or self.is_closed():
            return

        self.stopping = True
        await self.close()
        self.clear()

# Discord database tables and field definitions.
db_tables = {
        'discord_users' : [
            {'name'    : 'id',
             'type'    : int,
             'primary' : True,
            },
            {'name'    : 'is_banned',
             'type'    : bool,
             'default' : False,
            },
            {'name'    : 'dcss_nick',
             'type'    : str,
             'default' : None,
            },
        ],
        'discord_servers' : [
            {'name'    : 'id',
             'type'    : int,
             'primary' : True,
            },
            {'name'    : 'allowed',
             'type'    : bool,
             'default' : False,
            },
            {'name'    : 'moderator_role',
             'type'    : int,
             'default' : None,
            },
            {'name'    : 'text_filter',
             'type'    : str,
             'default' : None,
            },
        ],
        'discord_channels' : [
            {'name'    : 'id',
             'type'    : int,
             'primary' : True,
            },
            {'name'    : 'allow_edits',
             'type'    : bool,
             'default' : False,
            },
        ],
        'irc_nicks' : [
            {'name'    : 'nick',
             'type'    : str,
             'primary' : True,
            },
            {'name'    : 'discord_id',
             'type'    : int,
             'unique'  : True,
             'default' : None,
            },
        ],
}

# Bot command functions
async def bot_help_command(source, user, args):
    """!help bot command"""

    help_text = source.help_text
    help_text = help_text.replace('\n', ' ')
    help_text = help_text.replace('%n', source.get_chat_name(source.bot_user))
    # Avoid the markdown filter.
    await source.channel.send(source.filter_text(help_text))

async def bot_listcommands_command(source, requester, args):
    """!listcommands chat command"""

    commands = []
    for com in bot_commands:
        try:
            source.check_bot_command(requester, bot_commands[com])

        except BotCommandException:
            continue

        commands.append(source.bot_command_prefix + com)

    commands.sort()
    await source.send_chat(f"Available commands: {', '.join(commands)}")

async def bot_botstatus_command(source, requester, args):
    """!botstatus chat command"""

    names = []
    for guild in source.manager.guilds:
        if guild.id in source.manager.allowed_servers:
            names.append(guild.name)
    await source.send_chat(f"Version: {__version__}; Listening to servers: "
            f"{', '.join(sorted(names))}")

async def bot_debugmode_command(source, requester, args):
    """!debugmode chat command"""

    state_desc = "on" if _log.isEnabledFor(logging.DEBUG) else "off"
    if args.toggle is None:
        await source.send_chat(
                f"DEBUG level logging is currently {state_desc}.")
        return

    if args.toggle == state_desc:
        raise BotCommandException(f"DEBUG level already set to {state_desc}")

    state_val = "DEBUG" if args.toggle == "on" else "INFO"
    _log.setLevel(state_val)

    await source.send_chat(f"DEBUG level logging set to {args.toggle}.")

async def bot_listroles_command(source, requester, args):
    """!listroles chat command"""

    roles = source.get_managed_roles()
    if not roles:
        raise BotCommandException("No available roles found.")

    await source.send_chat(', '.join(sorted(r.name for r in roles)))

async def bot_addrole_command(source, requester, args):
    """!addrole chat command"""

    if args.managed_role in args.user.roles:
        raise BotCommandException(
                f"User {args.user.name} already has role {args.managed_role}")

    await args.user.add_roles(args.managed_role)
    await source.send_chat(
            f"User {args.user.name} has been given role {args.managed_role}")

async def bot_removerole_command(source, requester, args):
    """!removerole chat command"""

    if args.managed_role not in args.user.roles:
        raise BotCommandException(
                f"User {args.user.name} does not have role {args.managed_role}")

    await args.user.remove_roles(args.managed_role)
    await source.send_chat(f"User {args.user.name} has lost role "
            f"{args.managed_role}")

async def bot_listfactions_command(source, requester, args):
    """!listfactions chat command"""

    factions = source.get_faction_roles()
    if not factions:
        raise BotCommandException("No available faction roles found.")

    await source.send_chat(', '.join(sorted(
        f.name[:-len(_faction_suffix)] for f in factions)))

async def bot_addfaction_command(source, requester, args):
    """!addfaction chat command"""

    factions = source.get_faction_roles()
    if not factions:
        raise BotCommandException("No available faction roles found.")

    faction = None
    role_lname = args.faction_role.name.lower()
    to_remove = list()
    lsuff = _faction_suffix.lower()
    for f in factions:
        base_name = f.name[:-len(_faction_suffix)]
        base_lname = base_name.lower()
        if (base_lname == role_lname
            or base_lname + lsuff == role_lname):
            faction = f

            if faction in args.user.roles:
                raise BotCommandException(f"User {args.user.name} already "
                        f"has faction {base_name}")

        elif f in args.user.roles:
            to_remove.append(f)

    if not faction:
        raise BotCommandException(f"Unknown faction: {args.faction_role}")

    # First remove any existing faction roles we had.
    if to_remove:
        await args.user.remove_roles(*to_remove)
        await asyncio.sleep(0.5)

    await args.user.add_roles(faction)
    await source.send_chat(f"User {args.user.name} has faction set to "
            f"{faction.name[:-len(_faction_suffix)]}")

    return

async def bot_removefaction_command(source, requester, args):
    """!removefaction chat command"""

    factions = source.get_faction_roles()
    to_remove = list()
    for f in factions:
        # We remove all faction roles in case there's been a mixup.
        if f in args.user.roles:
            to_remove.append(f)

    if to_remove:
        await args.user.remove_roles(*to_remove)
        await source.send_chat(f"User {args.user.name} has lost faction "
                "{}".format(', '.join(
                    [f.name[:-len(_faction_suffix)] for f in to_remove])))
    else:
        raise BotCommandException(f"User {args.user.name} has no faction role")

async def bot_glasses_command(source, requester, args):
    """!glasses chat command"""

    message = await source.channel.send('( •_•)')
    await asyncio.sleep(0.5)
    message = await message.edit(content='( •_•)>⌐■-■')
    await asyncio.sleep(0.5)
    await message.edit(content='(⌐■_■)')

async def bot_deal_command(source, requester, args):
    """!deal chat command"""

    glasses = '    ⌐■-■    '
    glasson = '   (⌐■_■)   '
    dealwith = 'deal with it'
    lines = ['            ',
             '            ',
             '            ',
             '    (•_•)   ']
    message = await source.channel.send("```{}```".format(
        '\n'.join(lines)))
    await asyncio.sleep(0.5)

    for i in range(3):
        msg = '\n'.join(lines[:i] + [glasses] + lines[i + 1:])
        message = await message.edit(content=f"```{msg}```")
        await asyncio.sleep(0.5)

    await message.edit(content="```{}```".format(
        '\n'.join(lines[:1] + [dealwith] + lines[2:3] + [glasson])))

async def bot_dance_command(source, requester, args):
    """!dance chat command"""

    figures = [':D|-<', ':D/-<', ':D|-<', r':D\\-<']
    message = await source.channel.send(figures[0])
    await asyncio.sleep(0.25)

    for n in range(2):
        for f in figures[0 if n else 1:]:
            message = await message.edit(content=f)
            await asyncio.sleep(0.25)

    await message.edit(content=figures[0])

async def bot_botdance_command(source, requester, args):
    """!botdance chat command"""

    figures = ['└[^_^]┐', '┌[^_^]┘']
    message = await source.channel.send(figures[0])
    await asyncio.sleep(0.25)

    for n in range(2):
        for f in figures[0 if n else 1:]:
            message = await message.edit(content=f)
            await asyncio.sleep(0.25)

    await message.edit(content=figures[0])

async def bot_say_command(source, requester, args):
    """!say chat command"""

    await args.channel.send(source.filter_text(args.message))

def center_string_in_line(string, line):
    len_line = len(line)
    len_str = len(string)

    if len_str >= len_line:
        num_trim = len_str - len_line
        start =  int(num_trim / 2)

        end = len_str - start
        if num_trim % 2:
            end -= 1

        return string[start:end]

    line_keep = len_line - len_str
    left_end = int(line_keep / 2)
    right_start = len_line - left_end
    if line_keep % 2:
       left_end += 1

    return f"{line[0:left_end]}{string}{line[right_start:]}"

def render_firestorm_explosion(lines, radius):
    newlines = list(lines)
    explosion = "#" + "#" * 2 * radius
    for n in range(0, len(lines)):
        newlines[n] = center_string_in_line(explosion, lines[n])

    return newlines

async def bot_firestorm_command(source, requester, args):
    """!firestorm chat command"""

    if not args.target:
        args.target = '#' + str(source.channel)

    floor_lines = [
            '...............',
            '...............',
            '...............',
            '...............',
            '...............',
            '...............',
            '...............']

    fire_lines = [
            '....§§§§§§§....',
            '....§§§§§§§....',
            '....§§§§§§§....',
            '....§§§§§§§....',
            '....§§§§§§§....',
            '....§§§§§§§....',
            '....§§§§§§§....']

    mid = int(len(floor_lines) / 2)
    floor_lines[mid] = center_string_in_line(args.target, floor_lines[mid])

    message = await source.channel.send("```\n{}```".format(
        '\n'.join(floor_lines)))
    await asyncio.sleep(1)

    for r in range(1, 5, 2):
        explosion = render_firestorm_explosion(floor_lines, r)
        message = await message.edit(content="```\n{}```".format('\n'.join(explosion)))
        await asyncio.sleep(0.2)

    await asyncio.sleep(0.6)
    fire_lines[mid] = center_string_in_line(args.target, fire_lines[mid])
    for i in range(0, 3):
        lines = list(fire_lines)
        for n in range(0, len(fire_lines)):
            if n == mid:
                continue

            num = random.randint(1, 4)
            coords = random.sample(range(0, 7), num)
            for c in coords:
                lines[n] = lines[n][:4 + c] + 'v' + lines[n][4 + c + 1:]

        message = await message.edit(content="```\n{}```".format('\n'.join(lines)))
        await asyncio.sleep(0.8)

def render_glaciate_explosion(lines, radius):
    newlines = list(lines)
    explosion = "#" + "#" * 2 * radius
    mid_n = len(lines) / 2
    for n in range(1, radius):
        i = 7 - n
        explosion = "#" + "#" * 2 * (n - 1)
        newlines[i] = center_string_in_line(explosion, lines[i])

    return newlines

async def bot_glaciate_command(source, requester, args):
    """!glaciate chat command"""

    if not args.target:
        args.target = '#' + str(source.channel)

    floor_lines = [
            '...............',
            '...............',
            '...............',
            '...............',
            '...............',
            '...............',
            '...............']

    ice_lines = [
            '.§§§§§§§§§§§§§.',
            '..§§§§§§§§§§§..',
            '...§§§§§§§§§...',
            '....§§§§§§§....',
            '.....§§§§§.....',
            '......§§§......',
            '.......§.......']

    mid = int(len(floor_lines) / 2)
    floor_lines[mid] = center_string_in_line(args.target, floor_lines[mid])

    message = await source.channel.send("```\n{}```".format(
        '\n'.join(floor_lines)))
    await asyncio.sleep(1)

    for r in range(1, 8, 2):
        explosion = render_glaciate_explosion(floor_lines, r)
        message = await message.edit(content="```\n{}```".format('\n'.join(explosion)))
        await asyncio.sleep(0.2)

    blasted = args.target
    if len(args.target) > 1:
        block_max = max(1, int(len(args.target) / 2))
        num = random.randint(1, block_max)
        coords = random.sample(range(0, len(args.target)), num)
        for c in coords:
            blasted = blasted[:c] + '8' + blasted[c + 1:]

    ice_lines[mid] = center_string_in_line(blasted, ice_lines[mid])
    await message.edit(content="```\n{}```".format('\n'.join(ice_lines)))

async def react_message(source, message, num):
    emoji = [e for e in message.channel.guild.emojis if not e.managed]
    for i in range(0, num):
        if not emoji:
            return

        ind = random.randint(0, len(emoji) - 1)
        await message.add_reaction(emoji[ind])

        emoji.remove(emoji[ind])
        await asyncio.sleep(0.25)

async def bot_reactstorm_command(source, requester, args):
    """!reactstorm chat command"""

    if not source.channel.guild.emojis:
        return

    if args.user is requester:
        args.user = None

    max_hist = 10
    reacts_left = 15
    seen_command = False
    async for m in source.channel.history(limit=max_hist):
        if m.author is args.user:
            num_reacts = random.randint(8, reacts_left)
            await react_message(source, m, num_reacts)
            return

        elif not args.user:
            # Don't react to the command itself.
            if (not seen_command
                and m.author is requester
                and m.content.startswith("!reactstorm")):
                seen_command = True
                continue

            num_reacts = random.randint(min(reacts_left, 5), reacts_left)
            await react_message(source, m, num_reacts)

            if reacts_left > 0:
                reacts_left = reacts_left - num_reacts
            else:
                return

async def bot_relay_command(source, requester, args):
    """!relay chat command"""

    dest_source = source.manager.make_channel_source(args.channel)
    await dest_source.read_chat(requester, args.message)

async def bot_pregen_command(source, requester, args):
    """!pregen chat command"""

    memes = ['elves', 'tentacles', 'free beer', 'optimal play',
            'Guaranteed Damage Reduction', 'dragon scales', 'Mephitic Cloud',
            "Borgnjor's Vile Clutch", 'casters', 'sword-and-board melee toons',
            'forks', 'wiki guides', 'crabs', 'crawlcode', 'Cheibriados', 'Xom',
            'Qazlal', 'Beogh', 'Sif Muna', 'Pakellas',
            'the Crown of Eternal Torment', 'purple chunks', 'bloatcrawl',
            "that's BANANAS", 'mikee teleport', 'splatratios', 'winrate',
            'high scores', 'ghost vaults', 'transporter vaults', 'runed doors',
            'role playing', 'traps',
            ]

    if not args.target:
        args.target = '#' + str(source.channel)

    header = f"Generating {args.target}...\n "
    footer = None
    footer_delay = 0
    message = None
    for i in range(1, 16):
        if i % 2 == 1:
            widget = '/o/'
        else:
            widget = '\\o\\'

        prefix = ' ' * (i - 1)
        suffix = ' ' * (12 - i)

        if not footer:
            footer = f"\n building {random.sample(memes, 1)[0]}   "
            footer_delay = 2
        else:
            footer_delay = footer_delay - 1

        content = f"```{header}\n{prefix}{widget}{suffix}\n{footer}```"

        if not message:
            message = await source.channel.send(content)
        else:
            await message.edit(content=content)

        if footer_delay <= 0:
            footer = None

        await asyncio.sleep(0.33)

async def bot_reactbomb_command(source, requester, args):
    """!reactbomb chat command"""

    if args.emote:
        match = _emote_regexp.match(args.emote)
        if match:
            emote = match.group(1)
            emote = discord.utils.get(source.channel.guild.emojis, id=int(emote))
        else:
            search = args.emote.lower()
            emote = discord.utils.find(lambda e: e.name.lower() == search,
                    source.channel.guild.emojis)

        if not emote:
            raise BotCommandException(f"Argument {args.emote} does not match a "
                    "server emote.")
    else:
        emote = random.choice([e for e in source.channel.guild.emojis
                               if not e.managed])
        if not emote:
            raise BotCommandException(f"Emote not specified and server "
                    "{source.channel.guild} has no emotes.")

    max_hist = 10
    seen_command = False
    reacts_left = random.randint(5, max_hist)
    async for m in source.channel.history(limit=max_hist):
        # Don't react to the command itself.
        if (not seen_command
            and m.author is requester
            and m.content.startswith("!reactbomb")):
            seen_command = True
            continue

        if reacts_left > 0:
            reacts_left = reacts_left - 1
        else:
            return

        await m.add_reaction(emote)
        await asyncio.sleep(0.25)

async def bot_dcssnick_command(source, requester, args):
    """!dcssnick chat command"""

    user_data = source.manager.get_user_data(args.user)
    if args.remove:
        if not user_data['dcss_nick']:
            await source.send_chat(f"User {args.user.name} already has no "
                                   "DCSS nick.")
            return

        await source.send_chat("Removed the DCSS nick of user "
                               f"{args.user.name}.")
        source.manager.set_user_field(args.user, 'dcss_nick', None)
        return

    if not args.nick:
        if user_data['dcss_nick']:
            await source.send_chat(f"User {args.user.name} has DCSS nick "
                    f"{user_data['dcss_nick']}")
        else:
            await source.send_chat(f"User {args.user.name} has no DCSS nick")

        return

    if not nick_regexp.match(args.nick):
        raise BotCommandException("DCSS nicks must contain only alphanumeric "
                "characters and '_' or '-'")

    source.manager.set_user_field(args.user, 'dcss_nick', args.nick)
    await source.send_chat(
            f"DCSS nick for user {args.user.name} is set to {args.nick}")

async def bot_allowserver_command(source, requester, args):
    """!allowserver chat command"""

    source.manager.set_server_field(args.server, 'allowed', True)
    source.manager.update_allowed_servers()
    await source.send_chat(f"Server {args.server.name} has been allowed.")

async def bot_disallowserver_command(source, requester, args):
    """!disallowserver chat command"""

    source.manager.set_server_field(args.server, 'allowed', False)
    source.manager.update_allowed_servers()
    await source.send_chat(f"Server {args.server.name} has been disallowed.")

async def bot_setmodrole_command(source, requester, args):
    """!setmodrole chat command"""

    source.manager.set_server_field(args.server, 'moderator_role', args.role.id)
    await source.send_chat(f"Moderator role for server {args.server.name} has "
            f"been set to role {args.role.name}.")

async def bot_removemodrole_command(source, requester, args):
    """!removemodrole chat command"""

    source.manager.set_server_field(args.server, 'moderator_role', 0)
    await source.send_chat(f"Moderator role for server {args.server.name} has "
            "been removed.")

async def bot_textfilter_command(source, requester, args):
    """!textfilter chat command"""

    server_data = source.manager.get_server_data(args.server)
    if not args.filter:
        if server_data['text_filter']:
            terms = server_data['text_filter']
            if not source.is_private:
                terms = ','.join([t[0] + "*" * len(t[1:])
                                  for t in terms.split(',')])
            await source.send_chat(f"Server {args.server.name} has text "
                                   f"filter {terms}")
        else:
            await source.send_chat(f"Server {args.server.name} has no text "
                    "filter")

        return

    source.manager.set_server_field(args.server, 'text_filter', args.filter)
    source.manager.update_text_filters()

    terms = args.filter
    if not source.is_private:
        terms = ','.join([t[0] + "*" * len(t[1:]) for t in terms.split(',')])
    await source.send_chat(
            f"Text filter for server {args.server.name} is set to {terms}")

async def bot_removetextfilter_command(source, requester, args):
    """!removetextfilter chat command"""

    source.manager.set_server_field(args.server, 'text_filter', '')
    source.manager.update_text_filters()
    await source.send_chat(f"Text filter for server {args.server.name} has "
            "been removed.")

async def bot_allowedits_command(source, requester, args):
    """!allowedits chat command"""

    source.manager.set_channel_field(args.channel, 'allow_edits', True)
    await source.send_chat(
            f"Channel <#{args.channel.id}> now allows Sequell edits.")

async def bot_disallowedits_command(source, requester, args):
    """!disallowedits chat command"""

    source.manager.set_channel_field(args.channel, 'allow_edits', False)
    await source.send_chat(
            f"Channel <#{args.channel.id}> no longer allows Sequell edits.")

async def bot_ban_command(source, requester, args):
    """!ban chat command"""

    if requester == args.user:
        await source.send_chat("You can't ban yourself!")
        return

    user_level = source.user_access_level(args.user)
    if user_level >= AccessLevel.BOT_ADMIN:
        await source.send_chat(f"User {args.user} is a bot admin and can't "
                               "be banned.")
        return

    if user_level <= AccessLevel.BANNED:
        await source.send_chat(f"User {args.user} is already banned.")
        return

    source.manager.set_user_field(args.user, 'is_banned', True)
    await source.send_chat(f"User {args.user} is now banned.")

async def bot_unban_command(source, requester, args):
    """!unban chat command"""

    if source.user_access_level(args.user) > AccessLevel.BANNED:
        await source.send_chat(f"User {args.user} is already not banned.")
        return

    source.manager.set_user_field(args.user, 'is_banned', False)
    await source.send_chat(f"User {args.user} is now unbanned.")

async def bot_ircnick_command(source, requester, args):
    """!ircnick chat command"""

    id_nicks = {}
    for nick, discord_id in source.manager.irc_nicks.items():
        id_nicks[discord_id] = nick

    if args.remove:
        if not args.nick:
            raise BotCommandException("No IRC nick provided to remove")

        if args.nick not in source.manager.irc_nicks:
            await source.send_chat(f"IRC nick is already unregistered.")
            return

        source.manager.remove_irc_nick(args.nick)
        await source.send_chat(f"Removed IRC nick {nick}.")
        return

    if args.reserve:
        if not args.nick:
            raise BotCommandException("No IRC nick provided to reserve")

        if not nick_regexp.match(args.nick):
            raise BotCommandException("IRC nicks must contain only"
                " alphanumeric characters and '_' or '-'")

        if args.nick in source.manager.irc_nicks:
            user_id = source.manager.irc_nicks[args.nick]
            if user_id:
                user = await source.manager.get_user(user_id)
                if user:
                    name = f"user {user.name}"
                else:
                    name = f"user ID {user_id}"
                await source.send_chat(f"Can't reserve IRC nick {args.nick}:"
                                       f" already registered to {name}")
            else:
                await source.send_chat(f"IRC nick {args.nick} is already"
                                       " reserved.")
            return

        source.manager.register_irc_nick(args.nick, None)
        await source.send_chat(f"IRC nick {args.nick} is now reserved.")
        return

    if not args.nick:
        if args.user.id in id_nicks:
            await source.send_chat(f"User {args.user.name} has IRC nick "
                                   f"{id_nicks[args.user.id]}")
        else:
            await source.send_chat(f"User {args.user.name} has no IRC nick")
        return

    if not nick_regexp.match(args.nick):
        raise BotCommandException("IRC nicks must contain only"
            " alphanumeric characters and '_' or '-'")

    reg_id = source.manager.irc_nicks.get(args.nick)
    # If the id for this nick is not None, we require the caller to remove
    # the existing registration first.
    if reg_id:
        if reg_id == args.user.id:
            await source.send_chat(f"IRC nick {args.nick} is already"
                                   f" registered to user {args.user.name}")
            return

        reg_user = await source.manager.get_user(reg_id)
        if reg_user:
            name = f"user {reg_user.name}"
        else:
            name = f"user ID {reg_id}"
        await source.send_chat(f"IRC nick {args.nick} is already registered to"
                               f" {name}. Remove this registration with -r"
                               " first.")
        return

    source.manager.register_irc_nick(args.nick, args.user)
    await source.send_chat(f"IRC nick for user {args.user.name} is now"
                           f" {args.nick}")


# Some common arguments.

# Designate an optional target user for an across-server command. Requires bot
# admin.
user_option = {
        'name'         : '-u',
        'metavar'      : 'USERNAME',
        'dest'         : 'user',
        'type'         : str,
        'default'      : None,
        'access_level' : AccessLevel.BOT_ADMIN,
        }

# Like user_option, but required.
user_arg = {
        'name'         : 'user',
        'metavar'      : 'USERNAME',
        'type'         : str,
        'aggregate'    : True,
        'access_level' : AccessLevel.BOT_ADMIN,
        }

# A user option for commands server mods can run.
server_user_option = user_option.copy()
server_user_option['access_level'] = AccessLevel.SERVER_MOD

# Designate an optional target server for an across-server command. Requires
# bot admin.
server_option = {
        'name'         : '-s',
        'dest'         : 'server',
        'type'         : str,
        'default'      : None,
        'access_level' : AccessLevel.BOT_ADMIN,
        }

# Like server_option, but required.
server_arg = {
        'name'         : 'server',
        'type'         : str,
        'aggregate'    : True,
        'access_level' : AccessLevel.BOT_ADMIN,
        }

channel_option = {
        'name'    : '-c',
        'dest'    : 'channel',
        'type'    : str,
        'default' : None,
        }

# Like channel_option, but as an argument.
channel_option_arg = {
        'name'      : 'channel',
        'type'      : str,
        'nargs'     : '?',
        'aggregate' : True,
        }

# Arguments for looking up discord roles. Managed and faction arguments
role_arg = { 'name' : 'role', 'type' : str, 'aggregate' : True }
managed_role_arg = { 'name' : 'managed_role', 'type' : str, 'metavar' : 'ROLE',
'aggregate' : True }
faction_role_arg = { 'name' : 'faction_role', 'type' : str, 'metavar' : 'ROLE',
'aggregate' : True }

# An optional target string for joke commands
target_arg = {
        'name'      : 'target',
        'type'      : str,
        'nargs'     : '?',
        'default'   : None,
        'aggregate' : True,
        }

# Discord bot commands
bot_commands = {
    # Bot admin commands.
    'botstatus' : {
        'access_level' : AccessLevel.BOT_ADMIN,
        'function'     : bot_botstatus_command,
    },
    'debugmode' : {
        'access_level' : AccessLevel.BOT_ADMIN,
        'logged'       : True,
        'function'     : bot_debugmode_command,
        'args'         : [ toggle_arg ],
    },
    'allowserver' : {
        'access_level' : AccessLevel.BOT_ADMIN,
        'logged'       : True,
        'function'     : bot_allowserver_command,
        'args'         : [ server_arg ],
    },
    'disallowserver' : {
        'access_level' : AccessLevel.BOT_ADMIN,
        'logged'       : True,
        'function'     : bot_disallowserver_command,
        'args'         : [ server_arg ],
    },
    'allowedits' : {
        'access_level' : AccessLevel.BOT_ADMIN,
        'require_guild': True,
        'logged'       : True,
        'function'     : bot_allowedits_command,
        'args'         : [ server_option, channel_option_arg ],
    },
    'disallowedits' : {
        'access_level' : AccessLevel.BOT_ADMIN,
        'require_guild': True,
        'logged'       : True,
        'function'     : bot_disallowedits_command,
        'args'         : [ server_option, channel_option_arg ],
    },
    'ban' : {
        'access_level' : AccessLevel.BOT_ADMIN,
        'logged'       : True,
        'function'     : bot_ban_command,
        'args'         : [ user_arg ],
    },
    'unban' : {
        'access_level' : AccessLevel.BOT_ADMIN,
        'logged'       : True,
        'function'     : bot_unban_command,
        'args'         : [ user_arg ],
    },
    'ircnick'     : {
        'function' : bot_ircnick_command,
        'args'     : [
            user_option,
            { 'name' : '-r', 'dest': 'remove', 'action' : 'store_true' },
            { 'name' : '-R', 'dest': 'reserve', 'action' : 'store_true' },
            { 'name' : 'nick', 'type' : str, 'nargs' : '?', 'default' : None },
            ],
    },

    # Server admin commands.
    'setmodrole' : {
        'function'      : bot_setmodrole_command,
        'access_level'  : AccessLevel.SERVER_ADMIN,
        'require_guild' : True,
        'args'          : [ server_option, role_arg ],
    },
    'removemodrole' : {
        'access_level'  : AccessLevel.SERVER_ADMIN,
        'require_guild' : True,
        'function'      : bot_removemodrole_command,
        'args'          : [ server_option ],
    },

    # Server mod commands.
    'textfilter' : {
        'access_level'  : AccessLevel.SERVER_MOD,
        'function'      : bot_textfilter_command,
        'args'          : [
            server_option,
            { 'name'      : 'filter',
              'type'      : str,
              'nargs'     : '?',
              'aggregate' : True},
            ],
    },
    'removetextfilter' : {
        'access_level'  : AccessLevel.SERVER_MOD,
        'function'      : bot_removetextfilter_command,
        'args'          : [ server_option ],
    },

    # User-level commands.
    'listcommands' : {
        'function' : bot_listcommands_command,
    },
    'bothelp' : {
        'function' : bot_help_command,
    },
    'listroles' : {
        'require_guild' : True,
        'function'      : bot_listroles_command,
        'args'          : [ server_option ],
    },
    'addrole' : {
        'require_guild' : True,
        'function'      : bot_addrole_command,
        'args'          : [ server_user_option, managed_role_arg ],
    },
    'removerole' : {
        'require_guild' : True,
        'function'      : bot_removerole_command,
        'args'          : [ server_user_option, managed_role_arg ],
    },
    'removefaction' : {
        'require_guild' : True,
        'function'      : bot_removefaction_command,
        'args'          : [ server_option, server_user_option ],
    },
    'listfactions' : {
        'require_guild' : True,
        'function'      : bot_listfactions_command,
        'args'          : [ server_option ],
    },
    'addfaction' : {
        'require_guild' : True,
        'function'      : bot_addfaction_command,
        'args'          : [ server_option, server_user_option,
            faction_role_arg ],
    },
    'dcssnick'     : {
        'function' : bot_dcssnick_command,
        'args'     : [
            user_option,
            { 'name' : '-r', 'dest': 'remove', 'action' : 'store_true' },
            { 'name' : 'nick', 'type' : str, 'nargs' : '?', 'default' : None },
            ],
    },

    # Joke commands
    'glasses' : {
        'access_level'  : AccessLevel.SERVER_MOD,
        'require_guild' : True,
        'function'      : bot_glasses_command,
    },
    'deal' : {
        'access_level'  : AccessLevel.SERVER_MOD,
        'require_guild' : True,
        'function'      : bot_deal_command,
    },
    'dance' : {
        'require_guild' : True,
        'function'      : bot_dance_command,
    },
    'botdance' : {
        'require_guild' : True,
        'function'      : bot_botdance_command,
    },
    'say' : {
        'access_level'  : AccessLevel.SERVER_MOD,
        'function'      : bot_say_command,
        'args'          : [
            server_option,
            channel_option,
            { 'name' : 'message', 'type' : str, 'aggregate' : True},
        ],
    },
    'firestorm' : {
        'access_level'  : AccessLevel.SERVER_MOD,
        'require_guild' : True,
        'function'      : bot_firestorm_command,
        'args'          : [ target_arg ],
    },
    'glaciate' : {
        'access_level'  : AccessLevel.SERVER_MOD,
        'require_guild' : True,
        'function'      : bot_glaciate_command,
        'args'          : [ target_arg ],
    },
    'reactstorm' : {
        'access_level'  : AccessLevel.SERVER_MOD,
        'require_guild' : True,
        'function'      : bot_reactstorm_command,
        'args'          : [
            { 'name' : 'user', 'type' : str, 'nargs' : '?', 'default' : None },
            ],
    },
    'relay' : {
        'access_level' : AccessLevel.SERVER_MOD,
        'function'     : bot_relay_command,
        'args'         : [
            server_option,
            channel_option,
            { 'name' : 'message', 'type' : str, 'aggregate' : True},
        ],
    },
    'pregen' : {
        'access_level'  : AccessLevel.SERVER_MOD,
        'require_guild' : True,
        'function'      : bot_pregen_command,
        'args'          : [ target_arg ],
    },
    'reactbomb' : {
        'access_level'  : AccessLevel.SERVER_MOD,
        'require_guild' : True,
        'function'      : bot_reactbomb_command,
        'args'          : [
            { 'name' : 'emote', 'type' : str, 'nargs' : '?', 'default' : None},
        ],
    },
}
