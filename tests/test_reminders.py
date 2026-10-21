"""Unit tests for the pure logic behind the reminder feature.

Run from the repository root::

    python -m unittest discover -s tests -v

No network, database or Discord connection is required.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import discord

from bot.commands import reminders as reminder_cmds
from bot.services import reminders as reminder_service
from bot.texts import t


def utc(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute)


def reminder(**kwargs) -> SimpleNamespace:
    defaults = dict(
        id=1,
        user_id=10,
        channel_id=20,
        message="Do the thing",
        timezone="UTC",
        kind=reminder_service.KIND_DAILY,
        hour=15,
        weekday=None,
        day_of_month=None,
        month_end_offset=None,
        interval_days=None,
        run_date=None,
        run_at=None,
        role_id=None,
        next_run_at=utc(2026, 10, 20, 15),
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class ParseWeekdayTests(unittest.TestCase):
    def test_full_names(self) -> None:
        self.assertEqual(reminder_service.parse_weekday("monday"), 0)
        self.assertEqual(reminder_service.parse_weekday("SUNDAY"), 6)

    def test_abbreviations(self) -> None:
        self.assertEqual(reminder_service.parse_weekday("thu"), 3)
        self.assertEqual(reminder_service.parse_weekday("Fri"), 4)

    def test_none_passthrough(self) -> None:
        self.assertIsNone(reminder_service.parse_weekday(None))

    def test_unknown_raises(self) -> None:
        with self.assertRaises(reminder_service.InvalidWeekday):
            reminder_service.parse_weekday("someday")


class ParseOneOffDateTests(unittest.TestCase):
    NOW = utc(2026, 10, 20, 14)  # Tuesday

    def _parse(self, value: str, *, hour: int = 15) -> date:
        return reminder_service.parse_one_off_date(
            value, hour=hour, timezone="UTC", now_utc=self.NOW
        )

    def test_future_iso_date(self) -> None:
        self.assertEqual(self._parse("2026-10-29"), date(2026, 10, 29))

    def test_past_iso_date_is_rejected(self) -> None:
        with self.assertRaises(reminder_service.PastDate):
            self._parse("2026-10-19")

    def test_today_with_the_hour_still_ahead_is_kept(self) -> None:
        self.assertEqual(self._parse("2026-10-20", hour=16), date(2026, 10, 20))

    def test_today_with_the_hour_passed_is_rejected(self) -> None:
        with self.assertRaises(reminder_service.PastDate):
            self._parse("2026-10-20", hour=13)

    def test_weekday_name_resolves_forward(self) -> None:
        self.assertEqual(self._parse("thursday"), date(2026, 10, 22))

    def test_weekday_name_today_but_the_hour_passed_rolls_a_week(self) -> None:
        self.assertEqual(self._parse("tuesday", hour=13), date(2026, 10, 27))

    def test_weekday_name_today_with_the_hour_ahead_is_kept(self) -> None:
        now = utc(2026, 10, 20, 9)
        self.assertEqual(
            reminder_service.parse_one_off_date(
                "tuesday", hour=15, timezone="UTC", now_utc=now
            ),
            date(2026, 10, 20),
        )

    def test_unparseable_value_is_rejected(self) -> None:
        with self.assertRaises(reminder_service.InvalidDate):
            self._parse("someday")

    def test_empty_value_is_rejected(self) -> None:
        with self.assertRaises(reminder_service.MissingDate):
            self._parse("  ")


class NextDailyTests(unittest.TestCase):
    def test_before_the_hour_keeps_today(self) -> None:
        self.assertEqual(
            reminder_service.next_daily(utc(2026, 10, 20, 9), 15, "UTC"),
            utc(2026, 10, 20, 15),
        )

    def test_after_the_hour_moves_to_tomorrow(self) -> None:
        self.assertEqual(
            reminder_service.next_daily(utc(2026, 10, 20, 16), 15, "UTC"),
            utc(2026, 10, 21, 15),
        )

    def test_exactly_on_the_hour_moves_forward(self) -> None:
        self.assertEqual(
            reminder_service.next_daily(utc(2026, 10, 20, 15), 15, "UTC"),
            utc(2026, 10, 21, 15),
        )

    def test_dst_change_keeps_the_local_wall_time(self) -> None:
        # 2026-03-08 03:00 in New York is EDT (UTC-4), one hour earlier in
        # UTC than the day before, which was EST (UTC-5).
        now = utc(2026, 3, 7, 12)  # 07:00 EST, past 3AM
        self.assertEqual(
            reminder_service.next_daily(now, 3, "America/New_York"),
            utc(2026, 3, 8, 7),
        )


class NextWeekdayTests(unittest.TestCase):
    def test_earlier_weekday_moves_forward(self) -> None:
        # Tuesday -> Sunday (weekday 6)
        self.assertEqual(
            reminder_service.next_weekday(6, utc(2026, 10, 20, 16), 15, "UTC"),
            utc(2026, 10, 25, 15),
        )

    def test_same_weekday_before_the_hour_is_kept(self) -> None:
        self.assertEqual(
            reminder_service.next_weekday(1, utc(2026, 10, 20, 9), 15, "UTC"),
            utc(2026, 10, 20, 15),
        )

    def test_same_weekday_after_the_hour_rolls_a_week(self) -> None:
        self.assertEqual(
            reminder_service.next_weekday(1, utc(2026, 10, 20, 16), 15, "UTC"),
            utc(2026, 10, 27, 15),
        )

    def test_past_weekday_in_the_same_week(self) -> None:
        # Monday of the same week, after the hour -> next Monday.
        self.assertEqual(
            reminder_service.next_weekday(0, utc(2026, 10, 20, 16), 15, "UTC"),
            utc(2026, 10, 26, 15),
        )


class NextMonthlyTests(unittest.TestCase):
    def test_mid_month_target(self) -> None:
        self.assertEqual(
            reminder_service.next_monthly(20, utc(2026, 10, 10, 9), 15, "UTC"),
            utc(2026, 10, 20, 15),
        )

    def test_target_already_passed_moves_to_next_month(self) -> None:
        self.assertEqual(
            reminder_service.next_monthly(20, utc(2026, 10, 21, 9), 15, "UTC"),
            utc(2026, 11, 20, 15),
        )

    def test_day_30_clamps_in_february(self) -> None:
        self.assertEqual(
            reminder_service.next_monthly(30, utc(2026, 2, 1, 9), 15, "UTC"),
            utc(2026, 2, 28, 15),
        )

    def test_day_30_clamps_to_29_in_a_leap_february(self) -> None:
        self.assertEqual(
            reminder_service.next_monthly(30, utc(2028, 2, 1, 9), 15, "UTC"),
            utc(2028, 2, 29, 15),
        )

    def test_day_31_clamps_in_april(self) -> None:
        self.assertEqual(
            reminder_service.next_monthly(31, utc(2026, 4, 1, 9), 15, "UTC"),
            utc(2026, 4, 30, 15),
        )

    def test_clamped_run_does_not_block_the_next_month(self) -> None:
        # After February's clamped 28th, March still fires on the 30th.
        self.assertEqual(
            reminder_service.next_monthly(30, utc(2026, 2, 28, 16), 15, "UTC"),
            utc(2026, 3, 30, 15),
        )

    def test_month_end_offset_zero_fires_on_the_last_day(self) -> None:
        self.assertEqual(
            reminder_service.next_monthly(
                None, utc(2026, 10, 10, 9), 15, "UTC", month_end_offset=0
            ),
            utc(2026, 10, 31, 15),
        )

    def test_month_end_offset_counts_back_from_the_last_day(self) -> None:
        self.assertEqual(
            reminder_service.next_monthly(
                None, utc(2026, 10, 10, 9), 15, "UTC", month_end_offset=2
            ),
            utc(2026, 10, 29, 15),
        )

    def test_month_end_target_already_passed_rolls_forward(self) -> None:
        self.assertEqual(
            reminder_service.next_monthly(
                None, utc(2026, 10, 31, 16), 15, "UTC", month_end_offset=0
            ),
            utc(2026, 11, 30, 15),
        )

    def test_month_end_offset_follows_the_real_month_length(self) -> None:
        self.assertEqual(
            reminder_service.next_monthly(
                None, utc(2026, 2, 1, 9), 15, "UTC", month_end_offset=2
            ),
            utc(2026, 2, 26, 15),
        )
        self.assertEqual(
            reminder_service.next_monthly(
                None, utc(2028, 2, 1, 9), 15, "UTC", month_end_offset=2
            ),
            utc(2028, 2, 27, 15),
        )


class NextIntervalTests(unittest.TestCase):
    def test_keeps_the_original_cadence(self) -> None:
        now = utc(2026, 10, 20, 15, 5)
        self.assertEqual(
            reminder_service.next_interval(
                date(2026, 10, 20), now, 15, "UTC", 3
            ),
            utc(2026, 10, 23, 15),
        )

    def test_jumps_forward_after_a_long_outage(self) -> None:
        # Due Oct 1; the bot comes back Oct 4 at 2AM, so Oct 4 3PM is next.
        now = utc(2026, 10, 4, 2)
        self.assertEqual(
            reminder_service.next_interval(
                date(2026, 10, 1), now, 15, "UTC", 3
            ),
            utc(2026, 10, 4, 15),
        )

    def test_still_late_in_the_day(self) -> None:
        # Due Oct 1; back Oct 4 at 5PM, so the Oct 4 slot has passed too.
        now = utc(2026, 10, 4, 17)
        self.assertEqual(
            reminder_service.next_interval(
                date(2026, 10, 1), now, 15, "UTC", 3
            ),
            utc(2026, 10, 7, 15),
        )


class ComputeNextRunTests(unittest.TestCase):
    def test_once_expires_after_firing(self) -> None:
        row = reminder(kind=reminder_service.KIND_ONCE, run_date=date(2026, 10, 20))
        self.assertIsNone(
            reminder_service.compute_next_run(row, utc(2026, 10, 20, 16))
        )

    def test_once_returns_its_scheduled_moment(self) -> None:
        row = reminder(kind=reminder_service.KIND_ONCE, run_date=date(2026, 10, 22))
        self.assertEqual(
            reminder_service.compute_next_run(row, utc(2026, 10, 20, 16)),
            utc(2026, 10, 22, 15),
        )

    def test_once_with_a_minute_precise_time(self) -> None:
        row = reminder(
            kind=reminder_service.KIND_ONCE,
            run_at=utc(2026, 10, 20, 16, 30),
        )
        self.assertEqual(
            reminder_service.compute_next_run(row, utc(2026, 10, 20, 16)),
            utc(2026, 10, 20, 16, 30),
        )
        self.assertIsNone(
            reminder_service.compute_next_run(row, utc(2026, 10, 20, 16, 30))
        )

    def test_daily_advances_from_now_not_from_the_run(self) -> None:
        # Fired two hours late; the next one is tomorrow, not today again.
        row = reminder()
        self.assertEqual(
            reminder_service.compute_next_run(row, utc(2026, 10, 20, 17)),
            utc(2026, 10, 21, 15),
        )

    def test_weekly(self) -> None:
        row = reminder(kind=reminder_service.KIND_WEEKLY, weekday=6)
        self.assertEqual(
            reminder_service.compute_next_run(row, utc(2026, 10, 20, 16)),
            utc(2026, 10, 25, 15),
        )

    def test_monthly(self) -> None:
        row = reminder(kind=reminder_service.KIND_MONTHLY, day_of_month=30)
        self.assertEqual(
            reminder_service.compute_next_run(row, utc(2026, 2, 28, 16)),
            utc(2026, 3, 30, 15),
        )

    def test_monthly_with_a_month_end_offset(self) -> None:
        row = reminder(
            kind=reminder_service.KIND_MONTHLY, month_end_offset=0
        )
        self.assertEqual(
            reminder_service.compute_next_run(row, utc(2026, 10, 20, 16)),
            utc(2026, 10, 31, 15),
        )

    def test_interval_keeps_its_cadence_from_the_run(self) -> None:
        row = reminder(
            kind=reminder_service.KIND_INTERVAL,
            interval_days=3,
            next_run_at=utc(2026, 10, 20, 15),
        )
        self.assertEqual(
            reminder_service.compute_next_run(row, utc(2026, 10, 20, 15, 1)),
            utc(2026, 10, 23, 15),
        )

    def test_kinds_without_their_field_never_fire_again(self) -> None:
        self.assertIsNone(
            reminder_service.compute_next_run(
                reminder(kind=reminder_service.KIND_WEEKLY), utc(2026, 10, 20)
            )
        )
        self.assertIsNone(
            reminder_service.compute_next_run(
                reminder(kind=reminder_service.KIND_MONTHLY), utc(2026, 10, 20)
            )
        )
        self.assertIsNone(
            reminder_service.compute_next_run(
                reminder(kind=reminder_service.KIND_INTERVAL), utc(2026, 10, 20)
            )
        )


class StalenessTests(unittest.TestCase):
    def test_fresh_enough_to_post(self) -> None:
        self.assertFalse(
            reminder_service.is_stale(utc(2026, 10, 20, 14), utc(2026, 10, 20, 15))
        )

    def test_exactly_24h_old_is_still_posted(self) -> None:
        self.assertFalse(
            reminder_service.is_stale(utc(2026, 10, 19, 15), utc(2026, 10, 20, 15))
        )

    def test_older_than_24h_is_skipped(self) -> None:
        self.assertTrue(
            reminder_service.is_stale(utc(2026, 10, 19, 14), utc(2026, 10, 20, 15))
        )

    def test_snoozes_fire_one_hour_later(self) -> None:
        self.assertEqual(
            reminder_service.snooze_due_at(utc(2026, 10, 20, 15)),
            utc(2026, 10, 20, 16),
        )


class ValidateTests(unittest.TestCase):
    def test_message_is_trimmed(self) -> None:
        self.assertEqual(
            reminder_service.validate_message("  hello  "), "hello"
        )

    def test_empty_message_is_rejected(self) -> None:
        with self.assertRaises(reminder_service.MissingMessage):
            reminder_service.validate_message("   ")

    def test_overlong_message_is_rejected(self) -> None:
        with self.assertRaises(reminder_service.MessageTooLong):
            reminder_service.validate_message(
                "x" * (reminder_service.MESSAGE_MAX_LENGTH + 1)
            )

    def test_maximum_length_message_is_accepted(self) -> None:
        value = "x" * reminder_service.MESSAGE_MAX_LENGTH
        self.assertEqual(reminder_service.validate_message(value), value)

    def test_hours_must_be_0_to_23(self) -> None:
        self.assertEqual(reminder_service.validate_hour(0), 0)
        self.assertEqual(reminder_service.validate_hour(23), 23)
        for hour in (-1, 24):
            with self.subTest(hour=hour), self.assertRaises(
                reminder_service.InvalidHour
            ):
                reminder_service.validate_hour(hour)


class ValidateMonthlyTargetTests(unittest.TestCase):
    def test_a_fixed_day_clears_the_offset(self) -> None:
        self.assertEqual(
            reminder_service.validate_monthly_target(15, None), (15, None)
        )

    def test_an_offset_clears_the_fixed_day(self) -> None:
        self.assertEqual(
            reminder_service.validate_monthly_target(None, 2), (None, 2)
        )

    def test_offset_zero_means_the_last_day(self) -> None:
        self.assertEqual(
            reminder_service.validate_monthly_target(None, 0), (None, 0)
        )

    def test_both_targets_are_rejected(self) -> None:
        with self.assertRaises(reminder_service.BothDayAndMonthEnd):
            reminder_service.validate_monthly_target(15, 2)

    def test_no_target_is_rejected(self) -> None:
        with self.assertRaises(reminder_service.MissingDay):
            reminder_service.validate_monthly_target(None, None)

    def test_fixed_day_must_be_1_to_31(self) -> None:
        for day in (0, 32):
            with self.subTest(day=day), self.assertRaises(
                reminder_service.InvalidDay
            ):
                reminder_service.validate_monthly_target(day, None)

    def test_offset_must_be_0_to_27(self) -> None:
        for offset in (-1, 28):
            with self.subTest(offset=offset), self.assertRaises(
                reminder_service.InvalidMonthEnd
            ):
                reminder_service.validate_monthly_target(None, offset)


class ScheduleSummaryTests(unittest.TestCase):
    def test_daily(self) -> None:
        text = reminder_cmds.schedule_summary(reminder(hour=15))
        self.assertEqual(
            text,
            t("reminders.schedule.daily", hour=t("reminders.shared.hour_value_pm", hour=3)),
        )

    def test_weekly(self) -> None:
        text = reminder_cmds.schedule_summary(
            reminder(kind=reminder_service.KIND_WEEKLY, weekday=6, hour=9)
        )
        self.assertEqual(
            text,
            t(
                "reminders.schedule.weekly",
                weekday=t("components.weekday.sunday"),
                hour=t("reminders.shared.hour_value_am", hour=9),
            ),
        )

    def test_monthly_uses_an_ordinal(self) -> None:
        text = reminder_cmds.schedule_summary(
            reminder(kind=reminder_service.KIND_MONTHLY, day_of_month=30, hour=15)
        )
        self.assertEqual(
            text,
            t(
                "reminders.schedule.monthly",
                day=30,
                suffix=t("reminders.shared.suffix.th"),
                hour=t("reminders.shared.hour_value_pm", hour=3),
            ),
        )

    def test_monthly_ordinal_suffixes(self) -> None:
        for day, suffix in ((1, "st"), (2, "nd"), (3, "rd"), (11, "th"),
                            (21, "st"), (22, "nd"), (23, "rd"), (31, "st")):
            with self.subTest(day=day):
                self.assertEqual(
                    reminder_cmds._ordinal(day), t(f"reminders.shared.suffix.{suffix}")
                )

    def test_monthly_month_end_wordings(self) -> None:
        hour = t("reminders.shared.hour_value_pm", hour=3)
        last = reminder_cmds.schedule_summary(
            reminder(
                kind=reminder_service.KIND_MONTHLY, month_end_offset=0, hour=15
            )
        )
        self.assertEqual(last, t("reminders.schedule.monthly_last", hour=hour))
        one = reminder_cmds.schedule_summary(
            reminder(
                kind=reminder_service.KIND_MONTHLY, month_end_offset=1, hour=15
            )
        )
        self.assertEqual(
            one, t("reminders.schedule.monthly_before", days=1, hour=hour)
        )
        two = reminder_cmds.schedule_summary(
            reminder(
                kind=reminder_service.KIND_MONTHLY, month_end_offset=2, hour=15
            )
        )
        self.assertEqual(
            two, t("reminders.schedule.monthly_before_plural", days=2, hour=hour)
        )

    def test_interval_singular_and_plural(self) -> None:
        one = reminder_cmds.schedule_summary(
            reminder(kind=reminder_service.KIND_INTERVAL, interval_days=1, hour=15)
        )
        three = reminder_cmds.schedule_summary(
            reminder(kind=reminder_service.KIND_INTERVAL, interval_days=3, hour=15)
        )
        hour = t("reminders.shared.hour_value_pm", hour=3)
        self.assertEqual(one, t("reminders.schedule.interval", days=1, hour=hour))
        self.assertEqual(
            three, t("reminders.schedule.interval_plural", days=3, hour=hour)
        )

    def test_once_embeds_the_next_run_timestamp(self) -> None:
        text = reminder_cmds.schedule_summary(
            reminder(
                kind=reminder_service.KIND_ONCE,
                run_date=date(2026, 10, 22),
                next_run_at=datetime(2026, 10, 22, 15, tzinfo=timezone.utc),
            )
        )
        self.assertEqual(
            text,
            t(
                "reminders.schedule.once",
                when=discord.utils.format_dt(
                    datetime(2026, 10, 22, 15, tzinfo=timezone.utc), "f"
                ),
            ),
        )

    def test_description_is_empty_for_one_offs_only(self) -> None:
        self.assertIsNone(
            reminder_cmds.schedule_description(
                reminder(kind=reminder_service.KIND_ONCE)
            )
        )
        self.assertIsNotNone(reminder_cmds.schedule_description(reminder()))


class EmbedTests(unittest.TestCase):
    class Client:
        transparent_embed_image = None
        default_embed_color = None

    def test_reminder_embed_titles_the_task_text(self) -> None:
        embed = reminder_cmds.reminder_embed(self.Client(), "Water the plants")
        self.assertEqual(embed.title, "Water the plants")
        self.assertIsNone(embed.footer.text)

    def test_reminder_embed_keeps_the_schedule_description(self) -> None:
        embed = reminder_cmds.reminder_embed(
            self.Client(), "Water", description="Every day"
        )
        self.assertEqual(embed.description, "Every day")
        self.assertIsNone(embed.footer.text)

    def test_confirmation_has_no_id_footer(self) -> None:
        embed = reminder_cmds._confirmation_embed(
            self.Client(), reminder(), "Saved"
        )
        self.assertIsNone(embed.footer.text)

    def test_list_entry_has_no_id_footer(self) -> None:
        member = SimpleNamespace(
            display_name="Alice",
            display_avatar=SimpleNamespace(url="https://example.com/a.png"),
        )
        embed = reminder_cmds._list_embed(
            self.Client(), reminder(), member
        )
        self.assertIsNone(embed.footer.text)

    def test_completed_embed_crosses_out_the_task(self) -> None:
        embed = reminder_cmds.completed_embed(self.Client(), "Water the plants")
        self.assertEqual(
            embed.title, t("reminders.post.completed_title", task="Water the plants")
        )
        self.assertEqual(
            embed.description, t("reminders.post.completed_description")
        )
        self.assertIsNone(embed.footer.text)
        self.assertEqual(embed.colour, discord.Colour.green())

    def test_snoozed_embed_reports_the_repost_time(self) -> None:
        when = datetime(2026, 10, 20, 16, tzinfo=timezone.utc)
        embed = reminder_cmds.snoozed_embed(self.Client(), "Water", when)
        self.assertEqual(embed.title, "Water")
        self.assertEqual(
            embed.description,
            t(
                "reminders.post.snoozed_description",
                time=discord.utils.format_dt(when, "t"),
            ),
        )
        self.assertIsNone(embed.footer.text)


class ReminderActionsViewTests(unittest.TestCase):
    def test_has_the_two_expected_buttons(self) -> None:
        view = reminder_cmds.ReminderActionsView(1, 42, "Task")
        complete, snooze = view.children
        self.assertEqual(complete.label, t("reminders.buttons.complete"))
        self.assertEqual(complete.emoji.name, t("reminders.buttons.complete_emoji"))
        self.assertEqual(complete.style, discord.ButtonStyle.success)
        self.assertEqual(snooze.label, t("reminders.buttons.snooze"))
        self.assertEqual(snooze.emoji.name, t("reminders.buttons.snooze_emoji"))
        self.assertEqual(snooze.style, discord.ButtonStyle.secondary)

    def test_default_lifetime_is_two_hours(self) -> None:
        view = reminder_cmds.ReminderActionsView(1, 42, "Task")
        self.assertEqual(view.timeout, reminder_cmds.BUTTON_LIFETIME_SECONDS)
        self.assertEqual(reminder_cmds.BUTTON_LIFETIME_SECONDS, 2 * 60 * 60)


class ParseTimeTests(unittest.TestCase):
    def test_meridiem_forms(self) -> None:
        self.assertEqual(reminder_service.parse_time("2pm"), 14)
        self.assertEqual(reminder_service.parse_time("2 pm"), 14)
        self.assertEqual(reminder_service.parse_time("2:00pm"), 14)
        self.assertEqual(reminder_service.parse_time("9am"), 9)
        self.assertEqual(reminder_service.parse_time("12am"), 0)
        self.assertEqual(reminder_service.parse_time("12pm"), 12)

    def test_twenty_four_hour_forms(self) -> None:
        self.assertEqual(reminder_service.parse_time("14:00"), 14)
        self.assertEqual(reminder_service.parse_time("23"), 23)
        self.assertEqual(reminder_service.parse_time("00:00"), 0)

    def test_ambiguous_plain_hour_is_rejected(self) -> None:
        for value in ("2", "9", "12"):
            with self.subTest(value=value), self.assertRaises(
                reminder_service.InvalidTime
            ):
                reminder_service.parse_time(value)

    def test_off_the_hour_is_rejected(self) -> None:
        with self.assertRaises(reminder_service.InvalidTime):
            reminder_service.parse_time("2:30pm")

    def test_out_of_range_is_rejected(self) -> None:
        for value in ("24", "25:00", "13pm", "0pm", "abc", ""):
            with self.subTest(value=value), self.assertRaises(
                reminder_service.InvalidTime
            ):
                reminder_service.parse_time(value)


class ParseScheduleTests(unittest.TestCase):
    NOW = utc(2026, 10, 20, 9)  # Tuesday

    def _parse(self, kind: str, text: str) -> reminder_service.ParsedSchedule:
        return reminder_service.parse_schedule(
            kind, text, timezone="UTC", now=self.NOW
        )

    def test_once_iso_date(self) -> None:
        parsed = self._parse(
            reminder_service.KIND_ONCE, "2026-10-29 at 14:00"
        )
        self.assertEqual(parsed.hour, 14)
        self.assertEqual(parsed.run_date, date(2026, 10, 29))

    def test_once_weekday(self) -> None:
        parsed = self._parse(reminder_service.KIND_ONCE, "thursday at 2pm")
        self.assertEqual(parsed.run_date, date(2026, 10, 22))

    def test_once_tomorrow(self) -> None:
        parsed = self._parse(reminder_service.KIND_ONCE, "tomorrow at 9am")
        self.assertEqual(parsed.run_date, date(2026, 10, 21))
        self.assertEqual(parsed.hour, 9)

    def test_once_past_date_is_rejected(self) -> None:
        with self.assertRaises(reminder_service.PastDate):
            self._parse(reminder_service.KIND_ONCE, "2026-10-19 at 14:00")

    def test_once_in_minutes(self) -> None:
        parsed = self._parse(reminder_service.KIND_ONCE, "in 30 minutes")
        self.assertEqual(
            parsed.run_at, self.NOW + timedelta(minutes=30)
        )
        self.assertEqual(parsed.hour, 9)
        self.assertEqual(parsed.run_date, date(2026, 10, 20))

    def test_once_in_one_minute(self) -> None:
        parsed = self._parse(reminder_service.KIND_ONCE, "in 1 minute")
        self.assertEqual(
            parsed.run_at, self.NOW + timedelta(minutes=1)
        )

    def test_once_minutes_must_be_within_the_allowed_range(self) -> None:
        for text in (
            "in 0 minutes",
            f"in {reminder_service.MINUTES_MAX + 1} minutes",
        ):
            with self.subTest(text=text), self.assertRaises(
                reminder_service.InvalidMinutes
            ):
                self._parse(reminder_service.KIND_ONCE, text)
        self.assertIsNotNone(
            self._parse(
                reminder_service.KIND_ONCE,
                f"in {reminder_service.MINUTES_MAX} minutes",
            ).run_at
        )

    def test_minutes_are_only_accepted_for_once(self) -> None:
        for kind in (
            reminder_service.KIND_DAILY,
            reminder_service.KIND_WEEKLY,
            reminder_service.KIND_MONTHLY,
            reminder_service.KIND_INTERVAL,
        ):
            with self.subTest(kind=kind), self.assertRaises(
                reminder_service.InvalidSchedule
            ):
                self._parse(kind, "in 30 minutes")

    def test_daily(self) -> None:
        self.assertEqual(
            self._parse(reminder_service.KIND_DAILY, "at 2pm").hour, 14
        )
        self.assertEqual(
            self._parse(reminder_service.KIND_DAILY, "every day at 2pm").hour,
            14,
        )
        self.assertEqual(
            self._parse(reminder_service.KIND_DAILY, "everyday at 2pm").hour,
            14,
        )

    def test_weekly(self) -> None:
        for text in (
            "on monday at 2pm",
            "every monday at 2pm",
            "monday at 2pm",
            "on mon at 2pm",
        ):
            with self.subTest(text=text):
                parsed = self._parse(reminder_service.KIND_WEEKLY, text)
                self.assertEqual(parsed.hour, 14)
                self.assertEqual(parsed.weekday, 0)

    def test_monthly_fixed_day(self) -> None:
        for text in (
            "on the 15th at 2pm",
            "the 15th at 2pm",
            "15 at 2pm",
            "every month on the 15th at 2pm",
        ):
            with self.subTest(text=text):
                parsed = self._parse(reminder_service.KIND_MONTHLY, text)
                self.assertEqual(parsed.hour, 14)
                self.assertEqual(parsed.day_of_month, 15)
                self.assertIsNone(parsed.month_end_offset)

    def test_monthly_last_day(self) -> None:
        for text in (
            "on the last day of the month at 2pm",
            "last day at 2pm",
            "every month on the last day at 2pm",
        ):
            with self.subTest(text=text):
                parsed = self._parse(reminder_service.KIND_MONTHLY, text)
                self.assertEqual(parsed.hour, 14)
                self.assertEqual(parsed.month_end_offset, 0)
                self.assertIsNone(parsed.day_of_month)

    def test_monthly_days_before_the_end(self) -> None:
        for text, days in (
            ("2 days before the end of the month at 2pm", 2),
            ("3 days before the end of the month at 2pm", 3),
            ("2 days before the end at 2pm", 2),
        ):
            with self.subTest(text=text):
                parsed = self._parse(reminder_service.KIND_MONTHLY, text)
                self.assertEqual(parsed.hour, 14)
                self.assertEqual(parsed.month_end_offset, days)
                self.assertIsNone(parsed.day_of_month)

    def test_interval(self) -> None:
        for text, days in (
            ("every 2 days at 2pm", 2),
            ("every 1 day at 9am", 1),
        ):
            with self.subTest(text=text):
                parsed = self._parse(reminder_service.KIND_INTERVAL, text)
                self.assertEqual(parsed.interval_days, days)

    def test_blank_phrase_is_rejected(self) -> None:
        with self.assertRaises(reminder_service.MissingSchedule):
            self._parse(reminder_service.KIND_DAILY, "   ")

    def test_wrong_phrasing_is_rejected(self) -> None:
        for kind, text in (
            (reminder_service.KIND_DAILY, "on monday at 2pm"),
            (reminder_service.KIND_WEEKLY, "at 2pm"),
            (reminder_service.KIND_MONTHLY, "every 2 days at 2pm"),
            (reminder_service.KIND_INTERVAL, "on the last day at 2pm"),
            (reminder_service.KIND_ONCE, "at 2pm"),
        ):
            with self.subTest(kind=kind, text=text), self.assertRaises(
                reminder_service.InvalidSchedule
            ):
                self._parse(kind, text)

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(reminder_service.InvalidKind):
            self._parse("sometimes", "at 2pm")


class ScheduleExampleTests(unittest.TestCase):
    def test_every_kind_has_examples(self) -> None:
        self.assertEqual(
            set(reminder_cmds.SCHEDULE_EXAMPLES), set(reminder_service.KINDS)
        )

    def test_examples_parse_with_their_kind(self) -> None:
        now = utc(2026, 10, 20, 9)
        for kind, examples in reminder_cmds.SCHEDULE_EXAMPLES.items():
            for example in examples:
                with self.subTest(kind=kind, example=example):
                    reminder_service.parse_schedule(
                        kind, example, timezone="UTC", now=now
                    )

    def test_help_keys_exist_for_every_kind(self) -> None:
        for kind in reminder_service.KINDS:
            with self.subTest(kind=kind):
                t(f"reminders.schedule_help.{kind}")


class ScheduleAutocompleteTests(unittest.TestCase):
    @staticmethod
    def _interaction(options: list[dict]) -> SimpleNamespace:
        return SimpleNamespace(data={"options": options})

    def test_offers_the_selected_kinds_examples(self) -> None:
        interaction = self._interaction(
            [{"name": "repeat", "value": reminder_service.KIND_MONTHLY}]
        )
        choices = asyncio.run(
            reminder_cmds.schedule_autocomplete(interaction, "")
        )
        self.assertEqual(
            [choice.value for choice in choices],
            list(reminder_cmds.SCHEDULE_EXAMPLES[reminder_service.KIND_MONTHLY]),
        )

    def test_filters_by_what_was_typed(self) -> None:
        interaction = self._interaction(
            [{"name": "repeat", "value": reminder_service.KIND_MONTHLY}]
        )
        choices = asyncio.run(
            reminder_cmds.schedule_autocomplete(interaction, "last")
        )
        self.assertEqual(
            [choice.value for choice in choices],
            ["on the last day of the month at 2pm"],
        )

    def test_add_defaults_to_once_examples(self) -> None:
        interaction = self._interaction([{"name": "message", "value": "Hi"}])
        choices = asyncio.run(
            reminder_cmds.schedule_autocomplete(interaction, "")
        )
        self.assertEqual(
            [choice.value for choice in choices],
            list(reminder_cmds.SCHEDULE_EXAMPLES[reminder_service.KIND_ONCE]),
        )

    def test_edit_without_repeat_offers_every_kind(self) -> None:
        interaction = self._interaction([{"name": "reminder", "value": 3}])
        choices = asyncio.run(
            reminder_cmds.schedule_autocomplete(interaction, "")
        )
        expected = [
            example
            for group in reminder_cmds.SCHEDULE_EXAMPLES.values()
            for example in group
        ]
        self.assertEqual([choice.value for choice in choices], expected)


class TextKeyTests(unittest.TestCase):
    def test_error_keys_all_exist(self) -> None:
        for error in (
            reminder_service.MissingMessage,
            reminder_service.MessageTooLong,
            reminder_service.InvalidHour,
            reminder_service.InvalidTimezone,
            reminder_service.InvalidKind,
            reminder_service.MissingDate,
            reminder_service.InvalidDate,
            reminder_service.PastDate,
            reminder_service.InvalidWeekday,
            reminder_service.MissingWeekday,
            reminder_service.MissingDay,
            reminder_service.InvalidDay,
            reminder_service.BothDayAndMonthEnd,
            reminder_service.InvalidMonthEnd,
            reminder_service.MissingSchedule,
            reminder_service.InvalidSchedule,
            reminder_service.InvalidTime,
            reminder_service.InvalidMinutes,
            reminder_service.MissingInterval,
            reminder_service.InvalidInterval,
            reminder_service.ReminderNotFound,
        ):
            with self.subTest(error=error.__name__):
                t(error.key)


if __name__ == "__main__":
    unittest.main()
