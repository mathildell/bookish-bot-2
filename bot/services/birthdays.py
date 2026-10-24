"""Birthday celebrations: registration, content pools and announcements.

Birthdays are stored yearless and announced at local midnight in the member's
timezone. The XP gift and the channel post are tracked separately so a failed
post can be retried without paying the gift twice. Announcement rules live as
module-level functions so they can be tested without a database.
"""

from __future__ import annotations

import calendar
import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import discord
from sqlalchemy import delete, select

from ..db import session_scope
from ..models import Birthday, BirthdayImage, BirthdayMessage, BirthdaySetting
from ..notifications import announce_level_up
from ..texts import t, t_choice
from . import level as level_service
from .level_rewards import sync_member_roles
from .members import resolve_members
from .timezones import (
    default_member_timezone,
    local_now,
    to_utc,
    valid_timezone,
)

logger = logging.getLogger("bh2.birthdays")

XP_GIFT = 5000
MESSAGE_MAX_LENGTH = 2000
IMAGE_URL_MAX_LENGTH = 1000
EMBEDS_PER_MESSAGE = 10

_MONTH_NAMES = tuple(calendar.month_name)
_MONTH_ABBREVIATIONS = tuple(calendar.month_abbr)


class BirthdayError(Exception):
    """Base class for user-facing birthday problems (carries a text key)."""

    key = "birthday.errors.generic"

    def __init__(self, **variables) -> None:
        super().__init__(self.key)
        self.variables = variables


class InvalidDate(BirthdayError):
    key = "birthday.errors.invalid_date"


class InvalidTimezone(BirthdayError):
    key = "birthday.errors.invalid_timezone"


class MessageEmpty(BirthdayError):
    key = "birthday.errors.message_empty"


class MessageTooLong(BirthdayError):
    key = "birthday.errors.message_too_long"


class InvalidImageUrl(BirthdayError):
    key = "birthday.errors.invalid_url"


class ContentEmpty(BirthdayError):
    key = "birthday.errors.content_empty"


class ContentTooLong(BirthdayError):
    key = "birthday.errors.content_too_long"


class BirthdayNotRegistered(BirthdayError):
    key = "birthday.errors.not_registered"


class AlreadyCelebrated(BirthdayError):
    key = "birthday.errors.already_celebrated"


class MemberNotFound(BirthdayError):
    key = "birthday.errors.member_not_found"


class PostFailed(BirthdayError):
    key = "birthday.errors.post_failed"


# ----------------------------------------------------------------- pure rules


def days_in_month(month: int, year: int = 2000) -> int:
    """Number of days in ``month``; the default year allows February 29."""
    if month == 12:
        following = date(year + 1, 1, 1)
    else:
        following = date(year, month + 1, 1)
    return (following - date(year, month, 1)).days


def valid_date(month: int, day: int) -> bool:
    return 1 <= month <= 12 and 1 <= day <= days_in_month(month)


def date_value(month: int, day: int) -> str:
    """Stable autocomplete value for a yearless date."""
    return f"{month:02d}-{day:02d}"


def parse_date_value(raw: str) -> tuple[int, int]:
    """Parse ``MM-DD`` into a month and day."""
    try:
        month_raw, day_raw = (raw or "").split("-", 1)
        month, day = int(month_raw), int(day_raw)
    except (ValueError, AttributeError) as exc:
        raise InvalidDate() from exc
    if not valid_date(month, day):
        raise InvalidDate()
    return month, day


def date_label(month: int, day: int) -> str:
    return t(
        "components.date.month_day",
        month=t(f"components.months.{month}"),
        day=day,
    )


