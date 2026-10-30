"""Goodreads review sharing: poll members' profiles and post new reviews.

Goodreads retired the RSS feeds, so the bot reads the same "recent updates"
fragment the profile page lazy-loads, then fetches each new review's page with a
dedicated account's cookie to get the full text, precise rating, shelves and
cover. Reviews are posted at most once every 24h.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp
import discord
from bs4 import BeautifulSoup
from sqlalchemy import delete, select

from ..db import session_scope
from ..models import BotSetting, ReviewEntry, ReviewFeed, ReviewSetting
from ..texts import t
from .html import html_to_markdown
from .level import utcnow
from .members import resolve_members

logger = logging.getLogger("bh2.reviews")

FEED_COOLDOWN = timedelta(hours=24)
REVIEW_FIELD_LIMIT = 1024
FOOTER_LIMIT = 2048
GOODREADS_HOST = "goodreads.com"
GOODREADS_BASE = "https://www.goodreads.com"
PROFILE_FETCH_DELAY = 10.0
REVIEW_FETCH_DELAY = 5.0
AUTH_NOTICE_COOLDOWN = timedelta(days=1)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US",
}
# Used when the profile fragment has no logo of its own.
FALLBACK_LOGO_URL = (
    "https://encrypted-tbn0.gstatic.com/images"
    "?q=tbn:ANd9GcQcUlHk6BJ3pn20upzMcRofDF1wNeBRMCLN466YWOdcn4WqI36z-pT7ql8&s=10"
)
_ENTRY_ID_RE = re.compile(r"/review/show/(\d+)")
_PROFILE_ID_RE = re.compile(
    r"/(?:user/show|user/delayable_user_show|review/list_rss|review/list)/(\d+)"
)
_ARC_SHELF_RE = re.compile(r"\barcs?\b", re.IGNORECASE)
_FINISHED_READING_RE = re.compile(
    r"^(.*?)\s*[–-]\s*Finished Reading(?:\s*\([^)]*\))?$"
)


class ReviewAuthError(Exception):
    """The configured Goodreads cookie can no longer read review pages."""


@dataclass(frozen=True)
class FeedEntry:
    """One reviewable item parsed out of a Goodreads profile update."""

    entry_id: str
    link: str
    title: str
    cover_url: str | None
    author: str | None
    rating: str
    date_read: datetime | None
    review_html: str
    pub_date: datetime
    shelves: list[str]


@dataclass(frozen=True)
class FeedData:
    """A parsed profile update page: its logo and review entries."""

    logo_url: str | None
    entries: list[FeedEntry]


@dataclass(frozen=True)
class PollResult:
    """Outcome of one review poll."""

    posted: int = 0
    auth_failed: bool = False
    missing_cookie: bool = False


def normalize_entry_id(link: str) -> str:
    """Reduce a review URL to its numeric Goodreads review id."""
    match = _ENTRY_ID_RE.search(link or "")
    return match.group(1) if match else (link or "").strip()


def goodreads_user_id(value: str) -> str | None:
    """Extract the numeric user id from a Goodreads profile/reviews URL."""
    text = (value or "").strip()
    if text.isdigit():
        return text
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not (
        host == GOODREADS_HOST or host.endswith(f".{GOODREADS_HOST}")
    ):
        return None
    match = _PROFILE_ID_RE.search(parsed.path)
    return match.group(1) if match else None


def normalize_profile_url(value: str) -> str | None:
    """Return the canonical profile URL for a Goodreads user/reviews URL."""
    user_id = goodreads_user_id(value)
    if user_id is None:
        return None
    return f"{GOODREADS_BASE}/user/show/{user_id}"


def goodreads_link(href: str) -> str | None:
    """Resolve an href, refusing anything outside Goodreads.

    Review pages are fetched with the saved session cookie, so a link from a
    malicious page must never send that cookie to another host.
    """
    url = urljoin(GOODREADS_BASE, href or "")
    host = (urlparse(url).hostname or "").lower()
    if not (host == GOODREADS_HOST or host.endswith(f".{GOODREADS_HOST}")):
        return None
    return url


def is_goodreads_profile_url(value: str) -> bool:
    """Whether ``value`` looks like a Goodreads profile or reviews URL."""
    return normalize_profile_url(value) is not None


def parse_profile_timestamp(value: str | None) -> datetime | None:
    """Parse the profile fragment's ``October 21, 2026 08:46`` timestamps."""
    if not value:
        return None
    try:
        return datetime.strptime(" ".join(value.split()), "%B %d, %Y %H:%M")
    except ValueError:
        return None


