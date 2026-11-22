"""Tests for natural-language reminder schedules (no network or database)."""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from types import SimpleNamespace

from bot.services import reminder_interpreter as interp
from bot.services import reminders as rs
from bot.services.chat import ChatUnavailableError
from bot.texts import t

# A Tuesday.
NOW = datetime(2026, 10, 20, 9, 0)


def resolve(raw: dict, timezone: str = "UTC", now: datetime = NOW):
    return interp.resolve(raw, timezone=timezone, now=now)


class ParseClockTests(unittest.TestCase):
    def test_accepted_forms(self) -> None:
        for value, expected in (
            ("15:30", (15, 30)),
            ("9:05", (9, 5)),
            ("3pm", (15, 0)),
            ("3:30 PM", (15, 30)),
            ("12am", (0, 0)),
            ("12pm", (12, 0)),
            ("00:00", (0, 0)),
        ):
            with self.subTest(value=value):
                self.assertEqual(interp.parse_clock(value), expected)

    def test_absent_is_none(self) -> None:
        self.assertIsNone(interp.parse_clock(None))
        self.assertIsNone(interp.parse_clock("  "))

    def test_bad_values_are_rejected(self) -> None:
        for value in ("3", "25:00", "12:60", "13pm", "0am", "noon"):
            with self.subTest(value=value), self.assertRaises(rs.InvalidTime):
                interp.parse_clock(value)


