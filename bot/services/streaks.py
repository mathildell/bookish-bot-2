"""Reading streaks: register a daily goal, log progress, keep the streak alive.

Port of the old Ruby "Streaks" mini-game:

- a member registers a daily goal (amount, unit, timezone) and a reminder
  schedule;
- they log their day once; missing a day closes the streak;
- a closed streak can be revived with a rescue coupon (normal within 24h, then
  golden for a few more days);
- every five logged days completes a level, which pays XP and grants rescues.

The XP/pure rules live as module-level functions so they can be tested without
a database. The :class:`StreakService` owns the guild's settings and the
read/write database work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, available_timezones

from sqlalchemy import delete, select

from ..db import session_scope
from ..models import Streak, StreakGoal, StreakLog, StreakSetting
from . import level as level_service
from .level import utcnow

LEVEL_THRESHOLD = 5
XP_CAP = 3000
XP_BASE = 250
XP_EXPONENT = 0.73

RESCUE_NORMAL = "normal"
RESCUE_GOLDEN = "golden"
NORMAL_WINDOW = timedelta(hours=24)
GOLDEN_WINDOW = timedelta(days=5)

CUSTOM_HOUR_MIN = 1
CUSTOM_HOUR_MAX = 22
LAST_CHANCE_HOUR = 23

LOG_RETENTION_DAYS = 90

OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "failed"
OUTCOME_ZERO = "zero"

REMINDERS = ("none", "custom", "last_chance", "both")

STATUS_ONGOING = "ongoing"
STATUS_CLOSED = "closed"

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


class StreakError(Exception):
    """Base class for user-facing streak problems (carries a text key)."""

    key = "streaks.errors.generic"

    def __init__(self, **variables) -> None:
        super().__init__(self.key)
        self.variables = variables


class NotRegistered(StreakError):
    key = "streaks.errors.not_registered"


class InvalidAmount(StreakError):
    key = "streaks.errors.invalid_amount"


class InvalidTimezone(StreakError):
    key = "streaks.errors.invalid_timezone"


# ----------------------------------------------------------------- pure rules


def level_xp(level: int) -> int:
    """XP paid for completing ``level`` (capped, decelerating growth)."""
    if level <= 0:
        return 0
    return min(XP_CAP, round(XP_BASE * level**XP_EXPONENT))


def display_level(days_logged: int) -> int:
    """The level shown to members: 1 as soon as a day is logged, else 0.

    A level spans five logged days; level 0 is only ever the empty streak.
    """
    if days_logged <= 0:
        return 0
    return (days_logged - 1) // LEVEL_THRESHOLD + 1


def format_date(day: date) -> str:
    """Render a date as the plain "October 13, 2026" the feature shows."""
    return day.strftime("%B %d, %Y")


def window_xp(level: int, successes: int) -> int:
    """XP for a level window with ``successes`` goal-reaching days.

    Every day is binary: it either reached the goal (worth its full share) or
    did not (worth nothing). Achieved percentages never change the payout.
    """
    successes = max(0, min(LEVEL_THRESHOLD, successes))
    return round(level_xp(level) * successes / LEVEL_THRESHOLD)


def on_track_xp(
    current_level: int, days_logged: int, successes_so_far: int
) -> int:
    """Projected XP for the in-progress window if the remaining days succeed."""
    remainder = days_logged % LEVEL_THRESHOLD
    assumed = successes_so_far + (LEVEL_THRESHOLD - remainder)
    return window_xp(current_level + 1, assumed)


def percent_for(amount: int, goal_amount: int) -> int:
    if goal_amount <= 0:
        return 0
    return round(amount / goal_amount * 100)


def outcome_for(amount: int, goal_amount: int) -> str:
    """Classify a day: success (>= goal), failed (0 < amount < goal) or zero."""
    if amount <= 0:
        return OUTCOME_ZERO
    if amount >= goal_amount:
        return OUTCOME_SUCCESS
    return OUTCOME_FAILED


def parse_amount(raw: str, current: int = 0) -> int:
    """Parse ``50``, ``+50`` or ``-50`` against the current logged amount."""
    value = (raw or "").strip()
    if not value:
        raise InvalidAmount(raw=raw)
    try:
        if value[0] in "+-":
            number = int(value[1:])
            return max(0, current + number if value[0] == "+" else current - number)
        return max(0, int(value))
    except ValueError as exc:
        raise InvalidAmount(raw=raw) from exc


def local_now(now_utc: datetime, tz_name: str) -> datetime:
    """Interpret a naive UTC timestamp in the given timezone."""
    return now_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))


def local_date(now_utc: datetime, tz_name: str) -> date:
    return local_now(now_utc, tz_name).date()


def local_hour(now_utc: datetime, tz_name: str) -> int:
    return local_now(now_utc, tz_name).hour


def streak_deadline(last_log_date: date, tz_name: str) -> datetime:
    """The moment a streak closes if the next day is not logged.

    A day is granted after the last log; the streak closes at local midnight
    at the start of the day after that.
    """
    close_day = last_log_date + timedelta(days=2)
    return datetime.combine(close_day, time(0, 0), ZoneInfo(tz_name))


def day_end(day: date, tz_name: str) -> datetime:
    """Local midnight at the end of ``day`` (the start of the next day)."""
    return datetime.combine(day + timedelta(days=1), time(0, 0), ZoneInfo(tz_name))


def rescue_kind_for_closed(
    end_date: date, tz_name: str, now_utc: datetime
) -> str | None:
    """Which rescue can revive a streak that broke on ``end_date``.

    Normal for 24h after it closes, then golden for the next four days, then
    the streak is beyond saving.
    """
    closed_on = datetime.combine(
        end_date + timedelta(days=1), time(0, 0), ZoneInfo(tz_name)
    )
    elapsed = local_now(now_utc, tz_name) - closed_on
    if elapsed < timedelta(0):
        return None
    if elapsed < NORMAL_WINDOW:
        return RESCUE_NORMAL
    if elapsed < GOLDEN_WINDOW:
        return RESCUE_GOLDEN
    return None


def is_rescuable(streak: Streak, tz_name: str, now_utc: datetime) -> bool:
    if streak.status != STATUS_CLOSED or streak.end_date is None:
        return False
    kind = rescue_kind_for_closed(streak.end_date, tz_name, now_utc)
    if kind == RESCUE_NORMAL:
        return streak.rescues_available > 0
    if kind == RESCUE_GOLDEN:
        return streak.golden_available > 0
    return False


def slot_emoji(
    log: StreakLog | None,
    *,
    highlight_today: bool,
    today: date,
    icons: dict[str, str],
) -> str:
    """One glyph of the five-slot streak visual."""
    if log is None:
        return icons["upcoming"]
    if log.repair:
        return icons["golden"] if log.rescue_type == RESCUE_GOLDEN else icons["rescue"]
    if log.outcome == OUTCOME_SUCCESS:
        if highlight_today and log.log_date == today:
            return icons["success_today"]
        return icons["success"]
    if log.outcome == OUTCOME_ZERO:
        return icons["zero"]
    return icons["partial"]


def window_slots(
    logs: list[StreakLog],
    *,
    days_logged: int,
    highlight_today: bool,
    today: date,
    icons: dict[str, str],
) -> list[str]:
    """Render the current level's five slots, oldest day first.

    ``logs`` must be ordered oldest-first. The window is the group of five days
    that contains the most recent logged day, so a completed level stays on
    screen until the first day of the next level is logged. That also keeps
    days patched by a rescue visible, even when the patch completes a level.
    Purged history does not interfere: only the trailing ``count`` logs are used.
    """
    if days_logged <= 0:
        count = 0
    else:
        count = ((days_logged - 1) % LEVEL_THRESHOLD) + 1
    window = logs[-count:] if count else []
    return [
        slot_emoji(
            window[index] if index < len(window) else None,
            highlight_today=highlight_today,
            today=today,
            icons=icons,
        )
        for index in range(LEVEL_THRESHOLD)
    ]


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


# --------------------------------------------------------------- result types


@dataclass
class LevelProgress:
    previous_level: int
    level: int
    leveled: bool
    xp_awarded: int
    rescues_granted: int
    golden_granted: int
    anniversary: bool
    dry_run: bool = False
    top_up: bool = False
    xp_old_level: int | None = None
    xp_new_level: int | None = None


@dataclass
class TodayStatus:
    goal: StreakGoal
    logged: bool
    ever_logged: bool
    amount: int | None
    last_date: date | None
    closes_at: datetime | None


@dataclass
class LogResult:
    goal: StreakGoal
    streak: Streak
    log: StreakLog
    logs: list[StreakLog]
    created_streak: bool
    reopened: bool
    progress: LevelProgress
    rescuable: Streak | None
    closed: ClosedStreak | None = None


@dataclass
class ReviveResult:
    goal: StreakGoal
    streak: Streak
    logs: list[StreakLog]
    rescue_type: str
    patched_days: int
    moved: bool
    progress: LevelProgress


@dataclass
class ClosedStreak:
    """A streak that just closed, kept for the admin bot-updates notice."""

    user_id: int
    start_date: date
    end_date: date


@dataclass
class Reminder:
    streak: Streak
    goal: StreakGoal
    kind: str  # "custom" or "last_chance"


# --------------------------------------------------------------- DB helpers


async def _get_goal(session, *, guild_id: int, user_id: int) -> StreakGoal | None:
    return (
        await session.execute(
            select(StreakGoal).where(
                StreakGoal.guild_id == guild_id, StreakGoal.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def _ongoing_streak(session, *, goal_id: int) -> Streak | None:
    return (
        await session.execute(
            select(Streak)
            .where(Streak.goal_id == goal_id, Streak.status == STATUS_ONGOING)
            .order_by(Streak.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_streak(session, *, goal_id: int) -> Streak | None:
    return (
        await session.execute(
            select(Streak)
            .where(Streak.goal_id == goal_id)
            .order_by(Streak.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _recent_logs(session, *, streak_id: int, limit: int = 5) -> list[StreakLog]:
    rows = (
        await session.execute(
            select(StreakLog)
            .where(StreakLog.streak_id == streak_id)
            .order_by(StreakLog.log_date.desc())
            .limit(limit)
        )
    ).scalars()
    return list(reversed(list(rows)))


async def _get_log(
    session, *, streak_id: int, log_date: date
) -> StreakLog | None:
    return (
        await session.execute(
            select(StreakLog).where(
                StreakLog.streak_id == streak_id, StreakLog.log_date == log_date
            )
        )
    ).scalar_one_or_none()


async def _apply_level_progress(
    session,
    *,
    streak: Streak,
    guild_id: int,
    user_id: int,
    now: datetime,
    dry_run: bool = False,
) -> LevelProgress:
    """Pay and reward any levels completed by the latest log.

    Also tops up a just-completed window when the member raises their final day
    to reach the goal: the window is repaid up to its full value, but never
    clawed back (so a later lower re-log keeps the XP already earned).
    """
    previous_level = streak.level
    progress = LevelProgress(
        previous_level=previous_level,
        level=previous_level,
        leveled=False,
        xp_awarded=0,
        rescues_granted=0,
        golden_granted=0,
        anniversary=False,
        dry_run=dry_run,
    )
    target_level = streak.days_logged // LEVEL_THRESHOLD

    if target_level > previous_level:
        new_levels = target_level - previous_level
        recent = await _recent_logs(
            session, streak_id=streak.id, limit=new_levels * LEVEL_THRESHOLD
        )
        total_award = 0
        last_award = 0
        for step in range(new_levels):
            level = previous_level + 1 + step
            window = recent[step * LEVEL_THRESHOLD : (step + 1) * LEVEL_THRESHOLD]
            successes = sum(1 for entry in window if entry.outcome == OUTCOME_SUCCESS)
            award = window_xp(level, successes)
            anniversary = (level * LEVEL_THRESHOLD) % 365 == 0
            if anniversary:
                award *= 2
            total_award += award
            last_award = award
            streak.rescues_available += 1
            progress.rescues_granted += 1
            if anniversary or level % 50 == 0:
                streak.golden_available += 1
                progress.golden_granted += 1
            progress.level = level
            progress.anniversary = progress.anniversary or anniversary

        streak.level = target_level
        streak.window_xp_paid = last_award
        progress.leveled = True
        progress.xp_awarded = total_award

        if total_award > 0:
            streak.last_xp_amount = last_award
            streak.total_xp_awarded += total_award
            streak.last_xp_at = now
            if not dry_run:
                change = await level_service.add_xp(
                    session, guild_id=guild_id, user_id=user_id, amount=total_award
                )
                progress.xp_old_level = change.old_level
                progress.xp_new_level = change.new_level
        return progress

    # No new level was crossed: the window may have just been completed earlier
    # today and is only now being topped up to reach the goal.
    if (
        streak.level > 0
        and streak.days_logged % LEVEL_THRESHOLD == 0
        and streak.level == streak.days_logged // LEVEL_THRESHOLD
    ):
        window = await _recent_logs(
            session, streak_id=streak.id, limit=LEVEL_THRESHOLD
        )
        successes = sum(1 for entry in window if entry.outcome == OUTCOME_SUCCESS)
        award = window_xp(streak.level, successes)
        if (streak.level * LEVEL_THRESHOLD) % 365 == 0:
            award *= 2
        difference = award - streak.window_xp_paid
        if difference > 0:
            streak.window_xp_paid = award
            streak.last_xp_amount = award
            streak.total_xp_awarded += difference
            streak.last_xp_at = now
            progress.top_up = True
            progress.xp_awarded = difference
            if not dry_run:
                change = await level_service.add_xp(
                    session, guild_id=guild_id, user_id=user_id, amount=difference
                )
                progress.xp_old_level = change.old_level
                progress.xp_new_level = change.new_level
    return progress


# ------------------------------------------------------------------ service


class StreakService:
    """Settings plus the read/write streak operations."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self._channel_id: int | None = None
        self._dry_run = False
        self._log_retention_days = LOG_RETENTION_DAYS

    @property
    def channel_id(self) -> int | None:
        return self._channel_id

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def log_retention_days(self) -> int:
        return self._log_retention_days

    # ----------------------------------------------------------- settings

    async def _settings(self, session, *, create: bool = False) -> StreakSetting | None:
        row = (
            await session.execute(
                select(StreakSetting).where(
                    StreakSetting.guild_id == self.guild_id
                )
            )
        ).scalar_one_or_none()
        if row is None and create:
            row = StreakSetting(guild_id=self.guild_id, channel_id=0)
            session.add(row)
            await session.flush()
        return row

    async def reload(self) -> None:
        async with session_scope() as session:
            row = await self._settings(session)
        self._channel_id = (row.channel_id or None) if row else None
        self._dry_run = bool(row.dry_run) if row else False
        self._log_retention_days = (
            row.log_retention_days
            if row and row.log_retention_days
            else LOG_RETENTION_DAYS
        )

    async def set_channel(self, channel_id: int) -> None:
        async with session_scope() as session:
            row = await self._settings(session, create=True)
            row.channel_id = channel_id
            await session.commit()
        self._channel_id = channel_id

    async def set_dry_run(self, enabled: bool) -> None:
        async with session_scope() as session:
            row = await self._settings(session, create=True)
            row.dry_run = enabled
            await session.commit()
        self._dry_run = enabled

    async def set_log_retention_days(self, days: int) -> None:
        async with session_scope() as session:
            row = await self._settings(session, create=True)
            row.log_retention_days = days
            await session.commit()
        self._log_retention_days = days

    # -------------------------------------------------------------- goals

    async def get_goal(self, user_id: int) -> StreakGoal | None:
        async with session_scope() as session:
            return await _get_goal(
                session, guild_id=self.guild_id, user_id=user_id
            )

    async def save_goal(
        self,
        *,
        user_id: int,
        amount: int,
        unit: str,
        timezone: str,
        reminder: str,
        custom_hour: int | None,
    ) -> StreakGoal:
        if reminder not in REMINDERS:
            raise InvalidAmount(reminder=reminder)
        zone = valid_timezone(timezone)
        if zone is None:
            raise InvalidTimezone(timezone=timezone)
        if reminder in ("custom", "both") and (
            custom_hour is None
            or not CUSTOM_HOUR_MIN <= custom_hour <= CUSTOM_HOUR_MAX
        ):
            raise InvalidAmount(reminder=reminder)
        unit = (unit or "").strip()
        if not unit:
            raise InvalidAmount(raw=unit)

        async with session_scope() as session:
            goal = await _get_goal(
                session, guild_id=self.guild_id, user_id=user_id
            )
            if goal is None:
                goal = StreakGoal(guild_id=self.guild_id, user_id=user_id)
                session.add(goal)
            goal.amount = amount
            goal.unit = unit
            goal.timezone = zone
            goal.reminder = reminder
            goal.custom_hour = (
                custom_hour if reminder in ("custom", "both") else None
            )
            await session.commit()
            await session.refresh(goal)
            return goal

    # ---------------------------------------------------------- snapshot

    async def view(self, user_id: int) -> tuple[StreakGoal, Streak | None, list[StreakLog]]:
        """Goal, the most relevant streak and its current-window logs."""
        async with session_scope() as session:
            goal = await _get_goal(
                session, guild_id=self.guild_id, user_id=user_id
            )
            if goal is None:
                raise NotRegistered()
            streak = await _ongoing_streak(session, goal_id=goal.id)
            if streak is None:
                streak = await _latest_streak(session, goal_id=goal.id)
            logs: list[StreakLog] = []
            if streak is not None:
                logs = await _recent_logs(session, streak_id=streak.id)
            return goal, streak, logs

    async def today_status(
        self, user_id: int, now: datetime | None = None
    ) -> TodayStatus:
        now = now or utcnow()
        goal, streak, logs = await self.view(user_id)
        today = local_date(now, goal.timezone)
        if streak is not None and streak.last_log_date == today:
            log = next(
                (entry for entry in logs if entry.log_date == today), None
            )
            return TodayStatus(
                goal=goal,
                logged=True,
                ever_logged=True,
                amount=log.amount if log is not None else 0,
                last_date=today,
                closes_at=None,
            )
        last_date = streak.last_log_date if streak is not None else None
        closed = streak is not None and streak.status == STATUS_CLOSED
        closes_at = None if closed else day_end(today, goal.timezone)
        return TodayStatus(
            goal=goal,
            logged=False,
            ever_logged=last_date is not None,
            amount=None,
            last_date=last_date,
            closes_at=closes_at,
        )

    # -------------------------------------------------------------- logging

    async def log_daily(
        self, *, user_id: int, amount_raw: str, now: datetime | None = None
    ) -> LogResult:
        now = now or utcnow()
        async with session_scope() as session:
            goal = await _get_goal(
                session, guild_id=self.guild_id, user_id=user_id
            )
            if goal is None:
                raise NotRegistered()

            today = local_date(now, goal.timezone)
            streak = await _ongoing_streak(session, goal_id=goal.id)
            closed_streak: ClosedStreak | None = None
            # The hourly job may not have run yet; close a streak that is
            # already past its deadline before logging, so a missed day is not
            # quietly papered over.
            if (
                streak is not None
                and streak.last_log_date is not None
                and local_now(now, goal.timezone)
                >= streak_deadline(streak.last_log_date, goal.timezone)
            ):
                streak.end_date = streak.last_log_date + timedelta(days=1)
                streak.status = STATUS_CLOSED
                await session.flush()
                closed_streak = ClosedStreak(
                    user_id=streak.user_id,
                    start_date=streak.start_date,
                    end_date=streak.end_date,
                )
                streak = None

            created = False
            rescuable: Streak | None = None
            if streak is None:
                rescuable = await self._rescuable_streak(
                    session, goal=goal, now=now
                )
                streak = Streak(
                    guild_id=self.guild_id,
                    user_id=user_id,
                    goal_id=goal.id,
                    start_date=today,
                    status=STATUS_ONGOING,
                    level=0,
                    days_logged=0,
                )
                session.add(streak)
                await session.flush()
                created = True

            log = await _get_log(session, streak_id=streak.id, log_date=today)
            new_day = log is None
            current = log.amount if log is not None else 0
            amount = parse_amount(amount_raw, current)
            if log is None:
                log = StreakLog(streak_id=streak.id, log_date=today)
                session.add(log)
            log.amount = amount
            log.percent = percent_for(amount, goal.amount)
            log.outcome = outcome_for(amount, goal.amount)
            log.repair = False
            log.rescue_type = None

            if new_day:
                streak.days_logged += 1
            streak.last_log_date = today

            progress = await _apply_level_progress(
                session,
                streak=streak,
                guild_id=self.guild_id,
                user_id=user_id,
                now=now,
                dry_run=self._dry_run,
            )
            await session.commit()
            recent = await _recent_logs(
                session, streak_id=streak.id, limit=LEVEL_THRESHOLD
            )
            return LogResult(
                goal=goal,
                streak=streak,
                log=log,
                logs=recent,
                created_streak=created,
                reopened=False,
                progress=progress,
                rescuable=rescuable,
                closed=closed_streak,
            )

    # -------------------------------------------------------------- rescue

    async def _rescuable_streak(
        self, session, *, goal: StreakGoal, now: datetime
    ) -> Streak | None:
        streak = await _latest_streak(session, goal_id=goal.id)
        if streak is None or streak.status != STATUS_CLOSED:
            return None
        return streak if is_rescuable(streak, goal.timezone, now) else None

    async def revive(
        self, *, streak_id: int, user_id: int, now: datetime | None = None
    ) -> ReviveResult:
        now = now or utcnow()
        async with session_scope() as session:
            streak = (
                await session.execute(select(Streak).where(Streak.id == streak_id))
            ).scalar_one_or_none()
            if streak is None or streak.user_id != user_id:
                raise NotRegistered()
            goal = (
                await session.execute(
                    select(StreakGoal).where(StreakGoal.id == streak.goal_id)
                )
            ).scalar_one()
            kind = rescue_kind_for_closed(streak.end_date, goal.timezone, now)
            if kind is None or not is_rescuable(streak, goal.timezone, now):
                raise NotRegistered()

            today = local_date(now, goal.timezone)
            ongoing = await _ongoing_streak(session, goal_id=goal.id)
            moved = False
            target = today
            if ongoing is not None and ongoing.id != streak.id:
                moved_log = await _get_log(
                    session, streak_id=ongoing.id, log_date=today
                )
                if moved_log is not None:
                    target = moved_log.log_date
                    moved_log.streak_id = streak.id
                    moved = True
                else:
                    await session.execute(
                        delete(StreakLog).where(StreakLog.streak_id == ongoing.id)
                    )
                await session.delete(ongoing)

            patched = 0
            cursor = streak.last_log_date + timedelta(days=1)
            while cursor < target:
                session.add(
                    StreakLog(
                        streak_id=streak.id,
                        log_date=cursor,
                        amount=0,
                        percent=0,
                        outcome=OUTCOME_ZERO,
                        repair=True,
                        rescue_type=kind,
                    )
                )
                patched += 1
                cursor += timedelta(days=1)

            added = patched + (1 if moved else 0)
            if added:
                streak.days_logged += added
                streak.last_log_date = (
                    target if moved else target - timedelta(days=1)
                )

            if kind == RESCUE_NORMAL:
                streak.rescues_available -= 1
                streak.rescues_used += 1
            else:
                streak.golden_available -= 1
                streak.golden_used += 1

            streak.status = STATUS_ONGOING
            streak.end_date = None

            progress = await _apply_level_progress(
                session,
                streak=streak,
                guild_id=self.guild_id,
                user_id=user_id,
                now=now,
                dry_run=self._dry_run,
            )
            await session.commit()
            return ReviveResult(
                goal=goal,
                streak=streak,
                logs=await _recent_logs(
                    session, streak_id=streak.id, limit=LEVEL_THRESHOLD
                ),
                rescue_type=kind,
                patched_days=patched,
                moved=moved,
                progress=progress,
            )

    # ------------------------------------------------------------ backfill

    async def admin_log(
        self,
        *,
        user_id: int,
        amount_raw: str,
        log_date: date | None = None,
        now: datetime | None = None,
    ) -> LogResult:
        now = now or utcnow()
        async with session_scope() as session:
            goal = await _get_goal(
                session, guild_id=self.guild_id, user_id=user_id
            )
            if goal is None:
                raise NotRegistered()

            when = log_date or local_date(now, goal.timezone)
            streak = await _streak_containing(session, goal=goal, when=when)
            created = False
            if streak is None:
                streak = Streak(
                    guild_id=self.guild_id,
                    user_id=user_id,
                    goal_id=goal.id,
                    start_date=when,
                    status=STATUS_ONGOING,
                    level=0,
                    days_logged=0,
                )
                session.add(streak)
                await session.flush()
                created = True

            log = await _get_log(session, streak_id=streak.id, log_date=when)
            new_day = log is None
            current = log.amount if log is not None else 0
            amount = parse_amount(amount_raw, current)
            if log is None:
                log = StreakLog(streak_id=streak.id, log_date=when)
                session.add(log)
            log.amount = amount
            log.percent = percent_for(amount, goal.amount)
            log.outcome = outcome_for(amount, goal.amount)
            log.repair = False
            log.rescue_type = None

            if new_day:
                streak.days_logged += 1
            if streak.last_log_date is None or when > streak.last_log_date:
                streak.last_log_date = when
            # Backfilling a day inside a closed streak reopens it for free: the
            # gap that broke it no longer exists.
            reopened = False
            if (
                streak.status == STATUS_CLOSED
                and streak.end_date is not None
                and when <= streak.end_date
            ):
                streak.status = STATUS_ONGOING
                streak.end_date = None
                reopened = True

            progress = await _apply_level_progress(
                session,
                streak=streak,
                guild_id=self.guild_id,
                user_id=user_id,
                now=now,
                dry_run=self._dry_run,
            )
            await session.commit()
            return LogResult(
                goal=goal,
                streak=streak,
                log=log,
                logs=await _recent_logs(
                    session, streak_id=streak.id, limit=LEVEL_THRESHOLD
                ),
                created_streak=created,
                reopened=reopened,
                progress=progress,
                rescuable=None,
            )

    # -------------------------------------------------------- upkeep/polls

    async def close_due(self, now: datetime | None = None) -> list[ClosedStreak]:
        """Close every ongoing streak whose deadline has passed."""
        now = now or utcnow()
        closed: list[ClosedStreak] = []
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Streak, StreakGoal)
                    .join(StreakGoal, StreakGoal.id == Streak.goal_id)
                    .where(
                        Streak.status == STATUS_ONGOING,
                        Streak.last_log_date.is_not(None),
                    )
                )
            ).all()
            for streak, goal in rows:
                deadline = streak_deadline(streak.last_log_date, goal.timezone)
                if local_now(now, goal.timezone) >= deadline:
                    streak.end_date = streak.last_log_date + timedelta(days=1)
                    streak.status = STATUS_CLOSED
                    closed.append(
                        ClosedStreak(
                            user_id=streak.user_id,
                            start_date=streak.start_date,
                            end_date=streak.end_date,
                        )
                    )
            await session.commit()
        return closed

    async def due_reminders(self, now: datetime | None = None) -> list[Reminder]:
        """Ongoing streaks that are owed a ping at this exact local hour."""
        now = now or utcnow()
        due: list[Reminder] = []
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Streak, StreakGoal)
                    .join(StreakGoal, StreakGoal.id == Streak.goal_id)
                    .where(
                        Streak.status == STATUS_ONGOING,
                        StreakGoal.reminder != "none",
                    )
                )
            ).all()
            for streak, goal in rows:
                today = local_date(now, goal.timezone)
                if streak.last_log_date == today:
                    continue
                hour = local_hour(now, goal.timezone)
                if (
                    goal.reminder in ("custom", "both")
                    and goal.custom_hour is not None
                    and hour == goal.custom_hour
                    and streak.last_custom_reminder_on != today
                ):
                    due.append(Reminder(streak=streak, goal=goal, kind="custom"))
                elif (
                    goal.reminder in ("last_chance", "both")
                    and hour == LAST_CHANCE_HOUR
                    and streak.last_lastchance_reminder_on != today
                ):
                    due.append(
                        Reminder(streak=streak, goal=goal, kind="last_chance")
                    )
        return due

    async def mark_reminder(self, reminders: list[Reminder]) -> None:
        if not reminders:
            return
        async with session_scope() as session:
            for reminder in reminders:
                streak = (
                    await session.execute(
                        select(Streak).where(Streak.id == reminder.streak.id)
                    )
                ).scalar_one()
                today = local_date(utcnow(), reminder.goal.timezone)
                if reminder.kind == "custom":
                    streak.last_custom_reminder_on = today
                else:
                    streak.last_lastchance_reminder_on = today
            await session.commit()

    async def purge_old_logs(self, now: datetime | None = None) -> int:
        """Delete daily logs older than the retention window."""
        cutoff = local_date(now or utcnow(), "UTC") - timedelta(
            days=self._log_retention_days
        )
        async with session_scope() as session:
            result = await session.execute(
                delete(StreakLog).where(StreakLog.log_date < cutoff)
            )
            await session.commit()
            return int(result.rowcount or 0)

    # --------------------------------------------------------- leaderboards

    async def leaderboard(
        self, *, ongoing: bool, limit: int = 10
    ) -> list[tuple[Streak, StreakGoal]]:
        async with session_scope() as session:
            stmt = (
                select(Streak, StreakGoal)
                .join(StreakGoal, StreakGoal.id == Streak.goal_id)
                .where(Streak.guild_id == self.guild_id)
                .where(Streak.days_logged > 0)
                .order_by(Streak.days_logged.desc(), Streak.id.asc())
                .limit(limit)
            )
            if ongoing:
                stmt = stmt.where(Streak.status == STATUS_ONGOING)
            rows = (await session.execute(stmt)).all()
            return [(streak, goal) for streak, goal in rows]

    async def rescuable_for_user(
        self, user_id: int, now: datetime | None = None
    ) -> tuple[Streak, StreakGoal] | None:
        """The member's latest broken streak that can still be rescued.

        A fresh streak may already have been opened for today, so this scans
        closed streaks rather than simply taking the newest one.
        """
        now = now or utcnow()
        async with session_scope() as session:
            goal = await _get_goal(
                session, guild_id=self.guild_id, user_id=user_id
            )
            if goal is None:
                return None
            streaks = (
                await session.execute(
                    select(Streak)
                    .where(Streak.goal_id == goal.id)
                    .order_by(Streak.id.desc())
                )
            ).scalars()
            for streak in streaks:
                if is_rescuable(streak, goal.timezone, now):
                    return streak, goal
            return None


async def _streak_containing(session, *, goal: StreakGoal, when: date) -> Streak | None:
    """The latest streak whose range covers ``when``, else the ongoing one."""
    rows = (
        await session.execute(
            select(Streak)
            .where(Streak.goal_id == goal.id)
            .order_by(Streak.id.desc())
        )
    ).scalars()
    for streak in rows:
        if streak.start_date <= when and (
            streak.end_date is None or when <= streak.end_date
        ):
            return streak
    return await _ongoing_streak(session, goal_id=goal.id)
