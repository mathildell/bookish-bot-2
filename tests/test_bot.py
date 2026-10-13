"""Unit tests for the pure logic behind the booster commands.

Run from the repository root::

    python -m unittest discover -s tests -v

No network, database or Discord connection is required: the tests build small
stand-in objects and assert on the resulting embeds/values.
"""

from __future__ import annotations

import datetime
import unittest

import discord

from bot.commands import booster_react as react_cmds
from bot.commands import booster_role as role_cmds
from bot.commands.listing import (
    Paginator,
    boost_value,
    chunked,
    parse_hex_colour,
)
from bot.services.booster_react import (
    BoosterReactService,
    build_identifiers,
    clean_identifier,
    parse_emoji,
)


class FakeClient:
    transparent_embed_image = "https://example.com/bg.png"
    default_embed_color = discord.Colour(0x97A97C)


class FakeMember:
    display_name = "Alice"
    premium_since: datetime.datetime | None = None
    display_avatar = None


class FakeColour:
    def __init__(self, value: int) -> None:
        self.value = value


class FakeRole:
    mention = "<@&5>"
    colour = FakeColour(0xFF00FF)
    secondary_colour: FakeColour | None = None
    display_icon: str | None = "😀"


class FakeRow:
    def __init__(self, **kwargs) -> None:
        self.discord_user_id = 7
        self.role_id = 5
        self.emoji = "😀"
        self.identifiers: list[str] = []
        self.__dict__.update(kwargs)


class ParseEmojiTests(unittest.TestCase):
    def test_custom_emoji(self) -> None:
        parsed = parse_emoji("<:blob:12345>")
        self.assertIsInstance(parsed, discord.PartialEmoji)
        self.assertEqual(parsed.name, "blob")
        self.assertEqual(parsed.id, 12345)

    def test_animated_emoji(self) -> None:
        parsed = parse_emoji("<a:dance:999>")
        self.assertTrue(parsed.animated)
        self.assertEqual(parsed.id, 999)

    def test_unicode_emoji_is_returned_as_is(self) -> None:
        self.assertEqual(parse_emoji("😀"), "😀")

    def test_empty_emoji_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_emoji("")


class CleanIdentifierTests(unittest.TestCase):
    def test_strips_disallowed_characters(self) -> None:
        self.assertEqual(clean_identifier("  Foo_Bar!!  "), "foo_bar")

    def test_collapses_whitespace(self) -> None:
        self.assertEqual(clean_identifier("a  b\t c"), "a b c")

    def test_drops_non_ascii(self) -> None:
        self.assertEqual(clean_identifier("Joé"), "jo")


class BuildIdentifiersTests(unittest.TestCase):
    def test_order_and_deduplication(self) -> None:
        identifiers = build_identifiers(
            42, "global", "ServerNick", "Bookish, global , Extra"
        )
        self.assertEqual(identifiers[0], "42")
        self.assertIn("ServerNick", identifiers)
        self.assertIn("Extra", identifiers)
        self.assertEqual(identifiers.count("global"), 1)
        self.assertEqual(identifiers.count("Extra"), 1)

    def test_skips_nickname_equal_to_username(self) -> None:
        identifiers = build_identifiers(1, "alice", "Alice", "alice")
        self.assertEqual(
            [value.lower() for value in identifiers].count("alice"), 1
        )


class FindMatchingTests(unittest.TestCase):
    def _service(self) -> BoosterReactService:
        service = BoosterReactService()
        row = FakeRow(identifiers=build_identifiers(7, "alice", "Alice", "Ali"))
        service._records = {7: service._build_entry(row)}
        return service

    def test_matches_identifier(self) -> None:
        service = self._service()
        self.assertIsNotNone(service.find_matching("hey Ali how are you"))

    def test_no_match(self) -> None:
        service = self._service()
        self.assertIsNone(service.find_matching("hello world"))

    def test_respects_word_boundaries(self) -> None:
        service = self._service()
        self.assertIsNone(service.find_matching("Salivate"))


class ChunkedTests(unittest.TestCase):
    def test_chunks_evenly(self) -> None:
        self.assertEqual(list(chunked([1, 2, 3, 4, 5], 2)), [[1, 2], [3, 4], [5]])

    def test_empty(self) -> None:
        self.assertEqual(list(chunked([], 3)), [])


class BoostValueTests(unittest.TestCase):
    def test_not_boosting(self) -> None:
        self.assertEqual(boost_value(None), "Not boosting")
        self.assertEqual(boost_value(FakeMember()), "Not boosting")

    def test_boosting(self) -> None:
        member = FakeMember()
        member.premium_since = datetime.datetime(2024, 1, 2, 3, 4, 5)
        self.assertEqual(
            boost_value(member),
            f"<t:{int(member.premium_since.timestamp())}:f>",
        )


class ParseHexColourTests(unittest.TestCase):
    def test_accepts_hash_and_bare_hex(self) -> None:
        self.assertEqual(parse_hex_colour("#97A97C").value, 0x97A97C)
        self.assertEqual(parse_hex_colour("97a97c").value, 0x97A97C)

    def test_invalid_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_hex_colour("not-a-colour")


class ReactEmbedTests(unittest.TestCase):
    def test_custom_emoji_uses_thumbnail_and_names_field(self) -> None:
        row = FakeRow(emoji="<:blob:1>", identifiers=["7", "Alice", "alice"])
        embed = react_cmds._react_embed(row, None, FakeClient)
        self.assertEqual(embed.fields[0].name, "Names")
        self.assertEqual(embed.fields[1].name, "Boost date")
        self.assertEqual(embed.image.url, FakeClient.transparent_embed_image)
        self.assertEqual(embed.colour.value, 0x97A97C)

    def test_unicode_emoji_becomes_description(self) -> None:
        row = FakeRow(emoji="😀", identifiers=["7", "Alice", "alice"])
        embed = react_cmds._react_embed(row, None, FakeClient)
        self.assertEqual(embed.description, "# 😀")

    def test_no_aliases_placeholder(self) -> None:
        row = FakeRow(emoji="😀", identifiers=["7"])
        embed = react_cmds._react_embed(row, None, FakeClient)
        self.assertEqual(embed.fields[0].value, "(no extra aliases)")


class RoleEmbedTests(unittest.TestCase):
    def test_fields_and_values(self) -> None:
        embed = role_cmds._role_embed(None, FakeRole(), FakeClient)
        self.assertEqual(
            [field.name for field in embed.fields],
            ["Color", "Gradient", "Emoji/Icon", "Boost date"],
        )
        self.assertEqual(embed.fields[0].value, "#FF00FF")
        self.assertEqual(embed.fields[1].value, "None")
        self.assertEqual(embed.fields[2].value, "😀")
        self.assertEqual(embed.colour.value, 0x97A97C)

    def test_missing_role_has_no_fields(self) -> None:
        embed = role_cmds._role_embed(None, None, FakeClient)
        self.assertEqual(embed.fields, [])


class PaginatorTests(unittest.TestCase):
    def _pages(self, count: int) -> list[list[discord.Embed]]:
        return [[discord.Embed()] for _ in range(count)]

    def test_first_page_state(self) -> None:
        view = Paginator(self._pages(3), author_id=1, total_results=3)
        self.assertTrue(view.prev_button.disabled)
        self.assertFalse(view.next_button.disabled)
        self.assertEqual(view.page_label.label, "Page 1 of 3 • 3 results")

    def test_last_page_state(self) -> None:
        view = Paginator(self._pages(3), author_id=1, total_results=3)
        view.index = 2
        view._refresh()
        self.assertTrue(view.next_button.disabled)
        self.assertEqual(view.page_label.label, "Page 3 of 3 • 3 results")

    def test_single_page_disables_navigation(self) -> None:
        view = Paginator(self._pages(1), author_id=1)
        self.assertTrue(view.prev_button.disabled)
        self.assertTrue(view.next_button.disabled)


if __name__ == "__main__":
    unittest.main()
