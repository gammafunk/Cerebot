from setuptools import setup
from pathlib import Path

this_directory = Path(__file__).parent
exec((this_directory / 'cerebot/version.py').read_text(encoding='utf-8'))

short_description = ("A Discord chat bot that relays queries to IRC"
" knowledge bots for Dungeon Crawl: Stone Soup")
long_description = (this_directory / "README.md").read_text()

setup(
    name='Cerebot',
    version=__version__,
    description=short_description,
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='https://github.com/gammafunk/cerebot',
    author='gammafunk',
    author_email='gammafunk@gmail.com',
    packages=['cerebot'],
    setup_requires = [
        "beem",
        "discord",
        "irc",
        "pytoml",
        "websockets"
        ],
    data_files=[('share/cerebot', ['cerebot_config.toml.sample'])],
    entry_points={
        'console_scripts': [
            'cerebot=cerebot.app:main',
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Topic :: Games/Entertainment :: Role-Playing",
        "License :: OSI Approved :: GNU General Public License v2 (GPLv2)",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
    ],
    platforms='all',
    license='GPLv2',
)
