# Cerebot

Cerebot is a multi-user chat bot that can relay queries to the IRC knowledge
bots for [DCSS](http://crawl.develz.org/wordpress/) from Twitch chat. See the
[bot command
guide](https://github.com/crawl/crawl/wiki/Guide-to-the-DCSS-knowledge-Bots)
for details on using beem from WebTiles chat.

### Details

Cerebot can listen to one or more channels on an discord server for which it is
authorized, and can also respond to private channel queries. It supports
rate-limiting responses to excessive messages on the IRC connection. It
also supports basic vanity role modification commands.

Cerebot is single-threaded and uses
[asyncio](https://docs.python.org/3/library/asyncio.html) to manage an
event loop with concurrent tasks.

### Installation

Python 3.8 is required, as are the following additional modules:

* irc (20.0 tested)
* pytoml (0.1.21 tested)
* discord (2.0 or later)
* [beem](https://github.com/gammafunk/beem)

All packages above except *beem* are available in PyPI. You can install
*beem* directly from its github repository using pip3. For example:

    pip3 install --user git+https://github.com/gammafunk/beem.git

### Configuration

Copy the [cerebot_config.toml.sample](cerebot_config.toml.sample) file to
`cerebot_config.toml` and edit the necessary fields based on how you'd like to run
the bot. The config file format is [toml](https://github.com/toml-lang/toml),
and the various fields you can change are in this file are documented in
comments.