def parse_finished_date(value: str) -> datetime | None:
    """Parse the review page's "Finished Reading" date."""
    try:
        return datetime.strptime(" ".join(value.split()), "%B %d, %Y")
    except ValueError:
        return None


def parse_stars(element) -> str:
    """Read a Goodreads star widget as a numeric rating string."""
    if element is None:
        return ""
    full = len(element.select(".readOnlyStar.full"))
    half = len(element.select(".readOnlyStar.half"))
    rating = full + 0.5 * half
    if rating <= 0:
        return ""
    return f"{rating:g}"


def redact_spoilers(fragment: str) -> str:
    """Remove hidden spoiler content and replace its control with a marker."""
    soup = BeautifulSoup(fragment or "", "html.parser")
    for node in soup.select(
        "span.spoilerContainer, span.spoiler, input.spoiler__control"
    ):
        node.decompose()
    for node in soup.select("a.jsShowSpoiler, label.spoiler"):
        node.replace_with(t("reviews.embed.view_spoiler"))
    return soup.decode_contents()


def parse_profile_updates(html_text: str) -> FeedData:
    """Parse the delayable profile fragment, keeping only reviews with text."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    entries: list[FeedEntry] = []
    seen: set[str] = set()
    for update in soup.select("tr.update[data-timestamp]"):
        review_link = update.select_one(
            "a.updatedTimestamp[href*='/review/show/']"
        )
        if review_link is None:
            continue
        href = review_link.get("href") or ""
        link = goodreads_link(href)
        if link is None:
            continue
        entry_id = normalize_entry_id(href)
        if not entry_id or entry_id in seen:
            continue
        review_element = update.select_one(
            f"span#freeTextreview{entry_id}"
        ) or update.select_one(f"span#freeTextContainerreview{entry_id}")
        if review_element is None:
            review_element = soup.find(
                "span", id=f"freeTextreview{entry_id}"
            ) or soup.find("span", id=f"freeTextContainerreview{entry_id}")
        if review_element is None:
            continue
        review_html = review_element.decode_contents()
        if not review_html.strip():
            continue
        pub_date = parse_profile_timestamp(update.get("data-timestamp"))
        if pub_date is None:
            continue
        title_element = update.select_one("a.bookTitle")
        author_element = update.select_one("a.authorName")
        cover_element = update.select_one("div.updateImage img")
        entries.append(
            FeedEntry(
                entry_id=entry_id,
                link=link,
                title=(
                    title_element.get_text(" ", strip=True)
                    if title_element is not None
                    else ""
                ),
                cover_url=(
                    cover_element.get("src")
                    if cover_element is not None
                    else None
                ),
                author=(
                    author_element.get_text(" ", strip=True)
                    if author_element is not None
                    else None
                ),
                rating=parse_stars(update.select_one("span.readOnlyStars")),
                date_read=None,
                review_html=redact_spoilers(review_html),
                pub_date=pub_date,
                shelves=[],
            )
        )
        seen.add(entry_id)
    return FeedData(logo_url=FALLBACK_LOGO_URL, entries=entries)


def _review_page_rating(soup: BeautifulSoup) -> str:
    """Read the precise rating, preferring the half-star widget.

    The structured-data ``ratingValue`` rounds half stars down, so use the
    rendered stars first and only fall back to the metadata.
    """
    block = soup.select_one("div.hreview")
    scope = block if block is not None else soup
    stars = parse_stars(scope.select_one("span.readOnlyStars"))
    if stars:
        return stars
    meta = soup.select_one("div.rating meta[itemprop='ratingValue']")
    if meta is not None and meta.get("content"):
        return meta["content"].strip()
    value = soup.select_one("div.rating span.value-title[title]")
    if value is not None and value.get("title"):
        return value["title"].strip()
    return parse_stars(soup.select_one("span.readOnlyStars"))


def _review_page_shelves(soup: BeautifulSoup) -> list[str]:
    """Read the shelves shown on the review itself, not the book timeline."""
    block = soup.select_one("div.hreview")
    scope = block if block is not None else soup
    shelves: list[str] = []
    for link in scope.select("a[href*='?tag='], a[href*='?shelf=']"):
        text = link.get_text(" ", strip=True)
        if text.lower().endswith(("reviews", "highlights")):
            continue
        query = parse_qs(urlparse(link.get("href") or "").query)
        for name in query.get("tag", []) + query.get("shelf", []):
            if name and name.lower() != "all" and name not in shelves:
                shelves.append(name)
    return shelves


def _review_page_date_read(soup: BeautifulSoup) -> datetime | None:
    """Return the latest "Finished Reading" date on the review timeline."""
    latest: datetime | None = None
    for element in soup.select(".readingTimeline__text"):
        text = " ".join(element.get_text(" ", strip=True).split())
        match = _FINISHED_READING_RE.match(text)
        if not match:
            continue
        parsed = parse_finished_date(match.group(1))
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    return latest


def review_page_has_content(html_text: str) -> bool:
    """Whether a fetched review page contains an actual review block."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    return (
        soup.select_one("div.hreview") is not None
        or soup.select_one("div.reviewText") is not None
        or soup.select_one(".ReviewText__content") is not None
    )


