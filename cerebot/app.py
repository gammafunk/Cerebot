#!/usr/bin/env python3

"""cerebot: A Discord chat bot that can relay queries to the IRC
knowledge bots for DCSS.

"""

import argparse
import asyncio
import functools
import logging
import os
import signal
import sys
import traceback

from beem.dcss import DCSSManager

from .discord import DiscordManager
from .config import CerebotConfig
from .version import version

## Will be configured by Cerebot after the config is loaded.
_log = logging.getLogger()

_DEFAULT_CONFIG_FILE = "cerebot_config.toml"

class Cerebot:
    """Cerebot. Load the configuration and runs the tasks for the DCSS
    and Discord managers.

    """

    def __init__(self, config_file):
        self.dcss_task = None
        self.discord_task = None
        self.loop = asyncio.get_event_loop()
        self.shutdown_error = False

        self.conf = CerebotConfig(config_file)

        try:
            self.conf.load()

        except Exception:
            self.critical_error("App Error loading config file {}:".format(
                self.conf.path))

        self.dcss_manager = DCSSManager(self.conf.dcss)
        self.discord_manager = None

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
            msg = "Shutting down bot due to signal: {}".format(signame)

            if is_error:
                _log.error(msg)
            else:
                _log.info(msg)
            self.stop(is_error)

        for signame in ("SIGINT", "SIGTERM"):
            self.loop.add_signal_handler(getattr(signal, signame),
                                           functools.partial(do_exit, signame))

        print("Event loop running forever, press Ctrl+C to interrupt.")
        print("pid {}: send SIGINT or SIGTERM to exit.".format(os.getpid()))

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
            asyncio.ensure_future(self.discord_manager.disconnect(True))

    async def process(self):

        # This task is never restarted.
        self.dcss_task = asyncio.ensure_future(self.dcss_manager.start())

        while True:

            # Discord manager initial setup or it is reconnecting.
            if not self.discord_manager or not self.discord_manager.shutdown:
                # Let the current task finish.
                if self.discord_task and not self.discord_task.done():
                    await self.discord_task

                # We re-instantiate the manager and create a new websocket.
                self.discord_manager = DiscordManager(self.conf.discord,
                                                      self.dcss_manager)
                self.discord_task = asyncio.ensure_future(
                        self.discord_manager.start())

            await asyncio.wait([self.dcss_task, self.discord_task],
                    return_when=asyncio.FIRST_COMPLETED)

            # We are shutting down the bot.
            if self.dcss_task.done():
                if self.discord_task and not self.discord_task.done():
                    await self.discord_task
                return

def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("-c", dest="config_file", metavar="<toml-file>",
                        default=_DEFAULT_CONFIG_FILE,
                        help="bot config file.")
    parser.add_argument("--version", action="version", version=version)
    args = parser.parse_args()

    bot = Cerebot(args.config_file)
    bot.start()
