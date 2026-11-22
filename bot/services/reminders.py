"""Member reminders: one-off and recurring schedules posted into a channel.

Schedule rules live as module-level functions so they can be tested without a
database. The :class:`ReminderService` owns the read/write work and snoozes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..db import session_scope
from ..models import Reminder, ReminderSnooze
from .hours import check_hour
from .level import utcnow
from .timezones import default_member_timezone, local_now, to_utc, valid_timezone
from .weekdays import WEEKDAY_NAMES

KIND_ONCE = "once"
KIND_DAILY = "daily"
KIND_WEEKLY = "weekly"
KIND_MONTHLY = "monthly"
KIND_INTERVAL = "interval"
KIND_YEARLY = "yearly"
KIND_ROLLING = "rolling"

KINDS = (
    KIND_ONCE,
    KIND_DAILY,
    KIND_WEEKLY,
    KIND_MONTHLY,
    KIND_YEARLY,
    KIND_INTERVAL,
    KIND_ROLLING,
)

MESSAGE_MAX_LENGTH = 240
INTERVAL_MAX_DAYS = 365
# "in x minutes" one-offs may be scheduled up to a year ahead.
MINUTES_MAX = 365 * 24 * 60
MONTH_INTERVAL_MAX = 24
# Rolling reminders ("every 2 hours") stay clear of spam and of the poll rate.
ROLLING_MIN_MINUTES = 30
ROLLING_MAX_MINUTES = 7 * 24 * 60
# Which weekday of the month; -1 is the last one.
MONTH_WEEKS = (1, 2, 3, 4, -1)
# Keeps the target day at 1 or later even in the shortest February.
MONTH_END_MAX_OFFSET = 27

# Snoozed/missed reminders older than this are dropped instead of posted.
STALE_AFTER = timedelta(hours=24)


class ReminderError(Exception):
    """Base class for user-facing reminder problems (carries a text key)."""

    key = "reminders.errors.generic"

    def __init__(self, **variables) -> None:
        super().__init__(self.key)
        self.variables = variables


class MissingMessage(ReminderError):
    key = "reminders.errors.missing_message"


class MessageTooLong(ReminderError):
    key = "reminders.errors.message_too_long"


class InvalidHour(ReminderError):
    key = "reminders.errors.invalid_hour"


class InvalidTimezone(ReminderError):
    key = "reminders.errors.invalid_timezone"


class InvalidKind(ReminderError):
    key = "reminders.errors.invalid_kind"


class MissingDate(ReminderError):
    key = "reminders.errors.missing_date"


class InvalidDate(ReminderError):
    key = "reminders.errors.invalid_date"


class PastDate(ReminderError):
    key = "reminders.errors.past_date"


class InvalidWeekday(ReminderError):
    key = "reminders.errors.invalid_weekday"


class MissingWeekday(ReminderError):
    key = "reminders.errors.missing_weekday"


class MissingDay(ReminderError):
    key = "reminders.errors.missing_day"


class InvalidDay(ReminderError):
    key = "reminders.errors.invalid_day"


class BothDayAndMonthEnd(ReminderError):
    key = "reminders.errors.both_day_and_month_end"


class InvalidMonthEnd(ReminderError):
    key = "reminders.errors.invalid_month_end"


class InvalidTime(ReminderError):
    key = "reminders.errors.invalid_time"


class MissingTime(ReminderError):
    key = "reminders.errors.missing_time"


class InvalidMinutes(ReminderError):
    key = "reminders.errors.invalid_minutes"


class InvalidMonthInterval(ReminderError):
    key = "reminders.errors.invalid_month_interval"


class InvalidMonthWeek(ReminderError):
    key = "reminders.errors.invalid_month_week"


class InvalidMonth(ReminderError):
    key = "reminders.errors.invalid_month"


class InvalidRolling(ReminderError):
    key = "reminders.errors.invalid_rolling"


class MissingInterval(ReminderError):
    key = "reminders.errors.missing_interval"


class InvalidInterval(ReminderError):
    key = "reminders.errors.invalid_interval"


class ReminderNotFound(ReminderError):
    key = "reminders.errors.not_found"


def clean_message(raw: str) -> str:
    return (raw or "").strip()


def validate_message(raw: str) -> str:
    message = clean_message(raw)
    if not message:
        raise MissingMessage()
    if len(message) > MESSAGE_MAX_LENGTH:
        raise MessageTooLong(length=len(message), maximum=MESSAGE_MAX_LENGTH)
    return message


def validate_hour(hour: int) -> int:
    return check_hour(hour, InvalidHour)


def validate_minute(minute: int) -> int:
    if not isinstance(minute, int) or not 0 <= minute <= 59:
        raise InvalidTime()
    return minute


def parse_weekday(value: str | None) -> int | None:
    """Parse a weekday name or abbreviation into a Python weekday."""
    if value is None:
        return None
    needle = (value or "").strip().lower()
    for index, name in enumerate(WEEKDAY_NAMES):
        if needle == name or needle == name[:3]:
            return index
    raise InvalidWeekday(weekday=value)


@dataclass(frozen=True)
class Schedule:
    """A normalized schedule: everything ``Reminder`` stores about its timing.

    Which fields apply depends on ``kind``; :func:`validate_schedule` rejects
    incomplete ones and clears the fields a kind does not use.
    """

    kind: str
    hour: int
    minute: int = 0
    weekdays: tuple[int, ...] = ()
    day_of_month: int | None = None
    month_end_offset: int | None = None
    month_week: int | None = None
    month_interval: int = 1
    month: int | None = None
    interval_days: int | None = None
    interval_minutes: int | None = None
    run_date: date | None = None
    run_at: datetime | None = None


def validate_schedule(schedule: Schedule) -> Schedule:
    """Check a schedule is complete for its kind, dropping unrelated fields."""
    kind = schedule.kind
    if kind not in KINDS:
        raise InvalidKind(kind=kind)
    validate_hour(schedule.hour)
    validate_minute(schedule.minute)

    weekdays: tuple[int, ...] = ()
    day_of_month = month_end_offset = month_week = month = None
    month_interval = 1
    interval_days = interval_minutes = run_date = run_at = None
    if schedule.weekdays:
        weekdays = tuple(sorted(set(schedule.weekdays)))
        for weekday in weekdays:
            if not 0 <= weekday <= 6:
                raise InvalidWeekday(weekday=weekday)

    if kind == KIND_ONCE:
        weekdays = ()
        run_at = schedule.run_at
        run_date = schedule.run_date
        if run_at is None and run_date is None:
            raise MissingDate()
    elif kind == KIND_WEEKLY:
        if not weekdays:
            raise MissingWeekday()
    elif kind == KIND_MONTHLY:
        month_interval = schedule.month_interval
        if not 1 <= month_interval <= MONTH_INTERVAL_MAX:
            raise InvalidMonthInterval(
                months=month_interval, maximum=MONTH_INTERVAL_MAX
            )
        if schedule.month_week is not None:
            if schedule.month_week not in MONTH_WEEKS:
                raise InvalidMonthWeek(nth=schedule.month_week)
            if len(weekdays) != 1:
                raise MissingWeekday()
            month_week = schedule.month_week
        else:
            weekdays = ()
            day_of_month, month_end_offset = validate_monthly_target(
                schedule.day_of_month, schedule.month_end_offset
            )
    elif kind == KIND_YEARLY:
        weekdays = ()
        month = schedule.month
        if month is None or not 1 <= month <= 12:
            raise InvalidMonth(month=month)
        day_of_month = schedule.day_of_month
        if day_of_month is None:
            raise MissingDay()
        if not 1 <= day_of_month <= _last_day_of_month(2024, month):
            raise InvalidDay(day=day_of_month)
    elif kind == KIND_INTERVAL:
        interval_days = schedule.interval_days
        if interval_days is None:
            raise MissingInterval()
        if not 1 <= interval_days <= INTERVAL_MAX_DAYS:
            raise InvalidInterval(days=interval_days)
        weekdays = weekdays[:1]
    elif kind == KIND_ROLLING:
        weekdays = ()
        interval_minutes = schedule.interval_minutes
        if interval_minutes is None or not (
            ROLLING_MIN_MINUTES <= interval_minutes <= ROLLING_MAX_MINUTES
        ):
            raise InvalidRolling(
                minimum=ROLLING_MIN_MINUTES, maximum=ROLLING_MAX_MINUTES
            )
    else:
        weekdays = ()
    return Schedule(
        kind=kind,
        hour=schedule.hour,
        minute=schedule.minute,
        weekdays=weekdays,
        day_of_month=day_of_month,
        month_end_offset=month_end_offset,
        month_week=month_week,
        month_interval=month_interval,
        month=month,
        interval_days=interval_days,
        interval_minutes=interval_minutes,
        run_date=run_date,
        run_at=run_at,
    )


def validate_monthly_target(
    day_of_month: int | None, month_end_offset: int | None
) -> tuple[int | None, int | None]:
    """Resolve a monthly target: a fixed day or days before the month's end.

    Exactly one of the two must be set; the returned pair keeps whichever
    applies and clears the other.
    """
    if day_of_month is not None and month_end_offset is not None:
        raise BothDayAndMonthEnd()
    if day_of_month is not None:
        if not 1 <= day_of_month <= 31:
            raise InvalidDay(day=day_of_month)
        return day_of_month, None
    if month_end_offset is not None:
        if not 0 <= month_end_offset <= MONTH_END_MAX_OFFSET:
            raise InvalidMonthEnd(offset=month_end_offset)
        return None, month_end_offset
    raise MissingDay()


def weekdays_to_mask(weekdays: tuple[int, ...]) -> int | None:
    """The stored mask for several weekdays; None when one or none."""
    if len(weekdays) < 2:
        return None
    return sum(1 << weekday for weekday in weekdays)


def weekdays_of(reminder: Reminder) -> tuple[int, ...]:
    """The weekdays a stored reminder uses, from the mask or the single day."""
    mask = getattr(reminder, "weekdays_mask", None)
    if mask:
        return tuple(day for day in range(7) if mask & (1 << day))
    weekday = getattr(reminder, "weekday", None)
    return (weekday,) if weekday is not None else ()


def schedule_of(reminder: Reminder) -> Schedule:
    """The stored timing of ``reminder`` as a :class:`Schedule`."""
    return Schedule(
        kind=reminder.kind,
        hour=reminder.hour,
        minute=getattr(reminder, "minute", 0) or 0,
        weekdays=weekdays_of(reminder),
        day_of_month=reminder.day_of_month,
        month_end_offset=reminder.month_end_offset,
        month_week=getattr(reminder, "month_week", None),
        month_interval=getattr(reminder, "month_interval", None) or 1,
        month=getattr(reminder, "month", None),
        interval_days=reminder.interval_days,
        interval_minutes=getattr(reminder, "interval_minutes", None),
        run_date=reminder.run_date,
        run_at=reminder.run_at,
    )


def _occurrence_utc(
    day: date, hour: int, timezone: str, minute: int = 0
) -> datetime:
    """Naive UTC timestamp of ``day`` at ``hour:minute`` local wall-clock time."""
    return to_utc(
        datetime.combine(day, time(hour, minute), ZoneInfo(timezone))
    )


def next_daily(
    after: datetime, hour: int, timezone: str, minute: int = 0
) -> datetime:
    local = local_now(after, timezone)
    day = local.date()
    if _occurrence_utc(day, hour, timezone, minute) <= after:
        day += timedelta(days=1)
    return _occurrence_utc(day, hour, timezone, minute)


def next_weekday(
    weekday: int, after: datetime, hour: int, timezone: str, minute: int = 0
) -> datetime:
    local = local_now(after, timezone)
    day = local.date() + timedelta(days=(weekday - local.weekday()) % 7)
    if _occurrence_utc(day, hour, timezone, minute) <= after:
        day += timedelta(days=7)
    return _occurrence_utc(day, hour, timezone, minute)


def next_weekdays(
    weekdays: tuple[int, ...],
    after: datetime,
    hour: int,
    timezone: str,
    minute: int = 0,
) -> datetime:
    """The soonest next fire across several weekdays."""
    return min(
        next_weekday(weekday, after, hour, timezone, minute)
        for weekday in weekdays
    )


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        following = date(year + 1, 1, 1)
    else:
        following = date(year, month + 1, 1)
    return (following - timedelta(days=1)).day


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """The ``nth`` (1-4, or -1 for the last) ``weekday`` of a month."""
    if nth == -1:
        last = date(year, month, _last_day_of_month(year, month))
        return last - timedelta(days=(last.weekday() - weekday) % 7)
    first = date(year, month, 1)
    first += timedelta(days=(weekday - first.weekday()) % 7)
    return first + timedelta(weeks=nth - 1)


def next_monthly(
    day_of_month: int | None,
    after: datetime,
    hour: int,
    timezone: str,
    *,
    month_end_offset: int | None = None,
    minute: int = 0,
    month_week: int | None = None,
    weekday: int | None = None,
    every: int = 1,
    anchor: tuple[int, int] | None = None,
) -> datetime:
    """The next monthly fire after ``after``.

    The target is a fixed ``day_of_month`` (clamped in short months), days
    before the month's end (``month_end_offset``, 0 = the last day), or the
    ``month_week``-th ``weekday``. Only every ``every``-th month counts,
    starting from ``anchor`` (a ``(year, month)``; default: the current month).
    """
    if month_week is None and day_of_month is None and month_end_offset is None:
        raise MissingDay()
    local = local_now(after, timezone)
    year, month = anchor or (local.year, local.month)
    for _ in range(12 * every + 24):
        last = _last_day_of_month(year, month)
        if month_week is not None:
            day = _nth_weekday(year, month, weekday, month_week)
        elif month_end_offset is not None:
            day = date(year, month, last - month_end_offset)
        else:
            day = date(year, month, min(day_of_month, last))
        candidate = _occurrence_utc(day, hour, timezone, minute)
        if candidate > after:
            return candidate
        month += every
        while month > 12:
            month, year = month - 12, year + 1
    raise InvalidDay(day=day_of_month)


def next_yearly(
    month: int,
    day_of_month: int,
    after: datetime,
    hour: int,
    timezone: str,
    minute: int = 0,
) -> datetime:
    """The next yearly fire; the 29th of February falls on the 28th otherwise."""
    year = local_now(after, timezone).year
    for _ in range(9):
        day = date(
            year, month, min(day_of_month, _last_day_of_month(year, month))
        )
        candidate = _occurrence_utc(day, hour, timezone, minute)
        if candidate > after:
            return candidate
        year += 1
    raise InvalidDay(day=day_of_month)


def next_interval(
    day: date,
    after: datetime,
    hour: int,
    timezone: str,
    interval_days: int,
    minute: int = 0,
) -> datetime:
    """First occurrence at or after ``day`` that is strictly after ``after``."""
    while True:
        candidate = _occurrence_utc(day, hour, timezone, minute)
        if candidate > after:
            return candidate
        day += timedelta(days=interval_days)


def next_rolling(
    anchor: datetime, after: datetime, interval_minutes: int
) -> datetime:
    """First ``anchor`` + k intervals strictly after ``after``."""
    step = timedelta(minutes=interval_minutes)
    if anchor > after:
        return anchor
    skipped = (after - anchor) // step + 1
    return anchor + skipped * step


def first_run(
    schedule: Schedule, timezone: str, now: datetime
) -> datetime:
    """When a freshly created (or rescheduled) reminder fires first.

    One-offs must lie in the future; an interval starts on its next matching
    weekday when anchored to one, otherwise at the next daily occurrence;
    cadences such as "every 3 months" begin at the first matching occurrence.
    """
    hour, minute = schedule.hour, schedule.minute
    kind = schedule.kind
    if kind == KIND_ONCE:
        if schedule.run_at is not None:
            moment = schedule.run_at
        else:
            moment = _occurrence_utc(schedule.run_date, hour, timezone, minute)
        if moment <= now:
            raise PastDate(date=moment.isoformat())
        return moment
    if kind == KIND_WEEKLY or (kind == KIND_INTERVAL and schedule.weekdays):
        return next_weekdays(schedule.weekdays, now, hour, timezone, minute)
    if kind == KIND_MONTHLY:
        return next_monthly(
            schedule.day_of_month,
            now,
            hour,
            timezone,
            month_end_offset=schedule.month_end_offset,
            minute=minute,
            month_week=schedule.month_week,
            weekday=schedule.weekdays[0] if schedule.weekdays else None,
        )
    if kind == KIND_YEARLY:
        return next_yearly(
            schedule.month, schedule.day_of_month, now, hour, timezone, minute
        )
    if kind == KIND_ROLLING:
        return now.replace(second=0, microsecond=0) + timedelta(
            minutes=schedule.interval_minutes
        )
    return next_daily(now, hour, timezone, minute)


def compute_next_run(
    reminder: Reminder, after: datetime
) -> datetime | None:
    """The next fire time strictly after ``after``, or None once expired.

    Recurring kinds advance from ``after`` so a late catch-up never triggers a
    second immediate post; cadences (intervals, every N months, rolling) keep
    their original rhythm from the run that just fired.
    """
    hour = reminder.hour
    minute = getattr(reminder, "minute", 0) or 0
    timezone = reminder.timezone
    kind = reminder.kind
    if kind == KIND_ONCE:
        if reminder.run_at is not None:
            return reminder.run_at if reminder.run_at > after else None
        if reminder.run_date is None:
            return None
        candidate = _occurrence_utc(reminder.run_date, hour, timezone, minute)
        return candidate if candidate > after else None
    if kind == KIND_DAILY:
        return next_daily(after, hour, timezone, minute)
    if kind == KIND_WEEKLY:
        weekdays = weekdays_of(reminder)
        if not weekdays:
            return None
        return next_weekdays(weekdays, after, hour, timezone, minute)
    if kind == KIND_MONTHLY:
        month_week = getattr(reminder, "month_week", None)
        weekdays = weekdays_of(reminder)
        if month_week is not None:
            if not weekdays:
                return None
        elif (
            reminder.day_of_month is None
            and reminder.month_end_offset is None
        ):
            return None
        every = getattr(reminder, "month_interval", None) or 1
        anchor = None
        if every > 1:
            anchor_local = local_now(reminder.next_run_at, timezone)
            anchor = (anchor_local.year, anchor_local.month)
        return next_monthly(
            reminder.day_of_month,
            after,
            hour,
            timezone,
            month_end_offset=reminder.month_end_offset,
            minute=minute,
            month_week=month_week,
            weekday=weekdays[0] if weekdays else None,
            every=every,
            anchor=anchor,
        )
    if kind == KIND_YEARLY:
        month = getattr(reminder, "month", None)
        if month is None or reminder.day_of_month is None:
            return None
        return next_yearly(
            month, reminder.day_of_month, after, hour, timezone, minute
        )
    if kind == KIND_INTERVAL:
        if not reminder.interval_days or reminder.interval_days <= 0:
            return None
        anchor = local_now(reminder.next_run_at, timezone).date()
        return next_interval(
            anchor, after, hour, timezone, reminder.interval_days, minute
        )
    if kind == KIND_ROLLING:
        minutes = getattr(reminder, "interval_minutes", None)
        if not minutes or minutes <= 0:
            return None
        return next_rolling(reminder.next_run_at, after, minutes)
    return None


def is_stale(moment: datetime, now: datetime) -> bool:
    """Whether a due moment is too old to post (missed while offline)."""
    return now - moment > STALE_AFTER


def snooze_due_at(now: datetime, delay: timedelta) -> datetime:
    return now + delay


def _apply_schedule(
    reminder: Reminder, schedule: Schedule, timezone: str, next_run_at: datetime
) -> None:
    reminder.timezone = timezone
    reminder.kind = schedule.kind
    reminder.hour = schedule.hour
    reminder.minute = schedule.minute
    reminder.weekday = schedule.weekdays[0] if schedule.weekdays else None
    reminder.weekdays_mask = weekdays_to_mask(schedule.weekdays)
    reminder.day_of_month = schedule.day_of_month
    reminder.month_end_offset = schedule.month_end_offset
    reminder.month_week = schedule.month_week
    reminder.month_interval = (
        schedule.month_interval if schedule.month_interval > 1 else None
    )
    reminder.month = schedule.month
    reminder.interval_days = schedule.interval_days
    reminder.interval_minutes = schedule.interval_minutes
    reminder.run_date = schedule.run_date
    reminder.run_at = schedule.run_at
    reminder.next_run_at = next_run_at


def merge_edit(
    existing: Reminder,
    *,
    message: str | None = None,
    timezone: str | None = None,
    schedule: Schedule | None = None,
    role_id: int | None = None,
    channel_id: int | None = None,
    now: datetime | None = None,
) -> Reminder:
    """An unsaved copy of ``existing`` with the given changes applied.

    A new schedule or timezone reschedules it from ``now``; a timezone on its
    own keeps the stored schedule and re-reads it in the new zone.
    """
    now = now or utcnow()
    zone = existing.timezone
    if timezone is not None:
        checked = valid_timezone(timezone)
        if checked is None:
            raise InvalidTimezone(timezone=timezone)
        zone = checked
    merged = Reminder(
        id=existing.id,
        guild_id=existing.guild_id,
        user_id=existing.user_id,
        channel_id=channel_id if channel_id is not None else existing.channel_id,
        message=(
            validate_message(message) if message is not None
            else existing.message
        ),
        role_id=role_id if role_id is not None else existing.role_id,
        last_run_at=existing.last_run_at,
    )
    if schedule is not None or timezone is not None:
        timing = validate_schedule(
            schedule if schedule is not None else schedule_of(existing)
        )
        next_run_at = first_run(timing, zone, now)
    else:
        timing = validate_schedule(schedule_of(existing))
        next_run_at = existing.next_run_at
    _apply_schedule(merged, timing, zone, next_run_at)
    return merged


class ReminderService:
    """CRUD and the due-query behind the reminder poll."""

    def __init__(self, guild_id: int) -> None:
        self._guild_id = guild_id

    async def default_timezone(self, user_id: int) -> str:
        """The member's streak timezone, falling back to UTC."""
        return await default_member_timezone(self._guild_id, user_id)

    def build(
        self,
        *,
        user_id: int,
        channel_id: int,
        message: str,
        timezone: str | None,
        schedule: Schedule,
        role_id: int | None = None,
        now: datetime | None = None,
    ) -> Reminder:
        """A validated, unsaved reminder, as :meth:`add` would store it."""
        now = now or utcnow()
        cleaned = validate_message(message)
        zone = valid_timezone(timezone) if timezone else None
        if zone is None:
            raise InvalidTimezone(timezone=timezone)
        schedule = validate_schedule(schedule)
        reminder = Reminder(
            guild_id=self._guild_id,
            user_id=user_id,
            channel_id=channel_id,
            message=cleaned,
            role_id=role_id,
        )
        _apply_schedule(
            reminder, schedule, zone, first_run(schedule, zone, now)
        )
        return reminder

    async def add(self, **fields) -> Reminder:
        """Validate and store a new reminder (see :meth:`build`)."""
        reminder = self.build(**fields)
        async with session_scope() as session:
            session.add(reminder)
            await session.commit()
            await session.refresh(reminder)
        return reminder

    async def get(self, reminder_id: int) -> Reminder | None:
        async with session_scope() as session:
            return await self._get(session, reminder_id)

    async def edit(self, reminder_id: int, **changes) -> Reminder:
        """Apply the given fields on top of an existing reminder.

        See :func:`merge_edit` for what the fields mean.
        """
        async with session_scope() as session:
            reminder = await self._get(session, reminder_id)
            if reminder is None:
                raise ReminderNotFound(reminder_id=reminder_id)
            merged = merge_edit(reminder, **changes)
            reminder.message = merged.message
            reminder.channel_id = merged.channel_id
            reminder.role_id = merged.role_id
            _apply_schedule(
                reminder,
                schedule_of(merged),
                merged.timezone,
                merged.next_run_at,
            )
            await session.commit()
            await session.refresh(reminder)
            return reminder

    async def remove(self, reminder_id: int) -> bool:
        async with session_scope() as session:
            reminder = await self._get(session, reminder_id)
            if reminder is None:
                return False
            await session.delete(reminder)
            await session.commit()
            return True

    async def list_for(
        self, *, user_id: int | None = None
    ) -> list[Reminder]:
        async with session_scope() as session:
            statement = select(Reminder).where(
                Reminder.guild_id == self._guild_id
            )
            if user_id is not None:
                statement = statement.where(Reminder.user_id == user_id)
            statement = statement.order_by(Reminder.next_run_at)
            return list((await session.execute(statement)).scalars())

    async def due(
        self, now: datetime | None = None
    ) -> list[Reminder]:
        now = now or utcnow()
        async with session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(Reminder)
                        .where(
                            Reminder.guild_id == self._guild_id,
                            Reminder.next_run_at <= now,
                        )
                        .order_by(Reminder.next_run_at)
                    )
                ).scalars()
            )

    async def mark_done(
        self, reminder: Reminder, now: datetime | None = None
    ) -> None:
        """Advance or delete a reminder after its run was handled."""
        now = now or utcnow()
        async with session_scope() as session:
            row = await self._get(session, reminder.id)
            if row is None:
                return
            next_run = compute_next_run(row, now)
            if next_run is None:
                await session.delete(row)
            else:
                row.last_run_at = now
                row.next_run_at = next_run
            await session.commit()

    async def snooze(
        self,
        *,
        reminder_id: int | None,
        user_id: int,
        channel_id: int,
        message: str,
        delay: timedelta,
        role_id: int | None = None,
        now: datetime | None = None,
    ) -> ReminderSnooze:
        now = now or utcnow()
        snooze = ReminderSnooze(
            guild_id=self._guild_id,
            source_reminder_id=reminder_id,
            user_id=user_id,
            channel_id=channel_id,
            message=message,
            role_id=role_id,
            due_at=snooze_due_at(now, delay),
        )
        async with session_scope() as session:
            session.add(snooze)
            await session.commit()
            await session.refresh(snooze)
        return snooze

    async def due_snoozes(
        self, now: datetime | None = None
    ) -> list[ReminderSnooze]:
        now = now or utcnow()
        async with session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(ReminderSnooze)
                        .where(
                            ReminderSnooze.guild_id == self._guild_id,
                            ReminderSnooze.due_at <= now,
                        )
                        .order_by(ReminderSnooze.due_at)
                    )
                ).scalars()
            )

    async def discard_snooze(self, snooze: ReminderSnooze) -> None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(ReminderSnooze).where(
                        ReminderSnooze.id == snooze.id
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                await session.delete(row)
                await session.commit()

    async def _get(self, session, reminder_id: int) -> Reminder | None:
        return (
            await session.execute(
                select(Reminder).where(
                    Reminder.guild_id == self._guild_id,
                    Reminder.id == reminder_id,
                )
            )
        ).scalar_one_or_none()

