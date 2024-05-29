import configparser
import os
import sys
import random
import string
import typing

import msgspec
import platformdirs

from .client.client import (
    DEFAULT_RULES_URL,
    DEFAULT_DOWNLOAD_ROOT,
    DEFAULT_DOWNLOAD_TEMPLATE,
    DEFAULT_SKIP_TEMPORARY
)

DEFAULT_DIRECTORY = platformdirs.user_config_path('onlyfans_dl')
DEFAULT_PATH = DEFAULT_DIRECTORY / 'scrapers.conf'


def _x_bc() -> str:
    chars = string.digits + string.ascii_lowercase
    return ''.join(random.choice(chars) for _ in range(40))


class Scraper(msgspec.Struct, kw_only=True, omit_defaults=True):
    cookie: str = ''
    user_agent: str = ''
    proxy: str = ''
    x_bc: str = msgspec.field(default_factory=_x_bc)
    rules: str = DEFAULT_RULES_URL
    download_root: str = DEFAULT_DOWNLOAD_ROOT
    download_template: str = DEFAULT_DOWNLOAD_TEMPLATE
    skip_temporary: bool = DEFAULT_SKIP_TEMPORARY


Config = dict[str, Scraper]

ENCODING = 'utf-8-sig' if sys.platform == 'win32' else 'utf-8'


def read_config(config_file: str | os.PathLike[str] | None = None) -> Config:
    if config_file is None:
        config_file = DEFAULT_PATH

    with open(config_file, 'r', encoding=ENCODING) as f:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_file(f)

    result = msgspec.convert(parser, type=Config, strict=False)
    result.pop(parser.default_section, None)
    return result


def build_config(config_file: str | os.PathLike[str] | None = None, interactive: bool | None = None) -> Config:
    if config_file is None:
        config_file = DEFAULT_PATH
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()

    try:
        return read_config(config_file)
    except FileNotFoundError:
        if not interactive:
            raise

    scraper = Scraper()
    scraper_name = prompt_text('Enter a name for this scraper', 'scraper')
    scraper.cookie = prompt_text('Enter your cookie value', scraper.cookie)
    scraper.user_agent = prompt_text('Enter your user-agent', scraper.user_agent)
    scraper.x_bc = prompt_text('Enter your x-bc value', scraper.x_bc)
    scraper.rules = prompt_text('Enter rules URL', scraper.rules)
    scraper.download_root = prompt_text('Enter download root path', scraper.download_root)
    scraper.download_template = prompt_text('Enter download template', scraper.download_template)
    scraper.skip_temporary = prompt_yesno('Skip temporary posts', scraper.skip_temporary)

    data: dict[str, str | bool | int | float] = msgspec.to_builtins(scraper, str_keys=True)

    parser = configparser.ConfigParser(interpolation=None)
    assert isinstance(data, dict)
    parser.add_section(scraper_name)

    for key, value in data.items():
        assert isinstance(key, str)
        if isinstance(value, str):
            parser.set(scraper_name, key, value)
        elif isinstance(value, bool):
            parser.set(scraper_name, key, str(value).lower())
        elif isinstance(value, (int, float)):
            parser.set(scraper_name, key, str(value))
        else:
            typing.assert_never(value)

    os.makedirs(os.path.dirname(config_file), exist_ok=True)
    with open(config_file, 'w', encoding=ENCODING) as f:
        parser.write(f)

    print(f'Config file written: {config_file}')
    if not prompt_yesno('Would you like to begin scraping now', True):
        sys.exit()

    return { scraper_name: scraper }


def prompt_text(prompt: str, value: str = '') -> str:
    if value:
        prompt = '{} [{}]: '.format(prompt, value)
    else:
        prompt = '{}: '.format(prompt)

    result = input(prompt)

    if result:
        return result
    else:
        return value


def prompt_yesno(prompt: str, value: bool = False) -> bool:
    if value:
        prompt = '{}? (Y/n) '.format(prompt)
    else:
        prompt = '{}? (y/N) '.format(prompt)

    while True:
        text = input(prompt)
        if not text:
            return value

        text = text.lower()
        if text == 'y':
            return True
        elif text == 'n':
            return False
