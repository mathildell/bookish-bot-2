"""Natural-language reminder schedules, interpreted by the local LLM.

The model only extracts fields from the member's phrase; it never does date
arithmetic. Everything it returns is validated and resolved against the real
clock here, so a bad answer ends in a clear error instead of a wrong reminder.
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..texts import t
from .chat import ChatError, ChatService
from .reminders import (
    KIND_DAILY,
    KIND_INTERVAL,
    KIND_MONTHLY,
    KIND_ONCE,
    KIND_ROLLING,
    KIND_WEEKLY,
    KIND_YEARLY,
    MINUTES_MAX,
    InvalidDate,
    InvalidMinutes,
    InvalidTime,
    InvalidTimezone,
    MissingDate,
    MissingWeekday,
    ReminderError,
    Schedule,
    parse_weekday,
    validate_schedule,
)
from .timezones import local_now, valid_timezone

_TIME_RE = re.compile(
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<meridiem>am|pm)?", re.IGNORECASE
)
_MERIDIEM_TIME_RE = re.compile(
    r"(?P<hour>\d{1,2})\s*(?P<meridiem>am|pm)", re.IGNORECASE
)
_UNIT_MINUTES = {"minutes": 1, "hours": 60, "days": 24 * 60, "weeks": 7 * 24 * 60}
_KINDS = (
    KIND_ONCE,
    KIND_DAILY,
    KIND_WEEKLY,
    KIND_MONTHLY,
    KIND_YEARLY,
    KIND_ROLLING,
)
_NTH = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4, "last": -1,
}
_MONTHS = {
    name.lower(): index
    for index in range(1, 13)
    for name in (calendar.month_name[index], calendar.month_abbr[index])
}
# Used when the phrase names a day but no time of day.
DEFAULT_TIME = (9, 0)


class ScheduleNotUnderstood(ReminderError):
    key = "reminders.errors.not_understood"


class UnsupportedSchedule(ReminderError):
    key = "reminders.errors.unsupported_schedule"


class InterpreterDisabled(ReminderError):
    key = "reminders.errors.ai_disabled"


class InterpreterUnavailable(ReminderError):
    key = "reminders.errors.ai_unavailable"


@dataclass(frozen=True)
class Interpretation:
    """A resolved phrase: the schedule and the timezone it is read in."""

    schedule: Schedule
    timezone: str | None


def extract_json(text: str) -> dict:
    """The JSON object in a model reply, tolerating fences and chatter."""
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start == -1 or end <= start:
        raise ScheduleNotUnderstood()
    try:
        data = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise ScheduleNotUnderstood() from exc
    if not isinstance(data, dict):
        raise ScheduleNotUnderstood()
    return data


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def parse_clock(value: object) -> tuple[int, int] | None:
    """``15:30``, ``3:30 pm`` or ``3pm`` as ``(hour, minute)``; None if absent."""
    raw = _text(value)
    if raw is None:
        return None
    match = _TIME_RE.fullmatch(raw) or _MERIDIEM_TIME_RE.fullmatch(raw)
    if match is None:
        raise InvalidTime()
    hour = int(match["hour"])
    minute = int(match.groupdict().get("minute") or 0)
    meridiem = (match["meridiem"] or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            raise InvalidTime()
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if hour > 23 or minute > 59:
        raise InvalidTime()
    return hour, minute


def pick_timezone(raw: dict, explicit: str | None, default: str) -> str:
    """The zone to schedule in: the option, else the phrase, else the default."""
    if explicit is not None:
        return explicit
    named = _text(raw.get("timezone"))
    if named is None:
        return default
    zone = valid_timezone(named)
    if zone is None:
        raise InvalidTimezone(timezone=named)
    return zone


def resolve(raw: dict, *, timezone: str, now: datetime) -> Schedule:
    """Turn the model's extracted fields into a validated :class:`Schedule`."""
    kind = _text(raw.get("repeat"))
    kind = kind.lower() if kind else None
    if kind not in _KINDS:
        raise ScheduleNotUnderstood()
    given = parse_clock(raw.get("time"))
    clock = given or DEFAULT_TIME
    every = _number(raw.get("every"))
    every = 1 if every is None else every
    if every < 1:
        raise ScheduleNotUnderstood()
    today = local_now(now, timezone).date()

    if kind == KIND_ONCE:
        return _resolve_once(raw, clock, given is not None, today, now, timezone)

    hour, minute = clock
    weekdays = _weekdays(raw)
    # "every weekday" is a weekly reminder however the model labelled it.
    if kind == KIND_DAILY and _text_or_list(raw.get("weekdays")):
        kind = KIND_WEEKLY

    if kind == KIND_DAILY:
        if every == 1:
            return validate_schedule(Schedule(KIND_DAILY, hour, minute))
        return validate_schedule(
            Schedule(KIND_INTERVAL, hour, minute, interval_days=every)
        )

    if kind == KIND_WEEKLY:
        if not weekdays:
            raise MissingWeekday()
        if every == 1:
            return validate_schedule(
                Schedule(KIND_WEEKLY, hour, minute, weekdays=weekdays)
            )
        if len(weekdays) > 1:
            raise UnsupportedSchedule()
        return validate_schedule(
            Schedule(
                KIND_INTERVAL,
                hour,
                minute,
                weekdays=weekdays,
                interval_days=every * 7,
            )
        )

    if kind == KIND_YEARLY:
        if every != 1:
            raise UnsupportedSchedule()
        return validate_schedule(
            Schedule(
                KIND_YEARLY,
                hour,
                minute,
                month=_month(raw.get("month")),
                day_of_month=_number(raw.get("day_of_month")),
            )
        )

    if kind == KIND_ROLLING:
        unit = (_text(raw.get("unit")) or "").lower()
        if unit not in ("minutes", "hours"):
            raise ScheduleNotUnderstood()
        return validate_schedule(
            Schedule(
                KIND_ROLLING,
                0,
                0,
                interval_minutes=every * _UNIT_MINUTES[unit],
            )
        )

    nth = _nth(raw.get("nth"))
    return validate_schedule(
        Schedule(
            KIND_MONTHLY,
            hour,
            minute,
            weekdays=weekdays[:1] if nth is not None else (),
            day_of_month=_number(raw.get("day_of_month")),
            month_end_offset=_number(raw.get("days_before_month_end")),
            month_week=nth,
            month_interval=every,
        )
    )


def _text_or_list(value: object) -> list[str]:
    """A string or list of strings as a list of non-empty strings."""
    items = value if isinstance(value, list) else [value]
    return [item.strip() for item in items if _text(item)]


def _weekdays(raw: dict) -> tuple[int, ...]:
    """Weekday numbers from ``weekdays`` (names, "weekdays", "weekends") or ``day``."""
    names = _text_or_list(raw.get("weekdays"))
    if not names:
        names = _text_or_list(raw.get("day"))
    found: set[int] = set()
    for name in names:
        lowered = name.lower()
        if lowered in ("today", "tomorrow"):
            continue
        if lowered in ("weekday", "weekdays", "workday", "workdays"):
            found.update(range(5))
        elif lowered in ("weekend", "weekends"):
            found.update((5, 6))
        else:
            found.add(parse_weekday(lowered))
    return tuple(sorted(found))


def _nth(value: object) -> int | None:
    """``first`` ... ``fourth`` or ``last`` (or 1-4 / -1) as a month-week number."""
    if _text(value) is None and not isinstance(value, int):
        return None
    number = _number(value)
    if number is not None:
        return number
    return _NTH.get((_text(value) or "").lower(), 0)


def _month(value: object) -> int | None:
    number = _number(value)
    if number is not None:
        return number
    return _MONTHS.get((_text(value) or "").lower())


def _resolve_once(
    raw: dict,
    clock: tuple[int, int],
    explicit_time: bool,
    today: date,
    now: datetime,
    timezone: str,
) -> Schedule:
    amount = _number(raw.get("in_amount"))
    unit = (_text(raw.get("in_unit")) or "").lower()
    if amount is not None:
        if unit not in _UNIT_MINUTES:
            raise ScheduleNotUnderstood()
        minutes = amount * _UNIT_MINUTES[unit]
        if not 1 <= minutes <= MINUTES_MAX:
            raise InvalidMinutes(minutes=minutes, maximum=MINUTES_MAX)
        if explicit_time and unit in ("days", "weeks"):
            hour, minute = clock
            return validate_schedule(
                Schedule(
                    KIND_ONCE,
                    hour,
                    minute,
                    run_date=today + timedelta(minutes=minutes),
                )
            )
        run_at = now.replace(second=0, microsecond=0) + timedelta(
            minutes=minutes
        )
        local = local_now(run_at, timezone)
        return validate_schedule(
            Schedule(
                KIND_ONCE,
                local.hour,
                local.minute,
                run_date=local.date(),
                run_at=run_at,
            )
        )

    hour, minute = clock

    iso = _text(raw.get("date"))
    if iso is not None:
        try:
            run_date = date.fromisoformat(iso)
        except ValueError as exc:
            raise InvalidDate(date=iso) from exc
        return validate_schedule(
            Schedule(KIND_ONCE, hour, minute, run_date=run_date)
        )

    month = _month(raw.get("month"))
    month_day = _number(raw.get("day_of_month"))
    if month is not None and month_day is not None:
        return validate_schedule(
            Schedule(
                KIND_ONCE,
                hour,
                minute,
                run_date=_next_calendar_date(
                    month, month_day, hour, minute, now, timezone
                ),
            )
        )

    day = (_text(raw.get("day")) or "").lower()
    if day == "today":
        run_date = today
    elif day == "tomorrow":
        run_date = today + timedelta(days=1)
    elif day:
        weekday = parse_weekday(day)
        run_date = today + timedelta(days=(weekday - today.weekday()) % 7)
        # A weekday never means "earlier today": if the time is gone, roll on.
        local = local_now(now, timezone)
        if run_date == today and (hour, minute) <= (local.hour, local.minute):
            run_date += timedelta(days=7)
    else:
        raise MissingDate()
    return validate_schedule(
        Schedule(KIND_ONCE, hour, minute, run_date=run_date)
    )


def _next_calendar_date(
    month: int, day: int, hour: int, minute: int, now: datetime, timezone: str
) -> date:
    """The next time "March 4th" comes around, this year or the next."""
    local = local_now(now, timezone)
    for year in range(local.year, local.year + 5):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if (candidate, (hour, minute)) > (
            local.date(),
            (local.hour, local.minute),
        ):
            return candidate
    raise InvalidDate(date=f"{month}/{day}")


class ReminderInterpreter:
    """Asks the chat model to read a schedule phrase, then resolves it."""

    def __init__(self, chat: ChatService) -> None:
        self._chat = chat

    @staticmethod
    def system_prompt(today: date, timezone: str) -> str:
        return t(
            "reminders.interpreter.prompt",
            today=today.isoformat(),
            weekday=t(f"components.weekday.{today.strftime('%A').lower()}"),
            timezone=timezone,
        )

    async def interpret(
        self,
        phrase: str,
        *,
        default_timezone: str,
        explicit_timezone: str | None = None,
        now: datetime,
    ) -> Interpretation:
        zone_hint = explicit_timezone or default_timezone
        today = local_now(now, zone_hint).date()
        try:
            reply = await self._chat.complete_json(
                self.system_prompt(today, zone_hint), phrase
            )
        except ChatError as exc:
            raise InterpreterUnavailable() from exc
        raw = extract_json(reply)
        zone = pick_timezone(raw, explicit_timezone, default_timezone)
        schedule = resolve(raw, timezone=zone, now=now)
        return Interpretation(schedule=schedule, timezone=zone)
