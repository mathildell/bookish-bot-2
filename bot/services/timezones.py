"""IANA timezone helpers shared by the streak, reminder and birthday features.

Naive UTC timestamps (the shape every DATETIME column uses) are converted to
and from a local wall clock through :class:`zoneinfo`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, available_timezones

from sqlalchemy import select

from ..db import session_scope
from ..models import StreakGoal
from .level import utcnow

POPULAR_TIMEZONES = (
    "America/Toronto",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Vancouver",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Amsterdam",
    "Europe/Stockholm",
    "Africa/Cairo",
    "Africa/Johannesburg",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Australia/Sydney",
    "Australia/Perth",
    "Pacific/Auckland",
    "UTC",
)


def local_now(now_utc: datetime, tz_name: str) -> datetime:
    """Interpret a naive UTC timestamp in the given timezone."""
    return now_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))


def to_utc(local: datetime) -> datetime:
    """Convert a local, timezone-aware timestamp back to naive UTC."""
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def search_timezones(
    query: str, *, now_utc: datetime | None = None, limit: int = 25
) -> list[tuple[str, str]]:
    """Searchable timezone choices as ``(value, label)`` pairs."""
    now = now_utc or utcnow()
    needle = (query or "").strip().lower().replace(" ", "").replace("_", "")
    zones = available_timezones()

    if not needle:
        candidates = [zone for zone in POPULAR_TIMEZONES if zone in zones]
    else:
        scored: list[tuple[int, int, str]] = []
        for zone in zones:
            normal = zone.lower().replace(" ", "").replace("_", "")
            city = normal.rsplit("/", 1)[-1]
            if city == needle:
                score = 0
            elif city.startswith(needle):
                score = 1
            elif normal.startswith(needle):
                score = 2
            elif needle in normal:
                score = 3
            else:
                continue
            scored.append((score, len(zone), zone))
        scored.sort()
        candidates = [zone for _, _, zone in scored]
        candidates = candidates[:limit]

    return [(zone, _timezone_label(zone, now)) for zone in candidates[:limit]]


def _timezone_label(zone: str, now_utc: datetime) -> str:
    try:
        offset = local_now(now_utc, zone).utcoffset() or timedelta(0)
    except Exception:
        return zone.replace("_", " ")
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{zone.replace('_', ' ')} (UTC{sign}{hours:02d}:{minutes:02d})"


def valid_timezone(name: str) -> str | None:
    """Return the canonical zone name for ``name``, or ``None`` if unknown."""
    candidate = (name or "").strip()
    if not candidate:
        return None
    try:
        ZoneInfo(candidate)
    except Exception:
        return None
    return candidate


async def default_member_timezone(guild_id: int, user_id: int) -> str:
    """The member's streak timezone, falling back to UTC."""
    async with session_scope() as session:
        goal = (
            await session.execute(
                select(StreakGoal).where(
                    StreakGoal.guild_id == guild_id,
                    StreakGoal.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if goal is not None and valid_timezone(goal.timezone):
            return goal.timezone
    return "UTC"
