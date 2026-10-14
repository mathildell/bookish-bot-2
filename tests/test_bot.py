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
from bot.commands import level as level_cmds
from bot.commands.listing import (
    Paginator,
    boost_value,
    chunked,
    parse_hex_colour,
)
from bot.services import level as level_service
from bot.services.booster_react import (
    BoosterReactService,
    build_identifiers,
    clean_identifier,
    parse_emoji,
)
from bot.services.level_rewards import LevelRewardService, sync_member_roles


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


class FindMatchesTests(unittest.TestCase):
    def _service(self) -> BoosterReactService:
        service = BoosterReactService()
        leah = FakeRow(
            discord_user_id=7,
            identifiers=build_identifiers(7, "leah", "Leah", None),
        )
        bella = FakeRow(
            discord_user_id=8,
            identifiers=build_identifiers(8, "bella", "Bella", None),
        )
        service._records = {
            7: service._build_entry(leah),
            8: service._build_entry(bella),
        }
        return service

    def test_matches_identifier(self) -> None:
        service = self._service()
        matches = service.find_matches("hey Leah how are you")
        self.assertEqual([m["discord_user_id"] for m in matches], [7])

    def test_matches_everyone_mentioned(self) -> None:
        service = self._service()
        matches = service.find_matches("@Leah @Bella check this out")
        self.assertEqual([m["discord_user_id"] for m in matches], [7, 8])

    def test_no_match(self) -> None:
        service = self._service()
        self.assertEqual(service.find_matches("hello world"), [])

    def test_respects_word_boundaries(self) -> None:
        service = self._service()
        self.assertEqual(service.find_matches("Salivate"), [])


class EmojiOwnerTests(unittest.TestCase):
    def _service(self, entries) -> BoosterReactService:
        service = BoosterReactService()
        service._records = {
            user_id: service._build_entry(
                FakeRow(discord_user_id=user_id, emoji=emoji)
            )
            for user_id, emoji in entries
        }
        return service

    def test_unused_emoji(self) -> None:
        service = self._service([(7, "😀")])
        self.assertIsNone(service.emoji_owner("🐱", exclude_user_id=8))

    def test_detects_duplicate_unicode(self) -> None:
        service = self._service([(7, "😀")])
        self.assertEqual(service.emoji_owner("😀", exclude_user_id=8), 7)

    def test_custom_emoji_matched_by_id(self) -> None:
        service = self._service([(7, "<:cat:123>")])
        self.assertEqual(
            service.emoji_owner("<:other:123>", exclude_user_id=8), 7
        )

    def test_ignores_the_requesting_user(self) -> None:
        service = self._service([(7, "😀")])
        self.assertIsNone(service.emoji_owner("😀", exclude_user_id=7))


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


class LevelFormulaTests(unittest.TestCase):
    """The XP curve described in the feature spec."""

    def test_increment_per_level(self) -> None:
        self.assertEqual(level_service.xp_increment_to_reach_level(0), 75)
        self.assertEqual(level_service.xp_increment_to_reach_level(1), 175)
        self.assertEqual(level_service.xp_increment_to_reach_level(2), 275)
        self.assertEqual(level_service.xp_increment_to_reach_level(5), 575)

    def test_cumulative_totals(self) -> None:
        self.assertEqual(level_service.total_xp_for_level(0), 0)
        self.assertEqual(level_service.total_xp_for_level(1), 75)
        self.assertEqual(level_service.total_xp_for_level(2), 250)
        self.assertEqual(level_service.total_xp_for_level(3), 525)

    def test_level_from_total_xp(self) -> None:
        self.assertEqual(level_service.level_from_total_xp(0), 0)
        self.assertEqual(level_service.level_from_total_xp(74), 0)
        self.assertEqual(level_service.level_from_total_xp(75), 1)
        self.assertEqual(level_service.level_from_total_xp(249), 1)
        self.assertEqual(level_service.level_from_total_xp(250), 2)

    def test_random_gain_within_bounds(self) -> None:
        for _ in range(200):
            self.assertGreaterEqual(level_service.random_xp_gain(), 15)
            self.assertLessEqual(level_service.random_xp_gain(), 40)


class LevelRewardServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = LevelRewardService(1)
        self.service._rewards = {1: 10, 5: 50, 10: 100}

    def test_role_for_level_picks_highest_reached(self) -> None:
        self.assertIsNone(self.service.role_for_level(0))
        self.assertEqual(self.service.role_for_level(1), 10)
        self.assertEqual(self.service.role_for_level(4), 10)
        self.assertEqual(self.service.role_for_level(5), 50)
        self.assertEqual(self.service.role_for_level(999), 100)

    def test_role_ids(self) -> None:
        self.assertEqual(self.service.role_ids, {10, 50, 100})


class LevelProgressTests(unittest.TestCase):
    """The progress shown starts at 0 for every level."""

    class Row:
        def __init__(self, level: int, total_xp: int) -> None:
            self.level = level
            self.total_xp = total_xp

    def _fields(self, level: int, total_xp: int) -> dict[str, str]:
        service = LevelRewardService(1)
        service._rewards = {1: 10}
        row = self.Row(level, total_xp)
        return dict(level_cmds._progress_fields(row, service, None))

    def test_starts_at_zero(self) -> None:
        self.assertEqual(self._fields(0, 0)["Level progress"], "0/75")
        self.assertEqual(self._fields(1, 75)["Level progress"], "0/175")
        self.assertEqual(self._fields(2, 250)["Level progress"], "0/275")

    def test_partial_progress(self) -> None:
        self.assertEqual(self._fields(0, 30)["Level progress"], "30/75")
        self.assertEqual(self._fields(1, 75 + 40)["Level progress"], "40/175")


class FakeLevelRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id
        self.mention = f"<@&{role_id}>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakeLevelRole) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)


class FakeLevelMember:
    def __init__(self, *roles: FakeLevelRole) -> None:
        self.id = 42
        self.roles = list(roles)

    async def add_roles(self, role: FakeLevelRole, **_: object) -> None:
        if role not in self.roles:
            self.roles.append(role)

    async def remove_roles(self, *roles: FakeLevelRole, **_: object) -> None:
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)


class FakeLevelGuild:
    def __init__(self, *roles: FakeLevelRole) -> None:
        self._roles = {role.id: role for role in roles}

    def get_role(self, role_id: int) -> FakeLevelRole | None:
        return self._roles.get(role_id)


class SyncMemberRolesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = LevelRewardService(1)
        self.service._rewards = {1: 10, 5: 50, 10: 100}
        self.roles = {
            rid: FakeLevelRole(rid) for rid in (10, 50, 100, 999)
        }
        self.guild = FakeLevelGuild(*self.roles.values())

    async def test_promotes_and_removes_previous_reward(self) -> None:
        member = FakeLevelMember(self.roles[10])
        ok = await sync_member_roles(self.guild, member, 5, self.service)
        self.assertTrue(ok)
        self.assertEqual({r.id for r in member.roles}, {50})

    async def test_demotion_removes_higher_reward(self) -> None:
        member = FakeLevelMember(self.roles[100])
        await sync_member_roles(self.guild, member, 1, self.service)
        self.assertEqual({r.id for r in member.roles}, {10})

    async def test_below_first_reward_clears_all(self) -> None:
        member = FakeLevelMember(self.roles[10], self.roles[999])
        await sync_member_roles(self.guild, member, 0, self.service)
        self.assertEqual({r.id for r in member.roles}, {999})

    async def test_non_reward_roles_are_left_alone(self) -> None:
        member = FakeLevelMember(self.roles[10], self.roles[999])
        await sync_member_roles(self.guild, member, 10, self.service)
        self.assertEqual({r.id for r in member.roles}, {999, 100})


if __name__ == "__main__":
    unittest.main()
