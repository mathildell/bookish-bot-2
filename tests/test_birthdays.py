"""Unit tests for the pure birthday rules.

Run from the repository root::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import datetime
import unittest
from types import SimpleNamespace

from bot.commands import birthdays as birthday_commands
from bot.services import birthdays as birthday_service
from bot.services import level as level_service
from bot.texts import t


def _birthday(**kwargs):
    values = {
        "month": 10,
        "day": 23,
        "timezone": "UTC",
        "last_announced_on": None,
        "xp_awarded_on": None,
        "role_granted_on": None,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


class DateTests(unittest.TestCase):
    def test_parse_accepts_leap_day(self) -> None:
        self.assertEqual(birthday_service.parse_date_value("02-29"), (2, 29))

    def test_parse_rejects_impossible_dates(self) -> None:
        with self.assertRaises(birthday_service.InvalidDate):
            birthday_service.parse_date_value("02-30")
        with self.assertRaises(birthday_service.InvalidDate):
            birthday_service.parse_date_value("13-01")
        with self.assertRaises(birthday_service.InvalidDate):
            birthday_service.parse_date_value("nope")

    def test_search_filters_month_and_day(self) -> None:
        choices = birthday_service.search_birthday_dates("december 31")
        self.assertEqual(choices, [("12-31", "December 31")])

    def test_search_includes_february_29(self) -> None:
        choices = birthday_service.search_birthday_dates("february 29")
        self.assertEqual(choices[0][0], "02-29")

    def test_search_is_capped(self) -> None:
        self.assertLessEqual(
            len(birthday_service.search_birthday_dates("")), 25
        )

    def test_leap_day_falls_back_to_february_28(self) -> None:
        self.assertEqual(birthday_service.effective_day(2, 29, 2026), 28)
        self.assertEqual(birthday_service.effective_day(2, 29, 2028), 29)

    def test_birthday_matches_effective_local_day(self) -> None:
        birthday = _birthday(month=2, day=29)
        self.assertTrue(
            birthday_service.is_birthday_on(
                birthday, datetime.date(2026, 2, 28)
            )
        )
        self.assertFalse(
            birthday_service.is_birthday_on(
                birthday, datetime.date(2026, 3, 1)
            )
        )


class DueTests(unittest.TestCase):
    def test_needs_celebration_on_local_birthday(self) -> None:
        birthday = _birthday(month=10, day=23)
        now = datetime.datetime(2026, 10, 23, 0, 2)
        self.assertTrue(birthday_service.needs_celebration(birthday, now))

    def test_missing_xp_still_needs_work(self) -> None:
        birthday = _birthday(
            last_announced_on=datetime.date(2026, 10, 23),
            xp_awarded_on=None,
        )
        now = datetime.datetime(2026, 10, 23, 18, 0)
        self.assertTrue(birthday_service.needs_celebration(birthday, now))

    def test_uses_the_member_timezone(self) -> None:
        birthday = _birthday(month=10, day=23, timezone="Pacific/Auckland")
        now = datetime.datetime(2026, 10, 22, 12, 0)
        self.assertTrue(birthday_service.needs_celebration(birthday, now))

    def test_role_expires_after_the_local_day(self) -> None:
        birthday = _birthday(
            timezone="UTC", role_granted_on=datetime.date(2026, 10, 23)
        )
        self.assertFalse(
            birthday_service.role_expired(
                birthday, datetime.datetime(2026, 10, 23, 23, 59)
            )
        )
        self.assertTrue(
            birthday_service.role_expired(
                birthday, datetime.datetime(2026, 10, 24, 0, 1)
            )
        )


class NextBirthdayTests(unittest.TestCase):
    def test_future_birthday_this_year(self) -> None:
        birthday = _birthday(month=12, day=1)
        now = datetime.datetime(2026, 10, 24, 12, 0)
        self.assertEqual(
            birthday_service.next_birthday_at(birthday, now),
            datetime.datetime(2026, 12, 1, 0, 0),
        )

    def test_past_birthday_wraps_to_next_year(self) -> None:
        birthday = _birthday(month=10, day=23)
        now = datetime.datetime(2026, 10, 24, 12, 0)
        self.assertEqual(
            birthday_service.next_birthday_at(birthday, now),
            datetime.datetime(2027, 10, 23, 0, 0),
        )

    def test_birthday_today_resolves_to_now(self) -> None:
        birthday = _birthday(month=10, day=24)
        now = datetime.datetime(2026, 10, 24, 12, 0)
        self.assertEqual(
            birthday_service.next_birthday_at(birthday, now), now
        )

    def test_leap_day_falls_back_to_february_28(self) -> None:
        birthday = _birthday(month=2, day=29)
        now = datetime.datetime(2026, 10, 24, 12, 0)
        self.assertEqual(
            birthday_service.next_birthday_at(birthday, now),
            datetime.datetime(2027, 2, 28, 0, 0),
        )

    def test_uses_the_member_timezone(self) -> None:
        birthday = _birthday(month=10, day=25, timezone="Pacific/Auckland")
        now = datetime.datetime(2026, 10, 24, 12, 0)
        self.assertEqual(
            birthday_service.next_birthday_at(birthday, now), now
        )


class ContentTests(unittest.TestCase):
    def test_personal_content_wins_over_generic(self) -> None:
        messages = [
            SimpleNamespace(id=1, user_id=None, content="generic"),
            SimpleNamespace(id=2, user_id=7, content="personal"),
        ]
        images = [
            SimpleNamespace(id=1, user_id=None, url="https://example.com/a.png"),
            SimpleNamespace(id=2, user_id=7, url="https://example.com/b.png"),
        ]
        chosen = birthday_service.choose_content(
            messages, images, 7, fallback_message="fallback"
        )
        self.assertEqual(chosen.message, "personal")
        self.assertEqual(chosen.image_url, "https://example.com/b.png")

    def test_generic_pool_is_used_without_personal_content(self) -> None:
        messages = [
            SimpleNamespace(id=1, user_id=None, content="generic")
        ]
        images = [
            SimpleNamespace(id=1, user_id=None, url="https://example.com/a.png")
        ]
        chosen = birthday_service.choose_content(
            messages, images, 7, fallback_message="fallback"
        )
        self.assertEqual(chosen.message, "generic")
        self.assertEqual(chosen.image_url, "https://example.com/a.png")

    def test_fallback_message_and_no_image(self) -> None:
        chosen = birthday_service.choose_content(
            [], [], 7, fallback_message="fallback"
        )
        self.assertEqual(chosen.message, "fallback")
        self.assertIsNone(chosen.image_url)


class EmbedTests(unittest.TestCase):
    def test_embed_uses_the_member_as_author_and_shows_the_gift(self) -> None:
        class Client:
            default_embed_color = None
            transparent_embed_image = "https://example.com/transparent.png"

        member = SimpleNamespace(
            display_name="Ada",
            mention="<@1>",
            display_avatar=SimpleNamespace(
                url="https://example.com/avatar.png"
            ),
        )
        content = birthday_service.ContentChoice(
            message="Happy birthday %{mention}!", image_url=None
        )
        summary = birthday_service.xp_award_summary(
            level_service.preview_level_change(1, 100, 5000)
        )
        embed = birthday_service.build_birthday_embed(
            Client(), member, content, summary
        )
        self.assertEqual(embed.author.name, "Ada")
        self.assertEqual(embed.author.icon_url, "https://example.com/avatar.png")
        self.assertEqual(embed.description, "Happy birthday <@1>!")
        self.assertEqual(
            embed.image.url, "https://example.com/transparent.png"
        )
        self.assertIn("`5000` XP", embed.fields[0].value)


class XpTests(unittest.TestCase):
    def test_summary_and_text_match_the_weekly_format(self) -> None:
        change = level_service.preview_level_change(1, 100, 5000)
        summary = birthday_service.xp_award_summary(change)
        self.assertEqual(summary["old_level"], 1)
        self.assertGreater(summary["new_level"], summary["old_level"])
        text = birthday_service.xp_progress_text(summary)
        self.assertIn("`5000` XP", text)
        self.assertIn("Level", text)

    def test_same_level_progress_format(self) -> None:
        change = level_service.preview_level_change(1, 75, 10)
        summary = birthday_service.xp_award_summary(change)
        text = birthday_service.xp_progress_text(summary)
        self.assertIn("`5000` XP", text)
        self.assertIn("Level `1`", text)


class CommandTests(unittest.TestCase):
    def test_group_hosts_the_commands(self) -> None:
        names = {
            command.name for command in birthday_commands.birthday_group.commands
        }
        self.assertEqual(
            names,
            {
                "set",
                "remove",
                "list",
                "channel",
                "role",
                "content",
                "force",
            },
        )

    def test_content_subgroup_is_available(self) -> None:
        self.assertEqual(
            {
                command.name
                for command in birthday_commands.content_group.commands
            },
            {"add", "remove", "list", "edit"},
        )


    def test_every_error_text_exists(self) -> None:
        for key in (
            "birthday.errors.generic",
            "birthday.errors.invalid_date",
            "birthday.errors.invalid_timezone",
            "birthday.errors.message_empty",
            "birthday.errors.message_too_long",
            "birthday.errors.invalid_url",
            "birthday.errors.not_registered",
            "birthday.errors.member_not_found",
            "birthday.errors.no_guild",
            "birthday.errors.no_channel",
            "birthday.errors.post_failed",
        ):
            self.assertTrue(t(key))


if __name__ == "__main__":
    unittest.main()
