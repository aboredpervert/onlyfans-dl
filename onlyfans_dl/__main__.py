import argparse
import concurrent.futures
import configparser
import logging
import pathlib
import random
import string
import sys
import time

import platformdirs
import requests
from requests.adapters import HTTPAdapter, Retry

from .client import OnlyFansScraper, ScrapingException, get_header_rules
from .client.structs import NormalizedMedia, User


logger = logging.getLogger()
console_handler = logging.StreamHandler(sys.stderr)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname).4s - %(message)s - (%(filename)s:%(lineno)s)'))
logger.addHandler(console_handler)
logger.setLevel(logging.INFO)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', '-c', type=pathlib.Path, default=pathlib.Path(platformdirs.user_config_dir('onlyfans_dl'), 'scrapers.conf'))
    parser.add_argument('--run-forever', action='store_true')
    parser.add_argument('users', nargs='*')
    return parser.parse_args()

def build_config(config_file: pathlib.Path) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    if config_file.exists():
        config.read(config_file)
    else:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        scraper_name = input('Enter a name for this scraper: ')
        config[scraper_name] = {}
        config[scraper_name]['cookie'] = input('Enter your cookie value: ')
        config[scraper_name]['user_agent'] = input('Enter your user agent value: ')
        config[scraper_name]['x_bc'] = input('Enter your x-bc value: ')
        with open(config_file, 'w') as f:
            config.write(f)
        print(f'Config file written: {config_file}')
        if input('Would you like to begin scraping now? (y/n)') == 'n':
            sys.exit()
    return config


def configure_clients(args: argparse.Namespace) -> list[OnlyFansScraper]:
    config_path: pathlib.Path = args.config
    config = build_config(config_path)
    clients = []
    for section in config:
        if section == 'DEFAULT':
            continue
        session = requests.Session()
        # Configure this session object to retry up to 10 times.
        # https://findwork.dev/blog/advanced-usage-python-requests-timeouts-retries-hooks/#retry-on-failure
        session.mount('https://', HTTPAdapter(max_retries=Retry(total=10, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
        if 'proxy' in config[section]:
            session.proxies = {'https': config[section]['proxy']}
        clients.append(OnlyFansScraper(
            section,
            session=session,
            header_rules=get_header_rules(),
            cookie=config[section].get('cookie', ''),
            user_agent=config[section].get('user_agent', ''),
            x_bc=config[section].get('x_bc', ''.join(random.choice(string.digits + string.ascii_lowercase) for _ in range(40))),
            download_root=config[section].get('download_root', 'downloads'),
            skip_temporary=config[section].getboolean('skip_temporary', False),
        ))
    return clients


def download(client: OnlyFansScraper, *, users: list[User] | None, chats: list[User] | None) -> None:
    if users is None:
        users = []
    if chats is None:
        chats = []

    user_medias: dict[User, list[NormalizedMedia]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        logger.info('gathering posts with scraper %s', client.name)
        future_to_user = {executor.submit(client.get_post_media_by_id, user.id): user for user in users}
        for future in concurrent.futures.as_completed(future_to_user):
            media_list = user_medias.get(future_to_user[future])
            if not media_list:
                user_medias[future_to_user[future]] = []
            user_medias[future_to_user[future]] += future.result()

        logger.info('gathering archived posts with scraper %s', client.name)
        future_to_user = {executor.submit(client.get_archived_post_media_by_id, user.id): user for user in users}
        for future in concurrent.futures.as_completed(future_to_user):
            media_list = user_medias.get(future_to_user[future])
            if not media_list:
                user_medias[future_to_user[future]] = []
            user_medias[future_to_user[future]] += future.result()

        logger.info('gathering messages with scraper %s', client.name)
        future_to_user = {executor.submit(client.get_message_media_by_id, user.id): user for user in chats}
        for future in concurrent.futures.as_completed(future_to_user):
            media_list = user_medias.get(future_to_user[future])
            if not media_list:
                user_medias[future_to_user[future]] = []
            user_medias[future_to_user[future]] += future.result()

        logger.info('gathering highlights with scraper %s', client.name)
        future_to_user = {executor.submit(client.get_highlight_media_by_id, user.id): user for user in users}
        for future in concurrent.futures.as_completed(future_to_user):
            media_list = user_medias.get(future_to_user[future])
            if not media_list:
                user_medias[future_to_user[future]] = []
            user_medias[future_to_user[future]] += future.result()

        if not client.skip_temporary:
            logger.info('gathering stories with scraper %s', client.name)
            future_to_user = {executor.submit(client.get_story_media_by_id, user.id): user for user in users}
            for future in concurrent.futures.as_completed(future_to_user):
                media_list = user_medias.get(future_to_user[future])
                if not media_list:
                    user_medias[future_to_user[future]] = []
                user_medias[future_to_user[future]] += future.result()
        else:
            logger.debug('skipping temporary items')

    if not any([user_medias[user] for user in user_medias]):
        logger.info('no new medias found')
        return

    for user, medias in user_medias.items():
        if medias:
            logger.info('found %d new medias for %s', len(medias), user.username)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        for _ in executor.map(client.download_media, user_medias.keys(), user_medias.values()):
            pass


def get_status_code(exception: Exception) -> int | None:
    if not isinstance(exception.__context__, requests.RequestException):
        return None

    response = exception.__context__.response
    if response is None:
        return None

    return response.status_code


def main() -> None:
    args = parse_args()
    clients = configure_clients(args)

    run_forever: bool = args.run_forever
    args_users: list[str] = args.users

    if run_forever:
        iteration = 0
        while True:
            iteration += 1
            logger.info('Starting iteration %d', iteration)
            for client in clients:
                if args_users:
                    users = [client.get_user_details(user) for user in args_users]
                else:
                    try:
                        users = client.get_subscriptions()
                        logger.info('got %d subscriptions with scraper %s', len(users), client.name)

                        chats = client.get_chats()
                        logger.info('got %d chats with scraper %s', len(chats), client.name)
                    except ScrapingException as e:
                        if status_code := get_status_code(e):
                            logger.error('failed to get subscriptions for scraper %s - status code %s', client.name, status_code)
                        else:
                            logger.error('failed to get subscriptions for scraper %s', client.name)
                        continue
                download(client, users=client.get_subscriptions(), chats=client.get_chats())
            time.sleep(5)
    else:
        for client in clients:
            if args_users:
                users = chats = [client.get_user_details(user) for user in args_users]
            else:
                try:
                    users = client.get_subscriptions()
                    logger.info('got %d subscriptions with scraper %s', len(users), client.name)

                    chats = client.get_chats()
                    logger.info('got %d chats with scraper %s', len(chats), client.name)
                except ScrapingException as e:
                    if status_code := get_status_code(e):
                        logger.error('failed to get subscriptions for scraper %s - status code %s', client.name, status_code)
                    else:
                        logger.error('failed to get subscriptions for scraper %s', client.name)
                    continue
            download(client, users=users, chats=chats)


if __name__ == '__main__':
    main()
