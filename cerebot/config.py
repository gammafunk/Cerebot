"""Cerebot configuration data."""

from beem.config import BotConfig

class CerebotConfig(BotConfig):
    """Handle configuration data loading for Cerebot."""

    def check_discord(self):
        """Check that there is a 'discord' table in the TOML data and that it
        has the necessary entries."""

        if not self.get("discord"):
            raise Exception("The discord table is undefined")

        self.require_table_fields('discord', self.discord, ['token'])

    def init_logging(self, name='cerebot'):
        """Initialize the 'cerebot' logger based on the configuration data."""

        super().init_logging(name)

    def load(self):
        """Read the main TOML configuration data from self.path and check that
        the configuration is valid."""

        super().load()

        if not self.get('db_file'):
            raise Exception("Field db_file undefined.")

        self.check_dcss()
        self.check_discord()
