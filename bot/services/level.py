"""Leveling system: XP and level computation, and per-user XP storage."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from ..db import session_scope
from ..models import UserXP, WeeklySetting, WeeklyXP

DEFAULT_WEEKLY_POST_HOUR = 12
WEEKLY_LEADERBOARD_SIZE = 10
SUNDAY = 6


def utcnow() -> datetime:
    """Naive UTC timestamp, matching the naive DATETIME columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def week_start_for(day: date) -> date:
    """The Monday starting the week that contains ``day``."""
    return day - timedelta(days=day.weekday())


def xp_increment_to_reach_level(level: int) -> int:
    """XP needed to pass a level (reach the next one) given the current level.

    Formula: (100 * level number) + 75. A level 0 member needs 75 XP to reach
    level 1; a level 1 member needs 175 more to reach level 2; and so on.
    """
    if level < 0:
        level = 0
    return 100 * level + 75


def total_xp_for_level(level: int) -> int:
    """Cumulative XP accumulated to finish level ``level``.

    Level 0 -> 0, level 1 -> 75, level 2 -> 250, level 3 -> 525, ...
    Closed form of the sum of ``xp_increment_to_reach_level`` over each level.
    """
    if level <= 0:
        return 0
    return 50 * level * (level - 1) + 75 * level


def level_from_total_xp(xp: int) -> int:
    """Compute the level reached from a total accumulated XP amount.

    Closed form of the largest ``level`` with ``total_xp_for_level(level) <= xp``,
    i.e. the non-negative root of ``50 * level**2 + 25 * level - xp = 0``.
    ``isqrt`` floors the root, so at most one correction step is needed.
    """
    if xp <= 0:
        return 0
    level = (math.isqrt(625 + 200 * xp) - 25) // 100
    if total_xp_for_level(level + 1) <= xp:
        level += 1
    return level


def random_xp_gain() -> int:
    """Random XP between 15 and 40 inclusive."""
    return random.randint(15, 40)


@dataclass
class LevelChangeResult:
    old_level: int
    new_level: int


def _apply_total_xp(user_xp: UserXP, amount: int) -> LevelChangeResult:
    old_level = user_xp.level
    user_xp.total_xp = max(0, user_xp.total_xp + amount)
    new_level = level_from_total_xp(user_xp.total_xp)
    user_xp.level = new_level
    return LevelChangeResult(old_level=old_level, new_level=new_level)


async def _get_or_create(session, *, guild_id: int, user_id: int) -> UserXP:
    stmt = select(UserXP).where(
        UserXP.guild_id == guild_id, UserXP.user_id == user_id
    )
    user_xp = (await session.execute(stmt)).scalar_one_or_none()
    if user_xp is None:
        user_xp = UserXP(guild_id=guild_id, user_id=user_id, total_xp=0, level=0)
        session.add(user_xp)
    return user_xp


async def _get_or_create_weekly(
    session, *, guild_id: int, user_id: int
) -> WeeklyXP:
    stmt = select(WeeklyXP).where(
        WeeklyXP.guild_id == guild_id, WeeklyXP.user_id == user_id
    )
    weekly = (await session.execute(stmt)).scalar_one_or_none()
    if weekly is None:
        weekly = WeeklyXP(guild_id=guild_id, user_id=user_id, xp=0)
        session.add(weekly)
    return weekly


async def _bump_weekly_xp(
    session, *, guild_id: int, user_id: int, amount: int
) -> None:
    if amount == 0:
        return
    weekly = await _get_or_create_weekly(
        session, guild_id=guild_id, user_id=user_id
    )
    week_start = week_start_for(utcnow().date())
    if weekly.week_start != week_start:
        weekly.week_start = week_start
        weekly.xp = 0
    weekly.xp = max(0, weekly.xp + amount)


async def add_xp(
    session, *, guild_id: int, user_id: int, amount: int
) -> LevelChangeResult:
    if amount < 0:
        amount = 0
    user_xp = await _get_or_create(session, guild_id=guild_id, user_id=user_id)
    await _bump_weekly_xp(
        session, guild_id=guild_id, user_id=user_id, amount=amount
    )
    return _apply_total_xp(user_xp, amount)


async def remove_xp(
    session, *, guild_id: int, user_id: int, amount: int
) -> LevelChangeResult:
    if amount < 0:
        amount = 0
    user_xp = await _get_or_create(session, guild_id=guild_id, user_id=user_id)
    await _bump_weekly_xp(
        session, guild_id=guild_id, user_id=user_id, amount=-amount
    )
    return _apply_total_xp(user_xp, -amount)


