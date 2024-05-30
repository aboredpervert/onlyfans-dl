from collections import defaultdict
import contextlib
from datetime import datetime
import functools
import hashlib
import logging
from operator import itemgetter
import os
import pathlib
import re
import secrets
import sqlite3
import string
import time
import typing
import urllib.parse

import msgspec
import requests

from .structs import (
    Chat,
    HeaderRules,
    HighlightCategory,
    Highlight,
    NormalizedMedia,
    Messages,
    NextOffsetPagination,
    Pagination,
    Post,
    Story,
    User,
    normalize_archived_post_media,
    normalize_message_media,
    normalize_post_media,
    normalize_story_media,
)


LOGGER = logging.getLogger(__name__)

DEFAULT_RULES_URL = 'https://raw.githubusercontent.com/deviint/onlyfans-dynamic-rules/main/dynamicRules.json'
DEFAULT_DOWNLOAD_ROOT = 'downloads'
DEFAULT_DOWNLOAD_TEMPLATE = '{date:%Y-%m-%d}.{media_id}.{text:.35}.{extension}'
DEFAULT_SKIP_TEMPORARY = False


def sanitize_filename(file_name: str) -> str:
    '''
    Clean the post text.
    '''
    file_name = re.sub(r'\s', '_', file_name)
    file_name = re.sub(r'[^\w\d_.-]', '', file_name)
    file_name = re.sub(r'_+', '_', file_name)
    file_name = re.sub(r'\.+', '.', file_name)
    file_name = re.sub(r'(_\.|\._)', '.', file_name)
    return file_name.lower()


def get_header_rules(session: requests.Session, url: str = DEFAULT_RULES_URL) -> HeaderRules:
    with session.get(url) as response:
        response.raise_for_status()
        return msgspec.json.decode(response.content, type=HeaderRules)


Primitive = str | int | float | bool | None
def stringize(value: Primitive) -> str:
    if value is None:
        return ''
    elif isinstance(value, bool):
        return str(value).lower()
    else:
        return str(value)


def stringize_quote(value: Primitive) -> str:
    return urllib.parse.quote(stringize(value), safe='')


class ScrapingException(Exception):
    pass


