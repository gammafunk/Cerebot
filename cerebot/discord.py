"""Creating and managing the Discord connection."""

import asyncio
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

# How long to wait after a connection failure before reattempting the
# connection.
_RECONNECT_TIMEOUT = 5

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
            channel_name = 'PM:{}'.format(self.channel.id)
        else:
            channel_name = '{}:#{}'.format(self.channel.guild.name,
                    self.channel.name)
        return channel_name

    def should_limit_sequell_lines(self, sender):
        """We allow unlimited response lines for a single command in PM."""
        return isinstance(self.channel, discord.abc.GuildChannel)

    def get_chat_name(self, user, sanitize=False):
        return super().get_chat_name(user.name, sanitize)

    def get_dcss_nick(self, user):
        """Return the nick we have mapped for a given user. If the user's name
        has no valid characters, use their discord id instead."""
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

        if self.manager.user_is_admin(user):
            return True

        if user.bot or self.manager.user_is_ignored(user):
            return False

        return True

    def get_user_by_name(self, name):
        is_id = name.isdigit()
        if isinstance(self.channel, discord.abc.PrivateChannel):
            for g in self.manager.guilds:
                if is_id:
                    member = g.get_member(int(name))
                else:
                    member = g.get_member_named(name)

                if member:
                    return member

        else:
            if is_id:
                return self.channel.guild.get_member(int(name))
            else:
                return self.channel.guild.get_member_named(name)

    def get_vanity_roles(self):
        """Find which vanity roles are available on this guild for use with the
        !addrole bot command."""

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
        # ' Faction' and have a corresponding role without that suffix.
        role_names = [r.name for r in roles]
        faction_suff = " Faction"
        for r in guild.roles:
            if (r in roles
                and r.name.endswith(faction_suff)
                and r.name[:-len(faction_suff)] in role_names):
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
            if r.name == "Bot Faction" and r in guild.me.roles:
                bot_role = r
                break

        if not bot_role:
            return

        roles = self.get_vanity_roles()
        factions = []
        role_names = [r.name for r in roles]
        faction_suff = " Faction"
        for r in guild.roles:
            if (r.position < bot_role.position
                and r.name.endswith(faction_suff)
                and r.name[:-len(faction_suff)] in role_names):

                factions.append(r)

        return factions

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
            # URLs parts are at odd indices. Place angle brackes around these
            # to disable discord preview.
            if i % 2:
                p = '<' + p + '>'
            # Remove markdown characters from non-url parts.
            else:
                p = discord.utils.escape_markdown(p)
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

        if (entry.get("require_public_channel")
                and isinstance(self.channel, discord.abc.PrivateChannel)):
            raise BotCommandException(
                    "This command must be run in a public channel.")

    def expire_idle_chatters(self, current_time):
        """Remove any chatters from the list maintained for the $chat variable
        if they've been idle in the channel too long."""

        for c in list(self.chatters):
            if current_time - self.chatters[c] >= _chatter_idle_timeout:
                del self.chatters[c]

    async def send_chat(self, message, message_type="normal"):
        """Clean up message output before sending it to chat."""

        # Clean up any markdown we don't want.
        if message_type == "monster":
            message = message.replace('```', r'\`\`\`')
        else:
            message = self.filter_markdown(message)
            message = discord.utils.escape_mentions(message)

        if message_type == "action":
            message = '_' + message + '_'
        # Put monster output in a code block for readability of the tightly
        # spaced info.
        elif message_type == "monster":
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

    async def start_ping(self):
        """Start a repeating 10 second ping task to help connection
        stability."""

        while True:
            if self.is_closed:
                return

            try:
                await self.ws.ping()

            except asyncio.CancelledError:
                return

            except Exception:
                self.log_exception("Unable to send ping")
                asyncio.ensure_future(self.disconnect())
                return

            await asyncio.sleep(10)

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

    async def on_message(self, message):
        """Handle a Discord chat message."""

        # Will be defined only if we've logged in.
        if not self.user:
            return

        current_time = time.time()
        self.expire_idle_channels(current_time)

        source = self.make_channel_source(message.channel)
        source.time_last_message = current_time

        await source.read_chat(message.author, message.content)

    async def on_ready(self):
        """Handle anything that needs to be done only after Discord is fully
        connected and ready. Currently only needed by the ping task."""

        self.ping_task = asyncio.ensure_future(self.start_ping())

    async def on_member_update(self, before, after):
        """Handle Discord member state changes. Currently only used to set a
        "streaming" role."""

        if not self.conf.get("set_streaming_role"):
            return

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
            self.log_exception("Error when attempting connection")
            await asyncio.sleep(_RECONNECT_TIMEOUT)

    async def disconnect(self, shutdown=False):
        """Disconnect from Discord. This will log any disconnection error, but
        never raise."""

        if self.conf.get("fake_connect") or self.is_closed():
            return

        try:
            await self.close()

        except Exception:
            self.log_exception("Error when disconnecting")

        self.shutdown = shutdown


