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

from beem.chat import ChatWatcher, BotCommandException, bot_help_command
from beem.chat import ACCESS_BOT_ADMIN, toggle_arg

from .version import version as Version

_log = logging.getLogger()

class AccessLevel(enum.IntEnum):
    NORMAL       = 0
    SERVER_MOD   = enum.auto()
    SERVER_ADMIN = enum.auto()
    BOT_ADMIN    = ACCESS_BOT_ADMIN

# Used to split URLs in discord messages.
_url_regexp = re.compile(
r'(https?://(?:\S+(?::\S*)?@)?(?:(?:[1-9]\d?|1\d\d|2[01]\d|22'
r'[0-3])(?:\.(?:1?\d{1,2}|2[0-4]\d|25[0-5])){2}(?:\.(?:[1-9]\d?'
r'|1\d\d|2[0-4]\d|25[0-4]))|(?:(?:[a-z\u00a1-\uffff0-9]+-?)*'
r'[a-z\u00a1-\uffff0-9]+)(?:\.(?:[a-z\u00a1-\uffff0-9]+-?)*'
r'[a-z\u00a1-\uffff0-9]+)*(?:\.(?:[a-z\u00a1-\uffff]{2,})))'
r'(?::\d{2,5})?(?:/[^\s]*)?)')

_emote_regexp = re.compile(r'^<:[^>]+:([0-9]+)>$')

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
_reconnect_timeout = 5