T = typing.TypeVar('T')
class OnlyFansScraper:
    def __init__(self,
        name: str = __qualname__,
        *,
        session: requests.Session,
        request_timeout: int = 10,
        header_rules: HeaderRules | None = None,
        cookie: str | None = None,
        user_agent: str | None = None,
        x_bc: str,
        download_root: str = DEFAULT_DOWNLOAD_ROOT,
        download_template: str = DEFAULT_DOWNLOAD_TEMPLATE,
        skip_temporary: bool = DEFAULT_SKIP_TEMPORARY,
    ):
        self.session = session
        self.request_timeout = request_timeout
        self.name = name
        self.header_rules = header_rules
        self.cookie = cookie
        self.user_agent = user_agent
        self.x_bc = x_bc

        self.download_root = pathlib.Path(os.path.normpath(os.path.abspath(download_root)))
        self.download_template = download_template
        self.skip_temporary = skip_temporary

        # msgspec decoders
        # ref: https://jcristharif.com/msgspec/perf-tips.html#reuse-encoders-decoders
        self.user_decoder = msgspec.json.Decoder(User)
        self.user_dict_decoder = msgspec.json.Decoder(dict[int, User])
        self.user_page_decoder = msgspec.json.Decoder(Pagination[User])
        self.posts_decoder = msgspec.json.Decoder(list[Post])
        self.chat_page_decoder = msgspec.json.Decoder(NextOffsetPagination[Chat])
        self.messages_decoder = msgspec.json.Decoder(Messages)
        self.highlight_category_decoder = msgspec.json.Decoder(list[HighlightCategory])
        self.highlight_decoder = msgspec.json.Decoder(Highlight)
        self.stories_decoder = msgspec.json.Decoder(list[Story])

    def close(self) -> None:
        self.session.close()

    def __str__(self) -> str:
        return f'{self.name}\ncookie: {self.cookie}\nuser-agent: {self.user_agent}\nx-bc: {self.x_bc}'

    @classmethod
    def generate_url(cls, *args: Primitive, **kwargs: Primitive) -> str:
        return cls.generate_url_ex(args, kwargs.items())

    @classmethod
    def generate_url_ex(cls, parts: typing.Iterable[Primitive] = (), params: typing.Iterable[tuple[str, Primitive]] = ()) -> str:
        scheme = 'https'
        netloc = 'onlyfans.com'
        path = '/' + '/'.join(map(stringize_quote, parts))
        query = urllib.parse.urlencode([(k, stringize(v)) for (k, v) in params])
        fragment = ''
        return urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))

    def generate_headers(self, url: str, timer: typing.Callable[[], float] = time.time) -> dict[str, str]:
        '''Generates required headers for a request to the OnlyFans API.

        Args:
            `url`: The URL to be requested.

        Returns:
            A dictionary of headers to be used by `self.session`. See `onlyfans_dl.structs.HeaderRules` for more details.

        Raises:
            `ScrapingException`: The client was not initialized with a `header_rules` object.
        '''
        if self.header_rules is None:
            raise ScrapingException('client not initialized with header rules')

        split_url = urllib.parse.urlsplit(url)
        url_path = f'{split_url.path}?{split_url.query}' if split_url.query else split_url.path

        # DC's logic for generating the headers.
        # Only `time` and `sign` need to be generated for each request.
        # `x_bc` is a random 40-character string that can persist between requests.
        # https://github.com/DIGITALCRIMINALS/OnlyFans/blob/85aec02065f6065c5b99b6caedec20ce947ffc2d/apis/api_helper.py#L348-L373
        current_seconds = str(int(timer()))
        digest_hex = hashlib.sha1('\n'.join([self.header_rules.static_param, current_seconds, url_path, '0']).encode('utf-8')).hexdigest()
        headers = {
            'accept': 'application/json, text/plain, */*',
            'app-token': self.header_rules.app_token,
            'sign': self.header_rules.format.format(digest_hex, abs(sum(ord(digest_hex[num]) for num in self.header_rules.checksum_indexes) + self.header_rules.checksum_constant)),
            'time': current_seconds,
            'x-bc': self.x_bc,
        }
        if self.cookie:
            headers['cookie'] = self.cookie
        if self.user_agent:
            headers['user-agent'] = self.user_agent
        return headers

    def send_api_request(self, url: str, decoder: msgspec.json.Decoder[T]) -> T:
        '''Sends a request to a URL with OnlyFans headers, and parses the response.

        Args:
            `url`: The URL to be requested.
            `decoder`: Decoder to use for the response.

        Returns:
            The decoded object.

        Raises:
            `ScrapingException`: The request or decoding failed.
        '''
        url_path = urllib.parse.urlsplit(url).path
        LOGGER.debug('sending API request to %s', url_path)

        try:
            with self.session.get(url, headers=self.generate_headers(url), timeout=self.request_timeout) as response:
                response.raise_for_status()
                return decoder.decode(response.content)
        except requests.RequestException as e:
            raise ScrapingException(f'failed to send API request to {url_path}') from e
        except msgspec.DecodeError as e:
            LOGGER.debug('recevived unparseable response from %s: %r', url_path, response.content)
            raise ScrapingException(f'failed to parse API response from {url_path} of type {decoder.type.__name__}') from e

    @functools.cache
    def get_user_details(self, user: int | str) -> User:
        '''Retrieves the details of a user.

        Args:
            `user`: The user's ID or username.

        Returns:
            A `User` object describing the specified user.

        Raises:
            `ScrapingException`: An error occurred while retrieving or deserializing the user's details.
        '''
        url = self.generate_url('api2', 'v2', 'users', user)
        return self.send_api_request(url, self.user_decoder)

    def get_users_details(self, *user: int) -> dict[int, User]:
        '''Retrieves the details of multiple users at once.

        Args:
            `user`: The users' IDs.

        Returns:
            A dictionary of IDs to `User` objects describing the specified users.

        Raises:
            `ScrapingException`: An error occurred while retrieving or deserializing the users' details.
        '''
        if not user:
            return {}

        url = self.generate_url_ex(('api2', 'v2', 'users', 'list'), [('x[]', x) for x in user])
        return self.send_api_request(url, self.user_dict_decoder)

    def get_subscriptions(self) -> list[User]:
        '''Retrieves all active subscriptions.

        Returns:
            A list of `User` objects describing the subscriptions available to the scraper.

        Raises:
            `ScrapingException`: An error occurred while retrieving or deserializing the subscriptions.
        '''
        subscriptions: list[User] = []

        offset = 0
        has_more = True
        while has_more:
            url = self.generate_url('api2', 'v2', 'subscriptions', 'subscribes', limit=10, offset=offset, type='active', format='infinite')
            users = self.send_api_request(url, self.user_page_decoder)

            subscriptions.extend(users.items)
            offset += len(users.items)
            has_more = users.has_more

        return subscriptions

    def get_post_media_by_id(self, user_id: int, *, skip_db: bool = False) -> list[NormalizedMedia]:
        '''Retrieves all posts with viewable media by a user.

        Args:
            `user_id`: The user's ID.

        Returns:
            A list of `Post` objects describing the posts by the specified user that are available to the scraper.

        Raises:
            `ScrapingException`: An error occurred while retrieving or deserializing the posts.
        '''
        user = self.get_user_details(user_id)
        user_medias: list[NormalizedMedia] = []

        last_post_timestamp = 0
        if not skip_db:
            try:
                with contextlib.closing(sqlite3.connect(pathlib.Path(self.download_root, user.username, '.media.db'))) as database:
                    last_post_timestamp = database.execute('SELECT max(timestamp) FROM media WHERE source_type == "posts"').fetchone()[0] or last_post_timestamp
            except sqlite3.OperationalError:
                pass

        offset = 0
        while True:
            url = self.generate_url('api2', 'v2', 'users', user_id, 'posts', limit=10, offset=offset, order='publish_date_desc')
            decoded_posts = self.send_api_request(url, self.posts_decoder)

            if not decoded_posts:
                break
            for post in decoded_posts:
                if int(datetime.strptime(post.posted_at, '%Y-%m-%dT%H:%M:%S%z').timestamp()) > last_post_timestamp:
                    if post.media and any(media.can_view for media in post.media):
                        user_medias += normalize_post_media(post, self.skip_temporary)
                else:
                    return user_medias

            offset += 10
            LOGGER.debug('%s posts retrieved for user %s', len(user_medias), user_id)

        return user_medias

    def get_archived_post_media_by_id(self, user_id: int, *, skip_db: bool = False) -> list[NormalizedMedia]:
        '''Retrieves all archived posts with viewable media by a user.

        Args:
            `user_id`: The user's ID.

        Returns:
            A list of `Post` objects describing the archived posts by the specified user that are available to the scraper.

        Raises:
            `ScrapingException`: An error occurred while retrieving or deserializing the posts.
        '''
        user = self.get_user_details(user_id)
        user_medias: list[NormalizedMedia] = []

        last_post_timestamp = 0
        if not skip_db:
            try:
                with contextlib.closing(sqlite3.connect(pathlib.Path(self.download_root, user.username, '.media.db'))) as database:
                    last_post_timestamp = database.execute('SELECT max(timestamp) FROM media WHERE source_type == "archived"').fetchone()[0] or last_post_timestamp
            except sqlite3.OperationalError:
                pass


        offset = 0
        while True:
            url = self.generate_url('api2', 'v2', 'users', user_id, 'posts', 'archived', limit=10, offset=offset, order='publish_date_desc')
            decoded_posts = self.send_api_request(url, self.posts_decoder)

            if not decoded_posts:
                break
            for post in decoded_posts:
                if int(datetime.strptime(post.posted_at, '%Y-%m-%dT%H:%M:%S%z').timestamp()) > last_post_timestamp:
                    if post.media and any(media.can_view for media in post.media):
                        user_medias += normalize_archived_post_media(post, self.skip_temporary)
                else:
                    return user_medias

            offset += 10
            LOGGER.debug('%s archived posts retrieved for user %s', len(user_medias), user_id)

        return user_medias

    def get_chats(self) -> list[User]:
        '''Retrieves all active chats.

        Returns:
            A list of user IDs.

        Raises:
            `ScrapingException`: An error occurred while retrieving or deserializing the chats.
        '''
        chats: list[User] = []

        offset = 0
        has_more = True
        while has_more:
            url = self.generate_url('api2', 'v2', 'chats', limit=10, offset=offset, skip_users='all', order='recent')
            decoded_chats = self.send_api_request(url, self.chat_page_decoder)

            chats.extend(self.get_users_details(*(chat.with_user.id for chat in decoded_chats.items)).values())
            offset = decoded_chats.next_offset
            has_more = decoded_chats.has_more

        return chats

    def get_message_media_by_id(self, user_id: int, *, skip_db: bool = False) -> list[NormalizedMedia]:
        '''Retrieves all messages with viewable media from a user.

        Args:
            `user_id`: The user's ID.

        Returns:
            A list of `Message` objects describing the messages from the specified user that are available to the scraper.

        Raises:
            `ScrapingException`: An error occurred while retrieving or deserializing the messages.
        '''
        user = self.get_user_details(user_id)
        user_medias: list[NormalizedMedia] = []

        last_message_timestamp = 0
        if not skip_db:
            try:
                with contextlib.closing(sqlite3.connect(pathlib.Path(self.download_root, user.username, '.media.db'))) as database:
                    last_message_timestamp = database.execute('SELECT max(timestamp) FROM media WHERE source_type == "messages"').fetchone()[0] or last_message_timestamp
            except sqlite3.OperationalError:
                pass

        offset = 0
        while True:
            url = self.generate_url('api2', 'v2', 'chats', user_id, 'messages', limit=10, offset=offset, order='desc')
            decoded_messages = self.send_api_request(url, self.messages_decoder)

            for message in decoded_messages.messages:
                if int(datetime.strptime(message.created_at, '%Y-%m-%dT%H:%M:%S%z').timestamp()) > last_message_timestamp:
                    if message.from_user.id == user_id:
                        if message.media and any(media.can_view for media in message.media):
                            user_medias += normalize_message_media(message)
                else:
                    return user_medias
            if not decoded_messages.has_more:
                return user_medias
            offset += len(decoded_messages.messages)

    # def get_purchased_media(self, user_id: int, *, skip_db: bool = False) -> list[NormalizedMedia]:
    #     medias: list[NormalizedMedia] = []

    #     offset = 0
    #     while True:
    #         url = self.generate_url('api2', 'v2', 'posts', 'paid', limit=10, offset=offset)
    #         decoded_media = self.send_api_request(url, self.media_decoder)

    def get_highlight_media_by_id(self, user_id: int, *, skip_db: bool = False) -> list[NormalizedMedia]:
        user = self.get_user_details(user_id)
        user_medias: list[NormalizedMedia] = []

        last_highlight_timestamp = 0
        if not skip_db:
            try:
                with contextlib.closing(sqlite3.connect(pathlib.Path(self.download_root, user.username, '.media.db'))) as database:
                    last_highlight_timestamp = database.execute('SELECT max(timestamp) FROM media WHERE source_type == "highlights"').fetchone()[0] or last_highlight_timestamp
            except sqlite3.OperationalError:
                pass

        categories: list[HighlightCategory] = []

        offset = 0
        while True:
            categories_url = self.generate_url('api2', 'v2', 'users', user_id, 'stories', 'highlights', limit=5, offset=offset)
            decoded_categories = self.send_api_request(categories_url, self.highlight_category_decoder)

            if not decoded_categories:
                break
            categories += decoded_categories
            offset += 5

        for category in categories:
            highlights_url = self.generate_url('api2', 'v2', 'stories', 'highlights', category.id)
            decoded_highlight = self.send_api_request(highlights_url, self.highlight_decoder)
            for story in reversed(decoded_highlight.stories):
                if int(datetime.strptime(story.created_at, '%Y-%m-%dT%H:%M:%S%z').timestamp()) > last_highlight_timestamp:
                    user_medias += normalize_story_media(story, highlight_category=category.title)
                else:
                    break

        return user_medias

    def get_story_media_by_id(self, user_id: int, *, skip_db: bool = False) -> list[NormalizedMedia]:
        '''Fetches all stories from a user.

        Args:
            `user_id`: The user's ID.

        Returns:
            A list of `Story` objects describing the stories from the specified user.

        Raises:
            `ScrapingException`: An error occurred while retrieving or deserializing the stories.
        '''
        user = self.get_user_details(user_id)
        user_medias: list[NormalizedMedia] = []

        last_story_timestamp = 0
        if not skip_db:
            try:
                with contextlib.closing(sqlite3.connect(pathlib.Path(self.download_root, user.username, '.media.db'))) as database:
                    last_story_timestamp = database.execute('SELECT max(timestamp) FROM media WHERE source_type == "stories"').fetchone()[0] or last_story_timestamp
            except sqlite3.OperationalError:
                pass

        # TODO: Figure out how they paginate this endpoint.
        url = self.generate_url('api2', 'v2', 'users', user_id, 'stories')
        decoded_stories = self.send_api_request(url, self.stories_decoder)

        for story in reversed(decoded_stories):
            if int(datetime.strptime(story.created_at, '%Y-%m-%dT%H:%M:%S%z').timestamp()) > last_story_timestamp:
                user_medias += normalize_story_media(story)
            else:
                return user_medias
        return user_medias

    def download_media(self, user: User, medias: list[NormalizedMedia]) -> None:
        '''
        Download media from a list of posts.
        '''
        if not medias:
            return
        LOGGER.info('downloading media for %s', user.username)
        user_dir = pathlib.Path(self.download_root, user.username)
        user_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(pathlib.Path(self.download_root, user.username, '.media.db'))) as database:
            cur = database.execute('''
                CREATE TABLE IF NOT EXISTS media (
                    source_type TEXT,
                    timestamp INTEGER,
                    source_id INTEGER,
                    media_id INTEGER,
                    PRIMARY KEY (source_type, source_id, media_id)
                ) WITHOUT ROWID
            ''')
            cur.execute('''
                CREATE INDEX IF NOT EXISTS created_on
                ON media(timestamp)
            ''')

            if user.avatar:
                try:
                    with self.session.get(user.avatar, stream=True, timeout=self.request_timeout) as response:
                        response.raise_for_status()
                        timestamp = int(datetime.strptime(response.headers['last-modified'], '%a, %d %b %Y %X GMT').timestamp())
                        dest_file = pathlib.Path(user_dir, 'avatar.jpg')
                        if not cur.execute('SELECT * FROM media WHERE source_type = ? AND source_id = ? AND media_id = ?', ('avatar', timestamp, timestamp)).fetchone():
                            if existing_avatars := cur.execute('SELECT * FROM media WHERE source_type = ?', ('avatar',)).fetchall():
                                current_avatar_timestamp: int = max(existing_avatars, key=itemgetter(1))[1]
                                old_avatar_file = pathlib.Path(dest_file.parent, f'avatar-{current_avatar_timestamp}.jpg')
                                dest_file.rename(old_avatar_file)
                            temp_file = pathlib.Path(dest_file.parent, f'{dest_file.name}.{secrets.token_urlsafe(6)}.part')
                            with temp_file.open('wb') as f:
                                for chunk in response.iter_content(requests.models.CONTENT_CHUNK_SIZE):
                                    f.write(chunk)
                            temp_file.rename(dest_file)
                            cur.execute('INSERT INTO media VALUES (?, ?, ?, ?)', ('avatar', timestamp, timestamp, timestamp))
                except requests.RequestException:
                    LOGGER.exception('error getting avatar')

            if user.header:
                try:
                    with self.session.get(user.header, stream=True, timeout=self.request_timeout) as response:
                        response.raise_for_status()
                        timestamp = int(datetime.strptime(response.headers['last-modified'], '%a, %d %b %Y %X GMT').timestamp())
                        dest_file = pathlib.Path(user_dir, 'header.jpg')
                        if not cur.execute('SELECT * FROM media WHERE source_type = ? AND source_id = ? AND media_id = ?', ('header', timestamp, timestamp)).fetchone():
                            if existing_headers := cur.execute('SELECT * FROM media WHERE source_type = ?', ('header',)).fetchall():
                                current_header_timestamp: int = max(existing_headers, key=itemgetter(1))[1]
                                old_header_file = pathlib.Path(dest_file.parent, f'header-{current_header_timestamp}.jpg')
                                dest_file.rename(old_header_file)
                            temp_file = pathlib.Path(dest_file.parent, f'{dest_file.name}.{secrets.token_urlsafe(6)}.part')
                            with temp_file.open('wb') as f:
                                for chunk in response.iter_content(requests.models.CONTENT_CHUNK_SIZE):
                                    f.write(chunk)
                            temp_file.rename(dest_file)
                            cur.execute('INSERT INTO media VALUES (?, ?, ?, ?)', ('header', timestamp, timestamp, timestamp))
                except requests.RequestException:
                    LOGGER.exception('error getting header')

            formatter = string.Formatter()
            for index, media in enumerate(medias):
                if cur.execute('SELECT * FROM media WHERE source_type = ? AND source_id = ? AND media_id = ?', (media.source_type, media.source_id, media.id)).fetchone():
                    continue
                creation_date = datetime.strptime(media.created_at, '%Y-%m-%dT%H:%M:%S%z')
                match media.file_type:
                    case 'photo':
                        ext = 'jpg'
                    case 'video':
                        ext = 'mp4'
                    case 'audio':
                        ext = 'mp3'
                    case 'gif':
                        ext = 'mp4'
                    case _:
                        LOGGER.info(f'unknown media type: {media.file_type}')
                        continue

                fields: dict[str, object] = defaultdict(lambda: '')
                fields.update({
                    'date': creation_date,
                    'post_id': media.source_id,
                    'media_id': media.id,
                    'index': index,
                    'text': media.text,
                    'extension': ext
                })

                dest_file = pathlib.Path(
                    user_dir,
                    media.source_type,
                    media.file_type + 's',
                    sanitize_filename(formatter.vformat(self.download_template, (), fields)),
                )
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                temp_file = pathlib.Path(dest_file.parent, f'{dest_file.name}.{secrets.token_urlsafe(6)}.part')
                try:
                    with self.session.get(media.url, stream=True, timeout=self.request_timeout) as response:
                        response.raise_for_status()
                        if dest_file.exists() and dest_file.stat().st_size == int(response.headers.get('content-length', '0')):
                            cur.execute('INSERT INTO media VALUES (?, ?, ?, ?)', (media.source_type, int(creation_date.timestamp()), media.source_id, media.id))
                            continue
                        with open(temp_file, 'wb') as f:
                            for chunk in response.iter_content(requests.models.CONTENT_CHUNK_SIZE):
                                f.write(chunk)
                        temp_file.rename(dest_file)
                        cur.execute('INSERT INTO media VALUES (?, ?, ?, ?)', (media.source_type, int(creation_date.timestamp()), media.source_id, media.id))
                except requests.RequestException:
                    LOGGER.exception('error getting media')
            database.commit()
        LOGGER.info('finished downloading media for %s', user.username)