async def bot_listcommands_command(source, user):
    """!listcommands chat command"""

    commands = []
    for com in bot_commands:
        try:
            source.check_bot_command_restrictions(user, bot_commands[com])

        except BotCommandException:
            continue

        commands.append(source.bot_command_prefix + com)

    commands.sort()
    await source.send_chat("Available commands: {}".format(
        ', '.join(commands)))

async def bot_botstatus_command(source, user):
    """!botstatus chat command"""

    report = "Version {}".format(Version)
    names = []
    for s in source.manager.guilds:
        names.append(s.name)

    names.sort()
    report = "Version: {}; Listening to servers: {}".format(Version,
            ", ".join(names))
    await source.send_chat(report)

async def bot_debugmode_command(source, user, state=None):
    """!debugmode chat command"""

    state_desc = "on" if _log.isEnabledFor(logging.DEBUG) else "off"
    if state is None:
        await source.send_chat(
                "DEBUG level logging is currently {}.".format(state_desc))
        return

    if state == state_desc:
        raise BotCommandException("DEBUG level already set to {}".format(
            state))

    state_val = "DEBUG" if state == "on" else "INFO"
    _log.setLevel(state_val)

    await source.send_chat("DEBUG level logging set to {}.".format(state))

async def bot_listroles_command(source, user):
    """!listroles chat command"""

    roles = source.get_vanity_roles()
    if not roles:
        raise BotCommandException("No available roles found.")

    await source.send_chat(', '.join(sorted(r.name for r in roles)))

async def bot_addrole_command(source, user, rolename):
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

        await user.add_roles(r)
        await source.send_chat(
                "Member {} has been given role {}".format(user.name, rolename))
        return

    raise BotCommandException("Unknown role: {}".format(rolename))

async def bot_removerole_command(source, user, rolename):
    """!removerole chat command"""

    roles = source.get_vanity_roles()
    for r in roles:
        if rolename.lower() != r.name.lower():
            continue

        if r not in user.roles:
            raise BotCommandException(
                    "Member {} does not have role {}".format(user.name,
                        rolename))

        await user.remove_roles(r)
        await source.send_chat(
                "Member {} has lost role {}".format(user.name, rolename))
        return

    raise BotCommandException("Unknown role: {}".format(rolename))

async def bot_listfactions_command(source, user):
    """!listfactions chat command"""

    factions = source.get_faction_roles()
    if not factions:
        raise BotCommandException("No available faction roles found.")

    faction_suff = ' Faction'
    await source.send_chat(', '.join(sorted(
        f.name[:-len(faction_suff)] for f in factions)))

async def bot_addfaction_command(source, user, rolename):
    """!addfaction chat command"""

    factions = source.get_faction_roles()
    if not factions:
        raise BotCommandException("No available faction roles found.")

    faction = None
    role_lname = rolename.lower()
    faction_suff = ' Faction'
    to_remove = list()
    for f in factions:
        base_name = f.name[:-len(faction_suff)]
        base_lname = base_name.lower()
        if (base_lname == role_lname
            or base_lname + faction_suff.lower() == role_lname):
            faction = f

            if faction in user.roles:
                raise BotCommandException(
                        "Member {} already has faction {}".format(user.name,
                            base_name))

        elif f in user.roles:
            to_remove.append(f)

    if not faction:
        raise BotCommandException(
                "Unknown faction: {}".format(rolename))

    # First remove any existing faction roles we had.
    if to_remove:
        await user.remove_roles(*to_remove)
        await asyncio.sleep(0.5)

    await user.add_roles(faction)
    await source.send_chat(
            "Member {} has faction set to {}".format(user.name,
                faction.name[:-len(faction_suff)]))

    return

async def bot_removefaction_command(source, user):
    """!removefaction chat command"""

    factions = source.get_faction_roles()
    to_remove = list()
    for f in factions:
        if f in user.roles:
            to_remove.append(f)

    if to_remove:
        await user.remove_roles(*to_remove)
        await source.send_chat(
                    "Member {} has lost faction {}".format(user.name,
                        ", ".join([f.name for f in to_remove])))
        return

    raise BotCommandException("Member {} has no faction role".format(
        user.name))

async def bot_glasses_command(source, user):
    """!glasses chat command"""

    message = await source.channel.send('( •_•)')
    await asyncio.sleep(0.5)
    await message.edit(content='( •_•)>⌐■-■')
    await asyncio.sleep(0.5)
    await message.edit(content='(⌐■_■)')

