"""Bi-weekly themes: a deck of cards, one posted every two weeks as an image.

The rules (validation, the schedule check, the deck handling) live as
module-level functions so they can be tested without a database. The
:class:`BiweeklyThemeService` owns the guild's settings and the card CRUD.
"""

from __future__ import annotations

import asyncio
import logging
import random
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime

import discord
from sqlalchemy import select, update

from ..db import session_scope
from ..models import BiweeklyThemeCard, BiweeklyThemeSetting, BiweeklyThemeSuggestion
from ..texts import t
from .hours import check_hour
from . import level as level_service
from .level import utcnow
from .search import search_key
from .theme_images import render_theme, theme_period

CARD_TITLE_MAX_LENGTH = 256
CARD_DESCRIPTION_MAX_LENGTH = 2000
POST_INTERVAL_DAYS = 14

logger = logging.getLogger("bh2.biweekly_theme")


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
    """Rendering a theme image failed; there is no fallback."""

    key = "biweekly_theme.errors.image_failed"

    def __init__(self, *, card: BiweeklyThemeCard, cause: Exception) -> None:
        super().__init__(title=card.title, error=cause)
        self.card = card
        self.cause = cause


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
    return check_hour(hour, InvalidHour)


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


def build_post_content(
    title: str, description: str | None = None, role_id: int | None = None
) -> str:
    """The message body posted alongside the rendered theme card."""
    lines: list[str] = []
    if role_id:
        lines.append(f"-# <@&{role_id}>")
    lines.append(t("biweekly_theme.post.heading", title=title))
    if description:
        lines.append(t("biweekly_theme.post.second_heading", desc=description))
    lines.append(t("biweekly_theme.post.explanation"))
    lines.append(" ‎ ")
    return "\n".join(lines)


def credit_description(description: str | None, name: str) -> str:
    """Append the suggester credit to a description, truncating to fit."""
    credit = t("biweekly_theme.suggest.credit", name=name)
    if description:
        candidate = f"{description}\n{credit}"
        if len(candidate) > CARD_DESCRIPTION_MAX_LENGTH:
            space = CARD_DESCRIPTION_MAX_LENGTH - len(f"\n{credit}")
            if space < 0:
                return credit[:CARD_DESCRIPTION_MAX_LENGTH]
            trimmed = description[:space].rstrip()
            candidate = f"{trimmed}\n{credit}" if trimmed else credit
            if len(candidate) > CARD_DESCRIPTION_MAX_LENGTH:
                candidate = credit[:CARD_DESCRIPTION_MAX_LENGTH]
            return candidate
        return candidate
    candidate = credit
    if len(candidate) > CARD_DESCRIPTION_MAX_LENGTH:
        candidate = candidate[:CARD_DESCRIPTION_MAX_LENGTH]
    return candidate


@dataclass
class ThemeCycle:
    """One run of rewarding the previous card and posting the next.

    Saved with the settings so a restart can pick up where the run stopped:
    the XP is paid when the cycle starts, the announcement is replayed until
    ``announced`` is set, and the next card waits for ``ready_at``.
    """

    started_at: datetime
    message_id: int | None
    participants: list[int]
    winner: int | None
    changes: dict[int, level_service.LevelChangeResult]
    winner_change: level_service.LevelChangeResult | None = None
    announced: bool = False
    ready_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "message_id": self.message_id,
            "participants": self.participants,
            "winner": self.winner,
            "changes": {
                str(user_id): asdict(change)
                for user_id, change in self.changes.items()
            },
            "winner_change": (
                asdict(self.winner_change) if self.winner_change else None
            ),
            "announced": self.announced,
            "ready_at": self.ready_at.isoformat() if self.ready_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ThemeCycle:
        winner_change = data.get("winner_change")
        ready_at = data.get("ready_at")
        return cls(
            started_at=datetime.fromisoformat(data["started_at"]),
            message_id=data.get("message_id"),
            participants=list(data.get("participants") or []),
            winner=data.get("winner"),
            changes={
                int(user_id): level_service.LevelChangeResult(**change)
                for user_id, change in (data.get("changes") or {}).items()
            },
            winner_change=(
                level_service.LevelChangeResult(**winner_change)
                if winner_change
                else None
            ),
            announced=bool(data.get("announced")),
            ready_at=datetime.fromisoformat(ready_at) if ready_at else None,
        )


