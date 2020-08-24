"""Access and update the sqlite3 database."""

from beem.botdb import BotDB

class CerebotDB(BotDB):
    """Access to the database that's specific to tables used by Cerebot."""

    def get_user_data(self, username, default=True):
        """Get the user data of the given username from the cache of the user
        table as a dict. Return None if no entry exists."""

        return self.get_row('discord_users', [username], default)

    def set_user_field(self, username, field, value, create=True):
        """Set the given field to the given value in the user table for the
        given user. The database and user cache are both updated, and the user
        cache entry is returned. If the user doesn't exist and create is True,
        create the user first, otherwise missing users generate an
        exception."""

        return self.set_row_field('discord_users', [username], field, value,
                create)

    def get_server_data(self, server, default=False):
        """Get the server data of the given server from the cache of the server
        table. If default is True and the server isn't in the cache, return a
        row of default data. If default is False, missing servers generate
        None."""

        return self.get_row('discord_servers', [server], default)

    def set_server_field(self, server_id, field, value, create=True):
        return self.set_row_field('discord_servers', [server_id], field,
                value, create)