async def bot_deal_command(source, user):
    """!deal chat command"""

    glasses = '    ⌐■-■    '
    glasson = '   (⌐■_■)   '
    dealwith = 'deal with it'
    lines = ['            ',
             '            ',
             '            ',
             '    (•_•)   ']
    message = await source.channel.send('```{}```'.format('\n'.join(lines)))
    await asyncio.sleep(0.5)

    for i in range(3):
        await message.edit(content='```{}```'.format(
            '\n'.join(lines[:i] + [glasses]+lines[i + 1:])))
        await asyncio.sleep(0.5)

    await message.edit(content='```{}```'.format(
        '\n'.join(lines[:1] + [dealwith] + lines[2:3] + [glasson])))

async def bot_dance_command(source, user):
    """!dance chat command"""

    figures = [':D|-<', ':D/-<', ':D|-<', r':D\\-<']
    message = await source.channel.send(figures[0])
    await asyncio.sleep(0.25)

    for n in range(2):
        for f in figures[0 if n else 1:]:
            await message.edit(content=f)
            await asyncio.sleep(0.25)

    await message.edit(content=figures[0])

async def bot_botdance_command(source, user):
    """!botdance chat command"""

    figures = ['└[^_^]┐', '┌[^_^]┘']
    message = await source.channel.send(figures[0])
    await asyncio.sleep(0.25)

    for n in range(2):
        for f in figures[0 if n else 1:]:
            await message.edit(content=f)
            await asyncio.sleep(0.25)

    await message.edit(content=figures[0])

async def bot_say_command(source, user, guild, channel, message):
    """!say chat command"""

    dest_guild = None
    for g in source.manager.guilds:
        # Give exact matches priority
        if guild.lower() == g.name.lower():
            dest_guild = g
            break

        if guild.lower() in g.name.lower():
            dest_guild = g

    if not dest_guild:
        raise BotCommandException("Can't find server match for {}, must "
                "match one of: {}".format(guild, ", ".join(
                    sorted([g.name for g in source.manager.guilds]))))

    dest_channel = None
    for c in dest_guild.channels:
        if not isinstance(c, discord.TextChannel):
            continue

        if channel.lower() == c.name.lower():
            dest_channel = c
            break

        elif channel.lower() in c.name.lower():
            dest_channel = c

    if not dest_channel:
        raise BotCommandException("Can't find channel match for {}, must "
                "match one of: {}".format(channel,
                    ", ".join(sorted([c.name for c in channels]))))

    await dest_channel.send(message)

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

    return "{}{}{}".format(line[0:left_end], string, line[right_start:])

def render_firestorm_explosion(lines, radius):
    newlines = list(lines)
    explosion = "#" + "#" * 2 * radius
    for n in range(0, len(lines)):
        newlines[n] = center_string_in_line(explosion, lines[n])

    return newlines

async def bot_firestorm_command(source, user, target=None):
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

    mid = int(len(floor_lines) / 2)
    floor_lines[mid] = center_string_in_line(target, floor_lines[mid])

    message = await source.channel.send(
            '```\n{}```'.format('\n'.join(floor_lines)))
    await asyncio.sleep(1)

    for r in range(1, 5, 2):
        explosion = render_firestorm_explosion(floor_lines, r)
        await message.edit(content='```\n{}```'.format('\n'.join(explosion)))
        await asyncio.sleep(0.2)

    await asyncio.sleep(0.6)
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

        await message.edit(content='```\n{}```'.format('\n'.join(lines)))
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

async def bot_glaciate_command(source, user, target=None):
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

    mid = int(len(floor_lines) / 2)
    floor_lines[mid] = center_string_in_line(target, floor_lines[mid])

    message = await source.channel.send(
            '```\n{}```'.format('\n'.join(floor_lines)))
    await asyncio.sleep(1)

    for r in range(1, 8, 2):
        explosion = render_glaciate_explosion(floor_lines, r)
        await message.edit(content='```\n{}```'.format('\n'.join(explosion)))
        await asyncio.sleep(0.2)

    blasted = target
    if len(target) > 1:
        block_max = max(1, int(len(target) / 2))
        num = random.randint(1, block_max)
        coords = random.sample(range(0, len(target)), num)
        for c in coords:
            blasted = blasted[:c] + '8' + blasted[c + 1:]

    ice_lines[mid] = center_string_in_line(blasted, ice_lines[mid])
    await message.edit(content='```\n{}```'.format('\n'.join(ice_lines)))

async def react_message(source, message, num):
    emoji = [e for e in message.channel.guild.emojis if not e.managed]
    for i in range(0, num):
        if not emoji:
            return

        ind = random.randint(0, len(emoji) - 1)
        await message.add_reaction(emoji[ind])

        emoji.remove(emoji[ind])
        await asyncio.sleep(0.25)

