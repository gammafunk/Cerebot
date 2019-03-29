"""Creating and managing the Discord connection."""

import asyncio
if hasattr(asyncio, "async"):
    ensure_future = asyncio.async
else:
    ensure_future = asyncio.ensure_future

import discord
import logging
import os
import random
import re
import signal
import sys
import time
import traceback

from beem.chat import ChatWatcher, BotCommandException, bot_help_command

from .version import version as Version

_log = logging.getLogger()

# Used to split URLs in discord messages.
_url_regexp = (r'(https?://(?:\S+(?::\S*)?@)?(?:(?:[1-9]\d?|1\d\d|2[01]\d|22'
               r'[0-3])(?:\.(?:1?\d{1,2}|2[0-4]\d|25[0-5])){2}(?:\.(?:[1-9]\d?'
               r'|1\d\d|2[0-4]\d|25[0-4]))|(?:(?:[a-z\u00a1-\uffff0-9]+-?)*'
               r'[a-z\u00a1-\uffff0-9]+)(?:\.(?:[a-z\u00a1-\uffff0-9]+-?)*'
               r'[a-z\u00a1-\uffff0-9]+)*(?:\.(?:[a-z\u00a1-\uffff]{2,})))'
               r'(?::\d{2,5})?(?:/[^\s]*)?)')

# How long we allow inactivity in a channel before we remove its channel source
# object from the cache.
_channel_idle_timeout = 90 * 60

# How long we allow a chatter in a channel to be idle before we no longer
# include them in the $chat variable.
_chatter_idle_timeout = 60 * 60

class DiscordSource(ChatWatcher):
    """The channel source object that handles chat for any kind of discord
    channel. These objects are created as needed by the discord manager when
    activity is seen in a new discord channel and cached based on message
    activity."""

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
        if self.channel.is_private:
            return self.login_user
        else:
            return None

    @property
    def login_user(self):
        return self.manager.user

    def describe(self):
        channel_name = None
        if self.channel.is_private or not self.channel.name:
            channel_name = 'PM:{}'.format(self.channel.id)
        else:
            channel_name = '{}:#{}'.format(self.channel.server.name,
                    self.channel.name)
        return channel_name

    def should_limit_sequell_lines(self, sender):
        """We allow unlimited response lines for a single command in PM."""
        return not self.channel.is_private

    def get_chat_name(self, user, sanitize=False):
        return super().get_chat_name(user.name, sanitize)

    def get_dcss_nick(self, user):
        """Return the nick we have mapped for a given user. If the user's name
        has no valid characters, use their discord id instead."""
        name = self.get_chat_name(user, True)
        if not name:
            name = user.id

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

        if self.manager.user_is_admin(user):
            return True

        if user.bot or self.manager.user_is_ignored(user):
            return False

        return True

    def get_user_by_name(self, name):
        is_id = name.isdigit()
        if self.channel.is_private:
            for s in self.manager.servers:
                if is_id:
                    member = s.get_member(name)
                else:
                    member = s.get_member_named(name)

                if member:
                    return member

        else:
            if is_id:
                return self.channel.server.get_member(name)
            else:
                return self.channel.server.get_member_named(name)

    def get_vanity_roles(self):
        """Find which vanity roles are available on this server for use with
        the !addrole bot command."""

        # We must be associated with a server.
        if self.channel.is_private:
            return

        server = self.channel.server
        bot_role = None
        for r in server.roles:
            if r.name == "Bot" and r in server.me.roles:
                bot_role = r
                break

        if not bot_role:
            return

        roles = []
        for r in server.roles:
            # Only give roles with default permissions.
            if (r.position < bot_role.position
                and not r.is_everyone
                and r.permissions == server.default_role.permissions):
                roles.append(r)

        return roles

    def get_source_ident(self):
        """Get a unique identifier hash of the discord channel."""

        # Channels are uniquely identified by ID.
        return {"service" : self.manager.service, "id" : self.channel.id}

    def filter_markdown(self, message):
        """Escape most markdown from message output, being careful not to
        mangle any URLs and allowing backticks to remain."""

        parts = re.split(_url_regexp, message)
        result = ""
        for i, p in enumerate(parts):
            # URLs parts will always be at an odd index. These are
            # unmodified. Remove markdown characters from non-urls parts.
            if not i % 2:
                for c in "`*_~|":
                    p = p.replace(c, "\\" + c)
            result += p

        return result

    def filter_mentions(self, message):
        """Don't output anything that would be a mention, since people can
        abuse this to have the bot say them."""

        parts = re.split(r'(<@&?[0-9]+>)', message)
        result = ""
        for i, p in enumerate(parts):
            # The mentions will be at an even index.
            if i % 2:
                p = p.replace('@', '\\@')
            result += p

        return result

    def check_bot_command_restrictions(self, user, entry):
        super().check_bot_command_restrictions(user, entry)

        if self.manager.user_is_admin(user):
            return

        if entry.get("require_public_channel") and self.channel.is_private:
            raise BotCommandException(
                    "This command must be run in a public channel.")

    def expire_idle_chatters(self, current_time):
        """Remove any chatters from the list maintained for the $chat variable
        if they've been idle in the channel too long."""

        for c in list(self.chatters):
            if current_time - self.chatters[c] >= _chatter_idle_timeout:
                del self.chatters[c]

    @asyncio.coroutine
    def send_chat(self, message, message_type="normal"):
        """Clean up message output before sending it to chat."""

        # Clean up any markdown we don't want.
        if message_type == "monster":
            message = message.replace('```', r'\`\`\`')
        else:
            message = self.filter_markdown(message)
            message = self.filter_mentions(message)

        if message_type == "action":
            message = '_' + message + '_'
        # Put monster output in a code block for readability of the tightly
        # spaced info.
        elif message_type == "monster":
            message = '```\n' + message + '\n```'
        elif self.message_needs_escape(message):
            message = "]" + message

        yield from self.manager.send_message(self.channel, message)

    @asyncio.coroutine
    def read_chat(self, sender, content):
        current_time = time.time()
        if self.is_allowed_user(sender):
            self.chatters[sender] = current_time

        self.expire_idle_chatters(current_time)

        yield from super().read_chat(sender, content)


