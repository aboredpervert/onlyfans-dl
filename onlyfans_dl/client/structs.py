import typing

from msgspec import Struct, UnsetType, UNSET, field


class HeaderRules(Struct, kw_only=True):
    static_param: str
    format: str
    checksum_indexes: list[int]
    checksum_constant: int
    app_token: str


class UserRef(Struct, kw_only=True):
    id: int

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, UserRef):
            return NotImplemented
        return self.id == other.id


class User(UserRef, kw_only=True):
    username: str
    name: str
    avatar: str | None
    header: str | None


class Post(Struct, kw_only=True, rename='camel'):
    class Media(Struct, kw_only=True, rename='camel'):
        class Source(Struct, kw_only=True):
            source: str | None
            width: int
            height: int
            size: int
            duration: int

        id: int
        type: str
        can_view: bool
        source: Source

    id: int
    author: UserRef | UnsetType = UNSET # unset if reported
    posted_at: str
    posted_at_precise: str
    expired_at: str | UnsetType = UNSET # unset if there is no expiry
    is_pinned: bool = False
    is_archived: bool = False
    raw_text: str | UnsetType = UNSET # unset if reported
    price: float | UnsetType = UNSET # unset if free
    media: list[Media] = field(default_factory=list)
    preview: list[int] = field(default_factory=list)


class Posts(Struct, kw_only=True, rename={'has_more': 'hasMore', 'posts': 'list'}.get):

    """Useful attributes from the OnlyFans API. Inherits `Struct`."""

    posts: list[Post]
    has_more: bool


class Chat(Struct, kw_only=True, rename='camel'):
    with_user: UserRef


class Message(Struct, kw_only=True, rename='camel'):

    class Media(Struct, kw_only=True, rename='camel'):

        class Info(Struct, kw_only=True):

            class Source(Struct, kw_only=True):

                width: int
                height: int

            source: Source

        id: int
        can_view: bool
        type: str
        src: str | None
        duration: int
        info: Info

    text: str | None
    price: int | float
    media: list[Media]
    previews: list[int]
    from_user: UserRef
    id: int
    created_at: str


class Messages(Struct, kw_only=True, rename={'has_more': 'hasMore', 'messages': 'list'}.get):

    messages: list[Message]
    has_more: bool


class Story(Struct, kw_only=True, rename='camel'):

    '''
    Describes the relevant fields of OnlyFans's story struct.
    '''

    class Media(Struct, kw_only=True, rename='camel'):

        class Source(Struct, kw_only=True):

            source: str | None
            width: int
            height: int
            duration: int

        id: int
        type: str
        can_view: bool
        source: Source

    class Question(Struct, kw_only=True):

        class Entity(Struct, kw_only=True):

            text: str

        entity: Entity

    id: int
    user_id: int
    created_at: str
    media: list[Media]
    question: Question | None


class HighlightCategory(Struct, kw_only=True, rename='camel'):

    '''
    Describes the relevant fields of OnlyFans's highlight struct.
    '''

    id: int
    user_id: int
    title: str
    cover: str
    created_at: str


class Highlight(Struct, kw_only=True, rename='camel'):

    '''
    Describes the relevant fields of OnlyFans's highlights API response.
    '''

    id: int
    user_id: int
    title: str
    cover: str
    created_at: str
    stories: list[Story]


T = typing.TypeVar('T')
class Pagination(Struct, typing.Generic[T], kw_only=True):
    items: list[T] = field(default_factory=list, name='list')
    has_more: bool = field(default=False, name='hasMore')


class NextOffsetPagination(Pagination[T], kw_only=True):
    next_offset: int = field(default=False, name='nextOffset')


class NormalizedMedia(Struct, kw_only=True):
    '''Custom normalized media'''
    user_id: int
    source_type: str
    source_id: int
    id: int
    file_type: str
    created_at: str
    text: str
    width: int
    height: int
    duration: int
    url: str
    value: str = 'free'
    highlight_category: str | None = None


def normalize_post_media(post: Post, skip_temporary: bool = False) -> list[NormalizedMedia]:
    nm: list[NormalizedMedia] = []
    if post.author is UNSET or not post.media or \
        (post.expired_at is not UNSET and skip_temporary):
        return nm
    for media in post.media:
        if not media.can_view:
            continue
        nm.append(
            NormalizedMedia(
                user_id=post.author.id,
                source_type='posts',
                source_id=post.id,
                id=media.id,
                file_type=media.type,
                created_at=post.posted_at,
                value='paid' if post.price else 'free',
                text='' if post.raw_text is UNSET else post.raw_text,
                width=media.source.width,
                height=media.source.height,
                duration=media.source.duration,
                url=media.source.source,
            ),
        )
    return nm

def normalize_archived_post_media(post: Post, skip_temporary: bool = False) -> list[NormalizedMedia]:
    nm: list[NormalizedMedia] = []
    if post.author is UNSET or not post.media or \
        (post.expired_at is not UNSET and skip_temporary):
        return nm
    for media in post.media:
        if not media.can_view:
            continue
        nm.append(
            NormalizedMedia(
                user_id=post.author.id,
                source_type='archived',
                source_id=post.id,
                id=media.id,
                file_type=media.type,
                created_at=post.posted_at,
                value='paid' if post.price and media.id not in post.preview else 'free',
                text='' if post.raw_text is UNSET else post.raw_text,
                width=media.source.width,
                height=media.source.height,
                duration=media.source.duration,
                url=media.source.source
            ),
        )
    return nm

def normalize_message_media(message: Message) -> list[NormalizedMedia]:
    nm: list[NormalizedMedia] = []
    for media in message.media:
        if not media.can_view:
            continue
        nm.append(
            NormalizedMedia(
                user_id=message.from_user.id,
                source_type='messages',
                source_id=message.id,
                id=media.id,
                file_type=media.type,
                created_at=message.created_at,
                value='paid' if message.price and media.id not in message.previews else 'free',
                text=message.text,
                width=media.info.source.width,
                height=media.info.source.height,
                duration=media.duration,
                url=media.src,
            ),
        )
    return nm

def normalize_story_media(story: Story, *, highlight_category: str | None = None) -> list[NormalizedMedia]:
    nm: list[NormalizedMedia] = []
    for media in story.media:
        if not media.can_view:
            continue
        nm.append(
            NormalizedMedia(
                user_id=story.user_id,
                source_type='stories',
                source_id=story.id,
                highlight_category=highlight_category,
                id=media.id,
                file_type=media.type,
                created_at=story.created_at,
                text=f'{highlight_category}.{story.question.entity.text}' if story.question else highlight_category,
                width=media.source.width,
                height=media.source.height,
                duration=media.source.duration,
                url=media.source.source,
            ),
        )
    return nm