def search_birthday_dates(
    current: str, *, limit: int = 25
) -> list[tuple[str, str]]:
    """Searchable ``(value, label)`` date choices from January 1 to Dec 31."""
    needle = " ".join((current or "").strip().lower().split())
    scored: list[tuple[int, int, int, str, str]] = []
    for month in range(1, 13):
        name = _MONTH_NAMES[month].lower()
        abbreviation = _MONTH_ABBREVIATIONS[month].lower()
        for day in range(1, days_in_month(month) + 1):
            label = date_label(month, day)
            value = date_value(month, day)
            full = f"{name} {day}"
            short = f"{abbreviation} {day}"
            compact = f"{name}{day}"
            aliases = (full, short, compact)
            if not needle:
                score = 0
            elif needle == value or needle in aliases:
                score = 0
            elif needle == str(day):
                score = 1
            elif any(alias.startswith(needle) for alias in aliases):
                score = 1
            elif any(needle in alias for alias in aliases):
                score = 2
            elif needle.isdigit() and value.startswith(needle):
                score = 2
            else:
                continue
            scored.append((score, month, day, value, label))
    scored.sort()
    return [(value, label) for _, _, _, value, label in scored[:limit]]


def effective_day(month: int, day: int, year: int) -> int:
    """Clamp February 29 to February 28 in non-leap years."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        return 28
    return day


def is_birthday_on(birthday: Birthday, local_day: date) -> bool:
    return (
        birthday.month == local_day.month
        and effective_day(birthday.month, birthday.day, local_day.year)
        == local_day.day
    )


def local_birthday_date(birthday: Birthday, now: datetime) -> date:
    return local_now(now, birthday.timezone).date()


def next_birthday_at(birthday: Birthday, now: datetime) -> datetime:
    """Naive-UTC moment of the birthday's next local occurrence.

    A birthday falling today resolves to ``now`` so it sorts first and reads
    "now" rather than a time earlier that day.
    """
    local_today = local_now(now, birthday.timezone).date()
    year = local_today.year
    day = effective_day(birthday.month, birthday.day, year)
    occurrence = date(year, birthday.month, day)
    if occurrence < local_today:
        year += 1
        day = effective_day(birthday.month, birthday.day, year)
        occurrence = date(year, birthday.month, day)
    midnight = datetime.combine(
        occurrence, time.min, tzinfo=ZoneInfo(birthday.timezone)
    )
    moment = to_utc(midnight)
    return max(moment, now) if occurrence == local_today else moment


def needs_celebration(birthday: Birthday, now: datetime) -> bool:
    """Whether the birthday still needs its post or its XP gift today."""
    today = local_birthday_date(birthday, now)
    if not is_birthday_on(birthday, today):
        return False
    return (
        birthday.last_announced_on != today
        or birthday.xp_awarded_on != today
    )


def role_expired(birthday: Birthday, now: datetime) -> bool:
    if birthday.role_granted_on is None:
        return False
    return local_birthday_date(birthday, now) > birthday.role_granted_on


@dataclass(frozen=True)
class ContentChoice:
    message: str
    image_url: str | None


def _pick_content(rows: list, user_id: int):
    personal = [row for row in rows if row.user_id == user_id]
    pool = personal or [row for row in rows if row.user_id is None]
    return random.choice(pool) if pool else None


def is_image_url(url: str) -> bool:
    cleaned = (url or "").strip()
    if not cleaned.startswith(("http://", "https://")):
        return False
    if len(cleaned) > IMAGE_URL_MAX_LENGTH:
        return False
    return True


def choose_content(
    messages: list,
    images: list,
    user_id: int,
    *,
    fallback_message: str,
) -> ContentChoice:
    """Pick personal content when available, else the generic pool."""
    message = _pick_content(messages, user_id)
    image = _pick_content(images, user_id)
    return ContentChoice(
        message=message.content if message is not None else fallback_message,
        image_url=image.url if image is not None else None,
    )



def xp_award_summary(change: level_service.LevelChangeResult) -> dict:
    return {
        "old_level": change.old_level,
        "new_level": change.new_level,
        "progress_start": change.old_total_xp
        - level_service.total_xp_for_level(change.old_level),
        "progress_end": change.new_total_xp
        - level_service.total_xp_for_level(change.new_level),
        "needed": level_service.xp_increment_to_reach_level(change.new_level),
    }


def xp_progress_text(summary: dict) -> str:
    """Format the gift exactly like the weekly XP leaderboard details."""
    if summary["old_level"] == summary["new_level"]:
        return t(
            "level.xp.leaderboard.weekly_details_progress",
            xp=XP_GIFT,
            n1=summary["progress_start"],
            n2=summary["progress_end"],
            needed=summary["needed"],
        )
    return t(
        "level.xp.leaderboard.weekly_details_levels",
        xp=XP_GIFT,
        l1=summary["old_level"],
        l2=summary["new_level"],
        n1=summary["progress_start"],
        n2=summary["progress_end"],
        needed=summary["needed"],
    )


def render_birthday_message(template: str, member: discord.abc.User) -> str:
    """Fill the ``%{mention}`` placeholder in a birthday message."""
    return template.replace("%{mention}", member.mention)


def build_birthday_embed(
    client: discord.Client,
    member: discord.abc.User,
    content: ContentChoice,
    xp_award: dict | None,
) -> discord.Embed:
    embed = discord.Embed(colour=getattr(client, "default_embed_color", None))
    embed.set_author(
        name=discord.utils.escape_markdown(member.display_name),
        icon_url=member.display_avatar.url,
    )
    embed.title = t_choice("birthday.titles", user=member.display_name)
    embed.description = render_birthday_message(content.message, member)
    if content.image_url:
        embed.set_image(url=content.image_url)
    else:
        transparent = getattr(client, "transparent_embed_image", None)
        if transparent:
            embed.set_image(url=transparent)
    if xp_award:
        embed.add_field(
            name=t("birthday.post.xp_field"),
            value=xp_progress_text(xp_award),
            inline=False,
        )
    return embed


# ------------------------------------------------------------------- service


class BirthdayService:
    """Settings, CRUD and the scheduled celebration/role-expiry work."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self._channel_id: int | None = None
        self._role_id: int | None = None

    @property
    def channel_id(self) -> int | None:
        return self._channel_id

    @property
    def role_id(self) -> int | None:
        return self._role_id

    async def reload(self) -> None:
        async with session_scope() as session:
            row = await self._settings(session)
        self._channel_id = (row.channel_id or None) if row else None
        self._role_id = (row.role_id or None) if row else None

    async def _settings(self, session, *, create: bool = False):
        row = (
            await session.execute(
                select(BirthdaySetting).where(
                    BirthdaySetting.guild_id == self.guild_id
                )
            )
        ).scalar_one_or_none()
        if row is None and create:
            row = BirthdaySetting(guild_id=self.guild_id)
            session.add(row)
            await session.flush()
        return row

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

    async def get_birthday(self, user_id: int) -> Birthday | None:
        async with session_scope() as session:
            return (
                await session.execute(
                    select(Birthday).where(
                        Birthday.guild_id == self.guild_id,
                        Birthday.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()

    async def save_birthday(
        self,
        *,
        user_id: int,
        month: int,
        day: int,
        timezone: str | None,
    ) -> Birthday:
        if not valid_date(month, day):
            raise InvalidDate()
        zone = (
            valid_timezone(timezone)
            if timezone
            else await default_member_timezone(self.guild_id, user_id)
        )
        if zone is None:
            raise InvalidTimezone()
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Birthday).where(
                        Birthday.guild_id == self.guild_id,
                        Birthday.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = Birthday(guild_id=self.guild_id, user_id=user_id)
                session.add(row)
            row.month = month
            row.day = day
            row.timezone = zone
            await session.commit()
            await session.refresh(row)
            return row

    async def remove_birthday(self, user_id: int) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                delete(Birthday).where(
                    Birthday.guild_id == self.guild_id,
                    Birthday.user_id == user_id,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def remove_birthday_and_unlink_content(self, user_id: int) -> tuple[bool, bool]:
        removed_birthday = False
        had_linked = False
        async with session_scope() as session:
            result = await session.execute(
                delete(Birthday).where(
                    Birthday.guild_id == self.guild_id,
                    Birthday.user_id == user_id,
                )
            )
            removed_birthday = bool(result.rowcount)
            from sqlalchemy import update, select

            # Check if had linked content
            linked_msg = (
                await session.execute(
                    select(BirthdayMessage).where(
                        BirthdayMessage.guild_id == self.guild_id,
                        BirthdayMessage.user_id == user_id,
                    )
                )
            ).scalars().first()
            linked_img = (
                await session.execute(
                    select(BirthdayImage).where(
                        BirthdayImage.guild_id == self.guild_id,
                        BirthdayImage.user_id == user_id,
                    )
                )
            ).scalars().first()
            had_linked = linked_msg is not None or linked_img is not None
            await session.execute(
                update(BirthdayMessage)
                .where(
                    BirthdayMessage.guild_id == self.guild_id,
                    BirthdayMessage.user_id == user_id,
                )
                .values(user_id=None)
            )
            await session.execute(
                update(BirthdayImage)
                .where(
                    BirthdayImage.guild_id == self.guild_id,
                    BirthdayImage.user_id == user_id,
                )
                .values(user_id=None)
            )
            await session.commit()
        return removed_birthday, had_linked




    async def _list_content(self, model, *, user_id, search):
        async with session_scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(model)
                        .where(model.guild_id == self.guild_id)
                        .order_by(model.id)
                    )
                )
                .scalars()
                .all()
            )
        if user_id is not None:
            rows = [row for row in rows if row.user_id == user_id]
        needle = (search or "").strip().lower()
        if needle:
            attribute = "content" if model is BirthdayMessage else "url"
            rows = [
                row
                for row in rows
                if needle in str(getattr(row, attribute)).lower()
            ]
        return rows

    async def list_messages(
        self, *, user_id: int | None = None, search: str | None = None
    ) -> list[BirthdayMessage]:
        return await self._list_content(
            BirthdayMessage, user_id=user_id, search=search
        )

    async def list_images(
        self, *, user_id: int | None = None, search: str | None = None
    ) -> list[BirthdayImage]:
        return await self._list_content(
            BirthdayImage, user_id=user_id, search=search
        )

    async def add_message(
        self, *, content: str, user_id: int | None = None
    ) -> BirthdayMessage:
        message = (content or "").strip()
        if not message:
            raise MessageEmpty()
        if len(message) > MESSAGE_MAX_LENGTH:
            raise MessageTooLong(
                length=len(message), maximum=MESSAGE_MAX_LENGTH
            )
        async with session_scope() as session:
            row = BirthdayMessage(
                guild_id=self.guild_id, user_id=user_id, content=message
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def add_image(
        self, *, url: str, user_id: int | None = None
    ) -> BirthdayImage:
        cleaned = (url or "").strip()
        if (
            not cleaned.startswith(("http://", "https://"))
            or len(cleaned) > IMAGE_URL_MAX_LENGTH
        ):
            raise InvalidImageUrl()
        async with session_scope() as session:
            row = BirthdayImage(
                guild_id=self.guild_id, user_id=user_id, url=cleaned
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def add_content(self, *, content: str, user_id: int | None = None):
        cleaned = (content or "").strip()
        if not cleaned:
            raise ContentEmpty()
        if is_image_url(cleaned):
            try:
                return await self.add_image(url=cleaned, user_id=user_id)
            except InvalidImageUrl:
                raise
        if len(cleaned) > MESSAGE_MAX_LENGTH:
            raise ContentTooLong(length=len(cleaned), maximum=MESSAGE_MAX_LENGTH)
        try:
            return await self.add_message(content=cleaned, user_id=user_id)
        except MessageTooLong as exc:
            raise ContentTooLong(**exc.variables) from exc
        except MessageEmpty:
            raise ContentEmpty()

    async def remove_message(self, message_id: int) -> bool:
        return await self._remove_content(BirthdayMessage, message_id)

    async def remove_image(self, image_id: int) -> bool:
        return await self._remove_content(BirthdayImage, image_id)

    async def _remove_content(self, model, row_id: int) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                delete(model).where(
                    model.id == row_id, model.guild_id == self.guild_id
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def remove_content(self, row_id: int) -> bool:
        if await self.remove_message(row_id):
            return True
        if await self.remove_image(row_id):
            return True
        return False

    async def get_content(self, row_id: int):
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(BirthdayMessage).where(
                        BirthdayMessage.id == row_id,
                        BirthdayMessage.guild_id == self.guild_id,
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                return ("message", row)
            row = (
                await session.execute(
                    select(BirthdayImage).where(
                        BirthdayImage.id == row_id,
                        BirthdayImage.guild_id == self.guild_id,
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                return ("image", row)
        return None

    async def edit_content(
        self,
        row_id: int,
        *,
        content: str,
        user_id: int | None = None,
        clear_user: bool = False,
    ):
        cleaned = (content or "").strip()
        if not cleaned:
            raise ContentEmpty()
        kind_and_row = await self.get_content(row_id)
        if kind_and_row is None:
            return None
        kind, row = kind_and_row
        if is_image_url(cleaned):
            # If changing to URL, move to image type if currently message
            if kind == "message":
                # remove old message and add image
                async with session_scope() as session:
                    await session.execute(
                        delete(BirthdayMessage).where(
                            BirthdayMessage.id == row_id,
                            BirthdayMessage.guild_id == self.guild_id,
                        )
                    )
                    await session.commit()
                new_row = await self.add_image(
                    url=cleaned,
                    user_id=user_id if not clear_user else (row.user_id if user_id is not None or not clear_user else None),
                )
                # handle user assignment
                if clear_user:
                    async with session_scope() as session:
                        stored = (
                            await session.execute(
                                select(BirthdayImage).where(
                                    BirthdayImage.id == new_row.id
                                )
                            )
                        ).scalar_one_or_none()
                        if stored is not None:
                            stored.user_id = None
                            await session.commit()
                elif user_id is not None and user_id != new_row.user_id:
                    async with session_scope() as session:
                        stored = (
                            await session.execute(
                                select(BirthdayImage).where(
                                    BirthdayImage.id == new_row.id
                                )
                            )
                        ).scalar_one_or_none()
                        if stored is not None:
                            stored.user_id = user_id
                            await session.commit()
                # refresh
                new_row = await self.get_content(new_row.id)
                if new_row is not None:
                    return new_row[0], new_row[1]
                return None
            else:  # image
                if len(cleaned) > IMAGE_URL_MAX_LENGTH:
                    raise InvalidImageUrl()
                async with session_scope() as session:
                    stored = (
                        await session.execute(
                            select(BirthdayImage).where(
                                BirthdayImage.id == row_id,
                                BirthdayImage.guild_id == self.guild_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if stored is None:
                        return None
                    stored.url = cleaned
                    if clear_user:
                        stored.user_id = None
                    elif user_id is not None:
                        stored.user_id = user_id
                    await session.commit()
                    await session.refresh(stored)
                    return "image", stored
        else:
            # message
            if len(cleaned) > MESSAGE_MAX_LENGTH:
                raise ContentTooLong(length=len(cleaned), maximum=MESSAGE_MAX_LENGTH)
            if kind == "image":
                async with session_scope() as session:
                    await session.execute(
                        delete(BirthdayImage).where(
                            BirthdayImage.id == row_id,
                            BirthdayImage.guild_id == self.guild_id,
                        )
                    )
                    await session.commit()
                new_row = await self.add_message(content=cleaned, user_id=None)
                if clear_user:
                    pass
                elif user_id is not None:
                    async with session_scope() as session:
                        stored = (
                            await session.execute(
                                select(BirthdayMessage).where(
                                    BirthdayMessage.id == new_row.id
                                )
                            )
                        ).scalar_one_or_none()
                        if stored is not None:
                            stored.user_id = user_id
                            await session.commit()
                new_row_res = await self.get_content(new_row.id)
                if new_row_res is not None:
                    return new_row_res[0], new_row_res[1]
                return None
            else:  # message
                async with session_scope() as session:
                    stored = (
                        await session.execute(
                            select(BirthdayMessage).where(
                                BirthdayMessage.id == row_id,
                                BirthdayMessage.guild_id == self.guild_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if stored is None:
                        return None
                    stored.content = cleaned
                    if clear_user:
                        stored.user_id = None
                    elif user_id is not None:
                        stored.user_id = user_id
                    await session.commit()
                    await session.refresh(stored)
                    return "message", stored


    async def choose_content(self, user_id: int) -> ContentChoice:
        messages = await self.list_messages()
        images = await self.list_images()
        return choose_content(
            messages,
            images,
            user_id,
            fallback_message=t("birthday.post.fallback_description"),
        )

    async def list_birthdays(self) -> list[Birthday]:
        async with session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(Birthday).where(
                            Birthday.guild_id == self.guild_id
                        )
                    )
                )
                .scalars()
                .all()
            )

    async def due_birthdays(
        self, now: datetime | None = None
    ) -> list[Birthday]:
        now = now or level_service.utcnow()
        async with session_scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(Birthday).where(
                            Birthday.guild_id == self.guild_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [row for row in rows if needs_celebration(row, now)]

    async def expired_roles(
        self, now: datetime | None = None
    ) -> list[Birthday]:
        now = now or level_service.utcnow()
        async with session_scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(Birthday).where(
                            Birthday.guild_id == self.guild_id,
                            Birthday.role_granted_on.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [row for row in rows if role_expired(row, now)]

    async def _award_xp(self, birthday: Birthday, local_day: date) -> dict | None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Birthday).where(Birthday.id == birthday.id)
                )
            ).scalar_one_or_none()
            if row is None or row.xp_awarded_on == local_day:
                return row.xp_award if row is not None else None
            change = await level_service.add_xp(
                session,
                guild_id=self.guild_id,
                user_id=row.user_id,
                amount=XP_GIFT,
            )
            summary = xp_award_summary(change)
            row.xp_awarded_on = local_day
            row.xp_award = summary
            await session.commit()
            return summary

    async def _grant_role(
        self,
        birthday: Birthday,
        member: discord.Member,
        local_day: date,
        guild: discord.Guild,
    ) -> None:
        if self._role_id is None:
            return
        role = guild.get_role(self._role_id)
        if role is None:
            logger.warning("Birthday role %s no longer exists", self._role_id)
            return
        try:
            await member.add_roles(role, reason=t("birthday.role.reason"))
        except discord.HTTPException:
            logger.warning(
                "Could not give the birthday role to %s", member.id, exc_info=True
            )
            return
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Birthday).where(Birthday.id == birthday.id)
                )
            ).scalar_one_or_none()
            if row is not None:
                row.role_granted_on = local_day
                row.granted_role_id = role.id
                await session.commit()
        birthday.role_granted_on = local_day
        birthday.granted_role_id = role.id

    async def celebrate(
        self,
        *,
        guild: discord.Guild,
        channel: discord.abc.Messageable | None,
        client: discord.Client,
        birthdays: list[Birthday],
        now: datetime | None = None,
    ) -> int:
        """Post due celebrations, awarding XP and roles exactly once each."""
        if not birthdays:
            return 0
        now = now or level_service.utcnow()
        members = await resolve_members(
            guild, [birthday.user_id for birthday in birthdays]
        )

        embeds: list[tuple[Birthday, discord.Embed]] = []
        for birthday, member in zip(birthdays, members):
            if member is None:
                logger.warning(
                    "Skipping birthday for absent member %s", birthday.user_id
                )
                continue
            local_day = local_birthday_date(birthday, now)
            awarded_now = birthday.xp_awarded_on != local_day
            if awarded_now:
                summary = await self._award_xp(birthday, local_day)
                birthday.xp_awarded_on = local_day
                birthday.xp_award = summary
                if (
                    summary is not None
                    and summary["old_level"] != summary["new_level"]
                ):
                    await sync_member_roles(
                        guild,
                        member,
                        summary["new_level"],
                        client.level_rewards,
                    )
                    await announce_level_up(
                        channel, member, summary["new_level"]
                    )
            if birthday.role_granted_on != local_day:
                await self._grant_role(birthday, member, local_day, guild)
            if birthday.last_announced_on == local_day or channel is None:
                continue
            content = await self.choose_content(birthday.user_id)
            embeds.append(
                (
                    birthday,
                    build_birthday_embed(
                        client, member, content, birthday.xp_award
                    ),
                )
            )

        posted = 0
        for start in range(0, len(embeds), EMBEDS_PER_MESSAGE):
            chunk = embeds[start : start + EMBEDS_PER_MESSAGE]
            mentions = ", ".join(
                f"<@{birthday.user_id}>" for birthday, _ in chunk
            )
            try:
                await channel.send(
                    content=t(
                        "birthday.announcement", 
                        mentions=mentions,
                        channel=""
                    ),
                    embeds=[embed for _, embed in chunk],
                )
            except discord.HTTPException:
                logger.warning(
                    "Could not post birthday announcement", exc_info=True
                )
                continue
            for birthday, _ in chunk:
                local_day = local_birthday_date(birthday, now)
                async with session_scope() as session:
                    row = (
                        await session.execute(
                            select(Birthday).where(
                                Birthday.id == birthday.id
                            )
                        )
                    ).scalar_one_or_none()
                    if row is not None:
                        row.last_announced_on = local_day
                        await session.commit()
                birthday.last_announced_on = local_day
                posted += 1
        return posted

    async def force(
        self,
        *,
        user_id: int,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        client: discord.Client,
        now: datetime | None = None,
    ) -> None:
        now = now or level_service.utcnow()
        birthday = await self.get_birthday(user_id)
        if birthday is None:
            raise BirthdayNotRegistered()
        if birthday.last_announced_on == local_birthday_date(birthday, now):
            raise AlreadyCelebrated()
        members = await resolve_members(guild, [user_id])
        if members[0] is None:
            raise MemberNotFound()
        posted = await self.celebrate(
            guild=guild,
            channel=channel,
            client=client,
            birthdays=[birthday],
            now=now,
        )
        if not posted:
            raise PostFailed()

    async def expire_roles(
        self, guild: discord.Guild, now: datetime | None = None
    ) -> int:
        rows = await self.expired_roles(now)
        if not rows:
            return 0
        members = await resolve_members(
            guild, [row.user_id for row in rows]
        )
        cleared: list[Birthday] = []
        for row, member in zip(rows, members):
            if (
                member is not None
                and row.granted_role_id is not None
                and (role := guild.get_role(row.granted_role_id)) is not None
                and role in member.roles
            ):
                try:
                    await member.remove_roles(
                        role, reason=t("birthday.role.reason")
                    )
                except discord.HTTPException:
                    logger.warning(
                        "Could not remove the birthday role from %s",
                        row.user_id,
                        exc_info=True,
                    )
                    continue
            cleared.append(row)
        async with session_scope() as session:
            for row in cleared:
                stored = (
                    await session.execute(
                        select(Birthday).where(Birthday.id == row.id)
                    )
                ).scalar_one_or_none()
                if stored is not None:
                    stored.role_granted_on = None
                    stored.granted_role_id = None
            await session.commit()
        return len(cleared)