class DiscordManager(discord.Client):
    """Manages the discord client, recieving discord events and handling them
    or passing them to the appropriate channel source object."""

    def __init__(self, conf, dcss_manager, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.service = "Discord"
        self.conf = conf
        self.bot_commands = bot_commands

        self.single_user = False
        self.ping_task = None
        self.shutdown = False
        self.sources = set()

        self.dcss_manager = dcss_manager
        dcss_manager.managers["Discord"] = self

    def log_exception(self, error_msg):
        """Log an exception and the associated traceback."""

        exc_type, exc_value, exc_tb = sys.exc_info()
        _log.error("Discord Error: %s:", error_msg)
        _log.error("".join(traceback.format_exception(
            exc_type, exc_value, exc_tb)))

    @asyncio.coroutine
    def start_ping(self):
        """Start a repeating 10 second ping task to help connection
        stability."""

        while True:
            if self.is_closed:
                return

            try:
                yield from self.ws.ping()

            except asyncio.CancelledError:
                return

            except Exception:
                self.log_exception("Unable to send ping")
                ensure_future(self.disconnect())
                return

            yield from asyncio.sleep(10)

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
            if current_time - c.time_last_message >= _channel_idle_timeout:
                self.sources.remove(c)

    @asyncio.coroutine
    def on_message(self, message):
        """Handle a Discord chat message."""

        if not self.is_logged_in:
            return

        current_time = time.time()
        self.expire_idle_channels(current_time)

        source = self.get_channel_source(message.channel)
        if not source:
            source = DiscordSource(self, message.channel)
            self.sources.add(source)

        source.time_last_message = current_time

        # Make '*?' an alias to '@?' in Discord to avoid making mentions.
        content = message.content
        if content.startswith("*?"):
            content = '@' + content[1:]

        yield from source.read_chat(message.author, content)

    @asyncio.coroutine
    def on_ready(self):
        """Handle anything that needs to be done only after Discord is fully
        connected and ready. Currently only needed by the ping task."""

        self.ping_task = ensure_future(self.start_ping())

    @asyncio.coroutine
    def on_member_update(self, before, after):
        """Handle Discord member state changes. Currently only used to set a
        "streaming" role."""

        if not self.conf.get("set_streaming_role"):
            return

        streaming_role = None
        for r in after.server.roles:
            if r.name.lower() == "streaming":
                streaming_role = r
                break

        if not streaming_role:
            return

        if (after.game and after.game.type == 1
                and streaming_role not in after.roles):
            yield from self.add_roles(after, streaming_role)
            _log.info("Gave user %s on server %s streaming role", after,
                    after.server)
        elif ((not after.game or after.game.type != 1)
                and streaming_role in after.roles):
            yield from self.remove_roles(after, streaming_role)
            _log.info("Removed streaming role for user %s on server %s", after,
                    after.server)

    def get_source_by_ident(self, source_ident):
        """Given an 'identity' key tuple identifying a source, return the
        source object."""

        channel = self.get_channel(source_ident["id"])
        if not channel:
            return None

        return self.get_channel_source(channel)

    def user_is_admin(self, user):
        """Return True if the user is a bot admin in the given channel by our
        configuration."""

        if not self.conf.get('admins'):
            return False

        for u in self.conf['admins']:
            for s in self.servers:
                if s.get_member(u) == user:
                    return True

        return False

    def user_is_ignored(self, user):
        """Return True if the user is ignored in the given channel by our
        configuration."""

        if not self.conf.get('ignored_users'):
            return False

        for u in self.conf['ignored_users']:
            for s in self.servers:
                if s.get_member(u) == user:
                    return True

        return False

    @asyncio.coroutine
    def start(self):
        """Set the discord login token an connect, processing discord events
        indefinitely."""

        yield from self.login(self.conf['token'])
        yield from self.connect()

    @asyncio.coroutine
    def disconnect(self, shutdown=False):
        """Disconnect from Discord. This will log any disconnection error, but
        never raise."""

        if self.ping_task and not self.ping_task.done():
            self.ping_task.cancel()

        if self.conf.get("fake_connect") or self.is_closed:
            return

        try:
            yield from self.close()

        except Exception:
            self.log_exception("Error when disconnecting")

        self.shutdown = shutdown


@asyncio.coroutine
def bot_listcommands_command(source, user):
    """!listcommands chat command"""

    commands = []
    for com in bot_commands:
        try:
            source.check_bot_command_restrictions(user, bot_commands[com])

        except BotCommandException:
            continue

        commands.append(source.bot_command_prefix + com)

    commands.sort()
    yield from source.send_chat("Available commands: {}".format(
        ', '.join(commands)))

@asyncio.coroutine
def bot_botstatus_command(source, user):
    """!botstatus chat command"""

    mgr = source.manager
    report = "Version {}".format(Version)

    names = []
    for s in mgr.servers:
        names.append(s.name)

    names.sort()
    report = "Version: {}; Listening to servers: {}".format(Version,
            ", ".join(names))
    yield from source.send_chat(report)

@asyncio.coroutine
def bot_debugmode_command(source, user, state=None):
    """!debugmode chat command"""

    state_desc = "on" if _log.isEnabledFor(logging.DEBUG) else "off"
    if state is None:
        yield from source.send_chat(
                "DEBUG level logging is currently {}.".format(state_desc))
        return

    if state == state_desc:
        raise BotCommandException("DEBUG level already set to {}".format(
            state))

    state_val = "DEBUG" if state == "on" else "INFO"
    _log.setLevel(state_val)

    yield from source.send_chat("DEBUG level logging set to {}.".format(state))

@asyncio.coroutine
def bot_listroles_command(source, user):
    """!listroles chat command"""

    roles = source.get_vanity_roles()
    if not roles:
        raise BotCommandException("No available roles found.")

    yield from source.send_chat(', '.join(sorted(r.name for r in roles)))

@asyncio.coroutine
def bot_addrole_command(source, user, rolename):
    """!addrole chat command"""

    roles = source.get_vanity_roles()
    if not roles:
        raise BotCommandException("No available roles found.")

    for r in roles:
        if rolename.lower() != r.name.lower():
            continue

        if r in user.roles:
            raise BotCommandException(
                    "Member {} already has role {}".format(user.name,
                        rolename))

        yield from source.manager.add_roles(user, r)
        yield from source.send_chat(
                "Member {} has been given role {}".format(user.name, rolename))
        return

    raise BotCommandException("Unknown role: {}".format(rolename))

@asyncio.coroutine
def bot_removerole_command(source, user, rolename):
    """!removerole chat command"""

    roles = source.get_vanity_roles()
    for r in roles:
        if rolename.lower() != r.name.lower():
            continue

        if r not in user.roles:
            raise BotCommandException(
                    "Member {} does not have role {}".format(user.name,
                        rolename))

        yield from source.manager.remove_roles(user, r)
        yield from source.send_chat(
                "Member {} has lost role {}".format(user.name, rolename))
        return

    raise BotCommandException("Unknown role: {}".format(rolename))

@asyncio.coroutine
def bot_glasses_command(source, user):
    """!glasses chat command"""

    message = yield from source.manager.send_message(source.channel, '( •_•)')
    yield from asyncio.sleep(0.5)
    yield from source.manager.edit_message(message, '( •_•)>⌐■-■')
    yield from asyncio.sleep(0.5)
    yield from source.manager.edit_message(message, '(⌐■_■)')

@asyncio.coroutine
def bot_deal_command(source, user):
    """!deal chat command"""

    glasses = '    ⌐■-■    '
    glasson = '   (⌐■_■)   '
    dealwith = 'deal with it'
    lines = ['            ',
             '            ',
             '            ',
             '    (•_•)   ']
    mgr = source.manager
    message = yield from mgr.send_message(source.channel,
            '```{}```'.format('\n'.join(lines)))
    yield from asyncio.sleep(0.5)

    for i in range(3):
        yield from mgr.edit_message(message, '```{}```'.format(
            '\n'.join(lines[:i] + [glasses]+lines[i + 1:])))
        yield from asyncio.sleep(0.5)

    yield from mgr.edit_message(message, '```{}```'.format(
        '\n'.join(lines[:1] + [dealwith] + lines[2:3] + [glasson])))

@asyncio.coroutine
def bot_dance_command(source, user):
    """!dance chat command"""

    mgr = source.manager
    figures = [':D|-<', ':D/-<', ':D|-<', r':D\\-<']
    message = yield from mgr.send_message(source.channel, figures[0])
    yield from asyncio.sleep(0.25)

    for n in range(2):
        for f in figures[0 if n else 1:]:
            yield from mgr.edit_message(message, f)
            yield from asyncio.sleep(0.25)

    yield from mgr.edit_message(message, figures[0])

@asyncio.coroutine
def bot_botdance_command(source, user):
    """!botdance chat command"""

    mgr = source.manager
    figures = ['└[^_^]┐', '┌[^_^]┘']
    message = yield from mgr.send_message(source.channel, figures[0])
    yield from asyncio.sleep(0.25)

    for n in range(2):
        for f in figures[0 if n else 1:]:
            yield from mgr.edit_message(message, f)
            yield from asyncio.sleep(0.25)

    yield from mgr.edit_message(message, figures[0])

@asyncio.coroutine
def bot_say_command(source, user, server, channel, message):
    """!say chat command"""

    mgr = source.manager
    dest_server = None
    for s in mgr.servers:
        # Give exact matches priority
        if server.lower() == s.name.lower():
            dest_server = s
            break

        if server.lower() in s.name.lower():
            dest_server = s

    if not dest_server:
        raise BotCommandException("Can't find server match for {}, must "
                "match one of: {}".format(server, ", ".join(
                    sorted([s.name for s in mgr.servers]))))

    dest_channel = None
    chan_filt = lambda c: c.type == discord.ChannelType.text
    channels = list(filter(chan_filt, dest_server.channels))
    for c in channels:
        if channel.lower() == c.name.lower():
            dest_channel = c
            break

        elif channel.lower() in c.name.lower():
            dest_channel = c

    if not dest_channel:
        raise BotCommandException("Can't find channel match for {}, must "
                "match one of: {}".format(channel,
                    ", ".join(sorted([c.name for c in channels]))))

    yield from mgr.send_message(dest_channel, message)

def center_string_in_line(string, line):
   leftn = int((len(line) - len(string))/2)
   if len(string) % 2 == 0:
       leftn += 1

   rightn = int((len(string) - len(line))/2)
   if len(string) >= len(line):
       return string[rightn:leftn]
   else:
       return "{}{}{}".format(line[0:leftn], string, line[rightn:])

def render_firestorm_explosion(lines, radius):
    newlines = list(lines)
    explosion = "#" + "#" * 2 * radius
    for n in range(0, len(lines)):
        newlines[n] = center_string_in_line(explosion, lines[n])

    return newlines

@asyncio.coroutine
def bot_firestorm_command(source, user, target=None):
    """!firestorm chat command"""

    if not target:
        target = '#' + str(source.channel)

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

    mgr = source.manager
    mid = int(len(floor_lines) / 2)
    floor_lines[mid] = center_string_in_line(target, floor_lines[mid])

    message = yield from mgr.send_message(source.channel,
            '```{}```'.format('\n'.join(floor_lines)))
    yield from asyncio.sleep(1)

    for r in range(1, 5, 2):
        explosion = render_firestorm_explosion(floor_lines, r)
        message = yield from mgr.edit_message(message,
             '```{}```'.format('\n'.join(explosion)))
        yield from asyncio.sleep(0.2)

    yield from asyncio.sleep(0.6)
    fire_lines[mid] = center_string_in_line(target, fire_lines[mid])
    for i in range(0, 3):
        lines = list(fire_lines)
        for n in range(0, len(fire_lines)):
            if n == mid:
                continue

            num = random.randint(1, 4)
            coords = random.sample(range(0, 7), num)
            for c in coords:
                lines[n] = lines[n][:4 + c] + 'v' + lines[n][4 + c + 1:]

        yield from mgr.edit_message(message,
                '```{}```'.format('\n'.join(lines)))
        yield from asyncio.sleep(0.8)

def render_glaciate_explosion(lines, radius):
    newlines = list(lines)
    explosion = "#" + "#" * 2 * radius
    mid_n = len(lines) / 2
    for n in range(1, radius):
        i = 7 - n
        explosion = "#" + "#" * 2 * (n - 1)
        newlines[i] = center_string_in_line(explosion, lines[i])

    return newlines

@asyncio.coroutine
def bot_glaciate_command(source, user, target=None):
    """!glaciate chat command"""

    if not target:
        target = '#' + str(source.channel)

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

    mgr = source.manager
    mid = int(len(floor_lines) / 2)
    floor_lines[mid] = center_string_in_line(target, floor_lines[mid])

    message = yield from mgr.send_message(source.channel,
            '```{}```'.format('\n'.join(floor_lines)))
    yield from asyncio.sleep(1)

    for r in range(1, 8, 2):
        explosion = render_glaciate_explosion(floor_lines, r)
        message = yield from mgr.edit_message(message,
             '```{}```'.format('\n'.join(explosion)))
        yield from asyncio.sleep(0.2)

    blasted = target
    if len(target) > 1:
        block_max = max(1, int(len(target) / 2))
        num = random.randint(1, block_max)
        coords = random.sample(range(0, len(target)), num)
        for c in coords:
            blasted = blasted[:c] + '8' + blasted[c + 1:]

    ice_lines[mid] = center_string_in_line(blasted, ice_lines[mid])
    yield from mgr.edit_message(message,
            '```{}```'.format('\n'.join(ice_lines)))

# Discord bot commands
bot_commands = {
    "listcommands" : {
        "unlogged" : True,
        "function" : bot_listcommands_command,
    },
    "botstatus" : {
        "require_admin" : True,
        "function" : bot_botstatus_command,
    },
    "debugmode" : {
        "require_admin" : True,
        "args" : [
            {
                "pattern" : r"(on|off)$",
                "description" : "on|off",
                "required" : False
            } ],
        "source_restriction" : "admin",
        "function" : bot_debugmode_command,
    },
    "bothelp" : {
        "unlogged" : True,
        "function" : bot_help_command,
    },
    "listroles" : {
        "require_public_channel" : True,
        "unlogged" : True,
        "function" : bot_listroles_command,
    },
    "addrole" : {
        "require_public_channel" : True,
        "args" : [
            {
                "pattern" : r".+$",
                "description" : "ROLE",
                "required" : True
            } ],
        "function" : bot_addrole_command,
    },
    "removerole" : {
        "require_public_channel" : True,
        "args" : [
            {
                "pattern" : r".+$",
                "description" : "ROLE",
                "required" : True
            } ],
        "function" : bot_removerole_command,
    },
    "glasses" : {
        "require_public_channel" : True,
        "unlogged" : True,
        "source_restriction" : "channel",
        "function" : bot_glasses_command,
    },
    "deal" : {
        "require_public_channel" : True,
        "unlogged" : True,
        "function" : bot_deal_command,
    },
    "dance" : {
        "require_public_channel" : True,
        "unlogged" : True,
        "function" : bot_dance_command,
    },
    "botdance" : {
        "require_public_channel" : True,
        "unlogged" : True,
        "function" : bot_botdance_command,
    },
    "say" : {
        "require_admin" : True,
        "args" : [
            {
                "pattern" : r".+$",
                "description" : "SERVER",
                "required" : True
            },
            {
                "pattern" : r".+$",
                "description" : "CHANNEL",
                "required" : True
            },
            {
                "pattern" : r".+$",
                "description" : "MESSAGE",
                "required" : True
            },
        ],
        "function" : bot_say_command,
    },
    "firestorm" : {
        "require_public_channel" : True,
        "unlogged" : True,
        "args" : [
            {
                "pattern" : r".*",
                "description" : "target",
                "required" : False
            } ],
        "function" : bot_firestorm_command,
    },
    "glaciate" : {
        "require_public_channel" : True,
        "unlogged" : True,
        "args" : [
            {
                "pattern" : r".*",
                "description" : "target",
                "required" : False
            } ],
        "function" : bot_glaciate_command,
    },
}
