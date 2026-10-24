"""Unit tests for the old birthday import helpers."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "import_birthdays", ROOT / "scripts" / "import_birthdays.py"
)
import_birthdays = importlib.util.module_from_spec(_spec)
sys.modules["import_birthdays"] = import_birthdays
_spec.loader.exec_module(import_birthdays)


def user_row(old_id: str, discord_id: int, timezone: str = "UTC") -> dict:
    return {
        "_id": {"$oid": old_id},
        "discord_user_id": {"$numberLong": str(discord_id)},
        "timezone": timezone,
    }


def birthday_row(old_id: str, month: int, day: int, year=1990) -> dict:
    return {
        "user_id": {"$oid": old_id},
        "month": month,
        "day": day,
        "year": year,
    }


def card_row(kind: str, content: str, old_id: str | None = None) -> dict:
    return {
        "_kind": kind,
        "content": content,
        "user_id": {"$oid": old_id} if old_id else None,
    }


class ParseUsersTests(unittest.TestCase):
    def test_maps_mongo_ids_to_discord_ids(self) -> None:
        users, warnings = import_birthdays.parse_users(
            [user_row("old-1", 123, "Europe/Paris")]
        )
        self.assertEqual(warnings, [])
        self.assertEqual(users["old-1"].discord_user_id, 123)
        self.assertEqual(users["old-1"].timezone, "Europe/Paris")

    def test_skips_rows_without_ids(self) -> None:
        users, warnings = import_birthdays.parse_users(
            [{"_id": {"$oid": "old-1"}, "discord_user_id": None}]
        )
        self.assertEqual(users, {})
        self.assertEqual(len(warnings), 1)


class ParseBirthdaysTests(unittest.TestCase):
    def test_resolves_user_and_timezone(self) -> None:
        users, _ = import_birthdays.parse_users(
            [user_row("old-1", 123, "Europe/Paris")]
        )
        records, warnings = import_birthdays.parse_birthdays(
            [birthday_row("old-1", 2, 29)], users
        )
        self.assertEqual(warnings, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].user_id, 123)
        self.assertEqual((records[0].month, records[0].day), (2, 29))
        self.assertEqual(records[0].timezone, "Europe/Paris")

    def test_unknown_timezone_falls_back_to_utc(self) -> None:
        users, _ = import_birthdays.parse_users(
            [user_row("old-1", 123, "Not/AZone")]
        )
        records, warnings = import_birthdays.parse_birthdays(
            [birthday_row("old-1", 4, 22)], users
        )
        self.assertEqual(records[0].timezone, "UTC")
        self.assertEqual(len(warnings), 1)

    def test_unknown_user_and_invalid_date_are_skipped(self) -> None:
        users, _ = import_birthdays.parse_users(
            [user_row("old-1", 123)]
        )
        records, warnings = import_birthdays.parse_birthdays(
            [
                birthday_row("missing", 4, 22),
                birthday_row("old-1", 2, 30),
            ],
            users,
        )
        self.assertEqual(records, [])
        self.assertEqual(len(warnings), 2)


class ParseCardsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.users, _ = import_birthdays.parse_users(
            [user_row("old-1", 123)]
        )

    def test_generic_text_and_personal_image(self) -> None:
        records, warnings = import_birthdays.parse_cards(
            [
                card_row("text", "Happy birthday %{mention}!"),
                card_row("image", "https://example.com/cake.png", "old-1"),
            ],
            self.users,
        )
        self.assertEqual(warnings, [])
        self.assertIsNone(records[0].user_id)
        self.assertEqual(records[0].kind, "text")
        self.assertEqual(records[1].user_id, 123)
        self.assertEqual(records[1].kind, "image")

    def test_unknown_user_and_kind_are_skipped(self) -> None:
        records, warnings = import_birthdays.parse_cards(
            [
                card_row("image", "https://example.com/cake.png", "missing"),
                card_row("video", "https://example.com/clip.mp4"),
            ],
            self.users,
        )
        self.assertEqual(records, [])
        self.assertEqual(len(warnings), 2)


class FindExportTests(unittest.TestCase):
    def test_finds_the_export_by_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "production.users.json"
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(
                import_birthdays.find_export(Path(folder), "users.json"),
                path,
            )

    def test_missing_export_raises(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileNotFoundError):
                import_birthdays.find_export(Path(folder), "users.json")


if __name__ == "__main__":
    unittest.main()