class BiweeklyThemeService:
    """Settings plus the card deck behind the bi-weekly theme posts."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self._channel_id: int | None = None
        self._role_id: int | None = None
        self._weekday: int | None = None
        self._hour: int | None = None
        self._enabled = True
        self._last_posted_on: date | None = None
        self._last_attempted_on: date | None = None
        self._pinned_channel_id: int | None = None
        self._pinned_message_id: int | None = None
        self._settled_message_id: int | None = None
        self._submission_emoji: str | None = None
        self._cycle: ThemeCycle | None = None
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

    @property
    def pinned_message_id(self) -> int | None:
        return self._pinned_message_id

    @property
    def pinned_channel_id(self) -> int | None:
        return self._pinned_channel_id

    @property
    def settled_message_id(self) -> int | None:
        return self._settled_message_id

    @property
    def submission_emoji(self) -> str:
        return self._submission_emoji or t("biweekly_theme.settlement.emoji")

    @property
    def cycle(self) -> ThemeCycle | None:
        return self._cycle

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
        self._pinned_channel_id = row.pinned_channel_id if row else None
        self._pinned_message_id = row.pinned_message_id if row else None
        self._settled_message_id = row.settled_message_id if row else None
        self._submission_emoji = row.submission_emoji if row else None
        self._cycle = (
            ThemeCycle.from_dict(row.cycle) if row and row.cycle else None
        )

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

    async def set_submission_emoji(self, emoji: str) -> None:
        async with session_scope() as session:
            row = await self._settings(session, create=True)
            row.submission_emoji = emoji
            await session.commit()
        self._submission_emoji = emoji

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
            row.cycle = None
            await session.commit()
        self._last_posted_on = posted_on
        self._last_attempted_on = posted_on
        self._cycle = None

    async def mark_attempted(self, attempted_on: date) -> None:
        """Remember a failed scheduled run so it is not retried that day."""
        async with session_scope() as session:
            row = await self._settings(session, create=True)
            row.last_attempted_on = attempted_on
            row.cycle = None
            await session.commit()
        self._last_attempted_on = attempted_on
        self._cycle = None

    async def set_pinned_post(
        self, channel_id: int | None, message_id: int | None
    ) -> None:
        """Remember the theme post that is pinned, or clear it."""
        async with session_scope() as session:
            row = await self._settings(session, create=True)
            row.pinned_channel_id = channel_id
            row.pinned_message_id = message_id
            await session.commit()
        self._pinned_channel_id = channel_id
        self._pinned_message_id = message_id

    async def start_cycle(
        self,
        *,
        now: datetime,
        message_id: int | None,
        participants: list[int],
        winner: int | None,
        submission_xp: int,
        winner_xp: int,
    ) -> ThemeCycle:
        """Pay the participants and the winner and record the run.

        The XP, the settled marker and the saved run are written in one
        transaction, so a post is rewarded exactly once and everything the
        announcement needs survives a restart.
        """
        changes: dict[int, level_service.LevelChangeResult] = {}
        winner_change = None
        async with session_scope() as session:
            for user_id in participants:
                changes[user_id] = await level_service.add_xp(
                    session,
                    guild_id=self.guild_id,
                    user_id=user_id,
                    amount=submission_xp,
                )
            if winner is not None:
                winner_change = await level_service.add_xp(
                    session,
                    guild_id=self.guild_id,
                    user_id=winner,
                    amount=winner_xp,
                )
            cycle = ThemeCycle(
                started_at=now,
                message_id=message_id,
                participants=participants,
                winner=winner,
                changes=changes,
                winner_change=winner_change,
                announced=not participants,
                ready_at=None if participants else now,
            )
            row = await self._settings(session, create=True)
            if message_id is not None:
                row.settled_message_id = message_id
            row.cycle = cycle.to_dict()
            await session.commit()
        if message_id is not None:
            self._settled_message_id = message_id
        self._cycle = cycle
        return cycle

    async def save_cycle(self, cycle: ThemeCycle | None) -> None:
        async with session_scope() as session:
            row = await self._settings(session, create=True)
            row.cycle = cycle.to_dict() if cycle else None
            await session.commit()
        self._cycle = cycle

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

    async def list_cards(self) -> list[BiweeklyThemeCard]:
        async with session_scope() as session:
            statement = (
                select(BiweeklyThemeCard)
                .where(BiweeklyThemeCard.guild_id == self.guild_id)
                .order_by(BiweeklyThemeCard.id)
            )
            return list((await session.execute(statement)).scalars())

    async def search_cards(self, keyword: str) -> list[BiweeklyThemeCard]:
        needle = search_key(keyword)
        if not needle:
            return []
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(BiweeklyThemeCard).where(
                        BiweeklyThemeCard.guild_id == self.guild_id
                    )
                )
            ).scalars()
            cards = list(rows)
        matches: list[BiweeklyThemeCard] = []
        for card in cards:
            haystack = f"{card.title or ''} {card.description or ''}"
            if needle in search_key(haystack):
                matches.append(card)
        matches.sort(key=lambda card: card.id)
        return matches

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

    async def post_card(
        self,
        channel: discord.abc.Messageable,
        client: discord.Client,
        *,
        posted_on: date | None = None,
        card_id: int | None = None,
        view: discord.ui.View | None = None,
    ) -> BiweeklyThemeCard | None:
        """Render and post the next theme; returns the card that was posted.

        The card is only retired once Discord accepted the message. A failed
        render raises :class:`ThemeImageError` without falling back to a
        placeholder image.
        """
        async with self._post_lock:
            if card_id is not None:
                card = await self.get_card(card_id)
                if card is None:
                    raise CardNotFound(id=card_id)
            else:
                card = await self.pick_card()
                if card is None:
                    return None
            posted_on = posted_on or utcnow().date()
            start, end = theme_period(posted_on)
            try:
                path = render_theme(
                    prompt=card.title,
                    description=card.description,
                    start=start,
                    end=end,
                )
            except Exception as exc:
                raise ThemeImageError(card=card, cause=exc) from exc
            try:
                message = await channel.send(
                    content=build_post_content(card.title, card.description, self._role_id),
                    file=discord.File(path, filename="biweekly-theme.png"),
                    view=view,
                )
            finally:
                path.unlink(missing_ok=True)

            await self.mark_card_sent(card.id)
            await self._replace_pin(client, channel, message)
            return card

    async def _replace_pin(
        self,
        client: discord.Client,
        channel: discord.abc.Messageable,
        message: discord.Message,
    ) -> None:
        """Pin the new theme, unpinning the one posted two weeks earlier."""
        await self._unpin_previous(client, channel)
        try:
            await message.pin(reason=t("biweekly_theme.audit.pin"))
        except discord.HTTPException:
            logger.warning(
                "Could not pin the theme post %s", message.id, exc_info=True
            )
            return
        await self.set_pinned_post(channel.id, message.id)

    async def _unpin_previous(
        self, client: discord.Client, channel: discord.abc.Messageable
    ) -> None:
        message_id = self._pinned_message_id
        if message_id is None:
            return
        previous: discord.abc.Messageable | None = channel
        if getattr(channel, "id", None) != self._pinned_channel_id:
            previous = await self._channel(client, self._pinned_channel_id)
        if previous is not None:
            try:
                message = await previous.fetch_message(message_id)
                try:
                    await message.edit(view=None)
                except discord.HTTPException:
                    pass
                await message.unpin(reason=t("biweekly_theme.audit.unpin"))
            except discord.NotFound:
                pass
            except discord.HTTPException:
                logger.warning(
                    "Could not unpin the previous theme post %s",
                    message_id,
                    exc_info=True,
                )
        await self.set_pinned_post(None, None)

    async def add_suggestion(
        self,
        user_id: int,
        title: str,
        description: str | None,
        message_id: int,
    ) -> BiweeklyThemeSuggestion:
        clean_title = validate_title(title)
        clean_description = validate_description(description)
        async with session_scope() as session:
            suggestion = BiweeklyThemeSuggestion(
                guild_id=self.guild_id,
                user_id=user_id,
                title=clean_title,
                description=clean_description,
                message_id=message_id,
            )
            session.add(suggestion)
            await session.commit()
            await session.refresh(suggestion)
        return suggestion

    async def get_suggestion_by_message(
        self, message_id: int
    ) -> BiweeklyThemeSuggestion | None:
        async with session_scope() as session:
            return (
                await session.execute(
                    select(BiweeklyThemeSuggestion).where(
                        BiweeklyThemeSuggestion.guild_id == self.guild_id,
                        BiweeklyThemeSuggestion.message_id == message_id,
                    )
                )
            ).scalar_one_or_none()

    async def remove_suggestion(self, suggestion_id: int) -> bool:
        async with session_scope() as session:
            suggestion = (
                await session.execute(
                    select(BiweeklyThemeSuggestion).where(
                        BiweeklyThemeSuggestion.id == suggestion_id,
                        BiweeklyThemeSuggestion.guild_id == self.guild_id,
                    )
                )
            ).scalar_one_or_none()
            if suggestion is None:
                return False
            await session.delete(suggestion)
            await session.commit()
            return True

    async def _channel(
        self, client: discord.Client, channel_id: int | None
    ) -> discord.abc.Messageable | None:
        """Resolve a channel by id, falling back to an API fetch."""
        if not channel_id:
            return None
        channel = client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await client.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel
