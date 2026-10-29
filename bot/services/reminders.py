"""Member reminders: one-off and recurring schedules posted into a channel.

Schedule rules live as module-level functions so they can be tested without a
database. The :class:`ReminderService` owns the read/write work and snoozes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..db import session_scope
from ..models import Reminder, ReminderSnooze
from .level import utcnow
from .timezones import default_member_timezone, local_now, to_utc, valid_timezone
from .weekdays import WEEKDAY_NAMES

KIND_ONCE = "once"
KIND_DAILY = "daily"
KIND_WEEKLY = "weekly"
KIND_MONTHLY = "monthly"
KIND_INTERVAL = "interval"

KINDS = (KIND_ONCE, KIND_DAILY, KIND_WEEKLY, KIND_MONTHLY, KIND_INTERVAL)

MESSAGE_MAX_LENGTH = 240
INTERVAL_MAX_DAYS = 365
# "in x minutes" one-offs may be scheduled up to a year ahead.
MINUTES_MAX = 365 * 24 * 60
# Keeps the target day at 1 or later even in the shortest February.
MONTH_END_MAX_OFFSET = 27

# Snoozed/missed reminders older than this are dropped instead of posted.
STALE_AFTER = timedelta(hours=24)

_WEEKDAY_PATTERN = "|".join(
    sorted(
        {word for name in WEEKDAY_NAMES for word in (name, name[:3])},
        key=len,
        reverse=True,
    )
)
_TIME_PATTERN = r"(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
_MINUTES_PATTERN = re.compile(
    r"^in\s+(?P<minutes>\d+)\s+minutes?$", re.IGNORECASE
)
_ONCE_PATTERN = re.compile(
    rf"^(?:on\s+)?"
    rf"(?P<date>\d{{4}}-\d{{2}}-\d{{2}}|today|tomorrow|{_WEEKDAY_PATTERN})"
    rf"\s+at\s+{_TIME_PATTERN}$",
    re.IGNORECASE,
)
_SCHEDULE_PATTERNS = {
    KIND_DAILY: (
        re.compile(
            rf"^(?:(?:every\s+day|everyday)\s+)?at\s+{_TIME_PATTERN}$",
            re.IGNORECASE,
        ),
    ),
    KIND_WEEKLY: (
        re.compile(
            rf"^(?:every\s+)?(?:on\s+)?(?P<weekday>{_WEEKDAY_PATTERN})"
            rf"\s+at\s+{_TIME_PATTERN}$",
            re.IGNORECASE,
        ),
    ),
    KIND_MONTHLY: (
        re.compile(
            rf"^(?:every\s+month\s+)?(?:on\s+)?(?:the\s+)?(?P<day>\d+)"
            rf"(?:st|nd|rd|th)?\s+at\s+{_TIME_PATTERN}$",
            re.IGNORECASE,
        ),
        re.compile(
            rf"^(?:every\s+month\s+)?(?:on\s+)?(?:the\s+)?last\s+day"
            rf"(?:\s+of\s+the\s+month)?\s+at\s+{_TIME_PATTERN}$",
            re.IGNORECASE,
        ),
        re.compile(
            rf"^(?:every\s+month\s+)?(?:on\s+)?(?P<offset>\d+)\s+days?"
            rf"\s+before\s+the\s+end(?:\s+of\s+the\s+month)?"
            rf"\s+at\s+{_TIME_PATTERN}$",
            re.IGNORECASE,
        ),
    ),
    KIND_INTERVAL: (
        re.compile(
            rf"^every\s+(?P<days>\d+)\s+days?\s+at\s+{_TIME_PATTERN}$",
            re.IGNORECASE,
        ),
    ),
}


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


class ScheduleError(ReminderError):
    """A schedule phrase problem; carries the kind so the command can help."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind=kind)


class MissingSchedule(ScheduleError):
    key = "reminders.errors.missing_schedule"


class InvalidSchedule(ScheduleError):
    key = "reminders.errors.invalid_schedule"


class InvalidTime(ReminderError):
    key = "reminders.errors.invalid_time"


class InvalidMinutes(ReminderError):
    key = "reminders.errors.invalid_minutes"


class MissingInterval(ReminderError):
    key = "reminders.errors.missing_interval"


class InvalidInterval(ReminderError):
    key = "reminders.errors.invalid_interval"


class ReminderNotFound(ReminderError):
    key = "reminders.errors.not_found"


# ----------------------------------------------------------------- pure rules


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
    if not isinstance(hour, int) or not 0 <= hour <= 23:
        raise InvalidHour(hour=hour)
    return hour


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


def parse_weekday(value: str | None) -> int | None:
    """Parse a weekday name or abbreviation into a Python weekday."""
    if value is None:
        return None
    needle = (value or "").strip().lower()
    for index, name in enumerate(WEEKDAY_NAMES):
        if needle == name or needle == name[:3]:
            return index
    raise InvalidWeekday(weekday=value)


def parse_one_off_date(
    raw: str, *, hour: int, timezone: str, now_utc: datetime
) -> date:
    """Parse an ISO date or a weekday name into the reminder's local date.

    Explicit dates in the past are rejected; a weekday name always resolves to
    its next occurrence, rolling to next week when today's has passed.
    """
    value = (raw or "").strip()
    if not value:
        raise MissingDate()

    try:
        candidate = date.fromisoformat(value)
    except ValueError:
        candidate = None
    else:
        if _occurrence_utc(candidate, hour, timezone) <= now_utc:
            raise PastDate(date=value)
        return candidate

    try:
        weekday = parse_weekday(value)
    except InvalidWeekday as exc:
        raise InvalidDate(date=value) from exc
    if weekday is None:
        raise InvalidDate(date=value)
    today = local_now(now_utc, timezone).date()
    candidate = today + timedelta(days=(weekday - today.weekday()) % 7)
    if _occurrence_utc(candidate, hour, timezone) <= now_utc:
        candidate += timedelta(days=7)
    return candidate


def _occurrence_utc(day: date, hour: int, timezone: str) -> datetime:
    """Naive UTC timestamp of ``day`` at ``hour`` local wall-clock time."""
    return to_utc(datetime.combine(day, time(hour), ZoneInfo(timezone)))


def _parse_run_at(raw: str) -> datetime:
    """Parse the naive-UTC ISO timestamp of an "in x minutes" run."""
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidDate(date=raw) from exc
    if moment.tzinfo is not None:
        moment = moment.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return moment


def parse_time(raw: str) -> int:
    """Parse an on-the-hour time: ``2pm``, ``2:00 pm`` or ``14:00``.

    ``2`` on its own is ambiguous, so a plain 1-12 needs am/pm.
    """
    match = re.fullmatch(
        r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?",
        (raw or "").strip(),
        re.IGNORECASE,
    )
    if match is None:
        raise InvalidTime()
    hour = int(match["hour"])
    minute = match["minute"]
    meridiem = match["meridiem"]
    if minute is not None and int(minute) != 0:
        raise InvalidTime()
    if meridiem is not None:
        if not 1 <= hour <= 12:
            raise InvalidTime()
        if meridiem.lower() == "am":
            return 0 if hour == 12 else hour
        return 12 if hour == 12 else hour + 12
    if hour != 0 and hour <= 12:
        raise InvalidTime()
    if hour > 23:
        raise InvalidTime()
    return hour


@dataclass(frozen=True)
class ParsedSchedule:
    """The structured fields a schedule phrase resolves to."""

    hour: int
    weekday: int | None = None
    day_of_month: int | None = None
    month_end_offset: int | None = None
    interval_days: int | None = None
    run_date: date | None = None
    run_at: datetime | None = None


def parse_schedule(
    kind: str, raw: str, *, timezone: str, now: datetime | None = None
) -> ParsedSchedule:
    """Parse a schedule phrase for ``kind`` into the fields ``Reminder`` stores.

    Only the documented phrasings are accepted; anything else raises
    :class:`InvalidSchedule` so the command can show the expected shapes.
    """
    if kind not in KINDS:
        raise InvalidKind(kind=kind)
    text = " ".join((raw or "").split())
    if not text:
        raise MissingSchedule(kind=kind)
    now = now or utcnow()

    if kind == KIND_ONCE:
        match = _MINUTES_PATTERN.fullmatch(text)
        if match is not None:
            minutes = int(match["minutes"])
            if not 1 <= minutes <= MINUTES_MAX:
                raise InvalidMinutes(minutes=minutes, maximum=MINUTES_MAX)
            run_at = now + timedelta(minutes=minutes)
            local = local_now(run_at, timezone)
            return ParsedSchedule(
                hour=local.hour, run_date=local.date(), run_at=run_at
            )
        match = _ONCE_PATTERN.fullmatch(text)
        if match is None:
            raise InvalidSchedule(kind=kind)
        hour = parse_time(match["time"])
        date_text = match["date"].lower()
        if date_text in ("today", "tomorrow"):
            shift = timedelta(days=1) if date_text == "tomorrow" else timedelta()
            date_text = (local_now(now, timezone).date() + shift).isoformat()
        run_date = parse_one_off_date(
            date_text, hour=hour, timezone=timezone, now_utc=now
        )
        return ParsedSchedule(hour=hour, run_date=run_date)

    for pattern in _SCHEDULE_PATTERNS[kind]:
        match = pattern.fullmatch(text)
        if match is None:
            continue
        groups = match.groupdict()
        hour = parse_time(groups["time"])
        if kind == KIND_WEEKLY:
            return ParsedSchedule(
                hour=hour, weekday=parse_weekday(groups["weekday"])
            )
        if kind == KIND_MONTHLY:
            if groups.get("offset") is not None:
                return ParsedSchedule(
                    hour=hour, month_end_offset=int(groups["offset"])
                )
            if groups.get("day") is not None:
                return ParsedSchedule(
                    hour=hour, day_of_month=int(groups["day"])
                )
            return ParsedSchedule(hour=hour, month_end_offset=0)
        if kind == KIND_INTERVAL:
            return ParsedSchedule(
                hour=hour, interval_days=int(groups["days"])
            )
        return ParsedSchedule(hour=hour)
    raise InvalidSchedule(kind=kind)


def next_daily(after: datetime, hour: int, timezone: str) -> datetime:
    local = local_now(after, timezone)
    day = local.date()
    if _occurrence_utc(day, hour, timezone) <= after:
        day += timedelta(days=1)
    return _occurrence_utc(day, hour, timezone)


def next_weekday(
    weekday: int, after: datetime, hour: int, timezone: str
) -> datetime:
    local = local_now(after, timezone)
    day = local.date() + timedelta(days=(weekday - local.weekday()) % 7)
    if _occurrence_utc(day, hour, timezone) <= after:
        day += timedelta(days=7)
    return _occurrence_utc(day, hour, timezone)


def next_monthly(
    day_of_month: int | None,
    after: datetime,
    hour: int,
    timezone: str,
    *,
    month_end_offset: int | None = None,
) -> datetime:
    """The next monthly fire after ``after``.

    ``day_of_month`` clamps to short months (the 31st fires on the 30th in
    April); ``month_end_offset`` counts days before the month's last day
    (0 = the last day itself).
    """
    if day_of_month is None and month_end_offset is None:
        raise MissingDay()
    local = local_now(after, timezone)
    year, month = local.year, local.month
    for _ in range(24):
        last = _last_day_of_month(year, month)
        if month_end_offset is not None:
            day = date(year, month, last - month_end_offset)
        else:
            day = date(year, month, min(day_of_month, last))
        if _occurrence_utc(day, hour, timezone) > after:
            return _occurrence_utc(day, hour, timezone)
        month += 1
        if month > 12:
            month, year = 1, year + 1
    raise InvalidDay(day=day_of_month)


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        following = date(year + 1, 1, 1)
    else:
        following = date(year, month + 1, 1)
    return (following - timedelta(days=1)).day


def next_interval(
    day: date, after: datetime, hour: int, timezone: str, interval_days: int
) -> datetime:
    """First occurrence at or after ``day`` that is strictly after ``after``."""
    while True:
        candidate = _occurrence_utc(day, hour, timezone)
        if candidate > after:
            return candidate
        day += timedelta(days=interval_days)


def compute_next_run(
    reminder: Reminder, after: datetime
) -> datetime | None:
    """The next fire time strictly after ``after``, or None once expired.

    Recurring kinds advance from ``after`` so a late catch-up never triggers a
    second immediate post; intervals keep their original cadence.
    """
    hour = reminder.hour
    timezone = reminder.timezone
    kind = reminder.kind
    if kind == KIND_ONCE:
        if reminder.run_at is not None:
            return reminder.run_at if reminder.run_at > after else None
        if reminder.run_date is None:
            return None
        candidate = _occurrence_utc(reminder.run_date, hour, timezone)
        return candidate if candidate > after else None
    if kind == KIND_DAILY:
        return next_daily(after, hour, timezone)
    if kind == KIND_WEEKLY:
        if reminder.weekday is None:
            return None
        return next_weekday(reminder.weekday, after, hour, timezone)
    if kind == KIND_MONTHLY:
        if (
            reminder.day_of_month is None
            and reminder.month_end_offset is None
        ):
            return None
        return next_monthly(
            reminder.day_of_month,
            after,
            hour,
            timezone,
            month_end_offset=reminder.month_end_offset,
        )
    if kind == KIND_INTERVAL:
        if not reminder.interval_days or reminder.interval_days <= 0:
            return None
        anchor = local_now(reminder.next_run_at, timezone).date()
        return next_interval(anchor, after, hour, timezone, reminder.interval_days)
    return None


def is_stale(moment: datetime, now: datetime) -> bool:
    """Whether a due moment is too old to post (missed while offline)."""
    return now - moment > STALE_AFTER


def snooze_due_at(now: datetime, delay: timedelta) -> datetime:
    return now + delay


# ------------------------------------------------------------------- service


class ReminderService:
    """CRUD and the due-query behind the reminder poll."""

    def __init__(self, guild_id: int) -> None:
        self._guild_id = guild_id

    # ------------------------------------------------------------- timezone

    async def default_timezone(self, user_id: int) -> str:
        """The member's streak timezone, falling back to UTC."""
        return await default_member_timezone(self._guild_id, user_id)

    # ----------------------------------------------------------------- CRUD

    async def add(
        self,
        *,
        user_id: int,
        channel_id: int,
        message: str,
        timezone: str | None,
        kind: str,
        hour: int,
        weekday: int | None = None,
        day_of_month: int | None = None,
        month_end_offset: int | None = None,
        interval_days: int | None = None,
        date_raw: str | None = None,
        run_at_raw: str | None = None,
        role_id: int | None = None,
        now: datetime | None = None,
    ) -> Reminder:
        now = now or utcnow()
        cleaned = validate_message(message)
        validate_hour(hour)
        zone = valid_timezone(timezone) if timezone else None
        if zone is None:
            raise InvalidTimezone(timezone=timezone)
        if kind not in KINDS:
            raise InvalidKind(kind=kind)

        run_date: date | None = None
        run_at: datetime | None = None
        if kind == KIND_ONCE:
            if run_at_raw is not None:
                run_at = _parse_run_at(run_at_raw)
                if run_at <= now:
                    raise PastDate(date=run_at.isoformat())
                local = local_now(run_at, zone)
                run_date, hour = local.date(), local.hour
            elif not date_raw:
                raise MissingDate()
            else:
                run_date = parse_one_off_date(
                    date_raw, hour=hour, timezone=zone, now_utc=now
                )
        elif kind == KIND_WEEKLY:
            if weekday is None:
                raise MissingWeekday()
            if not 0 <= weekday <= 6:
                raise InvalidWeekday(weekday=weekday)
        elif kind == KIND_MONTHLY:
            day_of_month, month_end_offset = validate_monthly_target(
                day_of_month, month_end_offset
            )
        elif kind == KIND_INTERVAL:
            if interval_days is None:
                raise MissingInterval()
            if not 1 <= interval_days <= INTERVAL_MAX_DAYS:
                raise InvalidInterval(days=interval_days)

        if kind == KIND_ONCE:
            next_run_at = (
                run_at
                if run_at is not None
                else _occurrence_utc(run_date, hour, zone)
            )
        elif kind == KIND_DAILY:
            next_run_at = next_daily(now, hour, zone)
        elif kind == KIND_WEEKLY:
            next_run_at = next_weekday(weekday, now, hour, zone)
        elif kind == KIND_MONTHLY:
            next_run_at = next_monthly(
                day_of_month,
                now,
                hour,
                zone,
                month_end_offset=month_end_offset,
            )
        else:
            next_run_at = next_daily(now, hour, zone)

        reminder = Reminder(
            guild_id=self._guild_id,
            user_id=user_id,
            channel_id=channel_id,
            message=cleaned,
            timezone=zone,
            kind=kind,
            hour=hour,
            weekday=weekday if kind == KIND_WEEKLY else None,
            day_of_month=day_of_month if kind == KIND_MONTHLY else None,
            month_end_offset=(
                month_end_offset if kind == KIND_MONTHLY else None
            ),
            interval_days=interval_days if kind == KIND_INTERVAL else None,
            run_date=run_date,
            run_at=run_at,
            role_id=role_id,
            next_run_at=next_run_at,
        )
        async with session_scope() as session:
            session.add(reminder)
            await session.commit()
            await session.refresh(reminder)
        return reminder

    async def get(self, reminder_id: int) -> Reminder | None:
        async with session_scope() as session:
            return await self._get(session, reminder_id)

    async def edit(
        self,
        reminder_id: int,
        *,
        message: str | None = None,
        timezone: str | None = None,
        kind: str | None = None,
        hour: int | None = None,
        weekday: int | None = None,
        day_of_month: int | None = None,
        month_end_offset: int | None = None,
        interval_days: int | None = None,
        date_raw: str | None = None,
        run_at_raw: str | None = None,
        role_id: int | None = None,
        channel_id: int | None = None,
        reschedule: bool = True,
        now: datetime | None = None,
    ) -> Reminder:
        """Apply the given fields on top of an existing reminder."""
        now = now or utcnow()
        async with session_scope() as session:
            reminder = await self._get(session, reminder_id)
            if reminder is None:
                raise ReminderNotFound(reminder_id=reminder_id)

            merged_message = (
                validate_message(message) if message is not None
                else reminder.message
            )
            merged_zone = reminder.timezone
            if timezone is not None:
                zone = valid_timezone(timezone)
                if zone is None:
                    raise InvalidTimezone(timezone=timezone)
                merged_zone = zone
            merged_kind = kind or reminder.kind
            if merged_kind not in KINDS:
                raise InvalidKind(kind=merged_kind)
            merged_hour = validate_hour(hour if hour is not None else reminder.hour)
            merged_weekday = (
                weekday if weekday is not None else reminder.weekday
            )
            if day_of_month is not None and month_end_offset is not None:
                raise BothDayAndMonthEnd()
            if day_of_month is not None:
                merged_day, merged_month_end = day_of_month, None
            elif month_end_offset is not None:
                merged_day, merged_month_end = None, month_end_offset
            else:
                merged_day = reminder.day_of_month
                merged_month_end = reminder.month_end_offset
            merged_interval = (
                interval_days if interval_days is not None
                else reminder.interval_days
            )

            if merged_kind == KIND_WEEKLY:
                if merged_weekday is None:
                    raise MissingWeekday()
                if not 0 <= merged_weekday <= 6:
                    raise InvalidWeekday(weekday=merged_weekday)
            if merged_kind == KIND_MONTHLY:
                merged_day, merged_month_end = validate_monthly_target(
                    merged_day, merged_month_end
                )
            if merged_kind == KIND_INTERVAL:
                if merged_interval is None:
                    raise MissingInterval()
                if not 1 <= merged_interval <= INTERVAL_MAX_DAYS:
                    raise InvalidInterval(days=merged_interval)

            run_at = reminder.run_at
            run_date = reminder.run_date
            if merged_kind == KIND_ONCE:
                if run_at_raw is not None:
                    run_at = _parse_run_at(run_at_raw)
                    local = local_now(run_at, merged_zone)
                    merged_hour, run_date = local.hour, local.date()
                elif date_raw is not None:
                    run_at = None
                    run_date = parse_one_off_date(
                        date_raw,
                        hour=merged_hour,
                        timezone=merged_zone,
                        now_utc=now,
                    )
                elif run_at is None and run_date is None:
                    raise MissingDate()
                if run_at is not None:
                    if run_at <= now:
                        raise PastDate(date=run_at.isoformat())
                elif _occurrence_utc(run_date, merged_hour, merged_zone) <= now:
                    raise PastDate(date=run_date.isoformat())
            else:
                run_at = None

            reminder.message = merged_message
            reminder.timezone = merged_zone
            reminder.kind = merged_kind
            reminder.hour = merged_hour
            reminder.weekday = (
                merged_weekday if merged_kind == KIND_WEEKLY else None
            )
            reminder.day_of_month = (
                merged_day if merged_kind == KIND_MONTHLY else None
            )
            reminder.month_end_offset = (
                merged_month_end if merged_kind == KIND_MONTHLY else None
            )
            reminder.interval_days = (
                merged_interval if merged_kind == KIND_INTERVAL else None
            )
            reminder.run_date = run_date if merged_kind == KIND_ONCE else None
            reminder.run_at = run_at if merged_kind == KIND_ONCE else None
            if channel_id is not None:
                reminder.channel_id = channel_id
            if role_id is not None:
                reminder.role_id = role_id

            if reschedule:
                scheduled = Reminder(
                    guild_id=reminder.guild_id,
                    user_id=reminder.user_id,
                    channel_id=reminder.channel_id,
                    message=reminder.message,
                    timezone=reminder.timezone,
                    kind=reminder.kind,
                    hour=reminder.hour,
                    weekday=reminder.weekday,
                    day_of_month=reminder.day_of_month,
                    month_end_offset=reminder.month_end_offset,
                    interval_days=reminder.interval_days,
                    run_date=reminder.run_date,
                    run_at=reminder.run_at,
                    next_run_at=reminder.next_run_at,
                )
                if merged_kind == KIND_ONCE:
                    next_run = (
                        reminder.run_at
                        if reminder.run_at is not None
                        else _occurrence_utc(
                            reminder.run_date, reminder.hour, reminder.timezone
                        )
                    )
                elif merged_kind == KIND_INTERVAL:
                    next_run = next_daily(
                        now, reminder.hour, reminder.timezone
                    )
                else:
                    next_run = compute_next_run(scheduled, now)
                reminder.next_run_at = next_run or reminder.next_run_at

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

    # ------------------------------------------------------------ delivery

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

