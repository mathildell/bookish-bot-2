"""Bi-weekly themes: a deck of cards, one posted every two weeks as an image.

The rules (validation, the schedule check, the deck handling) live as
module-level functions so they can be tested without a database. The
:class:`BiweeklyThemeService` owns the guild's settings and the card CRUD.
"""

from __future__ import annotations

import asyncio
import random
import unicodedata
from datetime import date, datetime

import discord
from sqlalchemy import select, update

from ..db import session_scope
from ..models import BiweeklyThemeCard, BiweeklyThemeSetting
from ..texts import t
from .image_hosting import ImageHost
from .level import utcnow
from .theme_images import (
    THEME_IMGBB_EXPIRATION_SECONDS,
    render_theme,
    theme_period,
)

CARD_TITLE_MAX_LENGTH = 256
CARD_DESCRIPTION_MAX_LENGTH = 2000
POST_INTERVAL_DAYS = 14

# The weekday and hour the theme goes out on are both UTC.
DEFAULT_HOUR = 12


class BiweeklyThemeError(Exception):
    """Base class for user-facing problems (carries a text key)."""

    key = "biweekly_theme.errors.generic"

    def __init__(self, **variables) -> None:
        super().__init__(self.key)
        self.variables = variables


class MissingTitle(BiweeklyThemeError):
    key = "biweekly_theme.errors.missing_title"


class TitleTooLong(BiweeklyThemeError):
    key = "biweekly_theme.errors.title_too_long"


class DescriptionTooLong(BiweeklyThemeError):
    key = "biweekly_theme.errors.description_too_long"


class DuplicateCard(BiweeklyThemeError):
    key = "biweekly_theme.errors.duplicate_card"


class CardNotFound(BiweeklyThemeError):
    key = "biweekly_theme.errors.card_not_found"


class InvalidWeekday(BiweeklyThemeError):
    key = "biweekly_theme.errors.invalid_weekday"


class InvalidHour(BiweeklyThemeError):
    key = "biweekly_theme.errors.invalid_hour"


class ThemeImageError(BiweeklyThemeError):
    """Rendering or uploading a theme image failed; there is no fallback."""

    key = "biweekly_theme.errors.image_failed"

    def __init__(self, *, card: BiweeklyThemeCard, cause: Exception) -> None:
        super().__init__(title=card.title, error=cause)
        self.card = card
        self.cause = cause


# ----------------------------------------------------------------- pure rules


def normalize_title(title: str) -> str:
    """Fold a title to its comparison form.

    Case, repeated whitespace and compatibility characters (via NFKC) are
    ignored, so "Read  A Book" and "read a book" are the same card.
    """
    folded = unicodedata.normalize("NFKC", title or "")
    return " ".join(folded.split()).casefold()


def validate_title(raw: str) -> str:
    title = (raw or "").strip()
    if not title:
        raise MissingTitle()
    if len(title) > CARD_TITLE_MAX_LENGTH:
        raise TitleTooLong(
            length=len(title), maximum=CARD_TITLE_MAX_LENGTH
        )
    return title


def validate_description(raw: str | None) -> str | None:
    description = (raw or "").strip()
    if not description:
        return None
    if len(description) > CARD_DESCRIPTION_MAX_LENGTH:
        raise DescriptionTooLong(
            length=len(description), maximum=CARD_DESCRIPTION_MAX_LENGTH
        )
    return description


def validate_weekday(weekday: int) -> int:
    if not isinstance(weekday, int) or not 0 <= weekday <= 6:
        raise InvalidWeekday(weekday=weekday)
    return weekday


def validate_hour(hour: int) -> int:
    if not isinstance(hour, int) or not 0 <= hour <= 23:
        raise InvalidHour(hour=hour)
    return hour


def is_post_due(
    now: datetime,
    *,
    weekday: int | None,
    hour: int | None,
    enabled: bool,
    last_posted_on: date | None,
    last_attempted_on: date | None,
) -> bool:
    """Whether the bi-weekly theme should be posted at ``now``.

    The check is deliberately exact: a run missed while the bot was down is
    skipped rather than made up later. A failed attempt still advances the
    cadence so the hourly loop cannot repeat the same alert all day.
    """
    if not enabled or weekday is None or hour is None:
        return False
    if now.weekday() != weekday or now.hour != hour:
        return False
    if last_attempted_on == now.date():
        return False
    anchors = [
        day
        for day in (last_posted_on, last_attempted_on)
        if day is not None
    ]
    if not anchors:
        return True
    return (now.date() - max(anchors)).days >= POST_INTERVAL_DAYS


def format_posted_date(day: date) -> str:
    """Render the date a theme was last posted on."""
    return f"{day.strftime('%B')} {day.day}, {day.year}"


