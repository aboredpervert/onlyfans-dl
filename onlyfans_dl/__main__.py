import argparse
import concurrent.futures
import logging
import os
import pathlib
import time
import typing

import requests

from .logging import setup_logging
from .config import DEFAULT_PATH, build_config
from .client import OnlyFansScraper, ScrapingException, get_header_rules
from .client.structs import NormalizedMedia, User

LOGGER = logging.getLogger(__name__)


def parse_path(value: str) -> pathlib.Path:
    return pathlib.Path(os.path.normpath(os.path.abspath(value)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', '-c', type=parse_path, default=DEFAULT_PATH)
    parser.add_argument('--run-forever', action='store_true')
    parser.add_argument('users', nargs='*')
    return parser.parse_args()


def configure_clients(args: argparse.Namespace) -> list[OnlyFansScraper]:
    config_path: pathlib.Path = args.config
    config = build_config(config_path)
    clients = []
    for scraper_name, scraper in config.items():
        session = requests.Session()

        # Configure this session object to retry up to 10 times.
        # https://findwork.dev/blog/advanced-usage-python-requests-timeouts-retries-hooks/#retry-on-failure
        retry = requests.adapters.Retry(total=10, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        typing.cast(requests.adapters.HTTPAdapter, session.get_adapter('http://')).max_retries = retry
        typing.cast(requests.adapters.HTTPAdapter, session.get_adapter('https://')).max_retries = retry

        if scraper.proxy:
            session.proxies = { 'all': scraper.proxy }
            session.trust_env = False

        clients.append(OnlyFansScraper(
            scraper_name,
            session=session,
            header_rules=get_header_rules(session, scraper.rules),
            cookie=scraper.cookie,
            user_agent=scraper.user_agent,
            x_bc=scraper.x_bc,
            download_root=scraper.download_root,
            download_template=scraper.download_template,
            skip_temporary=scraper.skip_temporary,
        ))
    return clients


def download(client: OnlyFansScraper, *, users: list[User] | None, chats: list[User] | None) -> None:
    if users is None:
        users = []
    if chats is None:
        chats = []

    user_medias: dict[User, list[NormalizedMedia]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        LOGGER.info('gathering posts with scraper %s', client.name)
        future_to_user = {executor.submit(client.get_post_media_by_id, user.id): user for user in users}
        for future in concurrent.futures.as_completed(future_to_user):
            media_list = user_medias.get(future_to_user[future])
            if not media_list:
                user_medias[future_to_user[future]] = []
            user_medias[future_to_user[future]] += future.result()

        LOGGER.info('gathering archived posts with scraper %s', client.name)
        future_to_user = {executor.submit(client.get_archived_post_media_by_id, user.id): user for user in users}
        for future in concurrent.futures.as_completed(future_to_user):
            media_list = user_medias.get(future_to_user[future])
            if not media_list:
                user_medias[future_to_user[future]] = []
            user_medias[future_to_user[future]] += future.result()

        LOGGER.info('gathering messages with scraper %s', client.name)
        future_to_user = {executor.submit(client.get_message_media_by_id, user.id): user for user in chats}
        for future in concurrent.futures.as_completed(future_to_user):
            media_list = user_medias.get(future_to_user[future])
            if not media_list:
                user_medias[future_to_user[future]] = []
            user_medias[future_to_user[future]] += future.result()

        LOGGER.info('gathering highlights with scraper %s', client.name)
        future_to_user = {executor.submit(client.get_highlight_media_by_id, user.id): user for user in users}
        for future in concurrent.futures.as_completed(future_to_user):
            media_list = user_medias.get(future_to_user[future])
            if not media_list:
                user_medias[future_to_user[future]] = []
            user_medias[future_to_user[future]] += future.result()

        if not client.skip_temporary:
            LOGGER.info('gathering stories with scraper %s', client.name)
            future_to_user = {executor.submit(client.get_story_media_by_id, user.id): user for user in users}
            for future in concurrent.futures.as_completed(future_to_user):
                media_list = user_medias.get(future_to_user[future])
                if not media_list:
                    user_medias[future_to_user[future]] = []
                user_medias[future_to_user[future]] += future.result()
        else:
            LOGGER.debug('skipping temporary items')

    if not any([user_medias[user] for user in user_medias]):
        LOGGER.info('no new medias found')
        return

    for user, medias in user_medias.items():
        if medias:
            LOGGER.info('found %d new medias for %s', len(medias), user.username)

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
    setup_logging()
    args = parse_args()
    clients = configure_clients(args)

    run_forever: bool = args.run_forever
    args_users: list[str] = args.users

    iteration = 0
    while True:
        iteration += 1
        if run_forever:
            LOGGER.info('Starting iteration %d', iteration)
        for client in clients:
            if args_users:
                users = [client.get_user_details(user) for user in args_users]
            else:
                try:
                    users = client.get_subscriptions()
                    LOGGER.info('got %d subscriptions with scraper %s', len(users), client.name)

                    chats = client.get_chats()
                    LOGGER.info('got %d chats with scraper %s', len(chats), client.name)
                except ScrapingException as e:
                    if status_code := get_status_code(e):
                        LOGGER.error('failed to get subscriptions for scraper %s - status code %s', client.name, status_code)
                    else:
                        LOGGER.error('failed to get subscriptions for scraper %s', client.name)
                    continue
            download(client, users=users, chats=chats)
            if not run_forever:
                break
        time.sleep(5)


if __name__ == '__main__':
    main()