async def get_user_xp(session, *, guild_id: int, user_id: int) -> UserXP | None:
    stmt = select(UserXP).where(
        UserXP.guild_id == guild_id, UserXP.user_id == user_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def set_frozen(
    session, *, guild_id: int, user_id: int, frozen: bool
) -> UserXP:
    user_xp = await _get_or_create(session, guild_id=guild_id, user_id=user_id)
    user_xp.xp_frozen = frozen
    return user_xp


def can_gain_message_xp(
    user_xp: UserXP | None, now: datetime, cooldown_seconds: int = 60
) -> bool:
    """Whether a member may earn XP from a message (cooldown + freeze)."""
    if user_xp is None:
        return True
    if user_xp.xp_frozen:
        return False
    if user_xp.last_xp_at is None:
        return True
    return now - user_xp.last_xp_at >= timedelta(seconds=cooldown_seconds)


async def mark_xp_gained(
    session, *, guild_id: int, user_id: int, now: datetime
) -> None:
    user_xp = await _get_or_create(session, guild_id=guild_id, user_id=user_id)
    user_xp.last_xp_at = now


async def get_rank(session, *, guild_id: int, total_xp: int) -> int:
    stmt = select(func.count(UserXP.user_id)).where(
        UserXP.guild_id == guild_id, UserXP.total_xp > total_xp
    )
    return ((await session.execute(stmt)).scalar() or 0) + 1


async def all_user_xp(session, *, guild_id: int) -> list[UserXP]:
    stmt = (
        select(UserXP)
        .where(UserXP.guild_id == guild_id)
        .order_by(UserXP.total_xp.desc(), UserXP.user_id.asc())
    )
    return list((await session.execute(stmt)).scalars())


async def count_user_xp(session, *, guild_id: int) -> int:
    """Number of ranked members, for sizing the leaderboard pagination."""
    stmt = select(func.count()).select_from(UserXP).where(
        UserXP.guild_id == guild_id
    )
    return int((await session.execute(stmt)).scalar_one())


async def page_of_user_xp(
    session, *, guild_id: int, limit: int, offset: int
) -> list[tuple[UserXP, int]]:
    """One leaderboard page: members ordered by XP, with their competition rank.

    Ordering and ranking happen in MySQL, so only ``limit`` rows cross the wire.
    Ties on total XP share a rank (``RANK()``).
    """
    rank_expr = func.rank().over(order_by=UserXP.total_xp.desc()).label("rank")
    stmt = (
        select(UserXP, rank_expr)
        .where(UserXP.guild_id == guild_id)
        .order_by(UserXP.total_xp.desc(), UserXP.user_id.asc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return [(user_xp, int(rank)) for user_xp, rank in result]


async def page_of_weekly_xp(
    session, *, guild_id: int, week_start: date, limit: int, offset: int = 0
) -> list[WeeklyXP]:
    """Members ordered by XP earned in the week starting ``week_start``."""
    stmt = (
        select(WeeklyXP)
        .where(
            WeeklyXP.guild_id == guild_id,
            WeeklyXP.week_start == week_start,
        )
        .order_by(WeeklyXP.xp.desc(), WeeklyXP.user_id.asc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars())


def is_weekly_post_due(
    now: datetime, *, post_hour: int, last_posted_on: date | None
) -> bool:
    """Whether the weekly leaderboard should be posted at ``now``."""
    return (
        now.weekday() == SUNDAY
        and now.hour >= post_hour
        and last_posted_on != now.date()
    )


class WeeklyLeaderboardService:
    """Settings and data for the weekly XP leaderboard."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self._channel_id: int | None = None
        self._post_hour = DEFAULT_WEEKLY_POST_HOUR
        self._last_posted_on: date | None = None

    @property
    def channel_id(self) -> int | None:
        return self._channel_id

    @property
    def post_hour(self) -> int:
        return self._post_hour

    async def reload(self) -> None:
        async with session_scope() as session:
            row = await self._find_setting(session)
        self._channel_id = row.channel_id if row else None
        self._post_hour = (
            row.post_hour if row and row.post_hour else DEFAULT_WEEKLY_POST_HOUR
        )
        self._last_posted_on = row.last_posted_on if row else None

    async def set_channel(self, channel_id: int) -> None:
        async with session_scope() as session:
            row = await self._find_setting(session)
            if row is None:
                row = WeeklySetting(guild_id=self.guild_id)
                session.add(row)
            row.channel_id = channel_id
            await session.commit()
        self._channel_id = channel_id

    async def set_post_hour(self, hour: int) -> None:
        async with session_scope() as session:
            row = await self._find_setting(session)
            if row is None:
                row = WeeklySetting(guild_id=self.guild_id)
                session.add(row)
            row.post_hour = hour
            await session.commit()
        self._post_hour = hour

    def is_due(self, now: datetime) -> bool:
        return is_weekly_post_due(
            now, post_hour=self._post_hour, last_posted_on=self._last_posted_on
        )

    async def top(
        self, *, week_start: date, limit: int = WEEKLY_LEADERBOARD_SIZE
    ) -> list[WeeklyXP]:
        async with session_scope() as session:
            return await page_of_weekly_xp(
                session,
                guild_id=self.guild_id,
                week_start=week_start,
                limit=limit,
            )

    async def mark_posted(self, posted_on: date) -> None:
        """Remember this week's post; the XP rolls over the following Monday."""
        async with session_scope() as session:
            row = await self._find_setting(session)
            if row is None:
                row = WeeklySetting(guild_id=self.guild_id)
                session.add(row)
            row.last_posted_on = posted_on
            await session.commit()
        self._last_posted_on = posted_on

    async def _find_setting(self, session) -> WeeklySetting | None:
        return (
            await session.execute(
                select(WeeklySetting).where(
                    WeeklySetting.guild_id == self.guild_id
                )
            )
        ).scalar_one_or_none()