def parse_review_page(html_text: str, entry: FeedEntry) -> FeedEntry:
    """Overlay the full review page's details onto a profile-feed entry."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    # Comments and other widgets reuse .reviewText; stay inside the review
    # block so a comment is never mistaken for the review.
    block = soup.select_one("div.hreview")
    scope = block if block is not None else soup
    review_element = scope.select_one(
        "div.reviewText, .ReviewText__content"
    ) or soup.select_one(".ReviewText__content")
    review_html = entry.review_html
    if review_element is not None:
        fragment = review_element.decode_contents()
        if fragment.strip():
            review_html = redact_spoilers(fragment)

    cover_element = scope.select_one(
        "img[src*='/books/']"
    ) or soup.select_one("img[src*='/books/']")
    if cover_element is None:
        cover_element = soup.select_one("img[alt*=' by ']")
    title_element = scope.select_one("a.bookTitle")
    author_element = scope.select_one("a.authorName")
    return replace(
        entry,
        title=(
            title_element.get_text(" ", strip=True)
            if title_element is not None
            else entry.title
        ),
        cover_url=(
            cover_element.get("src")
            if cover_element is not None
            else entry.cover_url
        ),
        author=(
            author_element.get_text(" ", strip=True)
            if author_element is not None
            else entry.author
        ),
        rating=_review_page_rating(soup) or entry.rating,
        date_read=_review_page_date_read(soup) or entry.date_read,
        review_html=review_html,
        shelves=_review_page_shelves(soup) or entry.shelves,
    )


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


def format_shelf(owner: str, shelves: list[str]) -> str:
    """Render the footer: the member's shelf, plus its tags when present.

    The tags are joined with commas; the whole label is starred when the book
    sits on an ARC shelf.
    """
    if shelves:
        text = t(
            "reviews.embed.shelf_tags",
            user=owner,
            tags=", ".join(shelves),
        )
    else:
        text = t("reviews.embed.shelf_none", user=owner)
    if any(_ARC_SHELF_RE.search(shelf) for shelf in shelves):
        text = t("reviews.embed.shelf_arc", shelf=text)
    return _truncate(text, FOOTER_LIMIT)


def _truncate(value: str, limit: int = REVIEW_FIELD_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def should_post(
    entry: FeedEntry,
    row: ReviewEntry | None,
    now: datetime,
    feed_since: datetime | None,
    cooldown: timedelta = FEED_COOLDOWN,
) -> bool:
    """Decide whether ``entry`` should be posted to the reviews channel."""
    if row is None:
        # Never posted (or untracked). Anything predating tracking is backlog.
        return feed_since is not None and entry.pub_date >= feed_since
    if entry.pub_date <= row.last_pub_date:
        return False
    if row.last_posted_at is None:
        return True
    return now - row.last_posted_at >= cooldown


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
            name=member.display_name,
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
        text=format_shelf(
            discord.utils.escape_markdown(
                member.display_name
                if member is not None
                else t("reviews.embed.unknown_member")
            ),
            entry.shelves,
        ),
        icon_url=feed.logo_url,
    )

    background = getattr(client, "transparent_embed_image", None)
    if background:
        embed.set_image(url=background)
    return embed


async def fetch_profile_updates(
    http: aiohttp.ClientSession, profile_url: str
) -> str:
    """Download a member's recent-updates fragment."""
    user_id = goodreads_user_id(profile_url)
    if user_id is None:
        raise ValueError("Not a Goodreads profile URL")
    url = (
        f"{GOODREADS_BASE}/user/delayable_user_show/{user_id}"
        "?csm_scope=delayable_user_show&skip_cache=false"
    )
    async with http.get(url) as response:
        response.raise_for_status()
        return await response.text()