class ExtractJsonTests(unittest.TestCase):
    def test_plain_and_fenced(self) -> None:
        self.assertEqual(interp.extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(
            interp.extract_json('```json\n{"a": 1}\n```'), {"a": 1}
        )
        self.assertEqual(interp.extract_json('Sure! {"a": 1} done'), {"a": 1})

    def test_garbage_is_rejected(self) -> None:
        for text in ("", "no json", "{broken", "[1, 2]"):
            with self.subTest(text=text), self.assertRaises(
                interp.ScheduleNotUnderstood
            ):
                interp.extract_json(text)


class ResolveRecurringTests(unittest.TestCase):
    def test_every_two_weeks_on_saturday(self) -> None:
        schedule = resolve(
            {"repeat": "weekly", "every": 2, "day": "saturday", "time": "15:00"}
        )
        self.assertEqual(schedule.kind, rs.KIND_INTERVAL)
        self.assertEqual(schedule.interval_days, 14)
        self.assertEqual(schedule.weekdays, (5,))
        self.assertEqual((schedule.hour, schedule.minute), (15, 0))

    def test_plain_weekly(self) -> None:
        schedule = resolve({"repeat": "weekly", "day": "Monday", "time": "9:30"})
        self.assertEqual(schedule.kind, rs.KIND_WEEKLY)
        self.assertEqual(schedule.weekdays, (0,))
        self.assertEqual(schedule.minute, 30)

    def test_weekly_needs_a_weekday(self) -> None:
        for day in (None, "tomorrow"):
            with self.subTest(day=day), self.assertRaises(rs.MissingWeekday):
                resolve({"repeat": "weekly", "day": day, "time": "9:00"})

    def test_daily_and_every_n_days(self) -> None:
        self.assertEqual(
            resolve({"repeat": "daily", "time": "20:30"}).kind, rs.KIND_DAILY
        )
        schedule = resolve({"repeat": "daily", "every": 3, "time": "08:00"})
        self.assertEqual(schedule.kind, rs.KIND_INTERVAL)
        self.assertEqual(schedule.interval_days, 3)
        self.assertEqual(schedule.weekdays, ())

    def test_monthly_days_before_the_end(self) -> None:
        schedule = resolve(
            {"repeat": "monthly", "days_before_month_end": 3, "time": "09:00"}
        )
        self.assertEqual(schedule.kind, rs.KIND_MONTHLY)
        self.assertEqual(schedule.month_end_offset, 3)
        self.assertIsNone(schedule.day_of_month)

    def test_monthly_last_day_and_fixed_day(self) -> None:
        last = resolve(
            {"repeat": "monthly", "days_before_month_end": 0, "time": "9:00"}
        )
        self.assertEqual(last.month_end_offset, 0)
        fixed = resolve({"repeat": "monthly", "day_of_month": "15"})
        self.assertEqual(fixed.day_of_month, 15)

    def test_monthly_needs_a_day(self) -> None:
        with self.assertRaises(rs.MissingDay):
            resolve({"repeat": "monthly", "time": "9:00"})

    def test_every_few_months(self) -> None:
        schedule = resolve(
            {"repeat": "monthly", "every": 3, "day_of_month": 1, "time": "10:00"}
        )
        self.assertEqual(schedule.kind, rs.KIND_MONTHLY)
        self.assertEqual(schedule.month_interval, 3)
        self.assertEqual(schedule.day_of_month, 1)

    def test_month_interval_limit(self) -> None:
        with self.assertRaises(rs.InvalidMonthInterval):
            resolve({"repeat": "monthly", "every": 25, "day_of_month": 1})

    def test_missing_time_defaults_to_nine(self) -> None:
        schedule = resolve({"repeat": "weekly", "day": "saturday"})
        self.assertEqual((schedule.hour, schedule.minute), (9, 0))

    def test_unknown_repeat_is_not_understood(self) -> None:
        for raw in ({}, {"repeat": "sometimes"}, {"repeat": "daily", "every": 0}):
            with self.subTest(raw=raw), self.assertRaises(
                interp.ScheduleNotUnderstood
            ):
                resolve(raw)

    def test_out_of_range_values_are_rejected(self) -> None:
        with self.assertRaises(rs.InvalidDay):
            resolve({"repeat": "monthly", "day_of_month": 40})
        with self.assertRaises(rs.InvalidInterval):
            resolve({"repeat": "daily", "every": 400})


class ResolveOnceTests(unittest.TestCase):
    def test_next_saturday(self) -> None:
        schedule = resolve({"repeat": "once", "day": "saturday", "time": "15:00"})
        self.assertEqual(schedule.run_date, date(2026, 10, 24))
        self.assertIsNone(schedule.run_at)

    def test_weekday_today_with_the_time_gone_rolls_a_week(self) -> None:
        schedule = resolve({"repeat": "once", "day": "tuesday", "time": "08:00"})
        self.assertEqual(schedule.run_date, date(2026, 10, 27))

    def test_weekday_today_with_the_time_ahead_stays(self) -> None:
        schedule = resolve({"repeat": "once", "day": "tuesday", "time": "10:00"})
        self.assertEqual(schedule.run_date, date(2026, 10, 20))

    def test_today_and_tomorrow(self) -> None:
        self.assertEqual(
            resolve({"repeat": "once", "day": "today", "time": "18:00"}).run_date,
            date(2026, 10, 20),
        )
        self.assertEqual(
            resolve({"repeat": "once", "day": "tomorrow", "time": "9:00"}).run_date,
            date(2026, 10, 21),
        )

    def test_explicit_date(self) -> None:
        schedule = resolve({"repeat": "once", "date": "2027-03-04", "time": "07:00"})
        self.assertEqual(schedule.run_date, date(2027, 3, 4))

    def test_bad_date(self) -> None:
        with self.assertRaises(rs.InvalidDate):
            resolve({"repeat": "once", "date": "soon", "time": "07:00"})

    def test_no_day_at_all(self) -> None:
        with self.assertRaises(rs.MissingDate):
            resolve({"repeat": "once", "time": "07:00"})

    def test_in_minutes_and_hours(self) -> None:
        schedule = resolve({"repeat": "once", "in_amount": 45, "in_unit": "minutes"})
        self.assertEqual(schedule.run_at, datetime(2026, 10, 20, 9, 45))
        schedule = resolve({"repeat": "once", "in_amount": 2, "in_unit": "hours"})
        self.assertEqual(schedule.run_at, datetime(2026, 10, 20, 11, 0))
        self.assertEqual((schedule.hour, schedule.minute), (11, 0))

    def test_in_minutes_uses_the_local_clock(self) -> None:
        schedule = resolve(
            {"repeat": "once", "in_amount": 30, "in_unit": "minutes"},
            timezone="Europe/Berlin",
        )
        self.assertEqual((schedule.hour, schedule.minute), (11, 30))

    def test_in_days_with_a_time_lands_on_that_time(self) -> None:
        schedule = resolve(
            {"repeat": "once", "in_amount": 3, "in_unit": "days", "time": "14:00"}
        )
        self.assertEqual(schedule.run_date, date(2026, 10, 23))
        self.assertIsNone(schedule.run_at)
        self.assertEqual(schedule.hour, 14)

    def test_in_days_without_a_time_is_an_exact_delay(self) -> None:
        schedule = resolve({"repeat": "once", "in_amount": 1, "in_unit": "weeks"})
        self.assertEqual(schedule.run_at, datetime(2026, 10, 27, 9, 0))

    def test_in_amount_limits(self) -> None:
        for amount in (0, 10_000_000):
            with self.subTest(amount=amount), self.assertRaises(
                rs.InvalidMinutes
            ):
                resolve({"repeat": "once", "in_amount": amount, "in_unit": "minutes"})

    def test_in_unit_must_be_known(self) -> None:
        with self.assertRaises(interp.ScheduleNotUnderstood):
            resolve({"repeat": "once", "in_amount": 3, "in_unit": "fortnights"})

    def test_dates_follow_the_timezone(self) -> None:
        # 23:30 UTC Tuesday is already Wednesday in Berlin.
        late = datetime(2026, 10, 20, 23, 30)
        schedule = resolve(
            {"repeat": "once", "day": "today", "time": "23:50"},
            timezone="Europe/Berlin",
            now=late,
        )
        self.assertEqual(schedule.run_date, date(2026, 10, 21))


class PickTimezoneTests(unittest.TestCase):
    def test_precedence(self) -> None:
        raw = {"timezone": "Europe/Berlin"}
        self.assertEqual(interp.pick_timezone(raw, "Asia/Tokyo", "UTC"), "Asia/Tokyo")
        self.assertEqual(interp.pick_timezone(raw, None, "UTC"), "Europe/Berlin")
        self.assertEqual(interp.pick_timezone({}, None, "UTC"), "UTC")
        self.assertEqual(
            interp.pick_timezone({"timezone": " "}, None, "UTC"), "UTC"
        )

    def test_unknown_zone_is_an_error_unless_overridden(self) -> None:
        with self.assertRaises(rs.InvalidTimezone):
            interp.pick_timezone({"timezone": "Narnia/Cair"}, None, "UTC")
        self.assertEqual(
            interp.pick_timezone({"timezone": "Narnia/Cair"}, "Asia/Tokyo", "UTC"),
            "Asia/Tokyo",
        )


class FakeChat:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class InterpreterTests(unittest.IsolatedAsyncioTestCase):
    async def _interpret(self, chat: FakeChat, **kwargs):
        return await interp.ReminderInterpreter(chat).interpret(
            "next saturday at 3PM Berlin time",
            default_timezone="UTC",
            now=NOW,
            **kwargs,
        )

    async def test_reads_the_model_reply(self) -> None:
        chat = FakeChat(
            json.dumps(
                {
                    "repeat": "once",
                    "day": "saturday",
                    "time": "15:00",
                    "timezone": "Europe/Berlin",
                }
            )
        )
        result = await self._interpret(chat)
        self.assertEqual(result.timezone, "Europe/Berlin")
        self.assertEqual(result.schedule.run_date, date(2026, 10, 24))
        system, user = chat.calls[0]
        self.assertEqual(user, "next saturday at 3PM Berlin time")
        self.assertIn("Tuesday, 2026-10-20", system)

    async def test_explicit_timezone_wins(self) -> None:
        chat = FakeChat('{"repeat": "daily", "time": "08:00", "timezone": "Europe/Berlin"}')
        result = await self._interpret(chat, explicit_timezone="Asia/Tokyo")
        self.assertEqual(result.timezone, "Asia/Tokyo")

    async def test_unreadable_reply(self) -> None:
        with self.assertRaises(interp.ScheduleNotUnderstood):
            await self._interpret(FakeChat("I cannot do that"))

    async def test_chat_failure_becomes_unavailable(self) -> None:
        with self.assertRaises(interp.InterpreterUnavailable):
            await self._interpret(FakeChat(ChatUnavailableError()))


class TextTests(unittest.TestCase):
    def test_error_keys_exist(self) -> None:
        for error in (
            interp.ScheduleNotUnderstood,
            interp.UnsupportedSchedule,
            interp.InterpreterDisabled,
            interp.InterpreterUnavailable,
        ):
            with self.subTest(error=error.__name__):
                t(error.key)

    def test_prompt_renders(self) -> None:
        prompt = interp.ReminderInterpreter.system_prompt(date(2026, 10, 20), "UTC")
        self.assertIn("Tuesday, 2026-10-20", prompt)
        self.assertIn("UTC", prompt)

    def test_parameter_descriptions_fit_discord(self) -> None:
        for key in (
            "param_message",
            "param_schedule",
            "param_timezone",
            "param_channel",
            "param_role",
        ):
            with self.subTest(key=key):
                self.assertLessEqual(len(t(f"reminders.shared.{key}")), 100)


class FirstRunTests(unittest.TestCase):
    def test_interval_anchored_to_a_weekday_starts_on_it(self) -> None:
        schedule = rs.Schedule(
            rs.KIND_INTERVAL, 15, 30, weekdays=(5,), interval_days=14
        )
        self.assertEqual(
            rs.first_run(schedule, "UTC", NOW), datetime(2026, 10, 24, 15, 30)
        )

    def test_unanchored_interval_starts_at_the_next_occurrence(self) -> None:
        schedule = rs.Schedule(rs.KIND_INTERVAL, 8, 0, interval_days=3)
        self.assertEqual(
            rs.first_run(schedule, "UTC", NOW), datetime(2026, 10, 21, 8, 0)
        )

    def test_past_one_off_is_rejected(self) -> None:
        schedule = rs.Schedule(rs.KIND_ONCE, 8, 0, run_date=date(2026, 10, 20))
        with self.assertRaises(rs.PastDate):
            rs.first_run(schedule, "UTC", NOW)

    def test_minutes_matter(self) -> None:
        schedule = rs.Schedule(rs.KIND_DAILY, 9, 30)
        self.assertEqual(
            rs.first_run(schedule, "UTC", NOW), datetime(2026, 10, 20, 9, 30)
        )

    def test_week_interval_keeps_its_cadence(self) -> None:
        row = SimpleNamespace(
            kind=rs.KIND_INTERVAL,
            hour=15,
            minute=30,
            timezone="UTC",
            weekday=5,
            day_of_month=None,
            month_end_offset=None,
            interval_days=14,
            run_date=None,
            run_at=None,
            next_run_at=datetime(2026, 10, 24, 15, 30),
        )
        self.assertEqual(
            rs.compute_next_run(row, datetime(2026, 10, 24, 15, 35)),
            datetime(2026, 11, 7, 15, 30),
        )


class ResolveBroadPhrasesTests(unittest.TestCase):
    def test_weekdays_and_weekends(self) -> None:
        for names, expected in (
            (["weekdays"], (0, 1, 2, 3, 4)),
            ("weekends", (5, 6)),
            (["monday", "thursday"], (0, 3)),
            (["Mon", "wed", "fri"], (0, 2, 4)),
        ):
            with self.subTest(names=names):
                schedule = resolve(
                    {"repeat": "weekly", "weekdays": names, "time": "08:30"}
                )
                self.assertEqual(schedule.kind, rs.KIND_WEEKLY)
                self.assertEqual(schedule.weekdays, expected)

    def test_daily_with_weekdays_is_weekly(self) -> None:
        schedule = resolve(
            {"repeat": "daily", "weekdays": ["weekdays"], "time": "08:30"}
        )
        self.assertEqual(schedule.kind, rs.KIND_WEEKLY)

    def test_every_few_weeks_on_several_days_is_unsupported(self) -> None:
        with self.assertRaises(interp.UnsupportedSchedule):
            resolve(
                {"repeat": "weekly", "every": 2, "weekdays": ["monday", "friday"]}
            )

    def test_nth_weekday_of_the_month(self) -> None:
        for nth, expected in (("first", 1), ("last", -1), (3, 3), ("2nd", 2)):
            with self.subTest(nth=nth):
                schedule = resolve(
                    {
                        "repeat": "monthly",
                        "nth": nth,
                        "weekdays": ["monday"],
                        "time": "12:00",
                    }
                )
                self.assertEqual(schedule.month_week, expected)
                self.assertEqual(schedule.weekdays, (0,))

    def test_nth_weekday_needs_valid_input(self) -> None:
        with self.assertRaises(rs.InvalidMonthWeek):
            resolve({"repeat": "monthly", "nth": "fifth", "weekdays": ["monday"]})
        with self.assertRaises(rs.MissingWeekday):
            resolve({"repeat": "monthly", "nth": "first"})

    def test_yearly(self) -> None:
        schedule = resolve({"repeat": "yearly", "month": "March", "day_of_month": 4})
        self.assertEqual((schedule.month, schedule.day_of_month), (3, 4))
        self.assertEqual((schedule.hour, schedule.minute), (9, 0))

    def test_yearly_validation(self) -> None:
        with self.assertRaises(rs.InvalidMonth):
            resolve({"repeat": "yearly", "month": 13, "day_of_month": 4})
        with self.assertRaises(rs.InvalidDay):
            resolve({"repeat": "yearly", "month": 2, "day_of_month": 30})
        with self.assertRaises(interp.UnsupportedSchedule):
            resolve({"repeat": "yearly", "every": 2, "month": 3, "day_of_month": 4})

    def test_rolling(self) -> None:
        self.assertEqual(
            resolve({"repeat": "rolling", "every": 2, "unit": "hours"}).interval_minutes,
            120,
        )
        self.assertEqual(
            resolve({"repeat": "rolling", "every": 45, "unit": "minutes"}).interval_minutes,
            45,
        )

    def test_rolling_limits(self) -> None:
        for raw in (
            {"repeat": "rolling", "every": 5, "unit": "minutes"},
            {"repeat": "rolling", "every": 500, "unit": "hours"},
        ):
            with self.subTest(raw=raw), self.assertRaises(rs.InvalidRolling):
                resolve(raw)
        with self.assertRaises(interp.ScheduleNotUnderstood):
            resolve({"repeat": "rolling", "every": 2})

    def test_once_on_a_calendar_date_without_a_year(self) -> None:
        self.assertEqual(
            resolve(
                {"repeat": "once", "month": 12, "day_of_month": 24, "time": "18:00"}
            ).run_date,
            date(2026, 12, 24),
        )
        # March 4th already passed this year, so it means next year.
        self.assertEqual(
            resolve(
                {"repeat": "once", "month": 3, "day_of_month": 4, "time": "18:00"}
            ).run_date,
            date(2027, 3, 4),
        )

    def test_once_on_feb_29_finds_the_next_leap_year(self) -> None:
        self.assertEqual(
            resolve(
                {"repeat": "once", "month": 2, "day_of_month": 29, "time": "08:00"}
            ).run_date,
            date(2028, 2, 29),
        )


class EngineTests(unittest.TestCase):
    def test_every_three_months_keeps_its_rhythm(self) -> None:
        row = SimpleNamespace(
            kind=rs.KIND_MONTHLY,
            hour=10,
            minute=0,
            timezone="UTC",
            weekday=None,
            weekdays_mask=None,
            day_of_month=1,
            month_end_offset=None,
            month_week=None,
            month_interval=3,
            interval_days=None,
            run_date=None,
            run_at=None,
            next_run_at=datetime(2026, 11, 1, 10),
        )
        # Fired on 1 Nov; the next one is 1 Feb, even when handled late.
        self.assertEqual(
            rs.compute_next_run(row, datetime(2026, 11, 1, 10, 5)),
            datetime(2027, 2, 1, 10),
        )
        self.assertEqual(
            rs.compute_next_run(row, datetime(2026, 12, 20)),
            datetime(2027, 2, 1, 10),
        )

    def test_first_run_of_every_few_months_is_the_next_occurrence(self) -> None:
        schedule = rs.Schedule(
            rs.KIND_MONTHLY, 10, 0, day_of_month=1, month_interval=3
        )
        self.assertEqual(
            rs.first_run(schedule, "UTC", NOW), datetime(2026, 11, 1, 10)
        )

    def test_month_interval_crosses_the_year(self) -> None:
        self.assertEqual(
            rs.next_monthly(
                31, datetime(2026, 12, 31, 12), 9, "UTC", every=2,
                anchor=(2026, 12),
            ),
            datetime(2027, 2, 28, 9),
        )

    def test_nth_weekday_dates(self) -> None:
        # October 2026: Thursdays are the 1st, 8th, 15th, 22nd and 29th.
        self.assertEqual(rs._nth_weekday(2026, 10, 3, 1), date(2026, 10, 1))
        self.assertEqual(rs._nth_weekday(2026, 10, 3, 4), date(2026, 10, 22))
        self.assertEqual(rs._nth_weekday(2026, 10, 3, -1), date(2026, 10, 29))
        self.assertEqual(rs._nth_weekday(2026, 2, 6, -1), date(2026, 2, 22))

    def test_first_monday_of_the_month(self) -> None:
        schedule = rs.Schedule(
            rs.KIND_MONTHLY, 12, 0, weekdays=(0,), month_week=1
        )
        self.assertEqual(
            rs.first_run(schedule, "UTC", NOW), datetime(2026, 11, 2, 12)
        )

    def test_several_weekdays_pick_the_soonest(self) -> None:
        schedule = rs.Schedule(rs.KIND_WEEKLY, 18, 0, weekdays=(0, 3))
        # Tuesday 9:00 -> Thursday.
        self.assertEqual(
            rs.first_run(schedule, "UTC", NOW), datetime(2026, 10, 22, 18)
        )
        self.assertEqual(
            rs.next_weekdays((0, 3), datetime(2026, 10, 22, 18), 18, "UTC"),
            datetime(2026, 10, 26, 18),
        )

    def test_mask_round_trip(self) -> None:
        self.assertIsNone(rs.weekdays_to_mask((2,)))
        self.assertIsNone(rs.weekdays_to_mask(()))
        mask = rs.weekdays_to_mask((0, 3, 6))
        row = SimpleNamespace(weekdays_mask=mask, weekday=0)
        self.assertEqual(rs.weekdays_of(row), (0, 3, 6))
        self.assertEqual(rs.weekdays_of(SimpleNamespace(weekday=4)), (4,))

    def test_yearly(self) -> None:
        self.assertEqual(
            rs.next_yearly(3, 4, NOW, 9, "UTC"), datetime(2027, 3, 4, 9)
        )
        # Feb 29 falls on the 28th in common years and the 29th in leap years.
        self.assertEqual(
            rs.next_yearly(2, 29, NOW, 9, "UTC"), datetime(2027, 2, 28, 9)
        )
        self.assertEqual(
            rs.next_yearly(2, 29, datetime(2027, 3, 1), 9, "UTC"),
            datetime(2028, 2, 29, 9),
        )

    def test_rolling_keeps_its_rhythm_and_skips_missed_runs(self) -> None:
        anchor = datetime(2026, 10, 20, 9, 0)
        self.assertEqual(
            rs.next_rolling(anchor, datetime(2026, 10, 20, 9, 5), 120),
            datetime(2026, 10, 20, 11, 0),
        )
        self.assertEqual(
            rs.next_rolling(anchor, datetime(2026, 10, 20, 17, 0), 120),
            datetime(2026, 10, 20, 19, 0),
        )

    def test_rolling_first_run_is_one_interval_away(self) -> None:
        schedule = rs.Schedule(rs.KIND_ROLLING, 0, 0, interval_minutes=90)
        self.assertEqual(
            rs.first_run(schedule, "UTC", NOW), datetime(2026, 10, 20, 10, 30)
        )


class ValidateScheduleTests(unittest.TestCase):
    def test_unrelated_fields_are_cleared(self) -> None:
        cleaned = rs.validate_schedule(
            rs.Schedule(rs.KIND_DAILY, 9, 0, weekdays=(3,), day_of_month=4)
        )
        self.assertEqual(cleaned.weekdays, ())
        self.assertIsNone(cleaned.day_of_month)

    def test_round_trip_through_a_row(self) -> None:
        schedule = rs.Schedule(rs.KIND_WEEKLY, 9, 15, weekdays=(2,))
        row = SimpleNamespace(
            kind=schedule.kind,
            hour=9,
            minute=15,
            weekday=2,
            day_of_month=None,
            month_end_offset=None,
            interval_days=None,
            run_date=None,
            run_at=None,
        )
        self.assertEqual(rs.schedule_of(row), schedule)

    def test_bad_minute(self) -> None:
        with self.assertRaises(rs.InvalidTime):
            rs.validate_schedule(rs.Schedule(rs.KIND_DAILY, 9, 75))

    def test_incomplete_schedules(self) -> None:
        for schedule, error in (
            (rs.Schedule(rs.KIND_ONCE, 9), rs.MissingDate),
            (rs.Schedule(rs.KIND_WEEKLY, 9), rs.MissingWeekday),
            (rs.Schedule(rs.KIND_MONTHLY, 9), rs.MissingDay),
            (rs.Schedule(rs.KIND_INTERVAL, 9), rs.MissingInterval),
            (rs.Schedule("sometimes", 9), rs.InvalidKind),
        ):
            with self.subTest(kind=schedule.kind), self.assertRaises(error):
                rs.validate_schedule(schedule)


if __name__ == "__main__":
    unittest.main()