def build_theme_embed(
    image_url: str, client: discord.Client
) -> discord.Embed:
    """The embed a posted theme shows: a fixed title and the generated image."""
    embed = discord.Embed(
        title=t("biweekly_theme.post.embed_title"),
        description=t("biweekly_theme.post.embed_description"),
        colour=getattr(client, "default_embed_color", None),
    )
    embed.set_image(url=image_url)
    return embed


# ------------------------------------------------------------------- service


class BiweeklyThemeService:
    """Settings plus the card deck behind the bi-weekly theme posts."""

    def __init__(
        self, guild_id: int, image_host: ImageHost | None = None
    ) -> None:
        self.guild_id = guild_id
        self._image_host = image_host or ImageHost()
        self._channel_id: int | None = None
        self._role_id: int | None = None
        self._weekday: int | None = None
        self._hour: int | None = None
        self._enabled = True
        self._last_posted_on: date | None = None
        self._last_attempted_on: date | None = None
        # Serialises posts so a manual run cannot race the scheduled loop.
        self._post_lock = asyncio.Lock()

    @property
    def channel_id(self) -> int | None:
        return self._channel_id

    @property
    def role_id(self) -> int | None:
        return self._role_id

    @property
    def weekday(self) -> int | None:
        return self._weekday

    @property
    def hour(self) -> int | None:
        return self._hour

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def last_posted_on(self) -> date | None:
        return self._last_posted_on

    @property
    def last_attempted_on(self) -> date | None:
        return self._last_attempted_on

    # ------------------------------------------------------------- settings

    async def reload(self) -> None:
        async with session_scope() as session:
            row = await self._settings(session)
        self._channel_id = row.channel_id if row else None
        self._role_id = row.role_id if row else None
        self._weekday = row.weekday if row else None
        self._hour = row.hour if row else None
        self._enabled = bool(row.enabled) if row else True
        self._last_posted_on = row.last_posted_on if row else None
        self._last_attempted_on = row.last_attempted_on if row else None

    async def set_channel(self, channel_id: int) -> None:
        async with session_scope() as session:
            row = await self._settings(session, create=True)
            row.channel_id = channel_id
            await session.commit()
        self._channel_id = channel_id

    async def set_role(self, role_id: int | None) -> None:
        async with session_scope() as session:
            row = await self._settings(session, create=True)
            row.role_id = role_id
            await session.commit()
        self._role_id = role_id

    async def set_schedule(self, weekday: int, hour: int) -> None:
        validate_weekday(weekday)
        validate_hour(hour)
        async with session_scope() as session:
            row = await self._settings(session, create=True)
            row.weekday = weekday
            row.hour = hour
            await session.commit()
        self._weekday = weekday
        self._hour = hour

    async def set_enabled(self, enabled: bool) -> None:
        async with session_scope() as session:
            row = await self._settings(session, create=True)
            row.enabled = enabled
            await session.commit()
        self._enabled = enabled

    async def mark_posted(self, posted_on: date) -> None:
        """Remember a successful scheduled post."""
        async with session_scope() as session:
            row = await self._settings(session, create=True)
            row.last_posted_on = posted_on
            row.last_attempted_on = posted_on
            await session.commit()
        self._last_posted_on = posted_on
        self._last_attempted_on = posted_on

    async def mark_attempted(self, attempted_on: date) -> None:
        """Remember a failed scheduled run so it is not retried that day."""
        async with session_scope() as session:
            row = await self._settings(session, create=True)
            row.last_attempted_on = attempted_on
            await session.commit()
        self._last_attempted_on = attempted_on

    def is_due(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return is_post_due(
            now,
            weekday=self._weekday,
            hour=self._hour,
            enabled=self._enabled,
            last_posted_on=self._last_posted_on,
            last_attempted_on=self._last_attempted_on,
        )

    async def _settings(
        self, session, *, create: bool = False
    ) -> BiweeklyThemeSetting | None:
        row = (
            await session.execute(
                select(BiweeklyThemeSetting).where(
                    BiweeklyThemeSetting.guild_id == self.guild_id
                )
            )
        ).scalar_one_or_none()
        if row is None and create:
            row = BiweeklyThemeSetting(guild_id=self.guild_id)
            session.add(row)
            await session.flush()
        return row

    # ---------------------------------------------------------------- cards

    async def list_cards(self) -> list[BiweeklyThemeCard]:
        async with session_scope() as session:
            statement = (
                select(BiweeklyThemeCard)
                .where(BiweeklyThemeCard.guild_id == self.guild_id)
                .order_by(BiweeklyThemeCard.id)
            )
            return list((await session.execute(statement)).scalars())

    async def get_card(self, card_id: int) -> BiweeklyThemeCard | None:
        async with session_scope() as session:
            return await self._card(session, card_id)

    async def add_card(
        self, title: str, description: str | None = None
    ) -> BiweeklyThemeCard:
        clean_title = validate_title(title)
        clean_description = validate_description(description)
        async with session_scope() as session:
            if await self._title_taken(session, clean_title):
                raise DuplicateCard(title=clean_title)
            card = BiweeklyThemeCard(
                guild_id=self.guild_id,
                title=clean_title,
                description=clean_description,
            )
            session.add(card)
            await session.commit()
            await session.refresh(card)
        return card

    async def edit_card(
        self,
        card_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> BiweeklyThemeCard:
        """Apply the given fields on top of an existing card."""
        async with session_scope() as session:
            card = await self._card(session, card_id)
            if card is None:
                raise CardNotFound(id=card_id)
            if title is not None:
                clean_title = validate_title(title)
                if await self._title_taken(
                    session, clean_title, exclude_id=card.id
                ):
                    raise DuplicateCard(title=clean_title)
                card.title = clean_title
            if description is not None:
                card.description = validate_description(description)
            await session.commit()
            await session.refresh(card)
            return card

    async def remove_card(self, card_id: int) -> bool:
        async with session_scope() as session:
            card = await self._card(session, card_id)
            if card is None:
                return False
            await session.delete(card)
            await session.commit()
            return True

    async def _card(
        self, session, card_id: int
    ) -> BiweeklyThemeCard | None:
        return (
            await session.execute(
                select(BiweeklyThemeCard).where(
                    BiweeklyThemeCard.guild_id == self.guild_id,
                    BiweeklyThemeCard.id == card_id,
                )
            )
        ).scalar_one_or_none()

    async def _title_taken(
        self, session, title: str, *, exclude_id: int | None = None
    ) -> bool:
        """Whether the deck already holds a card with the same title."""
        cards = list(
            (
                await session.execute(
                    select(BiweeklyThemeCard).where(
                        BiweeklyThemeCard.guild_id == self.guild_id
                    )
                )
            ).scalars()
        )
        needle = normalize_title(title)
        return any(
            card.id != exclude_id and normalize_title(card.title) == needle
            for card in cards
        )

    # ----------------------------------------------------------------- deck

    async def unsent_cards(self) -> list[BiweeklyThemeCard]:
        async with session_scope() as session:
            statement = (
                select(BiweeklyThemeCard)
                .where(
                    BiweeklyThemeCard.guild_id == self.guild_id,
                    BiweeklyThemeCard.sent.is_(False),
                )
                .order_by(BiweeklyThemeCard.id)
            )
            return list((await session.execute(statement)).scalars())

    async def reset_deck(self) -> int:
        """Make every card available again; returns how many were reactivated."""
        async with session_scope() as session:
            result = await session.execute(
                update(BiweeklyThemeCard)
                .where(
                    BiweeklyThemeCard.guild_id == self.guild_id,
                    BiweeklyThemeCard.sent.is_(True),
                )
                .values(sent=False, sent_at=None)
            )
            await session.commit()
            return result.rowcount or 0

    async def pick_card(self) -> BiweeklyThemeCard | None:
        """Choose the card to post.

        A random card that has not been posted yet is used; once every card
        has been posted the deck starts over.
        """
        cards = await self.unsent_cards()
        if not cards:
            await self.reset_deck()
            cards = await self.unsent_cards()
        if not cards:
            return None
        return random.choice(cards)

    async def mark_card_sent(
        self, card_id: int, now: datetime | None = None
    ) -> None:
        now = now or utcnow()
        async with session_scope() as session:
            card = await self._card(session, card_id)
            if card is None:
                return
            card.sent = True
            card.sent_at = now
            await session.commit()

    # -------------------------------------------------------------- posting

    async def post_card(
        self,
        channel: discord.abc.Messageable,
        client: discord.Client,
        *,
        posted_on: date | None = None,
    ) -> BiweeklyThemeCard | None:
        """Render and post the next theme; returns the card that was posted.

        The card is only retired once Discord accepted the message. A failed
        render or upload raises :class:`ThemeImageError` without falling back
        to a placeholder image.
        """
        async with self._post_lock:
            card = await self.pick_card()
            if card is None:
                return None
            posted_on = posted_on or utcnow().date()
            start, end = theme_period(posted_on)
            path = None
            try:
                path = render_theme(
                    prompt=card.title,
                    description=card.description,
                    start=start,
                    end=end,
                )
                url = await self._image_host.upload(
                    path,
                    expiration_seconds=THEME_IMGBB_EXPIRATION_SECONDS,
                )
            except Exception as exc:
                raise ThemeImageError(card=card, cause=exc) from exc
            finally:
                if path is not None:
                    path.unlink(missing_ok=True)

            content = f"<@&{self._role_id}>" if self._role_id else None
            await channel.send(
                content=content, embed=build_theme_embed(url, client)
            )
            await self.mark_card_sent(card.id)
            return card
