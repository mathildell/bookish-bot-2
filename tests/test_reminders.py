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
from unittest.mock import AsyncMock

import discord

from bot.commands import reminders as reminder_cmds
from bot.services import reminders as reminder_service
from bot.services.text import ordinal
from bot.texts import t


def utc(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute)


def reminder(**kwargs) -> SimpleNamespace:
    defaults = dict(
        id=1,
        guild_id=1,
        last_run_at=None,
        user_id=10,
        channel_id=20,
        message="Do the thing",
        timezone="UTC",
        kind=reminder_service.KIND_DAILY,
        hour=15,
        minute=0,
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

    def test_snooze_due_at_adds_the_delay(self) -> None:
        self.assertEqual(
            reminder_service.snooze_due_at(
                utc(2026, 10, 20, 15), timedelta(hours=2)
            ),
            utc(2026, 10, 20, 17),
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
            t("reminders.schedule.daily", hour=t("reminders.shared.time_pm", hour=3, minute="00")),
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
                hour=t("reminders.shared.time_am", hour=9, minute="00"),
            ),
        )

    def test_minutes_are_shown(self) -> None:
        text = reminder_cmds.schedule_summary(reminder(hour=15, minute=30))
        self.assertIn("3:30 PM", text)

    def test_week_based_interval(self) -> None:
        text = reminder_cmds.schedule_summary(
            reminder(
                kind=reminder_service.KIND_INTERVAL,
                interval_days=14,
                weekday=5,
                hour=15,
            )
        )
        self.assertEqual(
            text,
            t(
                "reminders.schedule.interval_weeks",
                weeks=2,
                weekday=t("components.weekday.saturday"),
                hour="3:00 PM",
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
                day=ordinal(30),
                hour=t("reminders.shared.time_pm", hour=3, minute="00"),
            ),
        )

    def test_monthly_ordinal_suffixes(self) -> None:
        for day, suffix in ((1, "st"), (2, "nd"), (3, "rd"), (11, "th"),
                            (21, "st"), (22, "nd"), (23, "rd"), (31, "st")):
            with self.subTest(day=day):
                self.assertEqual(ordinal(day), f"{day}{suffix}")

    def test_monthly_month_end_wordings(self) -> None:
        hour = t("reminders.shared.time_pm", hour=3, minute="00")
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
        hour = t("reminders.shared.time_pm", hour=3, minute="00")
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


class SnoozedTitleTests(unittest.TestCase):
    def test_snoozed_title_prefixes_once(self) -> None:
        title = reminder_cmds.snoozed_title("Water")
        self.assertEqual(
            title, t("reminders.post.snoozed_title", task="Water")
        )
        self.assertEqual(reminder_cmds.snoozed_title(title), title)


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


class SnoozeStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_snooze_button_opens_the_modal(self) -> None:
        message = SimpleNamespace(
            content="<@42>",
            embeds=[SimpleNamespace(title="Task")],
            channel=SimpleNamespace(id=7),
        )
        interaction = SimpleNamespace(
            client=SimpleNamespace(view_messages=None),
            message=message,
            user=SimpleNamespace(id=42, roles=[]),
            channel_id=7,
            response=SimpleNamespace(send_modal=AsyncMock()),
        )
        view = reminder_cmds.ReminderActionsView(1, 42, "Task")
        await view.start_snooze(interaction)
        interaction.response.send_modal.assert_awaited_once()
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, reminder_cmds.SnoozeModal)
        self.assertEqual(modal.reminder_id, 1)
        self.assertEqual(modal.owner_id, 42)
        self.assertEqual(modal.channel_id, 7)
        self.assertEqual(modal.task_text, "Task")
        self.assertIs(modal.original_message, message)