def is_sign_in_page(html_text: str, final_url: str = "") -> bool:
    """Whether a fetched page is Goodreads' sign-in wall."""
    if "/user/sign_in" in (final_url or ""):
        return True
    soup = BeautifulSoup(html_text or "", "html.parser")
    title = (
        soup.title.get_text(" ", strip=True).lower()
        if soup.title is not None
        else ""
    )
    if "sign in" in title:
        return True
    return soup.select_one('form[action="/user/sign_in"]') is not None


async def fetch_review_page(
    http: aiohttp.ClientSession, review_url: str, cookie: str
) -> str:
    """Download a review page with the dummy account's session cookie."""
    if not cookie:
        raise ReviewAuthError
    async with http.get(
        review_url, headers={"Cookie": cookie}
    ) as response:
        text = await response.text()
        if (
            response.status in (401, 403)
            or "/user/sign_in" in str(response.url)
            or is_sign_in_page(text, str(response.url))
        ):
            raise ReviewAuthError
        response.raise_for_status()
        return text


class ReviewService:
    """CRUD for feeds/entries/settings plus the polling worker."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self._channel_id: int | None = None
        self._repost_cooldown_days = FEED_COOLDOWN.days
        self._cookie: str | None = None
        self._last_auth_notice_at: datetime | None = None
        # Serialises polls so a manual repost cannot race the scheduled loop.
        self._poll_lock = asyncio.Lock()

    @property
    def channel_id(self) -> int | None:
        return self._channel_id

    @property
    def repost_cooldown_days(self) -> int:
        return self._repost_cooldown_days

    @property
    def goodreads_session_cookie(self) -> str | None:
        return self._cookie

    def set_goodreads_session_cookie(self, cookie: str | None) -> None:
        self._cookie = cookie
        self._last_auth_notice_at = None

    def auth_notice_due(self, now: datetime) -> bool:
        """Whether the daily cookie-expiry notice may be posted again."""
        if self._last_auth_notice_at is None:
            return True
        return now - self._last_auth_notice_at >= AUTH_NOTICE_COOLDOWN

    def mark_auth_notice_sent(self, now: datetime) -> None:
        self._last_auth_notice_at = now

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
            bot_row = (
                await session.execute(
                    select(BotSetting).where(
                        BotSetting.guild_id == self.guild_id
                    )
                )
            ).scalar_one_or_none()
        self._channel_id = row.channel_id if row is not None else None
        self._repost_cooldown_days = (
            row.repost_cooldown_days
            if row is not None and row.repost_cooldown_days
            else FEED_COOLDOWN.days
        )
        self._cookie = (
            bot_row.goodreads_session_cookie if bot_row is not None else None
        )

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

    async def set_repost_cooldown_days(self, days: int) -> None:
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
                    ReviewSetting(
                        guild_id=self.guild_id,
                        channel_id=self._channel_id or 0,
                        repost_cooldown_days=days,
                    )
                )
            else:
                row.repost_cooldown_days = days
            await session.commit()
        self._repost_cooldown_days = days

    # -------------------------------------------------------------- feeds

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
        """Store a member's profile, resetting tracking when the URL changes.

        Returns the row and whether the feed was (re)initialised, i.e. its
        backlog should not be posted.
        """
        normalized = normalize_profile_url(feed_url) or feed_url.strip()
        async with session_scope() as session:
            row = await self._find_feed(session, discord_user_id)
            changed = row is None or row.feed_url != normalized
            if row is None:
                row = ReviewFeed(
                    discord_user_id=discord_user_id, feed_url=normalized
                )
                session.add(row)
            else:
                row.feed_url = normalized
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

    async def remove_feed(self, discord_user_id: int) -> bool:
        """Delete a member's feed and the tracking for its entries."""
        async with session_scope() as session:
            result = await session.execute(
                delete(ReviewFeed).where(
                    ReviewFeed.discord_user_id == discord_user_id
                )
            )
            await session.execute(
                delete(ReviewEntry).where(
                    ReviewEntry.discord_user_id == discord_user_id
                )
            )
            await session.commit()
        return result.rowcount > 0

    # ------------------------------------------------------------ entries

    async def force_repost(self, discord_user_id: int, entry_id: str) -> bool:
        """Forget an entry's tracking so it is posted again on the next poll."""
        async with session_scope() as session:
            result = await session.execute(
                delete(ReviewEntry).where(
                    ReviewEntry.discord_user_id == discord_user_id,
                    ReviewEntry.entry_id == entry_id,
                )
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

    @staticmethod
    async def _entries_for_feed(
        session, discord_user_id: int
    ) -> list[ReviewEntry]:
        return list(
            (
                await session.execute(
                    select(ReviewEntry).where(
                        ReviewEntry.discord_user_id == discord_user_id
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _record(
        self,
        discord_user_id: int,
        entry: FeedEntry,
        now: datetime,
        *,
        posted: bool,
    ) -> None:
        """Persist an entry's latest update time, and its post time when posted."""
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
    ) -> PollResult:
        """Poll every profile and post eligible review updates."""
        async with self._poll_lock:
            return await self._poll_feeds(guild, channel, client)

    async def repost_entry(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        client: discord.Client,
        *,
        discord_user_id: int,
        entry_id: str,
    ) -> PollResult:
        """Re-post a single tracked review, leaving every other entry alone."""
        async with self._poll_lock:
            return await self._poll_feeds(
                guild,
                channel,
                client,
                only=(discord_user_id, entry_id),
            )

    async def _poll_feeds(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        client: discord.Client,
        *,
        only: tuple[int, str] | None = None,
    ) -> PollResult:
        feeds = await self.all_feeds()
        if only is not None:
            feeds = [feed for feed in feeds if feed.discord_user_id == only[0]]
        if not feeds:
            return PollResult()
        # Without the session cookie the full review cannot be read, so hold
        # every post until an admin configures one.
        cookie = self._cookie
        if not cookie:
            return PollResult(missing_cookie=True)
        members = await resolve_members(
            guild, [feed.discord_user_id for feed in feeds]
        )
        now = utcnow()
        result = PollResult()
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(
            timeout=timeout, headers=DEFAULT_HEADERS
        ) as http:
            for feed, member in zip(feeds, members):
                try:
                    data = parse_profile_updates(
                        await fetch_profile_updates(http, feed.feed_url)
                    )
                except (
                    aiohttp.ClientError,
                    TimeoutError,
                    ValueError,
                ) as exc:
                    logger.warning(
                        "Could not read review profile for %s: %s",
                        feed.discord_user_id,
                        exc,
                    )
                    await asyncio.sleep(PROFILE_FETCH_DELAY)
                    continue
                async with session_scope() as session:
                    tracked = {
                        row.entry_id: row
                        for row in await self._entries_for_feed(
                            session, feed.discord_user_id
                        )
                    }
                stop = False
                for entry in data.entries:
                    if only is not None and entry.entry_id != only[1]:
                        continue
                    row = tracked.get(entry.entry_id)
                    if should_post(
                        entry,
                        row,
                        now,
                        feed.feed_since,
                        timedelta(days=self._repost_cooldown_days),
                    ):
                        if member is None:
                            logger.warning(
                                "Skipping review for unknown member %s",
                                feed.discord_user_id,
                            )
                            continue
                        try:
                            review_html = await fetch_review_page(
                                http, entry.link, cookie
                            )
                        except ReviewAuthError:
                            logger.warning(
                                "Goodreads session expired while reading "
                                "review %s",
                                entry.entry_id,
                            )
                            result = replace(result, auth_failed=True)
                            stop = True
                            break
                        except (aiohttp.ClientError, TimeoutError) as exc:
                            logger.warning(
                                "Could not read review %s, will retry: %s",
                                entry.entry_id,
                                exc,
                            )
                            await asyncio.sleep(REVIEW_FETCH_DELAY)
                            continue
                        if not review_page_has_content(review_html):
                            logger.warning(
                                "Could not parse review %s, will retry",
                                entry.entry_id,
                            )
                            await asyncio.sleep(REVIEW_FETCH_DELAY)
                            continue
                        enriched = parse_review_page(review_html, entry)
                        await asyncio.sleep(REVIEW_FETCH_DELAY)
                        embed = build_review_embed(enriched, data, member, client)
                        try:
                            await channel.send(embed=embed)
                        except discord.HTTPException:
                            logger.warning(
                                "Could not post review %s",
                                entry.entry_id,
                                exc_info=True,
                            )
                            continue
                        await self._record(
                            feed.discord_user_id, entry, now, posted=True
                        )
                        result = replace(result, posted=result.posted + 1)
                    elif row is not None and entry.pub_date > row.last_pub_date:
                        # An edit while the cooldown was active: consume it so
                        # it is not posted later once the cooldown expires.
                        await self._record(
                            feed.discord_user_id, entry, now, posted=False
                        )
                await asyncio.sleep(PROFILE_FETCH_DELAY)
                if stop:
                    break
        return result
