"""Goodreads review sharing: poll members' RSS feeds and post new reviews.

Port of the old Ruby "RssFeeds::PostReview" feature:
- each member registers their personal Goodreads ``list_rss`` feed;
- a scheduled job polls every feed and posts new review entries as embeds;
- entries with an empty review body are skipped;
- an entry is posted at most once every 24h, so edits made while a cooldown is
  active are silently dropped while a later edit is posted again.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlparse
from xml.etree import ElementTree

import aiohttp
import discord
from sqlalchemy import delete, select, update

from ..db import session_scope
from ..models import ReviewEntry, ReviewFeed, ReviewSetting
from ..texts import t
from .level import utcnow
from .members import resolve_members

logger = logging.getLogger("bh2.reviews")

FEED_COOLDOWN = timedelta(hours=24)
REVIEW_FIELD_LIMIT = 1024
GOODREADS_HOST = "goodreads.com"
_ENTRY_ID_RE = re.compile(r"/review/show/(\d+)")
_WS_BEFORE_NEWLINE_RE = re.compile(r"[ \t]+\n")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class FeedEntry:
    """One reviewable item parsed out of a Goodreads feed."""

    entry_id: str
    link: str
    title: str
    cover_url: str | None
    author: str | None
    rating: str
    date_read: datetime | None
    review_html: str
    pub_date: datetime


@dataclass(frozen=True)
class FeedData:
    """A parsed feed: its shelf name, logo, and review entries."""

    shelf: str
    logo_url: str | None
    entries: list[FeedEntry]


def normalize_entry_id(link: str) -> str:
    """Reduce a review URL to its numeric Goodreads review id."""
    match = _ENTRY_ID_RE.search(link or "")
    return match.group(1) if match else (link or "").strip()


def is_goodreads_feed_url(value: str) -> bool:
    """Whether ``value`` looks like a Goodreads ``list_rss`` feed URL."""
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    host = parsed.hostname or ""
    return (
        parsed.scheme in ("http", "https")
        and host.endswith(GOODREADS_HOST)
        and "list_rss" in parsed.path
    )


def _text(element: ElementTree.Element, tag: str) -> str | None:
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an RFC-822 date from the feed into naive UTC."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def parse_feed(xml_text: str) -> FeedData:
    """Parse a Goodreads RSS document, keeping only entries with a review."""
    root = ElementTree.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("Not an RSS feed")

    title = _text(channel, "title") or ""
    shelf = title.rsplit(": ", 1)[-1] if ": " in title else title
    image = channel.find("image")
    logo_url = _text(image, "url") if image is not None else None

    entries: list[FeedEntry] = []
    for item in channel.findall("item"):
        review_html = _text(item, "user_review") or ""
        if not review_html.strip():
            continue
        pub_date = _parse_datetime(_text(item, "pubDate"))
        if pub_date is None:
            continue
        link = _text(item, "link") or _text(item, "guid") or ""
        entries.append(
            FeedEntry(
                entry_id=normalize_entry_id(link) or link,
                link=link,
                title=_text(item, "title") or "",
                cover_url=_text(item, "book_image_url") or None,
                author=_text(item, "author_name") or None,
                rating=_text(item, "user_rating") or "",
                date_read=_parse_datetime(_text(item, "user_read_at")),
                review_html=review_html,
                pub_date=pub_date,
            )
        )
    return FeedData(shelf=shelf, logo_url=logo_url, entries=entries)


class _ReviewHtmlToMarkdown(HTMLParser):
    """Convert the small HTML dialect Goodreads uses into Discord markdown."""

    _INLINE = {
        "b": "**",
        "strong": "**",
        "i": "*",
        "em": "*",
        "u": "__",
        "s": "~~",
        "strike": "~~",
        "del": "~~",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._anchors: list[str | None] = []
        self._spans: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attributes = dict(attrs)
        if tag in self._INLINE:
            self.parts.append(self._INLINE[tag])
        elif tag == "br":
            self.parts.append("\n")
        elif tag in ("p", "div"):
            self.parts.append("\n")
        elif tag == "blockquote":
            self.parts.append("\n> ")
        elif tag == "a":
            href = attributes.get("href")
            self._anchors.append(href)
            if href:
                self.parts.append("[")
        elif tag == "img":
            src = attributes.get("src")
            if src:
                self.parts.append(src)
        elif tag == "span":
            spoiler = "spoiler" in (attributes.get("class") or "")
            self._spans.append(spoiler)
            if spoiler:
                self.parts.append("||")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._INLINE:
            self.parts.append(self._INLINE[tag])
        elif tag in ("p", "div"):
            self.parts.append("\n")
        elif tag == "blockquote":
            self.parts.append("\n")
        elif tag == "a":
            href = self._anchors.pop() if self._anchors else None
            if href:
                self.parts.append(f"]({href})")
        elif tag == "span":
            if self._spans and self._spans.pop():
                self.parts.append("||")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_markdown(value: str) -> str:
    """Convert a review body's HTML into Discord-friendly markdown."""
    parser = _ReviewHtmlToMarkdown()
    parser.feed(value or "")
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = _WS_BEFORE_NEWLINE_RE.sub("\n", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def format_rating(value: str) -> str:
    """Render a raw rating string as ``3 ⭐`` / ``3.5 ⭐``, or a placeholder."""
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return t("reviews.embed.rating_none")
    if rating <= 0:
        return t("reviews.embed.rating_none")
    return t("reviews.embed.rating_value", rating=f"{rating:g}")


def format_date_read(value: datetime | None) -> str:
    """Render the date a book was read, or a placeholder."""
    if value is None:
        return t("reviews.embed.date_read_none")
    timestamp = int(value.replace(tzinfo=timezone.utc).timestamp())
    return f"<t:{timestamp}:D>"


def _truncate(value: str, limit: int = REVIEW_FIELD_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def should_post(
    entry: FeedEntry,
    row: ReviewEntry | None,
    now: datetime,
    feed_since: datetime | None,
) -> bool:
    """Decide whether ``entry`` should be posted to the reviews channel."""
    if row is None:
        # Never posted (or untracked). Anything predating tracking is backlog.
        return feed_since is not None and entry.pub_date >= feed_since
    if entry.pub_date <= row.last_pub_date:
        return False
    if row.last_posted_at is None:
        return True
    return now - row.last_posted_at >= FEED_COOLDOWN


def build_review_embed(
    entry: FeedEntry,
    feed: FeedData,
    member: discord.Member | None,
    client: discord.Client,
) -> discord.Embed:
    """Build the review embed posted to the reviews channel."""
    embed = discord.Embed(colour=getattr(client, "default_embed_color", None))

    if member is not None:
        avatar = getattr(member, "display_avatar", None)
        embed.set_author(
            name=discord.utils.escape_markdown(member.display_name),
            icon_url=getattr(avatar, "url", None),
        )
    else:
        embed.set_author(
            name=discord.utils.escape_markdown(t("reviews.embed.unknown_member"))
        )

    title = entry.title or t("reviews.embed.untitled_book")
    embed.title = (
        f"{discord.utils.escape_markdown(title)}"
        f"{t('reviews.embed.title_suffix')}"
    )
    if entry.link:
        embed.url = entry.link
    if entry.cover_url:
        embed.set_thumbnail(url=entry.cover_url)

    author = (
        discord.utils.escape_markdown(entry.author)
        if entry.author
        else t("reviews.embed.author_none")
    )
    embed.add_field(
        name=t("reviews.embed.author_field"), value=author, inline=True
    )
    embed.add_field(
        name=t("reviews.embed.date_read_field"),
        value=format_date_read(entry.date_read),
        inline=True,
    )
    embed.add_field(
        name=t("reviews.embed.rating_field"),
        value=format_rating(entry.rating),
        inline=True,
    )

    review = html_to_markdown(entry.review_html) or t("reviews.embed.review_empty")
    embed.add_field(
        name=t("reviews.embed.review_field"),
        value=_truncate(review),
        inline=False,
    )

    embed.set_footer(
        text=feed.shelf or t("reviews.embed.unknown_shelf"),
        icon_url=feed.logo_url,
    )
    return embed


async def fetch_feed(http: aiohttp.ClientSession, url: str) -> str:
    """Download a feed's XML text."""
    async with http.get(url) as response:
        response.raise_for_status()
        return await response.text()


class ReviewService:
    """CRUD for feeds/entries/settings plus the polling worker."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self._channel_id: int | None = None

    @property
    def channel_id(self) -> int | None:
        return self._channel_id

    # ----------------------------------------------------------- settings

    async def reload(self) -> None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(ReviewSetting).where(
                        ReviewSetting.guild_id == self.guild_id
                    )
                )
            ).scalar_one_or_none()
        self._channel_id = row.channel_id if row is not None else None

    async def set_channel(self, channel_id: int) -> None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(ReviewSetting).where(
                        ReviewSetting.guild_id == self.guild_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    ReviewSetting(guild_id=self.guild_id, channel_id=channel_id)
                )
            else:
                row.channel_id = channel_id
            await session.commit()
        self._channel_id = channel_id

    # -------------------------------------------------------------- feeds

    async def get_feed(self, discord_user_id: int) -> ReviewFeed | None:
        async with session_scope() as session:
            return await self._find_feed(session, discord_user_id)

    async def all_feeds(self) -> list[ReviewFeed]:
        async with session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(ReviewFeed).order_by(ReviewFeed.id)
                    )
                )
                .scalars()
                .all()
            )

    async def set_feed(
        self, discord_user_id: int, feed_url: str
    ) -> tuple[ReviewFeed, bool]:
        """Store a member's feed, resetting tracking when the URL changes.

        Returns the row and whether the feed was (re)initialised, i.e. its
        backlog should not be posted.
        """
        async with session_scope() as session:
            row = await self._find_feed(session, discord_user_id)
            changed = row is None or row.feed_url != feed_url
            if row is None:
                row = ReviewFeed(
                    discord_user_id=discord_user_id, feed_url=feed_url
                )
                session.add(row)
            else:
                row.feed_url = feed_url
            if changed:
                row.feed_since = utcnow()
                await session.execute(
                    delete(ReviewEntry).where(
                        ReviewEntry.discord_user_id == discord_user_id
                    )
                )
            await session.commit()
            await session.refresh(row)
        return row, changed

    # ------------------------------------------------------------ entries

    async def reset_cooldown(self, discord_user_id: int, entry_id: str) -> bool:
        """Clear an entry's cooldown so its next edit is posted again."""
        async with session_scope() as session:
            result = await session.execute(
                update(ReviewEntry)
                .where(
                    ReviewEntry.discord_user_id == discord_user_id,
                    ReviewEntry.entry_id == entry_id,
                )
                .values(last_posted_at=None)
            )
            await session.commit()
        return result.rowcount > 0

    @staticmethod
    async def _find_feed(session, discord_user_id: int) -> ReviewFeed | None:
        return (
            await session.execute(
                select(ReviewFeed).where(
                    ReviewFeed.discord_user_id == discord_user_id
                )
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _find_entry(
        session, discord_user_id: int, entry_id: str
    ) -> ReviewEntry | None:
        return (
            await session.execute(
                select(ReviewEntry).where(
                    ReviewEntry.discord_user_id == discord_user_id,
                    ReviewEntry.entry_id == entry_id,
                )
            )
        ).scalar_one_or_none()

    async def _record(
        self,
        discord_user_id: int,
        entry: FeedEntry,
        now: datetime,
        *,
        posted: bool,
    ) -> None:
        """Persist an entry's latest pubDate, and its post time when posted."""
        async with session_scope() as session:
            row = await self._find_entry(
                session, discord_user_id, entry.entry_id
            )
            if row is None:
                if not posted:
                    return
                row = ReviewEntry(
                    discord_user_id=discord_user_id,
                    entry_id=entry.entry_id,
                    last_pub_date=entry.pub_date,
                )
                session.add(row)
            elif entry.pub_date > row.last_pub_date:
                row.last_pub_date = entry.pub_date
            if posted:
                row.last_posted_at = now
            await session.commit()

    # -------------------------------------------------------------- worker

    async def post_new_reviews(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        client: discord.Client,
    ) -> int:
        """Poll every feed and post eligible review updates. Returns the count."""
        feeds = await self.all_feeds()
        if not feeds:
            return 0
        members = await resolve_members(
            guild, [feed.discord_user_id for feed in feeds]
        )
        now = utcnow()
        posted = 0
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            for feed, member in zip(feeds, members):
                try:
                    data = parse_feed(await fetch_feed(http, feed.feed_url))
                except (
                    aiohttp.ClientError,
                    TimeoutError,
                    ValueError,
                    ElementTree.ParseError,
                ) as exc:
                    logger.warning(
                        "Could not read review feed for %s: %s",
                        feed.discord_user_id,
                        exc,
                    )
                    continue
                for entry in data.entries:
                    async with session_scope() as session:
                        row = await self._find_entry(
                            session, feed.discord_user_id, entry.entry_id
                        )
                    if should_post(entry, row, now, feed.feed_since):
                        if member is None:
                            logger.warning(
                                "Skipping review for unknown member %s",
                                feed.discord_user_id,
                            )
                            continue
                        embed = build_review_embed(entry, data, member, client)
                        try:
                            await channel.send(embed=embed)
                        except discord.HTTPException:
                            logger.warning(
                                "Could not post review %s", entry.entry_id,
                                exc_info=True,
                            )
                            continue
                        await self._record(
                            feed.discord_user_id, entry, now, posted=True
                        )
                        posted += 1
                    elif row is not None and entry.pub_date > row.last_pub_date:
                        # An edit while the cooldown was active: consume it so
                        # it is not posted later once the cooldown expires.
                        await self._record(
                            feed.discord_user_id, entry, now, posted=False
                        )
        return posted