class DiscordSource(ChatWatcher):
    """The channel source object that handles chat for any kind of discord
    channel. These objects are created as needed by the discord manager when
    activity is seen in a new discord channel and cached based on message
    activity."""

    # Users with access below this can be rate-limited.
    ACCESS_NO_LIMIT = AccessLevel.SERVER_MOD

    def __init__(self, manager, channel, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.manager = manager

        # The discord channel object this source is tied to.
        self.channel = channel
        self.source_type_desc = "channel"

        # Time since any message was last seen in the channel, used for the
        # Discord manager cache of these objects.
        self.time_last_message = None

        # Dict of recent chatters with users as keys and timestamp as values.
        self.chatters = {}

    # Set to the bot only if we're in PM, otherwise None.
    @property
    def user(self):
        if isinstance(self.channel, discord.abc.PrivateChannel):
            return self.login_user
        else:
            return None

    @property
    def login_user(self):
        return self.manager.user

    def describe(self):
        channel_name = None
        if isinstance(self.channel, discord.abc.PrivateChannel):
            channel_name = f'PM:{self.channel.id}'
        else:
            channel_name = f'{self.channel.guild.name}:#{self.channel.name}'
        return channel_name

    def should_limit_sequell_lines(self, sender):
        """We allow unlimited response lines for a single command in PM."""
        return isinstance(self.channel, discord.abc.GuildChannel)

    def get_chat_name(self, user, sanitize=False):
        return super().get_chat_name(user.name, sanitize)

    def get_dcss_nick(self, user):
        """Return the nick we have mapped for a given user. If the user's name
        has no valid characters, use their discord id instead."""

        user_data = self.manager.bot_db.get_user_data(user.id)
        if user_data.get('dcssnick'):
            return user_data['dcssnick']

        name = self.get_chat_name(user, True)
        if not name:
            name = str(user.id)

        return name

    def get_chat_dcss_nicks(self, sender):
        """Return a set of dcss nicks for users where we have a nick
        mapping."""

        nicks = set()
        for user in self.chatters:
            if not self.is_allowed_user(user):
                continue

            nick = self.get_dcss_nick(user)
            if nick:
                nicks.add(nick)

        return nicks

    def is_allowed_user(self, user):
        """Return true if the user is allowed to execute commands in the
        current channel."""

        return not user.bot

    def get_managed_roles(self):
        """Find which bot-managed roles are available on this guild for use
        with the !addrole bot command."""

        # We must be associated with a guild.
        if isinstance(self.channel, discord.abc.PrivateChannel):
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
        for r in guild.roles:
            if (r.position < bot_role.position
                # Don't want the everyone role
                and r is not guild.default_role
                # Don't want e.g. Twitch subscriber roles
                and not r.managed
                # We want only roles that have default role permissions, since
                # these are meant for users.
                and r.permissions == guild.default_role.permissions):
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
        if isinstance(self.channel, discord.abc.PrivateChannel):
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

        # Channels are uniquely identified by ID.
        return {'service' : self.manager.service, 'id' : self.channel.id}

    def filter_markdown(self, message):
        """Escape most markdown from message output, being careful not to
        mangle any URLs and allowing backticks to remain."""

        parts = _url_regexp.split(message)
        result = ""
        for i, p in enumerate(parts):
            # URLs parts are at odd indices. Place angle brackes around these
            # to disable discord preview.
            if i % 2:
                p = '<' + p + '>'
            # Remove markdown characters from non-url parts.
            else:
                p = discord.utils.escape_markdown(p)
            result += p

        return result

    def find_discord(self, iterable, search, no_match_error=True):
        """Find a discord object from an iterable by id, name string exact
        match, or name substring match, in order of preferance."""

        allow_substring = True
        if isinstance(iterable[0], discord.Guild):
            desc = "server"
        elif isinstance(iterable[0], discord.abc.GuildChannel):
            desc = "channel"
            # These have straightforward names and there can be a lot of them.
            allow_substring = False
        elif isinstance(iterable[0], discord.Role):
            desc = "role"
        else:
            raise Exception("Unknown discord object type search: {iterable[0]}")

        matches = []
        lsearch = search.lower()
        for i in iterable:
            # Lookup by id
            if search.isdigit() and i.id == int(search):
                return i

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
            if no_match_error:
                raise BotCommandException(f"Can't find {desc} match for "
                        f"{search}")
            else:
                return None

    def finalize_bot_command_args(self, user, args):
        """Set the target details of a bot command given its arguments. Find
        the guild and channel objects for the respective 'server' and 'channel'
        arguments."""

        def ambiguous_channel(search, desc):
            raise BotCommandException("Current channel has no server, so "
                    f"{desc} '{search}' is ambiguous")

        dest_server = None
        if isinstance(self.channel, discord.abc.GuildChannel):
            dest_server = self.channel.guild

        vargs = vars(args)
        if 'server' in vargs:
            if args.server:
                dest_server = self.find_discord(self.manager.guilds,
                        args.server)
            args.server = dest_server

        if 'user' in vargs:
            if args.user:
                if args.user.isdigit():
                    match = dest_server.get_member(int(args.user))
                else:
                    match = dest_server.get_member_named(args.user)
                if not match:
                    raise BotCommandException(
                            f"Can't find a user match with '{args.user}'")
                else:
                    args.user = match
            else:
                args.user = user

        if 'channel' in vargs:
            if args.channel:
                if not dest_server:
                    ambiguous_channel("channel name", args.channel)

                args.channel = self.find_discord(dest_server.channels,
                        args.channel)
            else:
                args.channel = self.channel

        if 'role' in vargs and args.role:
            if not dest_server:
                ambiguous_channel("role search", args.role)

            args.role = self.find_discord(dest_server.roles, args.role)

        if 'managed_role' in vargs and args.managed_role:
            if not dest_server:
                ambiguous_channel("role search", args.managed_role)

            args.managed_role = self.find_discord(self.get_managed_roles(),
                    args.managed_role)

        if 'faction_role' in vargs and args.faction_role:
            if not dest_server:
                ambiguous_channel("role search", args.faction_role)

            match = self.find_discord(self.get_managed_roles(),
                    args.faction_role, False)
            if match:
                args.faction_role = args.faction_role + _faction_suffix
            args.faction_role = self.find_discord(self.get_faction_roles(),
                    args.faction_role)


    def user_access_level(self, user):
        """Return True if the user is a bot admin in the given channel by our
        configuration."""

        user_data = self.manager.bot_db.get_user_data(user.id)
        if user_data['admin']:
            return AccessLevel.BOT_ADMIN

        if not isinstance(self.channel, discord.abc.GuildChannel):
            return AccessLevel.NORMAL

        if self.channel.permissions_for(user).administrator:
            return AccessLevel.SERVER_ADMIN

        server_data = self.manager.bot_db.get_server_data(
                self.channel.guild.id)
        if (server_data and server_data['moderator_role']
                and self.channel.guild.get_role(
                    server_data['moderator_role']) in user.roles):
            return AccessLevel.SERVER_MOD

        return AccessLevel.NORMAL

    def check_bot_command(self, user, entry):
        """Check overall bot command restrictions for this user."""

        super().check_bot_command(user, entry)

        if (entry.get('require_guild')
                and isinstance(self.channel, discord.abc.PrivateChannel)):
            raise BotCommandException(
                    "This command must be run in a public channel.")

    def expire_idle_chatters(self, current_time):
        """Remove any chatters from the list maintained for the $chat variable
        if they've been idle in the channel too long."""

        for c in list(self.chatters):
            if current_time - self.chatters[c] >= _chatter_idle_timeout:
                del self.chatters[c]

    async def send_chat(self, message, message_type='normal'):
        """Clean up message output before sending it to chat."""

        # Clean up any markdown we don't want.
        if message_type == 'monster':
            message = message.replace('```', r'\`\`\`')
        else:
            message = self.filter_markdown(message)
            message = discord.utils.escape_mentions(message)

        if message_type == 'action':
            message = '_' + message + '_'
        # Put monster output in a code block for readability of the tightly
        # spaced info.
        elif message_type == 'monster':
            message = '```\n' + message + '\n```'
        elif self.message_needs_escape(message):
            message = "]" + message

        await self.channel.send(message)

    async def read_chat(self, sender, content):
        current_time = time.time()
        if self.is_allowed_user(sender):
            self.chatters[sender] = current_time

        self.expire_idle_chatters(current_time)

        # Allow '*' instead of '@' for monster lookups to avoid mentions.
        if content.startswith("*?"):
            content = '@' + content[1:]

        await super().read_chat(sender, content)


class DiscordManager(discord.Client):
    """Manages the discord client, recieving discord events and handling them
    or passing them to the appropriate channel source object."""

    def __init__(self, conf, bot_db, dcss_manager, *args, **kwargs):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(*args, intents=intents, **kwargs)

        self.service = 'Discord'
        self.conf = conf

        self.bot_db = bot_db

        self.bot_commands = bot_commands

        self.shutdown = False
        self.sources = set()
        self.allowed_servers = set()
        self.allowed_pm = {}

        self.dcss_manager = dcss_manager
        dcss_manager.managers[self.service] = self

    def describe(self):
        return "Discord"

    def update_allowed_servers(self):
        self.allowed_servers = set()
        self.allowed_pm = {}
        for g in self.guilds:
            server_data = self.bot_db.get_server_data(g.id)
            if server_data and server_data['allowed']:
                self.allowed_servers.add(g.id)

    def log_error(self, error_msg):
        """Log an exception and the associated traceback."""

        exc_type, exc_value, exc_tb = sys.exc_info()
        _log.error(f"{self.describe()}: Error: {error_msg}:")
        _log.error("".join(traceback.format_exception(
            exc_type, exc_value, exc_tb)))

    def get_channel_source(self, channel):
        """Get the source object of the given discord channel object."""

        for s in self.sources:
            if s.channel == channel:
                return s

        return

    def expire_idle_channels(self, current_time):
        """Remove the cached source object for any channels that have been idle
        for too long."""

        for c in list(self.sources):
            if not c.time_last_message:
                continue

            if current_time - c.time_last_message >= _channel_idle_timeout:
                self.sources.remove(c)

    def make_channel_source(self, channel):

        source = self.get_channel_source(channel)
        if not source:
            source = DiscordSource(self, channel)
            self.sources.add(source)

        return source

    async def on_ready(self):
        """Handle anything that needs to be done only after Discord is fully
        connected and ready."""

        self.update_allowed_servers()

        appinfo = await self.application_info()
        if (appinfo.owner
                and not self.bot_db.get_user_data(appinfo.owner.id)['admin']):
            self.bot_db.set_user_field(appinfo.owner.id, 'admin', True)
            self.allowed_pm[appinfo.owner.id] = True

    async def on_message(self, message):
        """Handle a Discord chat message."""

        # Will be defined only if we've logged in.
        if not self.user:
            return

        allowed = False
        if (isinstance(message.channel, discord.abc.GuildChannel)
                and message.channel.guild.id in self.allowed_servers):
            allowed = True
        # Users are allowed to PM the bot if they're in an allowed server.
        # Cache this lookup for future messages.
        elif isinstance(message.channel, discord.abc.PrivateChannel):
            if message.author.id in self.allowed_pm:
                allowed = self.allowed_pm[message.author.id]
            else:
                # Admins are always allowed to PM.
                if self.bot_db.get_user_data(message.author.id)['admin']:
                    allowed = True
                else:
                    for s in self.allowed_servers:
                        s = self.get_guild(s)
                        member = s.get_member(message.author.id)
                        if member:
                            allowed = True
                            break

                self.allowed_pm[message.author.id] = allowed

        if not allowed:
            return

        current_time = time.time()
        self.expire_idle_channels(current_time)

        source = self.make_channel_source(message.channel)
        source.time_last_message = current_time

        await source.read_chat(message.author, message.content)

    async def on_member_update(self, before, after):
        """Handle Discord member state changes. Currently only used to set a
        "streaming" role."""

        streaming_role = None
        for r in after.guild.roles:
            if r.name.lower() == "streaming":
                streaming_role = r
                break

        if not streaming_role:
            return

        streaming = False
        for a in after.activities:
            if isinstance(a, discord.Streaming):
                streaming = True
                break

        if streaming and streaming_role not in after.roles:
            await after.add_roles(streaming_role)
            _log.info("Gave user %s on server %s streaming role", after,
                    after.guild)
        elif not streaming and streaming_role in after.roles:
            await after.remove_roles(streaming_role)
            _log.info("Removed streaming role for user %s on server %s", after,
                    after.guild)

    def get_source_by_ident(self, source_ident):
        """Given an 'identity' key tuple identifying a source, return the
        source object."""

        channel = self.get_channel(source_ident['id'])
        if not channel:
            return None

        return self.get_channel_source(channel)

    async def start(self):
        """Set the discord login token an connect, processing discord events
        indefinitely."""

        try:
            await self.login(self.conf['token'])
            await self.connect()
        except Exception:
            self.log_error("Error when attempting connection")
            await asyncio.sleep(_reconnect_timeout)

    async def disconnect(self, shutdown=False):
        """Disconnect from Discord. This will log any disconnection error, but
        never raise."""

        if self.conf.get('fake_connect') or self.is_closed():
            return

        try:
            await self.close()

        except Exception:
            self.log_error("Error when disconnecting")

        self.shutdown = shutdown

# Discord database tables and field definitions.
db_tables = {
        'discord_users' : [
            {'name'    : 'id',
             'type'    : int,
             'primary' : True,
            },
            {'name'    : 'dcssnick',
             'type'    : str,
             'default' : "",
            },
            {'name'    : 'admin',
             'type'    : bool,
             'default' : False,
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
             'default' : 0,
            },
        ],
}

# Bot command functions
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
    for s in source.manager.guilds:
        if s in source.manager.allowed_servers:
            s = source.manager.get_guild(s)
            names.append(s.name)
    await source.send_chat(f"Version: {Version}; Listening to servers: "
            f"{', '.join(sorted(names))}")

async def bot_debugmode_command(source, requester, args):
    """!debugmode chat command"""

    state_desc = "on" if _log.isEnabledFor(logging.DEBUG) else "off"
    if args.toggle is None:
        await source.send_chat(
                f"DEBUG level logging is currently {state_desc}.")
        return

    if args.toggle == state_desc:
        raise BotCommandException(f"DEBUG level already set to {state}")

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
    await message.edit(content='( •_•)>⌐■-■')
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
        await message.edit(content=f"```{msg}```")
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
            await message.edit(content=f)
            await asyncio.sleep(0.25)

    await message.edit(content=figures[0])

async def bot_botdance_command(source, requester, args):
    """!botdance chat command"""

    figures = ['└[^_^]┐', '┌[^_^]┘']
    message = await source.channel.send(figures[0])
    await asyncio.sleep(0.25)

    for n in range(2):
        for f in figures[0 if n else 1:]:
            await message.edit(content=f)
            await asyncio.sleep(0.25)

    await message.edit(content=figures[0])

async def bot_say_command(source, requester, args):
    """!say chat command"""

    await args.channel.send(args.message)

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
        await message.edit(content="```\n{}```".format('\n'.join(explosion)))
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

        await message.edit(content="```\n{}```".format('\n'.join(lines)))
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
        await message.edit(content="```\n{}```".format('\n'.join(explosion)))
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

    user_data = source.manager.bot_db.get_user_data(args.user.id)
    if not args.nick:
        if user_data['dcssnick']:
            await source.send_chat(f"User {args.user.name} has dcss nick "
                    f"{user_data['dcssnick']}")
        else:
            await source.send_chat(f"User {args.user.name} has no dcss nick")

        return

    nick_re = re.compile(r'^[a-zA-Z0-9_-]+$')
    if not nick_re.match(args.nick):
        raise BotCommandException("DCSS nicks must contain only alphanumeric "
                "characters and '_' or '-'")

    source.manager.bot_db.set_user_field(args.user.id, 'dcssnick', args.nick)
    await source.send_chat(
            f"DCSS nick for user {args.user.name} is set to {args.nick}")

async def bot_allowserver_command(source, requester, args):
    """!allowserver chat command"""

    mgr = source.manager
    server_data = mgr.bot_db.get_server_data(args.server.id)
    mgr.bot_db.set_server_field(args.server.id, 'allowed', True)
    mgr.update_allowed_servers()
    await source.send_chat(f"Server {args.server.name} has been allowed.")

async def bot_disallowserver_command(source, requester, args):
    """!disallowserver chat command"""

    mgr = source.manager
    server_data = mgr.bot_db.get_server_data(args.server.id)
    mgr.bot_db.set_server_field(args.server.id, 'allowed', False)
    mgr.update_allowed_servers()
    await source.send_chat(f"Server {args.server.name} has been disallowed.")

async def bot_setmodrole_command(source, requester, args):
    """!setmodrole chat command"""

    mgr = source.manager
    server_data = mgr.bot_db.get_server_data(args.server.id, True)
    mgr.bot_db.set_server_field(args.server.id, 'moderator_role', args.role.id)
    await source.send_chat(f"Moderator role for server {args.server.name} has "
            f"been set to role {args.role.name}.")

async def bot_removemodrole_command(source, requester, args):
    """!setmodrole chat command"""

    mgr = source.manager
    server_data = mgr.bot_db.get_server_data(args.server.id, True)
    mgr.bot_db.set_server_field(args.server.id, 'moderator_role', 0)
    await source.send_chat(f"Moderator role for server {args.server.name} has "
            "been removed.")

# Some common arguments.

# Designate an optional target user for an across-server command. Requires bot
# admin.
user_option = {
        'name'         : '-u',
        'dest'         : 'user',
        'type'         : str,
        'default'      : None,
        'access_level' : AccessLevel.BOT_ADMIN,
        }

# Like the above, but for server-level user commands mods can run.
server_user_option = user_option.copy()
server_user_option['access_level'] = AccessLevel.SERVER_MOD

# An optional user argument that can be used by anyone

# Designate an optional target server for an across-server command. Requires
# bot admin.
server_option = {
        'name'         : '-s',
        'dest'         : 'server',
        'type'         : str,
        'default'      : None,
        'access_level' : AccessLevel.BOT_ADMIN,
        }

# Like the above, but required.
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
    'listcommands' : {
        'function' : bot_listcommands_command,
    },
    'botstatus' : {
        'access_level' : AccessLevel.BOT_ADMIN,
        'function'     : bot_botstatus_command,
    },
    'debugmode' : {
        'access_level' : AccessLevel.BOT_ADMIN,
        'function'     : bot_debugmode_command,
        'args'         : [ toggle_arg ],
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
            { 'name' : 'nick', 'type' : str, 'nargs' : '?', 'default' : None },
            ],
    },
    'allowserver' : {
            'access_level' : AccessLevel.BOT_ADMIN,
            'function'     : bot_allowserver_command,
            'args'         : [ server_arg ],
    },
    'disallowserver' : {
            'access_level' : AccessLevel.BOT_ADMIN,
            'function'     : bot_disallowserver_command,
            'args'         : [ server_arg ],
    },
    'setmodrole' : {
            'require_guild' : True,
            'access_level'  : AccessLevel.SERVER_ADMIN,
            'function'      : bot_setmodrole_command,
            'args'          : [ server_option, role_arg ],
    },
    'removemodrole' : {
            'require_guild' : True,
            'access_level'  : AccessLevel.SERVER_ADMIN,
            'function'      : bot_removemodrole_command,
            'args'          : [ server_option ],
    },

    # Joke commands
    'glasses' : {
        'require_guild' : True,
        'function'      : bot_glasses_command,
    },
    'deal' : {
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
        'require_guild' : True,
        'function'      : bot_firestorm_command,
        'args'          : [ target_arg ],
    },
    'glaciate' : {
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
        'require_guild' : True,
        'function'      : bot_pregen_command,
        'args'          : [ target_arg ],
    },
    'reactbomb' : {
        'access_level'  : AccessLevel.SERVER_MOD,
        'require_guild' : True,
        'function'      : bot_reactbomb_command,
        'args'          : [ { 'name' : 'emote', 'type' : str, 'nargs' : '?',
            'default' : None} ],
    },
}
