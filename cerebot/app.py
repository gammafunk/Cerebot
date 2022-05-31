"""cerebot: A Discord chat bot that can relay queries to the DCSS knowledge
bots on IRC."""

import argparse
import asyncio
import functools
import logging
import os
import signal
import sys
import traceback

from beem.dcss import DCSSManager

from .botdb import CerebotDB
from .config import CerebotConfig
from .discord import DiscordManager, db_tables
from .version import __version__

## Will be configured by Cerebot after the config is loaded.
_log = logging.getLogger(__name__)

_DEFAULT_CONFIG_FILE = "cerebot_config.toml"

class Cerebot:
    """Cerebot. Load the configuration and runs the tasks for the DCSS and
    Discord managers."""

    def __init__(self, config_file):
        self.loop = asyncio.get_event_loop()
        self.shutdown_error = False

        self.conf = CerebotConfig(config_file)

        try:
            self.conf.load()

        except Exception:
            self.critical_error(
                    f"App Error loading config file {self.conf.path}:")

        self.bot_db = CerebotDB(self.conf.db_file, db_tables)
        try:
            self.bot_db.load_db()

        except Exception:
            self.critical_error(f"unable to load DB file {self.conf.db_file}:")

        self.dcss_task = None
        self.dcss_manager = DCSSManager(self.conf.dcss)

        self.discord_task = None
        self.discord_manager = DiscordManager(self.conf.discord,
                self.bot_db, self.dcss_manager)

    def critical_error(self, error_msg):
        exc_type, exc_value, exc_tb = sys.exc_info()
        _log.critical("App Error: %s", error_msg)
        _log.error("".join(traceback.format_exception(
            exc_type, exc_value, exc_tb)))
        sys.exit(1)

    def start(self):
        """Start the bot, set up the event loop and signal handlers, and exit
        when the manager tasks finish."""

        _log.info("Starting bot.")

        def do_exit(signame):
            is_error = True if signame == "SIGTERM" else False
            msg = f"Shutting bot down due to signal: {signame}"

            if is_error:
                _log.error(msg)
            else:
                _log.info(msg)
            self.stop(is_error)

        for signame in ("SIGINT", "SIGTERM"):
            self.loop.add_signal_handler(getattr(signal, signame),
                                           functools.partial(do_exit, signame))

        print("Bot event loop running, press Ctrl+C to interrupt.")
        print(f"Process ID {os.getpid()}: send SIGINT or SIGTERM to exit.")

        try:
            self.loop.run_until_complete(self.process())

        except asyncio.CancelledError:
            pass

        self.loop.close()
        sys.exit(self.shutdown_error)

    def stop(self, is_error=False):
        """Stop the app by canceling any ongoing manager tasks, which will
        cause this app process to exit."""

        _log.info("Stopping bot.")
        self.shutdown_error = is_error

        if self.dcss_task and not self.dcss_task.done():
            self.dcss_task.cancel()

        if self.discord_task and not self.discord_task.done():
            asyncio.ensure_future(self.discord_manager.stop())

    async def process(self):

        tasks = []
        # This task is never restarted.
        self.dcss_task = asyncio.ensure_future(self.dcss_manager.start())
        tasks.append(self.dcss_task)

        self.discord_task = asyncio.ensure_future(
                self.discord_manager.start())
        tasks.append(self.discord_task)

        await asyncio.wait(tasks, return_when=asyncio.ALL_COMPLETED)


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("-c", dest="config_file", metavar="<toml-file>",
                        default=_DEFAULT_CONFIG_FILE,
                        help="bot config file.")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    bot = Cerebot(args.config_file)
    bot.start()
