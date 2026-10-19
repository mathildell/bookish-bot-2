"""Unit tests for the pure logic behind the streak feature.

Run from the repository root::

    python -m unittest discover -s tests -v

No network, database or Discord connection is required.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from bot.commands import streaks as command_streaks
from bot.services import coupons
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


class CouponDateTests(unittest.TestCase):
    def test_uses_month_name_and_day(self) -> None:
        self.assertEqual(coupons.coupon_date(date(2026, 10, 5)), "October 05")
        self.assertEqual(coupons.coupon_date(date(2026, 1, 31)), "January 31")


class TmpfilesDirectUrlTests(unittest.TestCase):
    def test_extracts_the_direct_download_link(self) -> None:
        page = (
            '<a href="https://tmpfiles.org/dl/1792446954.token/id/'
            'coupon.png">Download</a>'
        )
        self.assertEqual(
            coupons._tmpfiles_direct_url(page),
            "https://tmpfiles.org/dl/1792446954.token/id/coupon.png",
        )

    def test_returns_none_when_the_link_is_missing(self) -> None:
        self.assertIsNone(coupons._tmpfiles_direct_url("<html></html>"))


class CouponGenerationTests(unittest.IsolatedAsyncioTestCase):
    class Coupons:
        def __init__(self) -> None:
            self.calls = []

        async def generate(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(url=f"https://example.com/{kwargs['level']}.png")

    def _interaction(self) -> SimpleNamespace:
        self.service = self.Coupons()
        return SimpleNamespace(
            client=SimpleNamespace(
                coupons=self.service,
                coupon_fallback_image="https://example.com/fallback.png",
            )
        )

    def _progress(self, **kwargs) -> streaks.LevelProgress:
        values = dict(
            previous_level=0,
            level=2,
            leveled=True,
            xp_awarded=750,
            rescues_granted=2,
            golden_granted=0,
            anniversary=False,
            awards=[
                streaks.LevelAward(level=1, xp=250),
                streaks.LevelAward(level=2, xp=500),
            ],
        )
        values.update(kwargs)
        return streaks.LevelProgress(**values)

    async def _generate(self, progress) -> list[str]:
        return await command_streaks._generate_coupons(
            self._interaction(),
            SimpleNamespace(display_name="Alice"),
            SimpleNamespace(timezone="UTC"),
            progress,
        )

    async def test_generates_one_coupon_for_each_award(self) -> None:
        urls = await self._generate(self._progress())

        self.assertEqual(
            urls,
            ["https://example.com/1.png", "https://example.com/2.png"],
        )
        self.assertEqual([call["level"] for call in self.service.calls], [1, 2])
        self.assertEqual([call["xp"] for call in self.service.calls], [250, 500])
        self.assertEqual(self.service.calls[0]["display_name"], "Alice")
        self.assertEqual(
            self.service.calls[0]["fallback_image"],
            "https://example.com/fallback.png",
        )

    async def test_generates_even_in_a_dry_run(self) -> None:
        urls = await self._generate(self._progress(dry_run=True))
        self.assertEqual(len(urls), 2)

    async def test_generates_when_a_level_cleared_without_recorded_awards(self) -> None:
        urls = await self._generate(
            self._progress(level=1, xp_awarded=250, awards=[])
        )

        self.assertEqual(urls, ["https://example.com/1.png"])
        self.assertEqual(self.service.calls[0]["level"], 1)
        self.assertEqual(self.service.calls[0]["xp"], 250)


class CouponRenderTests(unittest.TestCase):
    def test_renders_a_600x360_image_and_returns_a_temp_file(self) -> None:
        path = coupons.render_coupon(
            display_name="A Very Long Reader Name That Has To Shrink",
            level=123,
            on_date=date(2026, 10, 19),
            xp=9999,
        )
        try:
            self.assertTrue(path.exists())
            with coupons.Image.open(path) as image:
                self.assertEqual(image.size, (600, 360))
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.getchannel("A").getextrema()[0], 0)
        finally:
            path.unlink(missing_ok=True)


class CouponFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_and_deletes_the_local_file(self) -> None:
        path = coupons.render_coupon(
            display_name="Alice",
            level=1,
            on_date=date(2026, 10, 19),
            xp=250,
        )

        class Failing(coupons.CouponService):
            async def _upload_imgbb(self, http, path):
                raise coupons.CouponUploadError("imgbb down")

            async def _upload_tmpfiles(self, http, path):
                raise coupons.CouponUploadError("tmpfiles down")

        service = Failing("key")
        with patch("bot.services.coupons.render_coupon", return_value=path):
            coupon = await service.generate(
                display_name="Alice",
                level=1,
                on_date=date(2026, 10, 19),
                xp=250,
                fallback_image="https://example.com/fallback.gif",
            )

        self.assertEqual(coupon.url, "https://example.com/fallback.gif")
        self.assertFalse(path.exists())


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

    def test_completed_window_stays_until_next_day_is_logged(self) -> None:
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
        self.assertEqual(slots, ["S", "S", "S", "S", "S"])

    def test_next_level_window_starts_after_the_first_new_day(self) -> None:
        logs = [
            log(self.today - timedelta(days=2), streaks.OUTCOME_SUCCESS),
            log(self.today - timedelta(days=1), streaks.OUTCOME_SUCCESS),
            log(self.today, streaks.OUTCOME_FAILED),
        ]
        slots = streaks.window_slots(
            logs,
            days_logged=6,
            highlight_today=False,
            today=self.today,
            icons=ICONS,
        )
        self.assertEqual(slots, ["P", "U", "U", "U", "U"])

    def test_golden_repairs_fill_the_completed_window(self) -> None:
        logs = [
            log(self.today - timedelta(days=4), streaks.OUTCOME_SUCCESS),
        ] + [
            log(
                self.today - timedelta(days=offset),
                streaks.OUTCOME_ZERO,
                repair=True,
                rescue_type=streaks.RESCUE_GOLDEN,
            )
            for offset in range(3, -1, -1)
        ]
        slots = streaks.window_slots(
            logs,
            days_logged=5,
            highlight_today=False,
            today=self.today,
            icons=ICONS,
        )
        self.assertEqual(slots, ["S", "G", "G", "G", "G"])


class RescueButtonTests(unittest.TestCase):
    def test_uses_the_matching_rescue_icon(self) -> None:
        normal = command_streaks._RescueButton(streaks.RESCUE_NORMAL)
        golden = command_streaks._RescueButton(streaks.RESCUE_GOLDEN)
        self.assertEqual(normal.emoji.name, "rescue")
        self.assertEqual(golden.emoji.name, "golden")


class LocalDatetimeTests(unittest.TestCase):
    def test_formats_afternoon_without_a_leading_zero(self) -> None:
        self.assertEqual(
            command_streaks._format_local_datetime(
                datetime(2024, 1, 5, 13, 0)
            ),
            "Jan 05, 2024 1:00PM",
        )

    def test_formats_midnight_and_noon(self) -> None:
        self.assertEqual(
            command_streaks._format_local_datetime(
                datetime(2024, 1, 5, 0, 5)
            ),
            "Jan 05, 2024 12:05AM",
        )
        self.assertEqual(
            command_streaks._format_local_datetime(
                datetime(2024, 1, 5, 12, 0)
            ),
            "Jan 05, 2024 12:00PM",
        )


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
            t_choice("streaks.icons.upcoming"),
            t("streaks.icons.upcoming"),
        )

    def test_unknown_key_raises(self) -> None:
        with self.assertRaises(MissingText):
            t_choice("streaks.nope.missing")


class RegisterConfirmedEmbedTests(unittest.TestCase):
    def test_mentions_the_member(self) -> None:
        goal = SimpleNamespace(
            user_id=42,
            amount=30,
            unit="pages",
            timezone="UTC",
            reminder="none",
            custom_hour=None,
        )
        client = SimpleNamespace(
            default_embed_color=None, transparent_embed_image=None
        )

        embed = command_streaks._register_confirmed_embed(client, goal)

        self.assertIn("<@42>", embed.description)


class ModalLimitTests(unittest.TestCase):
    def test_modal_titles_and_labels_fit_discord_limits(self) -> None:
        goal = SimpleNamespace(
            amount=30, unit="pages", timezone="America/Toronto"
        )
        modals = [
            command_streaks._AmountUnitModal(goal),
            command_streaks._TimezoneModal(goal),
            command_streaks._LogModal(),
        ]
        for modal in modals:
            self.assertLessEqual(len(modal.title), 45, modal.title)
            for item in modal.children:
                self.assertLessEqual(len(item.label), 45, item.label)


class RescueEmbedTests(unittest.IsolatedAsyncioTestCase):
    def _result(
        self,
        *,
        patched_days: int = 3,
        rescue_type: str = streaks.RESCUE_GOLDEN,
        leveled: bool = True,
    ) -> streaks.ReviveResult:
        goal = SimpleNamespace(
            amount=30,
            unit="pages",
            timezone="UTC",
            reminder="none",
            custom_hour=None,
        )
        streak = SimpleNamespace(
            status=streaks.STATUS_ONGOING,
            start_date=date(2026, 1, 1),
            days_logged=255,
            level=51,
            rescues_available=50,
            rescues_used=0,
            golden_available=0,
            golden_used=1,
            last_log_date=date(2026, 10, 16),
            last_xp_at=datetime(2026, 10, 16),
            last_xp_amount=150,
            end_date=None,
        )
        progress = streaks.LevelProgress(
            previous_level=50,
            level=51,
            leveled=leveled,
            xp_awarded=150,
            rescues_granted=1,
            golden_granted=0,
            anniversary=False,
        )
        return streaks.ReviveResult(
            goal=goal,
            streak=streak,
            logs=[],
            rescue_type=rescue_type,
            patched_days=patched_days,
            moved=True,
            progress=progress,
        )

    def _embed(self, **kwargs):
        result = self._result(**kwargs)
        member = SimpleNamespace(
            display_name="Alice",
            display_avatar=SimpleNamespace(url="https://example.com/a.png"),
        )
        client = SimpleNamespace(
            default_embed_color=None, transparent_embed_image=None
        )
        return command_streaks._view_embed(
            client,
            member,
            result.goal,
            result.streak,
            result.logs,
            rescued=result,
        )

    def test_reports_the_level_completed_by_the_rescue(self) -> None:
        embed = self._embed()
        field = next(
            entry
            for entry in embed.fields
            if entry.name == t("streaks.rescue.title")
        )
        self.assertFalse(field.inline)
        self.assertIn("Level 51", field.value)
        self.assertIn("150 XP", field.value)

    def test_omitted_when_the_rescue_completes_no_level(self) -> None:
        embed = self._embed(leveled=False)
        names = [entry.name for entry in embed.fields]
        self.assertNotIn(t("streaks.rescue.title"), names)

    def test_singular_day(self) -> None:
        embed = self._embed(
            patched_days=1, rescue_type=streaks.RESCUE_NORMAL
        )
        field = next(
            entry
            for entry in embed.fields
            if entry.name == t("streaks.rescue.title")
        )
        self.assertIn("1 day", field.value)
        self.assertNotIn("1 days", field.value)

    async def test_button_edits_the_message_with_the_rescue_field(self) -> None:
        result = self._result()

        class Response:
            def __init__(self) -> None:
                self.embed = None

            async def edit_message(self, *, embed, view) -> None:
                self.embed = embed

        class Service:
            dry_run = False

            async def rescuable_for_user(self, user_id):
                return SimpleNamespace(id=1), result.goal

            async def revive(self, *, streak_id, user_id):
                return result

        class Client:
            default_embed_color = None
            transparent_embed_image = None

        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=2,
                display_name="Alice",
                display_avatar=SimpleNamespace(url="https://example.com/a.png"),
            ),
            client=Client(),
            response=Response(),
            guild=None,
            message=None,
        )
        interaction.client.streaks = Service()

        await command_streaks._do_rescue(interaction)

        names = [entry.name for entry in interaction.response.embed.fields]
        self.assertIn(t("streaks.rescue.title"), names)


class ExpiringViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_view_actions_put_log_before_edit(self) -> None:
        view = command_streaks._ViewActions(
            rescue=True, log=True, edit=True
        )
        self.assertEqual(
            [child.label for child in view.children],
            [
                t("streaks.shared.rescue_button"),
                t("streaks.log.button"),
                t("streaks.edit.button_on_view"),
            ],
        )

    async def test_timeout_disables_every_button(self) -> None:
        view = command_streaks._ViewActions(rescue=True, log=True, edit=True)
        self.assertTrue(all(not child.disabled for child in view.children))

        class Message:
            def __init__(self) -> None:
                self.edits = 0

            async def edit(self, *, view) -> None:
                self.edits += 1

        message = Message()
        view.message = message
        await view.on_timeout()

        self.assertTrue(all(child.disabled for child in view.children))
        self.assertEqual(message.edits, 1)

    async def test_timeout_without_a_message_is_safe(self) -> None:
        view = command_streaks._LeaderboardView(author_id=2)
        view.message = None
        await view.on_timeout()
        self.assertTrue(all(child.disabled for child in view.children))

    async def test_reminder_log_view_disables_its_button(self) -> None:
        view = command_streaks.StreakLogView()
        await view.on_timeout()
        self.assertTrue(all(child.disabled for child in view.children))

    async def test_reminder_choice_disables_every_button(self) -> None:
        view = command_streaks._ReminderChoiceView(1, None, None)
        await view.on_timeout()
        self.assertTrue(all(child.disabled for child in view.children))

    async def test_hour_select_disables_its_menu(self) -> None:
        view = command_streaks._HourSelectView(1, "custom", None, None)
        await view.on_timeout()
        self.assertTrue(all(child.disabled for child in view.children))


if __name__ == "__main__":
    unittest.main()