class SnoozeModalTests(unittest.IsolatedAsyncioTestCase):
    def _modal(self) -> reminder_cmds.SnoozeModal:
        return reminder_cmds.SnoozeModal(
            reminder_id=1,
            owner_id=42,
            role_id=None,
            task_text="Task",
            channel_id=7,
            original_message=None,
        )

    def _interaction(self) -> SimpleNamespace:
        service = SimpleNamespace(snooze=AsyncMock())
        return SimpleNamespace(
            client=SimpleNamespace(reminders=service),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

    def test_modal_offers_amount_and_units(self) -> None:
        modal = self._modal()
        self.assertEqual(modal.title, t("reminders.snooze.modal_title"))
        self.assertEqual(
            modal.timeout, reminder_cmds.SNOOZE_MODAL_LIFETIME_SECONDS
        )
        self.assertEqual(len(modal.children), 2)
        amount = modal.amount_input.component
        self.assertIsInstance(amount, discord.ui.TextInput)
        self.assertEqual(amount.default, "1")
        self.assertEqual(amount.max_length, 2)
        unit = modal.unit_input.component
        self.assertIsInstance(unit, discord.ui.Select)
        self.assertEqual(
            [option.value for option in unit.options],
            list(reminder_cmds.SNOOZE_UNITS),
        )
        self.assertEqual(
            [option.label for option in unit.options],
            [
                t(f"reminders.snooze.units.{unit}")
                for unit in reminder_cmds.SNOOZE_UNITS
            ],
        )
        self.assertEqual(reminder_cmds.SNOOZE_MAX_AMOUNT, 30)

    async def test_invalid_amount_is_rejected(self) -> None:
        modal = self._modal()
        modal.amount_input.component._value = "31"
        interaction = self._interaction()
        await modal.on_submit(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            t("reminders.snooze.invalid_amount", max=30),
            ephemeral=True,
        )
        interaction.client.reminders.snooze.assert_not_awaited()

    async def test_submit_snoozes_for_the_chosen_delay(self) -> None:
        modal = self._modal()
        modal.amount_input.component._value = "2"
        modal.unit_input.component._values = ["hours"]
        interaction = self._interaction()
        await modal.on_submit(interaction)
        interaction.client.reminders.snooze.assert_awaited_once_with(
            reminder_id=1,
            user_id=42,
            channel_id=7,
            message="Task",
            delay=timedelta(hours=2),
            role_id=None,
        )
        interaction.response.send_message.assert_awaited_once_with(
            t(
                "reminders.snooze.success",
                amount=2,
                unit=t("reminders.snooze.unit.hours"),
            ),
            ephemeral=True,
        )


class ReminderPermissionTests(unittest.IsolatedAsyncioTestCase):
    def _interaction(self, user, content: str):
        return SimpleNamespace(
            client=SimpleNamespace(view_messages=None),
            message=SimpleNamespace(content=content, embeds=[]),
            user=user,
            response=SimpleNamespace(send_message=AsyncMock()),
        )

    async def test_owner_may_use_the_buttons(self) -> None:
        view = reminder_cmds.ReminderActionsView(1, 42, "Task")
        interaction = self._interaction(SimpleNamespace(id=42), "<@42>")
        self.assertTrue(await view.interaction_check(interaction))

    async def test_role_holder_may_use_the_buttons(self) -> None:
        view = reminder_cmds.ReminderActionsView(1, 42, "Task")
        user = SimpleNamespace(id=99, roles=[SimpleNamespace(id=5)])
        interaction = self._interaction(user, "<@42> <@&5>")
        self.assertTrue(await view.interaction_check(interaction))

    async def test_other_members_are_denied(self) -> None:
        view = reminder_cmds.ReminderActionsView(1, 42, "Task")
        user = SimpleNamespace(id=99, roles=[])
        interaction = self._interaction(user, "<@42> <@&5>")
        self.assertFalse(await view.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once_with(
            t("reminders.errors.not_allowed"), ephemeral=True
        )


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
            reminder_service.InvalidTime,
            reminder_service.InvalidMinutes,
            reminder_service.MissingInterval,
            reminder_service.InvalidInterval,
            reminder_service.ReminderNotFound,
            reminder_service.InvalidMonthInterval,
            reminder_service.InvalidMonthWeek,
            reminder_service.InvalidMonth,
            reminder_service.InvalidRolling,
        ):
            with self.subTest(error=error.__name__):
                t(error.key)


class MergeEditTests(unittest.TestCase):
    NOW = utc(2026, 10, 20, 9)

    def test_message_only_keeps_the_schedule(self) -> None:
        existing = reminder(next_run_at=utc(2026, 10, 21, 15))
        merged = reminder_service.merge_edit(
            existing, message="New text", now=self.NOW
        )
        self.assertEqual(merged.message, "New text")
        self.assertEqual(merged.next_run_at, utc(2026, 10, 21, 15))
        self.assertEqual(merged.kind, reminder_service.KIND_DAILY)

    def test_new_schedule_reschedules(self) -> None:
        merged = reminder_service.merge_edit(
            reminder(),
            schedule=reminder_service.Schedule(
                reminder_service.KIND_WEEKLY, 10, 30, weekdays=(5,)
            ),
            now=self.NOW,
        )
        self.assertEqual(merged.kind, reminder_service.KIND_WEEKLY)
        self.assertEqual((merged.hour, merged.minute), (10, 30))
        self.assertEqual(merged.next_run_at, utc(2026, 10, 24, 10, 30))

    def test_timezone_alone_rereads_the_stored_schedule(self) -> None:
        merged = reminder_service.merge_edit(
            reminder(hour=15), timezone="Europe/Berlin", now=self.NOW
        )
        self.assertEqual(merged.timezone, "Europe/Berlin")
        self.assertEqual(merged.next_run_at, utc(2026, 10, 20, 13))

    def test_unknown_timezone_is_rejected(self) -> None:
        with self.assertRaises(reminder_service.InvalidTimezone):
            reminder_service.merge_edit(
                reminder(), timezone="Narnia/Cair", now=self.NOW
            )

    def test_does_not_touch_the_original(self) -> None:
        existing = reminder()
        reminder_service.merge_edit(existing, message="X", channel_id=99)
        self.assertEqual(existing.message, "Do the thing")
        self.assertEqual(existing.channel_id, 20)


class BuildTests(unittest.TestCase):
    def test_build_validates_without_saving(self) -> None:
        service = reminder_service.ReminderService(1)
        built = service.build(
            user_id=10,
            channel_id=20,
            message="  Hi  ",
            timezone="UTC",
            schedule=reminder_service.Schedule(
                reminder_service.KIND_DAILY, 9, 30
            ),
            now=utc(2026, 10, 20, 9),
        )
        self.assertEqual(built.message, "Hi")
        self.assertEqual(built.next_run_at, utc(2026, 10, 20, 9, 30))

    def test_build_rejects_bad_input(self) -> None:
        service = reminder_service.ReminderService(1)
        schedule = reminder_service.Schedule(reminder_service.KIND_DAILY, 9)
        for fields, error in (
            (dict(message=" ", timezone="UTC"), reminder_service.MissingMessage),
            (dict(message="x", timezone="Nope/Zone"), reminder_service.InvalidTimezone),
        ):
            with self.subTest(error=error.__name__), self.assertRaises(error):
                service.build(
                    user_id=1, channel_id=2, schedule=schedule, **fields
                )


class BroadSummaryTests(unittest.TestCase):
    def test_several_weekdays(self) -> None:
        cases = (
            ((0, 1, 2, 3, 4), t("reminders.schedule.weekdays_all")),
            ((5, 6), t("reminders.schedule.weekends_all")),
            ((0, 3), "Monday and Thursday"),
            ((0, 2, 4), "Monday, Wednesday and Friday"),
        )
        for days, expected in cases:
            with self.subTest(days=days):
                mask = reminder_service.weekdays_to_mask(days)
                text = reminder_cmds.schedule_summary(
                    reminder(
                        kind=reminder_service.KIND_WEEKLY,
                        weekday=days[0],
                        weekdays_mask=mask,
                        hour=9,
                    )
                )
                self.assertIn(expected, text)

    def test_monthly_every_n(self) -> None:
        base = dict(kind=reminder_service.KIND_MONTHLY, hour=10, month_interval=3)
        self.assertIn(
            "Every 3 months on the 1st",
            reminder_cmds.schedule_summary(reminder(day_of_month=1, **base)),
        )
        self.assertIn(
            "Every 3 months on the last day",
            reminder_cmds.schedule_summary(reminder(month_end_offset=0, **base)),
        )
        self.assertIn(
            "Every 3 months 2 days before the end",
            reminder_cmds.schedule_summary(reminder(month_end_offset=2, **base)),
        )

    def test_nth_weekday(self) -> None:
        text = reminder_cmds.schedule_summary(
            reminder(
                kind=reminder_service.KIND_MONTHLY,
                month_week=-1,
                weekday=4,
                hour=9,
            )
        )
        self.assertIn("last Friday", text)
        text = reminder_cmds.schedule_summary(
            reminder(
                kind=reminder_service.KIND_MONTHLY,
                month_week=2,
                weekday=1,
                month_interval=2,
                hour=9,
            )
        )
        self.assertIn("Every 2 months on the second Tuesday", text)

    def test_yearly_and_rolling(self) -> None:
        self.assertIn(
            "Every year on March 4th",
            reminder_cmds.schedule_summary(
                reminder(
                    kind=reminder_service.KIND_YEARLY, month=3, day_of_month=4
                )
            ),
        )
        for minutes, expected in (
            (45, "Every 45 minutes"),
            (60, "Every hour"),
            (120, "Every 2 hours"),
        ):
            with self.subTest(minutes=minutes):
                self.assertEqual(
                    reminder_cmds.schedule_summary(
                        reminder(
                            kind=reminder_service.KIND_ROLLING,
                            interval_minutes=minutes,
                        )
                    ),
                    expected,
                )


class ConfirmViewTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _interaction(user_id: int = 5) -> SimpleNamespace:
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            client=SimpleNamespace(),
            response=SimpleNamespace(
                edit_message=AsyncMock(), send_message=AsyncMock()
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_save_stores_and_announces(self) -> None:
        saved = reminder()
        save = AsyncMock(return_value=saved)
        view = reminder_cmds.ConfirmReminderView(5, save, "Reminder set")
        interaction = self._interaction()
        await view._on_save(interaction)
        save.assert_awaited_once()
        edited = interaction.response.edit_message.call_args.kwargs
        self.assertEqual(edited["content"], t("reminders.confirm.saved"))
        self.assertIsNone(edited["view"])
        interaction.followup.send.assert_awaited_once()

    async def test_save_failure_is_reported_and_not_announced(self) -> None:
        save = AsyncMock(side_effect=reminder_service.PastDate(date="x"))
        view = reminder_cmds.ConfirmReminderView(5, save, "Reminder set")
        interaction = self._interaction()
        await view._on_save(interaction)
        edited = interaction.response.edit_message.call_args.kwargs
        self.assertEqual(
            edited["content"], t("reminders.errors.past_date", date="x")
        )
        interaction.followup.send.assert_not_called()

    async def test_cancel_saves_nothing(self) -> None:
        save = AsyncMock()
        view = reminder_cmds.ConfirmReminderView(5, save, "Reminder set")
        interaction = self._interaction()
        await view._on_cancel(interaction)
        save.assert_not_called()
        edited = interaction.response.edit_message.call_args.kwargs
        self.assertEqual(edited["content"], t("reminders.confirm.cancelled"))

    async def test_only_the_author_may_answer(self) -> None:
        view = reminder_cmds.ConfirmReminderView(5, AsyncMock(), "x")
        stranger = self._interaction(user_id=6)
        self.assertFalse(await view.interaction_check(stranger))
        self.assertTrue(await view.interaction_check(self._interaction()))


if __name__ == "__main__":
    unittest.main()
