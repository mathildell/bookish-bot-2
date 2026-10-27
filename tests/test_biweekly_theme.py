"""Unit tests for the pure logic behind the bi-weekly themes.

Run from the repository root::

    python -m unittest discover -s tests -v

No network, database or Discord connection is required.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import discord

from bot.services import biweekly_theme as theme_service
from bot.services import theme_images
from bot.texts import t

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "import_biweekly_theme_cards",
    ROOT / "scripts" / "import_biweekly_theme_cards.py",
)
import_biweekly_theme_cards = importlib.util.module_from_spec(_spec)
sys.modules["import_biweekly_theme_cards"] = import_biweekly_theme_cards
_spec.loader.exec_module(import_biweekly_theme_cards)


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
            theme_service.normalize_title("Read A Book"),
            theme_service.normalize_title("read a book"),
        )

    def test_whitespace_is_collapsed(self) -> None:
        self.assertEqual(
            theme_service.normalize_title("  read   a book  "), "read a book"
        )

    def test_compatibility_characters_are_folded(self) -> None:
        self.assertEqual(
            theme_service.normalize_title("Cafe\u0301 Culture"),
            theme_service.normalize_title("Café Culture"),
        )

    def test_empty_title(self) -> None:
        self.assertEqual(theme_service.normalize_title(""), "")


class ValidateTitleTests(unittest.TestCase):
    def test_title_is_trimmed(self) -> None:
        self.assertEqual(theme_service.validate_title("  Read  "), "Read")

    def test_empty_title_is_rejected(self) -> None:
        with self.assertRaises(theme_service.MissingTitle):
            theme_service.validate_title("   ")

    def test_long_title_is_rejected(self) -> None:
        with self.assertRaises(theme_service.TitleTooLong):
            theme_service.validate_title("x" * 300)


class ValidateDescriptionTests(unittest.TestCase):
    def test_blank_description_becomes_none(self) -> None:
        self.assertIsNone(theme_service.validate_description("  "))
        self.assertIsNone(theme_service.validate_description(None))

    def test_description_is_trimmed(self) -> None:
        self.assertEqual(
            theme_service.validate_description("  hello  "), "hello"
        )

    def test_long_description_is_rejected(self) -> None:
        with self.assertRaises(theme_service.DescriptionTooLong):
            theme_service.validate_description("x" * 2500)


class ValidateScheduleTests(unittest.TestCase):
    def test_weekday_range(self) -> None:
        self.assertEqual(theme_service.validate_weekday(0), 0)
        self.assertEqual(theme_service.validate_weekday(6), 6)
        with self.assertRaises(theme_service.InvalidWeekday):
            theme_service.validate_weekday(7)

    def test_hour_range(self) -> None:
        self.assertEqual(theme_service.validate_hour(0), 0)
        self.assertEqual(theme_service.validate_hour(23), 23)
        with self.assertRaises(theme_service.InvalidHour):
            theme_service.validate_hour(24)


class IsPostDueTests(unittest.TestCase):
    def _due(self, now: datetime, **overrides) -> bool:
        settings = dict(
            weekday=0,
            hour=12,
            enabled=True,
            last_posted_on=None,
            last_attempted_on=None,
        )
        settings.update(overrides)
        return theme_service.is_post_due(now, **settings)

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
                last_posted_on=date(2026, 10, 19),
                last_attempted_on=date(2026, 10, 19),
            )
        )

    def test_posted_less_than_two_weeks_ago_is_not_due(self) -> None:
        self.assertFalse(
            self._due(
                utc(2026, 10, 19, 12),
                last_posted_on=date(2026, 10, 6),
            )
        )

    def test_posted_two_weeks_ago_is_due(self) -> None:
        self.assertTrue(
            self._due(
                utc(2026, 10, 19, 12),
                last_posted_on=date(2026, 10, 5),
            )
        )

    def test_failed_attempt_today_is_not_due(self) -> None:
        self.assertFalse(
            self._due(
                utc(2026, 10, 19, 12),
                last_posted_on=date(2026, 10, 5),
                last_attempted_on=date(2026, 10, 19),
            )
        )

    def test_failed_attempt_anchors_the_next_run(self) -> None:
        self.assertFalse(
            self._due(
                utc(2026, 10, 26, 12),
                last_posted_on=date(2026, 10, 5),
                last_attempted_on=date(2026, 10, 19),
            )
        )
        self.assertTrue(
            self._due(
                utc(2026, 11, 2, 12),
                last_posted_on=date(2026, 10, 5),
                last_attempted_on=date(2026, 10, 19),
            )
        )


class ThemePeriodTests(unittest.TestCase):
    def test_period_starts_the_day_after_posting(self) -> None:
        start, end = theme_images.theme_period(date(2026, 10, 19))
        self.assertEqual(start, "Oct 20, 2026")
        self.assertEqual(end, "Nov 2, 2026")
        start_date = datetime.strptime(start, "%b %d, %Y").date()
        end_date = datetime.strptime(end, "%b %d, %Y").date()
        self.assertEqual((end_date - start_date).days, 13)

    def test_date_format(self) -> None:
        self.assertEqual(
            theme_images.format_theme_date(date(2026, 10, 20)),
            "Oct 20, 2026",
        )


class BuildThemeEmbedTests(unittest.TestCase):
    def test_uses_the_generated_image(self) -> None:
        embed = theme_service.build_theme_embed(
            "https://example.com/theme.png",
            "Oct 20, 2026",
            "Nov 2, 2026",
            "A prompt",
            FakeClient(),
        )
        self.assertEqual(embed.title, t("biweekly_theme.post.embed_title"))
        self.assertEqual(embed.image.url, "https://example.com/theme.png")
        self.assertEqual(
            embed.description,
            t(
                "biweekly_theme.post.embed_description",
                date_start="Oct 20, 2026",
                date_end="Nov 2, 2026",
                title="A prompt",
            ),
        )


class CreditDescriptionTests(unittest.TestCase):
    def test_credit_with_description(self) -> None:
        result = theme_service.credit_description("Desc", "Alice")
        self.assertEqual(result, f"Desc\n{t('biweekly_theme.suggest.credit', name='Alice')}")

    def test_credit_without_description(self) -> None:
        result = theme_service.credit_description(None, "Bob")
        self.assertEqual(result, t("biweekly_theme.suggest.credit", name="Bob"))

    def test_truncates_when_too_long(self) -> None:
        long_desc = "x" * theme_service.CARD_DESCRIPTION_MAX_LENGTH
        result = theme_service.credit_description(long_desc, "Carol")
        self.assertLessEqual(len(result), theme_service.CARD_DESCRIPTION_MAX_LENGTH)
        self.assertIn(t("biweekly_theme.suggest.credit", name="Carol"), result)


class FormatPostedDateTests(unittest.TestCase):
    def test_date_is_readable(self) -> None:
        self.assertEqual(
            theme_service.format_posted_date(date(2026, 10, 9)),
            "October 9, 2026",
        )


class ThemeRenderTests(unittest.TestCase):
    def test_renders_the_1280x720_template(self) -> None:
        path = theme_images.render_theme(
            prompt="Read a book that features a library or a bookshop.",
            description="Tell us which place you would visit and why.",
            start=theme_images.format_theme_date(date(2026, 10, 20)),
            end=theme_images.format_theme_date(date(2026, 11, 2)),
        )
        try:
            self.assertTrue(path.exists())
            with theme_images.Image.open(path) as image:
                self.assertEqual(image.size, (1280, 720))
                self.assertEqual(image.mode, "RGBA")
        finally:
            path.unlink(missing_ok=True)

    def test_renders_without_a_description(self) -> None:
        path = theme_images.render_theme(
            prompt="Read a book with a map.",
            description=None,
            start=theme_images.format_theme_date(date(2026, 10, 20)),
            end=theme_images.format_theme_date(date(2026, 11, 2)),
        )
        try:
            self.assertTrue(path.exists())
        finally:
            path.unlink(missing_ok=True)


class _FakeHost:
    def __init__(self) -> None:
        self.expirations: list[int] = []

    async def upload(self, path: Path, *, expiration_seconds: int) -> str:
        self.expirations.append(expiration_seconds)
        return "https://example.com/theme.png"


class _FakeMessage:
    def __init__(self, channel, message_id: int) -> None:
        self.channel = channel
        self.id = message_id
        self.pinned = False
        self.unpinned = False
        self.edits: list[dict] = []

    async def pin(self, *, reason: str | None = None) -> None:
        self.pinned = True

    async def unpin(self, *, reason: str | None = None) -> None:
        self.pinned = False
        self.unpinned = True

    async def edit(self, **kwargs) -> "_FakeMessage":
        self.edits.append(kwargs)
        return self


class _FakeChannel:
    def __init__(self, channel_id: int = 99) -> None:
        self.id = channel_id
        self.sent: list[dict] = []
        self.messages: dict[int, _FakeMessage] = {}

    async def send(self, **kwargs) -> _FakeMessage:
        message = _FakeMessage(self, 1000 + len(self.sent))
        self.messages[message.id] = message
        self.sent.append(kwargs)
        return message

    async def fetch_message(self, message_id: int) -> _FakeMessage:
        message = self.messages.get(message_id)
        if message is None:
            raise discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"),
                "Unknown message",
            )
        return message


class _Service(theme_service.BiweeklyThemeService):
    def __init__(self, image_host=None, picked=None) -> None:
        super().__init__(10, image_host)
        self.picked = picked
        self.marked: list[int] = []

    async def pick_card(self):
        return self.picked

    async def mark_card_sent(self, card_id: int, now=None) -> None:
        self.marked.append(card_id)

    async def set_pinned_post(self, channel_id, message_id) -> None:
        self._pinned_channel_id = channel_id
        self._pinned_message_id = message_id


class ThemePostingTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_with_three_month_expiry_and_posts(self) -> None:
        host = _FakeHost()
        channel = _FakeChannel()
        picked = card()
        service = _Service(host, picked)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "theme.png"
            path.write_bytes(b"png")
            with patch.object(
                theme_service, "render_theme", return_value=path
            ):
                posted = await service.post_card(
                    channel,
                    FakeClient(),
                    posted_on=date(2026, 10, 19),
                )

        self.assertIs(posted, picked)
        self.assertEqual(host.expirations, [90 * 24 * 3600])
        self.assertEqual(service.marked, [picked.id])
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(
            channel.sent[0]["embed"].image.url,
            "https://example.com/theme.png",
        )

    async def test_force_card_posts_specific_card(self) -> None:
        host = _FakeHost()
        channel = _FakeChannel()
        picked = card(id=7)
        service = _Service(host, picked)
        service.picked = None

        async def fake_get_card(card_id: int):
            if card_id == 7:
                return picked
            return None

        service.get_card = fake_get_card  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "theme.png"
            path.write_bytes(b"png")
            with patch.object(
                theme_service, "render_theme", return_value=path
            ):
                posted = await service.post_card(
                    channel,
                    FakeClient(),
                    posted_on=date(2026, 10, 19),
                    card_id=7,
                )
        self.assertIs(posted, picked)
        self.assertEqual(service.marked, [7])

    async def test_pins_the_new_card(self) -> None:
        channel = _FakeChannel()
        service = _Service(_FakeHost(), card())

        message = await self._post(service, channel)

        self.assertTrue(message.pinned)
        self.assertEqual(service.pinned_message_id, message.id)
        self.assertEqual(service._pinned_channel_id, channel.id)

    async def test_unpins_the_previous_card(self) -> None:
        channel = _FakeChannel()
        service = _Service(_FakeHost(), card())
        previous = await self._post(service, channel)
        self.assertTrue(previous.pinned)

        newest = await self._post(service, channel)

        self.assertTrue(previous.unpinned)
        self.assertFalse(previous.pinned)
        self.assertTrue(newest.pinned)
        self.assertEqual(service.pinned_message_id, newest.id)
        self.assertTrue(any(edit.get("view") is None for edit in previous.edits))

    async def test_pin_failure_leaves_the_post_untracked(self) -> None:
        channel = _FakeChannel()
        service = _Service(_FakeHost(), card())

        with patch.object(
            _FakeMessage,
            "pin",
            side_effect=discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "no pin"
            ),
        ):
            await self._post(service, channel)

        self.assertIsNone(service.pinned_message_id)

    async def test_render_failure_has_no_fallback(self) -> None:
        host = _FakeHost()
        channel = _FakeChannel()
        picked = card()
        service = _Service(host, picked)

        with patch.object(
            theme_service,
            "render_theme",
            side_effect=theme_images.ThemeRenderError("boom"),
        ):
            with self.assertRaises(theme_service.ThemeImageError) as caught:
                await service.post_card(channel, FakeClient())

        self.assertIs(caught.exception.card, picked)
        self.assertEqual(channel.sent, [])
        self.assertEqual(service.marked, [])
        self.assertEqual(host.expirations, [])

    async def _post(self, service: _Service, channel: _FakeChannel):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "theme.png"
            path.write_bytes(b"png")
            with patch.object(
                theme_service, "render_theme", return_value=path
            ):
                await service.post_card(
                    channel,
                    FakeClient(),
                    posted_on=date(2026, 10, 19),
                )
        return channel.messages[max(channel.messages)]


class ParseCardsTests(unittest.TestCase):
    def _parse(self, content: str) -> list:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "themes.csv"
            path.write_text(content, encoding="utf-8")
            return import_biweekly_theme_cards.parse_cards(path)

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

    def test_theme_header(self) -> None:
        rows = self._parse("theme\nRead a poem\n")
        self.assertEqual([row.title for row in rows], ["Read a poem"])

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
