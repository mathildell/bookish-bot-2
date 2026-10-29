"""Upcoming book releases: track Goodreads/NetGalley books and announce them.

Port of the old Ruby "BookReleases" feature:

- members add a book from a Goodreads or NetGalley URL (scraped);
- the tracked books can be refreshed via ``/releases update`` (old vs new preview);
- ``/releases list`` paginates upcoming releases with a "jump to month" select;
- a scheduled job posts the day's releases and, on the 1st of the month, an
  overview of everything releasing that month.

Parsing/HTML fetching lives in ``release_sources``; this module owns storage,
embeds and the announcement worker.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import string
from collections.abc import Container
from datetime import date, datetime, timedelta

import discord
from sqlalchemy import delete, func, select, update

from ..db import session_scope
from ..models import BookRelease, ReleaseEditDraft, ReleaseSetting
from ..texts import t
from .level import utcnow
from .members import resolve_members
from .release_sources import BookData, book_data_to_dict
from .search import search_key

logger = logging.getLogger("bh2.releases")

FIELD_VALUE_LIMIT = 1024
BLURB_LIMIT = 350
DRAFT_TTL = timedelta(hours=2)
EMBEDS_PER_MESSAGE = 10
# Default retention for released books before the cleanup job purges them.
DEFAULT_PURGE_DAYS = 30
# Default UTC hour the daily release announcements are posted.
DEFAULT_POST_HOUR = 13
# Discord rejects a message whose embeds total more than 6000 characters;
# stay under it (the old bot used 5500 too).
EMBED_MESSAGE_LIMIT = 5500
MONTH_FIELDS = 25

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def today_utc() -> date:
    """The current UTC date, matching the naive UTC timestamps we store."""
    return utcnow().date()


def _embed_size(embed: discord.Embed) -> int:
    size = len(embed.title or "") + len(embed.description or "")
    if embed.footer:
        size += len(embed.footer.text or "")
    if embed.author:
        size += len(embed.author.name or "")
    for field in embed.fields:
        size += len(field.name or "") + len(field.value or "")
    return size


def _embed_batches(embeds: list[discord.Embed]) -> list[list[int]]:
    """Group embed indices so each message stays within Discord's limits."""
    batches: list[list[int]] = []
    current: list[int] = []
    total = 0
    for index, embed in enumerate(embeds):
        size = _embed_size(embed)
        if current and (
            len(current) >= EMBEDS_PER_MESSAGE
            or total + size > EMBED_MESSAGE_LIMIT
        ):
            batches.append(current)
            current, total = [], 0
        current.append(index)
        total += size
    if current:
        batches.append(current)
    return batches


async def _send_embed_batches(
    channel: discord.abc.Messageable,
    embeds: list[discord.Embed],
    *,
    content: str | None = None,
) -> list[int]:
    """Post embeds in as many messages as Discord's limits require.

    ``_embed_batches`` does the bulk of the packing, but Discord's own size
    accounting can disagree with ours; when a message is rejected anyway we
    split that batch in half and retry, so an oversized batch never costs us
    the releases. Returns the indices that were actually posted.
    """
    pending = list(reversed(_embed_batches(embeds)))
    posted: list[int] = []
    first = True
    while pending:
        indices = pending.pop()
        try:
            await channel.send(
                content=content if first else None,
                embeds=[embeds[index] for index in indices],
            )
        except discord.HTTPException as error:
            oversized = (
                getattr(error, "status", None) == 413
                or getattr(error, "code", None) == 40005
            )
            if oversized and len(indices) > 1:
                middle = len(indices) // 2
                pending.append(indices[middle:])
                pending.append(indices[:middle])
                continue
            logger.warning(
                "Could not post %d release embed(s)", len(indices), exc_info=True
            )
            continue
        first = False
        posted.extend(indices)
    return posted


