"""Unit tests for the pure logic behind the streak feature.

Run from the repository root::

    python -m unittest discover -s tests -v

No network, database or Discord connection is required.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from bot.commands import streaks as command_streaks
from bot.services import streaks
from bot.texts import MissingText, t, t_choice


ICONS = {
    "upcoming": "U",
    "success": "S",
    "success_today": "T",
    "partial": "P",
    "zero": "Z",
    "rescue": "R",
    "golden": "G",
}


def log(
    log_date: date,
    outcome: str,
    *,
    repair: bool = False,
    rescue_type: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        log_date=log_date,
        outcome=outcome,
        repair=repair,
        rescue_type=rescue_type,
    )


class LevelXpTests(unittest.TestCase):
    def test_zero_level_is_free(self) -> None:
        self.assertEqual(streaks.level_xp(0), 0)
        self.assertEqual(streaks.level_xp(-3), 0)

    def test_first_level_pays_250(self) -> None:
        self.assertEqual(streaks.level_xp(1), 250)

    def test_growth_is_monotonic_until_cap(self) -> None:
        values = [streaks.level_xp(level) for level in range(1, 31)]
        self.assertEqual(values, sorted(values))

    def test_caps_at_3000_around_level_30(self) -> None:
        self.assertLess(streaks.level_xp(29), 3000)
        self.assertEqual(streaks.level_xp(31), 3000)
        self.assertEqual(streaks.level_xp(200), 3000)


class DisplayLevelTests(unittest.TestCase):
    def test_empty_streak_is_level_zero(self) -> None:
        self.assertEqual(streaks.display_level(0), 0)

    def test_first_log_starts_level_one(self) -> None:
        self.assertEqual(streaks.display_level(1), 1)
        self.assertEqual(streaks.display_level(5), 1)

    def test_level_advances_every_five_days(self) -> None:
        self.assertEqual(streaks.display_level(6), 2)
        self.assertEqual(streaks.display_level(10), 2)
        self.assertEqual(streaks.display_level(11), 3)


class WindowXpTests(unittest.TestCase):
    def test_full_window_pays_level_xp(self) -> None:
        self.assertEqual(streaks.window_xp(1, 5), 250)

    def test_failed_days_are_worth_nothing(self) -> None:
        self.assertEqual(streaks.window_xp(1, 0), 0)
        self.assertEqual(streaks.window_xp(1, 2), 100)

    def test_successes_are_clamped(self) -> None:
        self.assertEqual(streaks.window_xp(1, 9), 250)

    def test_on_track_targets_the_next_level(self) -> None:
        self.assertEqual(streaks.on_track_xp(0, 2, 2), streaks.window_xp(1, 5))
        self.assertEqual(
            streaks.on_track_xp(0, 1, 1), streaks.window_xp(1, 5)
        )

    def test_on_track_fills_missing_days_with_successes(self) -> None:
        self.assertEqual(
            streaks.on_track_xp(0, 3, 1), streaks.window_xp(1, 3)
        )

    def test_on_track_new_window_after_completion(self) -> None:
        self.assertEqual(
            streaks.on_track_xp(1, 5, 0), streaks.window_xp(2, 5)
        )


class OutcomeTests(unittest.TestCase):
    def test_classification(self) -> None:
        self.assertEqual(streaks.outcome_for(30, 30), streaks.OUTCOME_SUCCESS)
        self.assertEqual(streaks.outcome_for(60, 30), streaks.OUTCOME_SUCCESS)
        self.assertEqual(streaks.outcome_for(15, 30), streaks.OUTCOME_FAILED)
        self.assertEqual(streaks.outcome_for(0, 30), streaks.OUTCOME_ZERO)

    def test_percent(self) -> None:
        self.assertEqual(streaks.percent_for(15, 30), 50)
        self.assertEqual(streaks.percent_for(0, 30), 0)


class ParseAmountTests(unittest.TestCase):
    def test_absolute(self) -> None:
        self.assertEqual(streaks.parse_amount("50"), 50)

    def test_relative(self) -> None:
        self.assertEqual(streaks.parse_amount("+10", 5), 15)
        self.assertEqual(streaks.parse_amount("-5", 12), 7)

    def test_relative_never_goes_negative(self) -> None:
        self.assertEqual(streaks.parse_amount("-50", 10), 0)

    def test_invalid(self) -> None:
        with self.assertRaises(streaks.InvalidAmount):
            streaks.parse_amount("abc")
        with self.assertRaises(streaks.InvalidAmount):
            streaks.parse_amount("")


class TimezoneTests(unittest.TestCase):
    def test_local_date_rolls_back_and_forward(self) -> None:
        now = datetime(2024, 1, 1, 3, 0, 0)
        self.assertEqual(
            streaks.local_date(now, "America/Toronto"), date(2023, 12, 31)
        )
        self.assertEqual(streaks.local_date(now, "Asia/Tokyo"), date(2024, 1, 1))

    def test_local_hour(self) -> None:
        now = datetime(2024, 1, 1, 3, 0, 0)
        self.assertEqual(streaks.local_hour(now, "UTC"), 3)
        self.assertEqual(streaks.local_hour(now, "Asia/Tokyo"), 12)

    def test_deadline_is_midnight_two_days_later(self) -> None:
        deadline = streaks.streak_deadline(date(2024, 1, 1), "UTC")
        self.assertEqual(deadline.replace(tzinfo=None), datetime(2024, 1, 3, 0, 0))

    def test_day_end_is_next_midnight(self) -> None:
        end = streaks.day_end(date(2024, 1, 1), "UTC")
        self.assertEqual(end.replace(tzinfo=None), datetime(2024, 1, 2, 0, 0))

    def test_valid_timezone(self) -> None:
        self.assertEqual(
            streaks.valid_timezone("America/Toronto"), "America/Toronto"
        )
        self.assertIsNone(streaks.valid_timezone("Nowhere/Nowhere"))
        self.assertIsNone(streaks.valid_timezone(""))

    def test_search_matches_city(self) -> None:
        results = streaks.search_timezones("toronto")
        values = [value for value, _ in results]
        self.assertIn("America/Toronto", values)

    def test_search_empty_returns_popular(self) -> None:
        self.assertTrue(streaks.search_timezones(""))


class RescueWindowTests(unittest.TestCase):
    end = date(2024, 1, 2)

    def test_within_24h_is_normal(self) -> None:
        now = datetime(2024, 1, 3, 10, 0, 0)
        self.assertEqual(
            streaks.rescue_kind_for_closed(self.end, "UTC", now),
            streaks.RESCUE_NORMAL,
        )

    def test_after_24h_is_golden(self) -> None:
        now = datetime(2024, 1, 4, 10, 0, 0)
        self.assertEqual(
            streaks.rescue_kind_for_closed(self.end, "UTC", now),
            streaks.RESCUE_GOLDEN,
        )

    def test_before_close_is_none(self) -> None:
        now = datetime(2024, 1, 2, 10, 0, 0)
        self.assertIsNone(streaks.rescue_kind_for_closed(self.end, "UTC", now))

    def test_after_five_days_is_dead(self) -> None:
        now = datetime(2024, 1, 8, 10, 0, 0)
        self.assertIsNone(streaks.rescue_kind_for_closed(self.end, "UTC", now))

    def test_is_rescuable_needs_a_coupon(self) -> None:
        now = datetime(2024, 1, 3, 10, 0, 0)
        broke = SimpleNamespace(
            status="closed",
            end_date=self.end,
            rescues_available=0,
            golden_available=0,
        )
        self.assertFalse(streaks.is_rescuable(broke, "UTC", now))
        broke.rescues_available = 1
        self.assertTrue(streaks.is_rescuable(broke, "UTC", now))


class SlotTests(unittest.TestCase):
    today = date(2024, 1, 5)

    def slot(self, entry, *, highlight: bool) -> str:
        return streaks.slot_emoji(
            entry, highlight_today=highlight, today=self.today, icons=ICONS
        )

    def test_missing_day_is_upcoming(self) -> None:
        self.assertEqual(self.slot(None, highlight=False), "U")

    def test_outcomes(self) -> None:
        self.assertEqual(
            self.slot(log(self.today, streaks.OUTCOME_SUCCESS), highlight=False),
            "S",
        )
        self.assertEqual(
            self.slot(log(self.today, streaks.OUTCOME_FAILED), highlight=False),
            "P",
        )
        self.assertEqual(
            self.slot(log(self.today, streaks.OUTCOME_ZERO), highlight=False),
            "Z",
        )

    def test_today_success_highlights_only_when_logging(self) -> None:
        entry = log(self.today, streaks.OUTCOME_SUCCESS)
        self.assertEqual(self.slot(entry, highlight=True), "T")
        self.assertEqual(self.slot(entry, highlight=False), "S")

    def test_rescued_days_colour_by_type(self) -> None:
        normal = log(self.today, streaks.OUTCOME_ZERO, repair=True)
        golden = log(
            self.today,
            streaks.OUTCOME_ZERO,
            repair=True,
            rescue_type=streaks.RESCUE_GOLDEN,
        )
        self.assertEqual(self.slot(normal, highlight=False), "R")
        self.assertEqual(self.slot(golden, highlight=False), "G")

    def test_window_shows_five_fixed_slots(self) -> None:
        logs = [
            log(date(2024, 1, 4), streaks.OUTCOME_SUCCESS),
            log(date(2024, 1, 5), streaks.OUTCOME_FAILED),
        ]
        slots = streaks.window_slots(
            logs,
            days_logged=2,
            highlight_today=True,
            today=self.today,
            icons=ICONS,
        )
        self.assertEqual(slots, ["S", "P", "U", "U", "U"])

    def test_completed_window_shows_empty(self) -> None:
        slots = streaks.window_slots(
            [],
            days_logged=5,
            highlight_today=False,
            today=self.today,
            icons=ICONS,
        )
        self.assertEqual(slots, ["U", "U", "U", "U", "U"])

    def test_completed_window_persists_when_ending_today(self) -> None:
        logs = [
            log(self.today - timedelta(days=4 - index), streaks.OUTCOME_SUCCESS)
            for index in range(5)
        ]
        slots = streaks.window_slots(
            logs,
            days_logged=5,
            highlight_today=False,
            today=self.today,
            icons=ICONS,
        )
        self.assertEqual(slots, ["S", "S", "S", "S", "S"])

    def test_completed_window_resets_after_midnight(self) -> None:
        logs = [
            log(self.today - timedelta(days=5 - index), streaks.OUTCOME_SUCCESS)
            for index in range(5)
        ]
        slots = streaks.window_slots(
            logs,
            days_logged=5,
            highlight_today=False,
            today=self.today,
            icons=ICONS,
        )
        self.assertEqual(slots, ["U", "U", "U", "U", "U"])


class RescueButtonTests(unittest.TestCase):
    def test_uses_the_matching_rescue_icon(self) -> None:
        normal = command_streaks._RescueButton(streaks.RESCUE_NORMAL)
        golden = command_streaks._RescueButton(streaks.RESCUE_GOLDEN)
        self.assertEqual(normal.emoji.name, "rescue")
        self.assertEqual(golden.emoji.name, "golden")


class TextChoiceTests(unittest.TestCase):
    def test_picks_from_a_variety_list(self) -> None:
        seen = {
            t_choice("streaks.reminders.content", mentions="@someone")
            for _ in range(200)
        }
        self.assertGreater(len(seen), 1)
        self.assertTrue(all("@someone" in entry for entry in seen))

    def test_single_string_is_accepted(self) -> None:
        self.assertEqual(
            t_choice("streaks.info.dry_run_value"),
            t("streaks.info.dry_run_value"),
        )

    def test_unknown_key_raises(self) -> None:
        with self.assertRaises(MissingText):
            t_choice("streaks.nope.missing")


class ModalLimitTests(unittest.TestCase):
    def test_modal_titles_and_labels_fit_discord_limits(self) -> None:
        goal = SimpleNamespace(
            amount=30, unit="pages", timezone="America/Toronto"
        )
        modals = [
            command_streaks._AmountUnitModal(goal),
            command_streaks._TimezoneModal(goal),
        ]
        for modal in modals:
            self.assertLessEqual(len(modal.title), 45, modal.title)
            for item in modal.children:
                self.assertLessEqual(len(item.label), 45, item.label)


if __name__ == "__main__":
    unittest.main()
