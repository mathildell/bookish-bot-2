"""Unit tests for the pure logic behind the weekly reading prompts.

Run from the repository root::

    python -m unittest discover -s tests -v

No network, database or Discord connection is required.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from bot.services import weekly_reading as reading_service
from bot.texts import t

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "import_weekly_reading_cards",
    ROOT / "scripts" / "import_weekly_reading_cards.py",
)
import_weekly_reading_cards = importlib.util.module_from_spec(_spec)
sys.modules["import_weekly_reading_cards"] = import_weekly_reading_cards
_spec.loader.exec_module(import_weekly_reading_cards)


def utc(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute)


def card(**kwargs) -> SimpleNamespace:
    defaults = dict(
        id=1,
        guild_id=10,
        title="Read a book from another decade",
        description=None,
        sent=False,
        sent_at=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class FakeClient:
    transparent_embed_image = None
    default_embed_color = None


class NormalizeTitleTests(unittest.TestCase):
    def test_case_is_ignored(self) -> None:
        self.assertEqual(
            reading_service.normalize_title("Read A Book"),
            reading_service.normalize_title("read a book"),
        )

    def test_whitespace_is_collapsed(self) -> None:
        self.assertEqual(
            reading_service.normalize_title("  read   a book  "), "read a book"
        )

    def test_compatibility_characters_are_folded(self) -> None:
        self.assertEqual(
            reading_service.normalize_title("Cafe\u0301 Culture"),
            reading_service.normalize_title("Café Culture"),
        )

    def test_empty_title(self) -> None:
        self.assertEqual(reading_service.normalize_title(""), "")


class ValidateTitleTests(unittest.TestCase):
    def test_title_is_trimmed(self) -> None:
        self.assertEqual(reading_service.validate_title("  Read  "), "Read")

    def test_empty_title_is_rejected(self) -> None:
        with self.assertRaises(reading_service.MissingTitle):
            reading_service.validate_title("   ")

    def test_long_title_is_rejected(self) -> None:
        with self.assertRaises(reading_service.TitleTooLong):
            reading_service.validate_title("x" * 300)


class ValidateDescriptionTests(unittest.TestCase):
    def test_blank_description_becomes_none(self) -> None:
        self.assertIsNone(reading_service.validate_description("  "))
        self.assertIsNone(reading_service.validate_description(None))

    def test_description_is_trimmed(self) -> None:
        self.assertEqual(
            reading_service.validate_description("  hello  "), "hello"
        )

    def test_long_description_is_rejected(self) -> None:
        with self.assertRaises(reading_service.DescriptionTooLong):
            reading_service.validate_description("x" * 2500)


class ValidateScheduleTests(unittest.TestCase):
    def test_weekday_range(self) -> None:
        self.assertEqual(reading_service.validate_weekday(0), 0)
        self.assertEqual(reading_service.validate_weekday(6), 6)
        with self.assertRaises(reading_service.InvalidWeekday):
            reading_service.validate_weekday(7)

    def test_hour_range(self) -> None:
        self.assertEqual(reading_service.validate_hour(0), 0)
        self.assertEqual(reading_service.validate_hour(23), 23)
        with self.assertRaises(reading_service.InvalidHour):
            reading_service.validate_hour(24)


class IsPostDueTests(unittest.TestCase):
    def _due(self, now: datetime, **overrides) -> bool:
        settings = dict(
            weekday=0,
            hour=12,
            enabled=True,
            last_posted_on=None,
        )
        settings.update(overrides)
        return reading_service.is_post_due(now, **settings)

    def test_matching_weekday_and_hour(self) -> None:
        # 2026-10-19 is a Monday.
        self.assertTrue(self._due(utc(2026, 10, 19, 12)))

    def test_other_weekday_is_not_due(self) -> None:
        self.assertFalse(self._due(utc(2026, 10, 20, 12)))

    def test_other_hour_is_not_due(self) -> None:
        self.assertFalse(self._due(utc(2026, 10, 19, 13)))

    def test_paused_is_not_due(self) -> None:
        self.assertFalse(self._due(utc(2026, 10, 19, 12), enabled=False))

    def test_unset_schedule_is_not_due(self) -> None:
        self.assertFalse(self._due(utc(2026, 10, 19, 12), weekday=None))
        self.assertFalse(self._due(utc(2026, 10, 19, 12), hour=None))

    def test_already_posted_today_is_not_due(self) -> None:
        self.assertFalse(
            self._due(
                utc(2026, 10, 19, 12),
                last_posted_on=utc(2026, 10, 19).date(),
            )
        )

    def test_posted_last_week_is_due_again(self) -> None:
        self.assertTrue(
            self._due(
                utc(2026, 10, 19, 12),
                last_posted_on=utc(2026, 10, 12).date(),
            )
        )


class BuildCardEmbedTests(unittest.TestCase):
    def test_title_only(self) -> None:
        embed = reading_service.build_card_embed(card(), FakeClient())
        self.assertEqual(embed.title, t("weekly_reading.post.embed_title"))
        self.assertEqual(
            embed.description, "### Read a book from another decade"
        )

    def test_description_is_added(self) -> None:
        embed = reading_service.build_card_embed(
            card(description="A 1970s classic."), FakeClient()
        )
        self.assertEqual(
            embed.description,
            "### Read a book from another decade\nA 1970s classic.",
        )


class FormatPostedDateTests(unittest.TestCase):
    def test_date_is_readable(self) -> None:
        self.assertEqual(
            reading_service.format_posted_date(utc(2026, 10, 9).date()),
            "October 9, 2026",
        )


class ParseCardsTests(unittest.TestCase):
    def _parse(self, content: str) -> list:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prompts.csv"
            path.write_text(content, encoding="utf-8")
            return import_weekly_reading_cards.parse_cards(path)

    def test_header_with_title_and_description(self) -> None:
        rows = self._parse(
            "title,description\nRead a poem,Something short\n"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, "Read a poem")
        self.assertEqual(rows[0].description, "Something short")

    def test_single_column_header(self) -> None:
        rows = self._parse("prompt\nRead a poem\nRead a play\n")
        self.assertEqual([row.title for row in rows], ["Read a poem", "Read a play"])
        self.assertIsNone(rows[0].description)

    def test_headerless_file(self) -> None:
        rows = self._parse("Read a poem,Something short\nRead a play\n")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].title, "Read a poem")
        self.assertEqual(rows[0].description, "Something short")
        self.assertIsNone(rows[1].description)

    def test_empty_rows_are_skipped(self) -> None:
        rows = self._parse("title,description\nRead a poem,\n,\n")
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