async def bot_reactstorm_command(source, user, target=None):
    """!reactstorm chat command"""

    if not source.channel.guild.emojis:
        return

    if target:
        target = source.get_user_by_name(target)
        if not target:
            return

    max_hist = 10
    reacts_left = 15
    seen_command = False
    async for m in source.channel.history(limit=max_hist):
        if target and m.author is target:
            num_reacts = random.randint(8, reacts_left)
            await react_message(source, m, num_reacts)
            return

        elif not target:
            # Don't react to the command itself.
            if (not seen_command
                and m.author is user
                and m.content.startswith("!reactstorm")):
                seen_command = True
                continue

            num_reacts = random.randint(min(reacts_left, 5), reacts_left)
            await react_message(source, m, num_reacts)

            if reacts_left > 0:
                reacts_left = reacts_left - num_reacts
            else:
                return

async def bot_relay_command(source, user, guild, channel, message):
    """!relay chat command"""

    dest_guild = None
    for g in source.manager.guilds:
        # Give exact matches priority
        if guild.lower() == g.name.lower():
            dest_guild = g
            break

        if guild.lower() in g.name.lower():
            dest_guild = g

    if not dest_guild:
        raise BotCommandException("Can't find server match for {}, must "
                "match one of: {}".format(guild, ", ".join(
                    sorted([g.name for g in source.manager.guilds]))))

    dest_channel = None
    for c in dest_guild.channels:
        if not isinstance(c, discord.TextChannel):
            continue

        if channel.lower() == c.name.lower():
            dest_channel = c
            break

        elif channel.lower() in c.name.lower():
            dest_channel = c

    if not dest_channel:
        raise BotCommandException("Can't find channel match for {}, must "
                "match one of: {}".format(channel,
                    ", ".join(sorted([c.name for c in channels]))))

    dest_source = source.manager.make_channel_source(dest_channel)
    await dest_source.read_chat(user, message)

async def bot_pregen_command(source, user, target=None):
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

    if not target:
        target = '#' + str(source.channel)

    header = 'Generating {}...\n '.format(target)
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
            footer = '\n building {}   '.format(random.sample(memes, 1)[0])
            footer_delay = 2
        else:
            footer_delay = footer_delay - 1

        content = '```{}\n{}{}{}\n{}```'.format(header, prefix, widget, suffix,
                footer)

        if not message:
            message = await source.channel.send(content)
        else:
            await message.edit(content=content)

        if footer_delay <= 0:
            footer = None

        await asyncio.sleep(0.33)

async def bot_reactbomb_command(source, user, emote=None):
    """!reactbomb chat command"""

    if not emote:
        emote = random.choice([e for e in source.channel.guild.emojis
                               if not e.managed])
    else:
        for e in source.channel.guild.emojis:
            if e.name == emote:
                emote = e
                break

    max_hist = 10
    seen_command = False
    reacts_left = random.randint(5, max_hist)
    async for m in source.channel.history(limit=max_hist):
        # Don't react to the command itself.
        if (not seen_command
            and m.author is user
            and m.content.startswith("!reactbomb")):
            seen_command = True
            continue

        if reacts_left > 0:
            reacts_left = reacts_left - 1
        else:
            return

        await m.add_reaction(emote)
        await asyncio.sleep(0.25)

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
    "removefaction" : {
        "require_public_channel" : True,
        "function" : bot_removefaction_command,
    },
    "listfactions" : {
        "require_public_channel" : True,
        "unlogged" : True,
        "function" : bot_listfactions_command,
    },
    "addfaction" : {
        "require_public_channel" : True,
        "args" : [
            {
                "pattern" : r".+$",
                "description" : "ROLE",
                "required" : True
            } ],
        "function" : bot_addfaction_command,
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
    "reactstorm" : {
        "require_admin" : True,
        "require_public_channel" : True,
        "unlogged" : True,
        "args" : [
            {
                "pattern" : r".*",
                "description" : "target",
                "required" : False
            } ],
        "function" : bot_reactstorm_command,
    },
    "relay" : {
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
            } ],
        "function" : bot_relay_command,
    },
    "pregen" : {
        "require_public_channel" : True,
        "unlogged" : True,
        "args" : [
            {
                "pattern" : r".*",
                "description" : "target",
                "required" : False
            } ],
        "function" : bot_pregen_command,
    },
    "reactbomb" : {
        "require_admin" : True,
        "require_public_channel" : True,
        "unlogged" : True,
        "args" : [
            {
                "pattern" : r".*",
                "description" : "emote",
                "required" : False
            } ],
        "function" : bot_reactbomb_command,
    },
}