def _truncate(value: str, limit: int = FIELD_VALUE_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


_REFERENCE_WORDS = 2
_REFERENCE_MAX_WORDS = 3
_REFERENCE_SUFFIX_LENGTH = 4
_REFERENCE_MAX_LENGTH = 64
_REFERENCE_FALLBACK = "book"
_REFERENCE_ALPHABET = string.ascii_lowercase + string.digits
_APOSTROPHE_RE = re.compile(r"['\u2019]")


def _reference_words(title: str, count: int) -> str:
    cleaned = _APOSTROPHE_RE.sub("", (title or "").lower())
    words = _WORD_RE.findall(cleaned)
    base = "-".join(words[:count]) or _REFERENCE_FALLBACK
    return base[: _REFERENCE_MAX_LENGTH - _REFERENCE_SUFFIX_LENGTH - 1]


def book_reference(title: str) -> str:
    """A short, URL-like handle: the title's first two words.

    Used as the display fallback for a book without a stored reference; e.g.
    ``"To Dream in Darkness"`` -> ``"to-dream"``.
    """
    return _reference_words(title, _REFERENCE_WORDS)


def _random_suffix() -> str:
    return "".join(
        secrets.choice(_REFERENCE_ALPHABET)
        for _ in range(_REFERENCE_SUFFIX_LENGTH)
    )


def unique_reference(title: str, taken: Container[str]) -> str:
    """A handle not present in ``taken``.

    Tries the title's first two words, then its first three, and finally
    appends a random four-character suffix until the handle is free.
    """
    candidate = _reference_words(title, _REFERENCE_WORDS)
    if candidate not in taken:
        return candidate
    candidate = _reference_words(title, _REFERENCE_MAX_WORDS)
    if candidate not in taken:
        return candidate
    while True:
        suffixed = f"{candidate}-{_random_suffix()}"
        if suffixed not in taken:
            return suffixed


def source_label(source: str) -> str:
    return t(f"releases.sources.{source}")


_MENTION_RE = re.compile(r"^<@!?(\d+)>$")
_SNOWFLAKE_MIN_LENGTH = 15


def parse_user_id(value: str) -> int | None:
    """Return the user id from a mention (``<@…>``/``<@!…>``) or numeric id.

    Bare numbers must be long enough to be a snowflake, so a title like
    ``1984`` is still searched as text rather than read as a user id.
    """
    text = (value or "").strip()
    match = _MENTION_RE.match(text)
    if match:
        return int(match.group(1))
    if text.isdigit() and len(text) >= _SNOWFLAKE_MIN_LENGTH:
        return int(text)
    return None


def format_plain_date(value: date, year=True) -> str:
    """Render a date as plain text (``October 13, 2026``)."""
    if year:
        return t(
            "releases.date.plain",
            month=t(f"components.months.{value.month}"),
            day=value.day,
            year=value.year
        )
    else:
        return t(
            "releases.date.plain_without_year",
            month=t(f"components.months.{value.month}"),
            day=value.day
        )


def build_book_embed(
    book,
    member: discord.Member | None,
    client: discord.Client,
    *,
    prefix: str | None = None,
    include_blurb: bool = True,
) -> discord.Embed:
    """Build the embed shown for a single release (command or announcement)."""
    embed = discord.Embed(colour=getattr(client, "default_embed_color", None))

    title = getattr(book, "title", None) or t("releases.embed.untitled_book")
    embed.title = (f"{prefix} ‎ " if prefix else "") + discord.utils.escape_markdown(
        title
    )
    if getattr(book, "url", None):
        embed.url = book.url
    if getattr(book, "image_url", None):
        embed.set_thumbnail(url=book.image_url)

    authors = getattr(book, "authors", None) or []
    embed.add_field(
        name=t("releases.embed.author_field"),
        value=(
            discord.utils.escape_markdown(", ".join(authors))
            if authors
            else t("releases.embed.author_none")
        ),
        inline=True,
    )
    embed.add_field(
        name=t("releases.embed.release_date_field"),
        value=format_plain_date(book.release_date),
        inline=True,
    )
    tags = getattr(book, "tags", None) or []
    embed.add_field(
        name=t("releases.embed.tags_field"),
        value=", ".join(tags[:3]) if tags else t("releases.embed.tags_none"),
        inline=True,
    )

    blurb = getattr(book, "blurb", None)
    if include_blurb and blurb and blurb.strip():
        embed.add_field(
            name=t("releases.embed.description_field"),
            value=_truncate(blurb, BLURB_LIMIT),
            inline=False,
        )

    reference = getattr(book, "reference", None) or book_reference(title)
    source = source_label(book.source)
    if member is not None:
        avatar = getattr(member, "display_avatar", None)
        embed.set_footer(
            text=t(
                "releases.embed.added_by",
                user=discord.utils.escape_markdown(member.display_name),
                source=source,
                reference=reference,
            ),
            icon_url=getattr(avatar, "url", None),
        )
    else:
        embed.set_footer(
            text=t(
                "releases.embed.added_by_unknown",
                source=source,
                reference=reference,
            )
        )

    background = getattr(client, "transparent_embed_image", None)
    if background:
        embed.set_image(url=background)
    return embed


def build_confirm_embed(client: discord.Client) -> discord.Embed:
    """The instructions embed shown above the update preview buttons."""
    embed = discord.Embed(
        title=t("releases.update.instruction_title"),
        description=t("releases.update.instruction_body"),
    )
    background = getattr(client, "transparent_embed_image", None)
    if background:
        embed.set_image(url=background)
    return embed


def disambiguate_embed_urls(embeds: list[discord.Embed]) -> None:
    """Give each embed a unique top-level ``url``, in place.

    Discord shows only the first embed of a group that shares a ``url`` (the
    rest are folded into its image gallery), which would hide the second
    preview since it points at the same page as the first.
    """
    for index, embed in enumerate(embeds):
        if embed.url:
            embed.url = f"{embed.url}#{index}"


def _added_at(book) -> datetime:
    return getattr(book, "created_at", None) or datetime.min


def _release_order(*, ascending: bool = False):
    """Oldest or latest release date first; within a date, oldest entry first."""
    date_column = (
        BookRelease.release_date.asc()
        if ascending
        else BookRelease.release_date.desc()
    )
    return (date_column, BookRelease.created_at.asc())


def group_by_date(books: list) -> list[tuple[date, list]]:
    """Group releases by date, oldest first, then by when they were added."""
    groups: list[tuple[date, list]] = []
    for book in sorted(
        books,
        key=lambda b: (b.release_date, _added_at(b), (b.title or "").lower()),
    ):
        if groups and groups[-1][0] == book.release_date:
            groups[-1][1].append(book)
        else:
            groups.append((book.release_date, [book]))
    return groups


_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(day: int) -> str:
    """``16`` -> ``"16th"``, taking the 11th/12th/13th exception into account."""
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = _ORDINAL_SUFFIXES.get(day % 10, "th")
    return f"{day}{suffix}"


def _month_day_label(day: date) -> str:
    return t(
        "releases.month_embed.date",
        month=t(f"components.months.{day.month}"),
        day=_ordinal(day.day),
    )


def _book_line(book) -> str:
    authors = (
        discord.utils.escape_markdown(", ".join(book.authors))
        if book.authors
        else t("releases.month_embed.unknown_author")
    )
    return "- " + t(
        "releases.month_embed.line",
        title=discord.utils.escape_markdown(book.title),
        authors=authors,
        user=f"<@{book.discord_user_id}>",
    )


def _field_values(lines: list[str]) -> list[str]:
    """Split lines into field values that each fit Discord's value limit."""
    values: list[str] = []
    current = ""
    for line in lines:
        line = _truncate(line, FIELD_VALUE_LIMIT)
        if not current:
            current = line
        elif len(current) + 1 + len(line) > FIELD_VALUE_LIMIT:
            values.append(current)
            current = line
        else:
            current = f"{current}\n{line}"
    if current:
        values.append(current)
    return values


def build_month_embeds(books: list, client: discord.Client) -> list[discord.Embed]:
    """Build the monthly overview, one field per release date.

    Each date is a field (labelled like ``October 16th``) whose value is a
    Discord list of that day's releases. When an embed fills up the rest
    continue in a new embed (sent as another message); the last embed's footer
    counts the month's releases.
    """
    groups = group_by_date(books)
    if not groups:
        return []

    month_title = t("releases.month_embed.title")
    embeds: list[discord.Embed] = []

    def start_embed() -> discord.Embed:
        return discord.Embed(
            colour=getattr(client, "default_embed_color", None),
            title=month_title,
        )

    embed = start_embed()
    for day, day_books in groups:
        name = _month_day_label(day)
        for value in _field_values([_book_line(book) for book in day_books]):
            too_many = len(embed.fields) >= MONTH_FIELDS
            too_big = (
                _embed_size(embed) + len(name) + len(value)
                > EMBED_MESSAGE_LIMIT
            )
            if embed.fields and (too_many or too_big):
                embeds.append(embed)
                embed = start_embed()
            embed.add_field(name=name, value=value, inline=False)
    embeds.append(embed)

    count = len(books)
    embeds[-1].set_footer(
        text=t(
            "releases.month_embed.footer_singular"
            if count == 1
            else "releases.month_embed.footer_plural",
            count=count,
        )
    )
    return embeds


class ReleaseService:
    """CRUD for releases/settings/drafts plus the announcement worker."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self._channel_id: int | None = None
        self._ping_role_id: int | None = None
        self._last_monthly_post: str | None = None
        self._purge_days = DEFAULT_PURGE_DAYS
        self._post_hour = DEFAULT_POST_HOUR
        # Serialises posts so a manual run cannot race the scheduled loop.
        self._post_lock = asyncio.Lock()

    @property
    def channel_id(self) -> int | None:
        return self._channel_id

    @property
    def ping_role_id(self) -> int | None:
        return self._ping_role_id

    @property
    def purge_days(self) -> int:
        return self._purge_days

    @property
    def post_hour(self) -> int:
        return self._post_hour

    # ----------------------------------------------------------- settings

    async def reload(self) -> None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(ReleaseSetting).where(
                        ReleaseSetting.guild_id == self.guild_id
                    )
                )
            ).scalar_one_or_none()
        self._channel_id = (row.channel_id or None) if row else None
        self._ping_role_id = row.ping_role_id if row else None
        self._last_monthly_post = row.last_monthly_post if row else None
        self._purge_days = (
            row.purge_days if row and row.purge_days else DEFAULT_PURGE_DAYS
        )
        self._post_hour = (
            row.post_hour if row and row.post_hour else DEFAULT_POST_HOUR
        )

    async def set_channel(self, channel_id: int) -> None:
        async with session_scope() as session:
            row = await self._find_setting(session)
            if row is None:
                session.add(
                    ReleaseSetting(
                        guild_id=self.guild_id,
                        channel_id=channel_id,
                        ping_role_id=self._ping_role_id,
                    )
                )
            else:
                row.channel_id = channel_id
            await session.commit()
        self._channel_id = channel_id

    async def set_ping_role(self, role_id: int | None) -> None:
        async with session_scope() as session:
            row = await self._find_setting(session)
            if row is None:
                session.add(
                    ReleaseSetting(
                        guild_id=self.guild_id,
                        channel_id=self._channel_id or 0,
                        ping_role_id=role_id,
                    )
                )
            else:
                row.ping_role_id = role_id
            await session.commit()
        self._ping_role_id = role_id

    async def set_purge_days(self, days: int) -> None:
        async with session_scope() as session:
            row = await self._find_setting(session)
            if row is None:
                session.add(
                    ReleaseSetting(
                        guild_id=self.guild_id,
                        channel_id=self._channel_id or 0,
                        ping_role_id=self._ping_role_id,
                        purge_days=days,
                    )
                )
            else:
                row.purge_days = days
            await session.commit()
        self._purge_days = days

    async def set_post_hour(self, hour: int) -> None:
        async with session_scope() as session:
            row = await self._find_setting(session)
            if row is None:
                session.add(
                    ReleaseSetting(
                        guild_id=self.guild_id,
                        channel_id=self._channel_id or 0,
                        ping_role_id=self._ping_role_id,
                        post_hour=hour,
                    )
                )
            else:
                row.post_hour = hour
            await session.commit()
        self._post_hour = hour

    async def _find_setting(self, session) -> ReleaseSetting | None:
        return (
            await session.execute(
                select(ReleaseSetting).where(
                    ReleaseSetting.guild_id == self.guild_id
                )
            )
        ).scalar_one_or_none()

    async def mark_monthly_posted(self, period: str) -> None:
        async with session_scope() as session:
            row = await self._find_setting(session)
            if row is None:
                session.add(
                    ReleaseSetting(
                        guild_id=self.guild_id,
                        channel_id=self._channel_id or 0,
                        last_monthly_post=period,
                    )
                )
            else:
                row.last_monthly_post = period
            await session.commit()
        self._last_monthly_post = period

    # ------------------------------------------------------------ releases

    async def get(self, book_id: int) -> BookRelease | None:
        async with session_scope() as session:
            book = await session.get(BookRelease, book_id)
            if book is None or book.guild_id != self.guild_id:
                return None
            return book

    async def get_by_reference(self, reference: str) -> BookRelease | None:
        async with session_scope() as session:
            return (
                await session.execute(
                    select(BookRelease).where(
                        BookRelease.guild_id == self.guild_id,
                        BookRelease.reference == reference,
                    )
                )
            ).scalar_one_or_none()

    async def _taken_references(self, session) -> set[str]:
        rows = await session.execute(
            select(BookRelease.reference).where(
                BookRelease.guild_id == self.guild_id
            )
        )
        return set(rows.scalars())

    async def find_by_source(
        self, source: str, source_id: str
    ) -> BookRelease | None:
        async with session_scope() as session:
            return (
                await session.execute(
                    select(BookRelease).where(
                        BookRelease.guild_id == self.guild_id,
                        BookRelease.source == source,
                        BookRelease.source_id == source_id,
                    )
                )
            ).scalar_one_or_none()

    async def search(
        self, keyword: str, *, include_announced: bool = False
    ) -> list[BookRelease]:
        """Books matching ``keyword``.

        The keyword is either a title/author fragment (matched ignoring case,
        punctuation and spacing, so ``"KJ Charles"`` finds ``"K. J. Charles"``)
        or a member mention/numeric id, which returns that member's releases.
        Books whose release has already been announced are left out unless
        ``include_announced`` is set.
        """
        user_id = parse_user_id(keyword)
        needle = None if user_id is not None else search_key(keyword)
        if user_id is None and not needle:
            return []
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(BookRelease).where(
                        BookRelease.guild_id == self.guild_id
                    )
                )
            ).scalars()
            books = list(rows)
        matches: list[BookRelease] = []
        for book in books:
            if not include_announced and book.announcement_sent:
                continue
            if user_id is not None:
                if book.discord_user_id == user_id:
                    matches.append(book)
            elif needle in search_key(book.title) or needle in search_key(
                " ".join(book.authors or [])
            ):
                matches.append(book)
        matches.sort(
            key=lambda book: (_added_at(book), (book.title or "").lower())
        )
        matches.sort(key=lambda book: book.release_date, reverse=True)
        return matches

    async def count_upcoming(self) -> int:
        async with session_scope() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(BookRelease)
                .where(
                    BookRelease.guild_id == self.guild_id,
                    BookRelease.release_date >= today_utc(),
                )
            )
        return count or 0

    async def upcoming_page(self, offset: int, limit: int) -> list[BookRelease]:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(BookRelease)
                    .where(
                        BookRelease.guild_id == self.guild_id,
                        BookRelease.release_date >= today_utc(),
                    )
                    .order_by(*_release_order(ascending=True))
                    .offset(offset)
                    .limit(limit)
                )
            ).scalars()
        return list(rows)

    async def page_index_for_date(self, target: date, per_page: int) -> int:
        """Page index (0-based) holding the first release on/after ``target``."""
        async with session_scope() as session:
            before = await session.scalar(
                select(func.count())
                .select_from(BookRelease)
                .where(
                    BookRelease.guild_id == self.guild_id,
                    BookRelease.release_date >= today_utc(),
                    BookRelease.release_date < target,
                )
            )
        return (before or 0) // per_page

    async def month_releases(self, year: int, month: int) -> list[BookRelease]:
        first = date(year, month, 1)
        next_first = (
            date(year + 1, 1, 1)
            if month == 12
            else date(year, month + 1, 1)
        )
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(BookRelease)
                    .where(
                        BookRelease.guild_id == self.guild_id,
                        BookRelease.release_date >= first,
                        BookRelease.release_date < next_first,
                    )
                    .order_by(*_release_order())
                )
            ).scalars()
        return list(rows)

    async def add(self, data: BookData, discord_user_id: int) -> BookRelease:
        async with session_scope() as session:
            reference = unique_reference(
                data.title, await self._taken_references(session)
            )
            book = BookRelease(
                guild_id=self.guild_id,
                discord_user_id=discord_user_id,
                title=data.title,
                reference=reference,
                authors=data.authors,
                release_date=data.release_date,
                blurb=data.blurb,
                image_url=data.image_url,
                source=data.source,
                source_id=data.source_id,
                url=data.url,
                tags=data.tags,
            )
            session.add(book)
            await session.commit()
            await session.refresh(book)
            return book

    async def update(
        self, book_id: int, data: BookData
    ) -> BookRelease | None:
        async with session_scope() as session:
            book = await session.get(BookRelease, book_id)
            if book is None or book.guild_id != self.guild_id:
                return None
            if data.title != book.title or not book.reference:
                taken = await self._taken_references(session)
                taken.discard(book.reference)
                book.reference = unique_reference(data.title, taken)
            book.title = data.title
            book.authors = data.authors
            book.release_date = data.release_date
            book.blurb = data.blurb
            book.image_url = data.image_url
            book.source = data.source
            book.source_id = data.source_id
            book.url = data.url
            book.tags = data.tags
            await session.commit()
            await session.refresh(book)
            return book

    async def remove(self, book_id: int) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                delete(BookRelease).where(
                    BookRelease.id == book_id,
                    BookRelease.guild_id == self.guild_id,
                )
            )
            await session.commit()
        return result.rowcount > 0

    async def purge_old_releases(self, *, today: date | None = None) -> int:
        """Delete releases older than the configured retention window."""
        cutoff = (today or today_utc()) - timedelta(days=self._purge_days)
        async with session_scope() as session:
            result = await session.execute(
                delete(BookRelease).where(
                    BookRelease.guild_id == self.guild_id,
                    BookRelease.release_date <= cutoff,
                )
            )
            await session.commit()
        return result.rowcount

    async def mark_announced(self, book_ids: list[int]) -> None:
        if not book_ids:
            return
        async with session_scope() as session:
            await session.execute(
                update(BookRelease)
                .where(BookRelease.id.in_(book_ids))
                .values(announcement_sent=True)
            )
            await session.commit()

    # -------------------------------------------------------------- drafts

    async def create_draft(
        self, book_id: int, author_id: int, data: BookData
    ) -> ReleaseEditDraft:
        async with session_scope() as session:
            draft = ReleaseEditDraft(
                guild_id=self.guild_id,
                book_id=book_id,
                author_id=author_id,
                data=book_data_to_dict(data),
                expires_at=utcnow() + DRAFT_TTL,
            )
            session.add(draft)
            await session.commit()
            await session.refresh(draft)
            return draft

    async def get_draft(self, draft_id: int) -> ReleaseEditDraft | None:
        async with session_scope() as session:
            draft = await session.get(ReleaseEditDraft, draft_id)
            if draft is None or draft.guild_id != self.guild_id:
                return None
            return draft

    async def delete_draft(self, draft_id: int) -> None:
        async with session_scope() as session:
            await session.execute(
                delete(ReleaseEditDraft).where(
                    ReleaseEditDraft.id == draft_id,
                    ReleaseEditDraft.guild_id == self.guild_id,
                )
            )
            await session.commit()

    async def purge_expired_drafts(self) -> int:
        async with session_scope() as session:
            result = await session.execute(
                delete(ReleaseEditDraft).where(
                    ReleaseEditDraft.expires_at < utcnow()
                )
            )
            await session.commit()
        return result.rowcount

    # -------------------------------------------------------------- worker

    async def post_due(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        client: discord.Client,
    ) -> int:
        """Post the day's releases and (once a month) the monthly overview."""
        async with self._post_lock:
            return await self._post_due(guild, channel, client)

    async def _post_due(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        client: discord.Client,
    ) -> int:
        today = today_utc()
        posted = await self._post_daily(
            guild, channel, client, await self._due_releases(today)
        )
        await self._maybe_post_monthly(guild, channel, client, today)
        return posted

    async def force_post_daily(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        client: discord.Client,
    ) -> int:
        """Post today's releases, even ones already announced."""
        async with self._post_lock:
            books = await self._today_releases(today_utc())
            return await self._post_daily(guild, channel, client, books)

    async def force_post_monthly(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        client: discord.Client,
    ) -> int:
        """Post the current month's overview without moving the monthly marker."""
        async with self._post_lock:
            return await self._post_monthly(
                guild, channel, client, today_utc(), mark_posted=False
            )

    async def _today_releases(self, today: date) -> list[BookRelease]:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(BookRelease)
                    .where(
                        BookRelease.guild_id == self.guild_id,
                        BookRelease.release_date == today,
                    )
                    .order_by(*_release_order())
                )
            ).scalars()
        return list(rows)

    async def _due_releases(self, today: date) -> list[BookRelease]:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(BookRelease)
                    .where(
                        BookRelease.guild_id == self.guild_id,
                        BookRelease.release_date <= today,
                        BookRelease.announcement_sent.is_(False),
                    )
                    .order_by(*_release_order())
                )
            ).scalars()
        return list(rows)

    async def _post_daily(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        client: discord.Client,
        books: list[BookRelease],
    ) -> int:
        if not books:
            return 0

        members = await resolve_members(
            guild, [book.discord_user_id for book in books]
        )
        member_by_id = {
            book.discord_user_id: member
            for book, member in zip(books, members)
        }
        mentions = " ".join(
            dict.fromkeys(f"<@{book.discord_user_id}>" for book in books)
        )
        singular = len(books) == 1

        if self._ping_role_id:
            content = f"-# <@&{self._ping_role_id}>\n"
        else:
            content = ""

        content += "\n".join(
            [
                t(
                    "releases.post.daily_title_singular"
                    if singular
                    else "releases.post.daily_title_plural"
                ),
                t(
                    "releases.post.daily_thanks_singular"
                    if singular
                    else "releases.post.daily_thanks_plural",
                    mentions=mentions,
                ),
            ]
        )

        embeds = [
            build_book_embed(
                book, member_by_id.get(book.discord_user_id), client
            )
            for book in books
        ]
        posted = await _send_embed_batches(channel, embeds, content=content)
        posted_ids = [books[index].id for index in posted]
        await self.mark_announced(posted_ids)
        return len(posted_ids)

    async def _maybe_post_monthly(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        client: discord.Client,
        today: date,
    ) -> None:
        period = today.strftime("%Y-%m")
        if self._last_monthly_post == period:
            return
        await self._post_monthly(guild, channel, client, today)

    async def _post_monthly(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        client: discord.Client,
        today: date,
        *,
        mark_posted: bool = True,
    ) -> int:
        period = today.strftime("%Y-%m")
        books = await self.month_releases(today.year, today.month)
        if books:
            month_name = t(f"components.months.{today.month}")
            embeds = build_month_embeds(books, client)
            if self._ping_role_id:
                content = f"-# <@&{self._ping_role_id}>\n"
            else:
                content = ""

            content += "\n".join(
                [
                    t("releases.post.monthly_title", month=month_name),
                    t("releases.post.monthly_thanks"),
                ]
            )

            posted = await _send_embed_batches(
                channel, embeds, content=content
            )
            if len(posted) < len(embeds):
                logger.warning(
                    "Monthly overview only partly posted (%d/%d embeds); "
                    "will retry",
                    len(posted),
                    len(embeds),
                )
                return len(books)

        if mark_posted:
            await self.mark_monthly_posted(period)
        return len(books)
