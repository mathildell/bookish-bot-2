"""Unit tests for the pure logic behind the booster commands.

Run from the repository root::

    python -m unittest discover -s tests -v

No network, database or Discord connection is required: the tests build small
stand-in objects and assert on the resulting embeds/values.
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
import unittest
from pathlib import Path

import discord

from bot import notifications
from bot.commands import booster_react as react_cmds
from bot.commands import booster_role as role_cmds
from bot.commands import level as level_cmds
from bot.commands import say as say_cmds
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
from bot.services import reviews as reviews_service
from bot.services import reference as reference_service
from bot.services.level_rewards import LevelRewardService, sync_member_roles
from bot.services.members import resolve_member_lookups, resolve_members
from bot.services.reviews import FeedData, FeedEntry
from bot.texts import t

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "import_old_xp", ROOT / "scripts" / "import_old_xp.py"
)
import_old_xp = importlib.util.module_from_spec(_spec)
sys.modules["import_old_xp"] = import_old_xp
_spec.loader.exec_module(import_old_xp)


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


class _FakeSource:
    def __init__(self, pages: list[list[discord.Embed]]) -> None:
        self.pages = pages
        self.page_count = len(pages)
        self.total_results = sum(len(page) for page in pages)
        self.fetched: list[int] = []

    async def get_page(self, index: int) -> list[discord.Embed]:
        self.fetched.append(index)
        return self.pages[index]


class _FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.edited: dict | None = None

    async def defer(self) -> None:
        self.deferred = True

    async def edit_message(self, **kwargs) -> None:
        self.edited = kwargs


class _FakeMessage:
    def __init__(self) -> None:
        self.edits: list[dict] = []

    async def edit(self, **kwargs) -> None:
        self.edits.append(kwargs)


class _FakeInteraction:
    def __init__(self) -> None:
        self.response = _FakeResponse()


class LazyPaginatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_pages_load_on_demand_and_cache(self) -> None:
        source = _FakeSource([[discord.Embed()] for _ in range(3)])
        view = Paginator(source=source, author_id=1)

        self.assertEqual(source.fetched, [])
        self.assertEqual(view.page_count, 3)
        self.assertEqual(view.total_results, 3)

        await view.load_page(0)
        self.assertEqual(source.fetched, [0])
        await view.load_page(0)
        self.assertEqual(source.fetched, [0])

        await view.load_page(2)
        self.assertEqual(source.fetched, [0, 2])

    def test_source_navigation_state(self) -> None:
        source = _FakeSource([[discord.Embed()] for _ in range(2)])
        view = Paginator(source=source, author_id=1)
        self.assertTrue(view.prev_button.disabled)
        self.assertFalse(view.next_button.disabled)
        view.index = 1
        view._refresh()
        self.assertFalse(view.prev_button.disabled)
        self.assertTrue(view.next_button.disabled)

    async def test_next_button_loads_next_page(self) -> None:
        source = _FakeSource([[discord.Embed()], [discord.Embed()]])
        view = Paginator(source=source, author_id=1)
        view.message = _FakeMessage()
        interaction = _FakeInteraction()

        await view.next_button.callback(interaction)

        self.assertEqual(view.index, 1)
        self.assertEqual(source.fetched, [1])
        self.assertTrue(interaction.response.deferred)
        self.assertEqual(len(view.message.edits), 1)
        self.assertTrue(view.next_button.disabled)
        self.assertFalse(view.prev_button.disabled)

    async def test_next_button_eager_edits_in_place(self) -> None:
        view = Paginator([[discord.Embed()], [discord.Embed()]], author_id=1)
        interaction = _FakeInteraction()

        await view.next_button.callback(interaction)

        self.assertEqual(view.index, 1)
        self.assertIsNotNone(interaction.response.edited)
        self.assertFalse(interaction.response.deferred)


class LeaderboardSourceTests(unittest.TestCase):
    def test_page_count_rounds_up(self) -> None:
        self.assertEqual(level_cmds._LeaderboardSource(None, 1).page_count, 1)
        self.assertEqual(level_cmds._LeaderboardSource(None, 6).page_count, 1)
        self.assertEqual(level_cmds._LeaderboardSource(None, 7).page_count, 2)
        self.assertEqual(level_cmds._LeaderboardSource(None, 13).page_count, 3)


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

    def test_cumulative_matches_increment_sum(self) -> None:
        total = 0
        for level in range(50):
            self.assertEqual(level_service.total_xp_for_level(level), total)
            total += level_service.xp_increment_to_reach_level(level)

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


class _FakeHTTPResponse:
    status = 403
    reason = "Forbidden"


class FakeNotifyChannel:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self._fail = fail

    async def send(self, content: str) -> None:
        if self._fail:
            raise discord.HTTPException(
                _FakeHTTPResponse(), {"message": "no", "code": 50013}
            )
        self.sent.append(content)


class FakeNotifyUser:
    mention = "<@42>"


class AnnounceLevelUpTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_formatted_message(self) -> None:
        channel = FakeNotifyChannel()
        await notifications.announce_level_up(channel, FakeNotifyUser(), 3)
        self.assertEqual(
            channel.sent,
            [t("level.level_up", user="<@42>", level=3)],
        )

    async def test_missing_channel_is_ignored(self) -> None:
        await notifications.announce_level_up(None, FakeNotifyUser(), 3)

    async def test_send_failure_is_swallowed(self) -> None:
        channel = FakeNotifyChannel(fail=True)
        await notifications.announce_level_up(channel, FakeNotifyUser(), 3)
        self.assertEqual(channel.sent, [])


class LevelProgressTests(unittest.TestCase):
    """The progress shown starts at 0 for every level."""

    class Row:
        def __init__(self, level: int, total_xp: int) -> None:
            self.level = level
            self.total_xp = total_xp
            self.xp_frozen = False

    def _fields(self, level: int, total_xp: int) -> dict[str, str]:
        service = LevelRewardService(1)
        service._rewards = {1: 10}
        row = self.Row(level, total_xp)
        return dict(level_cmds._progress_fields(row, 1, service, None))

    def test_starts_at_zero(self) -> None:
        self.assertEqual(self._fields(0, 0)["Level progress"], "0/75 XP")
        self.assertEqual(self._fields(1, 75)["Level progress"], "0/175 XP")
        self.assertEqual(self._fields(2, 250)["Level progress"], "0/275 XP")

    def test_partial_progress(self) -> None:
        self.assertEqual(self._fields(0, 30)["Level progress"], "30/75 XP")
        self.assertEqual(self._fields(1, 75 + 40)["Level progress"], "40/175 XP")

    def test_progress_bar_segments(self) -> None:
        segments = level_cmds.PROGRESS_BAR_SEGMENTS
        empty = level_cmds._progress_bar(0, 100)
        full = level_cmds._progress_bar(100, 100)
        self.assertEqual(empty.count("<:"), segments)
        self.assertEqual(full.count("<:"), segments)
        self.assertTrue(
            empty.startswith(t("level.xp.level.bar_start_empty"))
            and empty.endswith(t("level.xp.level.bar_end_empty"))
        )
        self.assertTrue(
            full.startswith(t("level.xp.level.bar_start_filled"))
            and full.endswith(t("level.xp.level.bar_end_filled"))
        )

    def test_progress_bar_partial_has_both_states(self) -> None:
        bar = level_cmds._progress_bar(50, 100)
        self.assertIn(t("level.xp.level.bar_middle_filled"), bar)
        self.assertIn(t("level.xp.level.bar_middle_empty"), bar)
        self.assertTrue(bar.startswith(t("level.xp.level.bar_start_filled")))
        self.assertTrue(bar.endswith(t("level.xp.level.bar_end_empty")))

    def test_progress_bar_clamps(self) -> None:
        segments = level_cmds.PROGRESS_BAR_SEGMENTS
        self.assertEqual(
            level_cmds._progress_bar(500, 100).count(
                t("level.xp.level.bar_middle_filled")
            ),
            segments - 2,
        )
        self.assertEqual(
            level_cmds._progress_bar(-5, 100), level_cmds._progress_bar(0, 100)
        )


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
        self.calls: list[str] = []

    async def add_roles(self, role: FakeLevelRole, **_: object) -> None:
        self.calls.append("add")
        if role not in self.roles:
            self.roles.append(role)

    async def remove_roles(self, *roles: FakeLevelRole, **_: object) -> None:
        self.calls.append("remove")
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

    async def test_no_api_call_when_reward_unchanged(self) -> None:
        member = FakeLevelMember(self.roles[10])
        ok = await sync_member_roles(self.guild, member, 4, self.service)
        self.assertTrue(ok)
        self.assertEqual(member.calls, [])


class FakeGuildMember:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeMemberGuild:
    def __init__(self, members, *, absent=(), failing=()) -> None:
        self._members = {member.id: member for member in members}
        self._absent = set(absent)
        self._failing = set(failing)

    def get_member(self, user_id: int):
        return self._members.get(user_id)

    async def fetch_member(self, user_id: int):
        if user_id in self._absent:
            raise discord.NotFound(_FakeHTTPResponse(), "Not Found")
        if user_id in self._failing:
            raise discord.HTTPException(_FakeHTTPResponse(), "boom")
        return self._members[user_id]


class ResolveMemberLookupsTests(unittest.IsolatedAsyncioTestCase):
    async def test_distinguishes_absent_from_failed_lookup(self) -> None:
        guild = FakeMemberGuild([FakeGuildMember(1)], absent=[2], failing=[3])
        lookups = await resolve_member_lookups(guild, [1, 2, 3])
        self.assertEqual(lookups[0].member.id, 1)
        self.assertFalse(lookups[0].absent)
        self.assertIsNone(lookups[1].member)
        self.assertTrue(lookups[1].absent)
        self.assertIsNone(lookups[2].member)
        self.assertFalse(lookups[2].absent)

    async def test_resolve_members_projects_to_members(self) -> None:
        guild = FakeMemberGuild([FakeGuildMember(1)], failing=[2])
        members = await resolve_members(guild, [1, 2])
        self.assertEqual(members[0].id, 1)
        self.assertIsNone(members[1])

    async def test_missing_guild_yields_empty_lookups(self) -> None:
        lookups = await resolve_member_lookups(None, [1])
        self.assertIsNone(lookups[0].member)
        self.assertFalse(lookups[0].absent)


class ImportOldXpTests(unittest.TestCase):
    """The old-XP importer reconstructs total XP from level + progress."""

    def test_parse_amount(self) -> None:
        self.assertEqual(import_old_xp.parse_amount("20"), 20)
        self.assertEqual(import_old_xp.parse_amount("1,200"), 1200)
        self.assertEqual(import_old_xp.parse_amount("3.6k"), 3600)
        self.assertEqual(import_old_xp.parse_amount("22.8k"), 22800)

    def test_parse_table_skips_header_and_separator(self) -> None:
        rows = import_old_xp.parse_table(ROOT / "backup_old_xp.md")
        self.assertTrue(rows)
        self.assertEqual(rows[0].user_id, 477581832611954690)
        self.assertEqual(rows[0].level, 1)
        self.assertEqual(rows[0].progress, 20)

    def test_total_xp_round_trips_to_same_level(self) -> None:
        rows = import_old_xp.parse_table(ROOT / "backup_old_xp.md")
        for row in rows:
            total = (
                level_service.total_xp_for_level(row.level) + row.progress
            )
            self.assertEqual(
                level_service.level_from_total_xp(total),
                row.level,
                msg=f"level {row.level} progress {row.progress}",
            )


FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>alice's bookshelf: all</title>
  <image><title>alice's bookshelf: all</title>
    <url>https://www.goodreads.com/images/layout/goodreads_logo_144.jpg</url>
  </image>
  <item>
    <guid>https://www.goodreads.com/review/show/111?utm_medium=api&amp;utm_source=rss</guid>
    <pubDate>Wed, 14 Oct 2026 10:02:18 -0700</pubDate>
    <title>Book One</title>
    <link>https://www.goodreads.com/review/show/111?utm_medium=api&amp;utm_source=rss</link>
    <book_image_url>https://img.example/cover.jpg</book_image_url>
    <author_name>Author A</author_name>
    <user_rating>4</user_rating>
    <user_read_at>Wed, 14 Oct 2026 00:00:00 +0000</user_read_at>
    <user_shelves>arc, 5-stars-read</user_shelves>
    <user_review><![CDATA[<b>Great</b><br />Loved <i>it</i>.]]></user_review>
  </item>
  <item>
    <guid>https://www.goodreads.com/review/show/222</guid>
    <pubDate>Wed, 14 Oct 2026 11:00:00 -0700</pubDate>
    <title>Book Two</title>
    <link>https://www.goodreads.com/review/show/222</link>
    <user_review></user_review>
  </item>
</channel></rss>
"""


class ParseFeedTests(unittest.TestCase):
    def test_parses_shelf_logo_and_entries(self) -> None:
        feed = reviews_service.parse_feed(FEED_XML)
        self.assertEqual(feed.bookshelf, "alice's bookshelf")
        self.assertTrue(feed.logo_url.endswith("goodreads_logo_144.jpg"))
        self.assertEqual(len(feed.entries), 1)
        entry = feed.entries[0]
        self.assertEqual(entry.entry_id, "111")
        self.assertEqual(entry.title, "Book One")
        self.assertEqual(entry.shelves, ["arc", "5-stars-read"])
        self.assertEqual(entry.author, "Author A")
        self.assertEqual(entry.rating, "4")
        self.assertEqual(entry.cover_url, "https://img.example/cover.jpg")
        self.assertEqual(entry.review_html, "<b>Great</b><br />Loved <i>it</i>.")

    def test_skips_entries_without_a_review(self) -> None:
        feed = reviews_service.parse_feed(FEED_XML)
        self.assertNotIn("222", [entry.entry_id for entry in feed.entries])

    def test_rejects_non_rss(self) -> None:
        with self.assertRaises(ValueError):
            reviews_service.parse_feed("<not-rss/>")


class NormalizeEntryIdTests(unittest.TestCase):
    def test_extracts_review_id(self) -> None:
        self.assertEqual(
            reviews_service.normalize_entry_id(
                "https://www.goodreads.com/review/show/9025538131?utm_medium=api"
            ),
            "9025538131",
        )

    def test_falls_back_to_raw_value(self) -> None:
        self.assertEqual(reviews_service.normalize_entry_id("  abc  "), "abc")


class GoodreadsFeedUrlTests(unittest.TestCase):
    def test_accepts_list_rss_feed(self) -> None:
        self.assertTrue(
            reviews_service.is_goodreads_feed_url(
                "https://www.goodreads.com/review/list_rss/123?key=abc&shelf=%23ALL%23"
            )
        )

    def test_rejects_other_urls(self) -> None:
        self.assertFalse(reviews_service.is_goodreads_feed_url("https://example.com/rss"))
        self.assertFalse(
            reviews_service.is_goodreads_feed_url("https://www.goodreads.com/author/1")
        )


class HtmlToMarkdownTests(unittest.TestCase):
    def test_common_tags(self) -> None:
        result = reviews_service.html_to_markdown(
            "<b>Bold</b> and <i>italic</i><br />next<br/>"
            "<u>under</u> <s>strike</s>"
        )
        self.assertEqual(
            result, "**Bold** and *italic*\nnext\n__under__ ~~strike~~"
        )

    def test_links_use_discord_markdown(self) -> None:
        result = reviews_service.html_to_markdown(
            'See <a href="https://example.com">this</a>.'
        )
        self.assertEqual(result, "See [this](https://example.com).")

    def test_blockquote_and_entities(self) -> None:
        result = reviews_service.html_to_markdown(
            "<blockquote>Tom &amp; Jerry</blockquote>"
        )
        self.assertEqual(result, "> Tom & Jerry")

    def test_collapses_extra_blank_lines(self) -> None:
        result = reviews_service.html_to_markdown("a<br /><br /><br />b")
        self.assertEqual(result, "a\n\nb")


class FormatRatingTests(unittest.TestCase):
    def test_whole_and_decimal(self) -> None:
        self.assertEqual(reviews_service.format_rating("3"), "3 ⭐")
        self.assertEqual(reviews_service.format_rating("3.5"), "3.5 ⭐")

    def test_zero_and_missing_use_placeholder(self) -> None:
        self.assertEqual(reviews_service.format_rating("0"), "No rating")
        self.assertEqual(reviews_service.format_rating(""), "No rating")

    def test_date_read(self) -> None:
        value = datetime.datetime(2026, 10, 6)
        self.assertEqual(
            reviews_service.format_date_read(value),
            f"<t:{int(value.replace(tzinfo=datetime.timezone.utc).timestamp())}:D>",
        )
        self.assertEqual(reviews_service.format_date_read(None), "Unknown")


class FormatShelfTests(unittest.TestCase):
    def test_shelves_are_joined_after_the_bookshelf(self) -> None:
        self.assertEqual(
            reviews_service.format_shelf(
                "alice's bookshelf", ["5-stars-read", "fav"]
            ),
            "alice's bookshelf: 5-stars-read, fav",
        )

    def test_no_shelves_shows_just_the_bookshelf(self) -> None:
        self.assertEqual(
            reviews_service.format_shelf("alice's bookshelf", []),
            "alice's bookshelf",
        )

    def test_arc_shelf_gets_star(self) -> None:
        self.assertEqual(
            reviews_service.format_shelf("alice's bookshelf", ["arc"]),
            t(
                "reviews.embed.shelf_arc",
                shelf="alice's bookshelf: arc",
            ),
        )

    def test_arc_variants_get_star(self) -> None:
        for shelf in ("ARC", "arcs", "ARCs"):
            self.assertEqual(
                reviews_service.format_shelf("alice's bookshelf", [shelf]),
                t(
                    "reviews.embed.shelf_arc",
                    shelf=f"alice's bookshelf: {shelf}",
                ),
            )

    def test_non_arc_shelves_are_unchanged(self) -> None:
        self.assertEqual(
            reviews_service.format_shelf("alice's bookshelf", ["arcade"]),
            "alice's bookshelf: arcade",
        )

    def test_all_shelf_is_dropped(self) -> None:
        self.assertEqual(reviews_service.parse_shelves("all"), [])
        self.assertEqual(
            reviews_service.parse_shelves("all, arc"), ["arc"]
        )


class _FakeLogoResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self) -> "_FakeLogoResponse":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


class _FakeHTTP:
    def __init__(self, status: int) -> None:
        self.status = status
        self.calls = 0

    def get(self, _url: str) -> _FakeLogoResponse:
        self.calls += 1
        return _FakeLogoResponse(self.status)


class ResolveLogoTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_url_when_reachable(self) -> None:
        logo = await reviews_service.resolve_logo(_FakeHTTP(200), "https://logo", {})
        self.assertEqual(logo, "https://logo")

    async def test_falls_back_on_404(self) -> None:
        logo = await reviews_service.resolve_logo(_FakeHTTP(404), "https://logo", {})
        self.assertEqual(logo, reviews_service.FALLBACK_LOGO_URL)

    async def test_missing_url_uses_fallback(self) -> None:
        logo = await reviews_service.resolve_logo(_FakeHTTP(200), None, {})
        self.assertEqual(logo, reviews_service.FALLBACK_LOGO_URL)

    async def test_result_is_cached(self) -> None:
        http = _FakeHTTP(200)
        cache: dict[str, str] = {}
        await reviews_service.resolve_logo(http, "https://logo", cache)
        await reviews_service.resolve_logo(http, "https://logo", cache)
        self.assertEqual(http.calls, 1)


class ShouldPostTests(unittest.TestCase):
    class Entry:
        def __init__(self, pub_date: datetime.datetime) -> None:
            self.pub_date = pub_date

    class Row:
        def __init__(
            self,
            last_pub_date: datetime.datetime,
            last_posted_at: datetime.datetime | None = None,
        ) -> None:
            self.last_pub_date = last_pub_date
            self.last_posted_at = last_posted_at

    NOW = datetime.datetime(2026, 10, 14, 12, 0, 0)
    RECENT = datetime.datetime(2026, 10, 14, 11, 0, 0)
    OLD = datetime.datetime(2026, 10, 13, 10, 0, 0)

    def _entry(self, moment: datetime.datetime) -> "ShouldPostTests.Entry":
        return self.Entry(moment)

    def test_untracked_without_tracking_time_is_skipped(self) -> None:
        self.assertFalse(
            reviews_service.should_post(self._entry(self.NOW), None, self.NOW, None)
        )

    def test_backlog_is_skipped(self) -> None:
        feed_since = datetime.datetime(2026, 10, 10)
        self.assertFalse(
            reviews_service.should_post(
                self._entry(datetime.datetime(2026, 10, 1)), None, self.NOW, feed_since
            )
        )

    def test_new_entry_is_posted(self) -> None:
        feed_since = datetime.datetime(2026, 10, 10)
        self.assertTrue(
            reviews_service.should_post(self._entry(self.NOW), None, self.NOW, feed_since)
        )

    def test_unchanged_entry_is_skipped(self) -> None:
        row = self.Row(self.NOW, self.OLD)
        self.assertFalse(
            reviews_service.should_post(self._entry(self.NOW), row, self.NOW, self.OLD)
        )

    def test_edit_after_cooldown_is_posted(self) -> None:
        entry = self._entry(self.NOW)
        row = self.Row(self.OLD, datetime.datetime(2026, 10, 13))
        self.assertTrue(
            reviews_service.should_post(entry, row, self.NOW, datetime.datetime(2026, 1, 1))
        )

    def test_edit_within_cooldown_is_skipped(self) -> None:
        entry = self._entry(self.NOW)
        row = self.Row(self.OLD, self.RECENT)
        self.assertFalse(
            reviews_service.should_post(entry, row, self.NOW, datetime.datetime(2026, 1, 1))
        )

    def test_no_last_post_allows_post_on_edit(self) -> None:
        entry = self._entry(self.NOW)
        row = self.Row(self.OLD, None)
        self.assertTrue(
            reviews_service.should_post(entry, row, self.NOW, datetime.datetime(2026, 1, 1))
        )


class BuildReviewEmbedTests(unittest.TestCase):
    def _entry(self) -> FeedEntry:
        return FeedEntry(
            entry_id="111",
            link="https://www.goodreads.com/review/show/111",
            title="Book One",
            cover_url="https://img.example/cover.jpg",
            author="Author A",
            rating="3.5",
            date_read=datetime.datetime(2026, 10, 6),
            review_html="<b>Great</b><br />Loved it.",
            pub_date=datetime.datetime(2026, 10, 14),
            shelves=["arc", "5-stars-read"],
        )

    def _feed(self) -> FeedData:
        return FeedData(
            bookshelf="mtrics's bookshelf",
            logo_url="https://www.goodreads.com/logo.jpg",
            entries=[],
        )

    def test_layout(self) -> None:
        embed = reviews_service.build_review_embed(
            self._entry(), self._feed(), FakeMember(), FakeClient
        )
        self.assertTrue(embed.title.endswith(t("reviews.embed.title_suffix")))
        self.assertEqual(embed.url, "https://www.goodreads.com/review/show/111")
        self.assertEqual(embed.thumbnail.url, "https://img.example/cover.jpg")
        self.assertEqual(
            [field.name for field in embed.fields],
            ["Author(s)", "Date read", "Rating", "Review"],
        )
        self.assertTrue(embed.fields[0].inline)
        self.assertTrue(embed.fields[2].inline)
        self.assertFalse(embed.fields[3].inline)
        self.assertEqual(embed.fields[0].value, "Author A")
        self.assertEqual(
            embed.fields[2].value,
            t("reviews.embed.rating_value", rating="3.5"),
        )
        self.assertIn("**Great**", embed.fields[3].value)
        self.assertEqual(
            embed.footer.text,
            t(
                "reviews.embed.shelf_arc",
                shelf="mtrics's bookshelf: arc, 5-stars-read",
            ),
        )
        self.assertEqual(embed.footer.icon_url, "https://www.goodreads.com/logo.jpg")
        self.assertEqual(embed.colour.value, 0x97A97C)


class FakeReferenceMessage:
    def __init__(self, content: str = "", embeds: list | None = None) -> None:
        self.id = 1411841682940493904
        self.content = content
        self.embeds = embeds or []


class FakeReferenceChannel:
    def __init__(self, message: object | None = None, *, fail: bool = False) -> None:
        self._message = message
        self._fail = fail
        self.fetched: list[int] = []

    async def fetch_message(self, message_id: int) -> object:
        self.fetched.append(message_id)
        if self._fail:
            raise discord.HTTPException(
                _FakeHTTPResponse(), {"message": "no", "code": 10008}
            )
        return self._message


class FakeReferenceBot:
    def __init__(
        self,
        channel: object | None = None,
        *,
        channel_id: int = 10,
        message_id: int = 20,
    ) -> None:
        self.reference_channel_id = channel_id
        self.reference_message_id = message_id
        self._channel = channel

    def get_channel(self, channel_id: int) -> object | None:
        return self._channel

    async def fetch_channel(self, channel_id: int) -> object | None:
        return self._channel


class FakeReferenceSent:
    def __init__(self, *, pin_fail: bool = False) -> None:
        self.id = 99
        self.pinned = False
        self._pin_fail = pin_fail

    async def pin(self) -> None:
        if self._pin_fail:
            raise discord.HTTPException(
                _FakeHTTPResponse(), {"message": "no", "code": 50013}
            )
        self.pinned = True


class FakeReferenceTarget:
    def __init__(self, *, fail: bool = False, pin_fail: bool = False) -> None:
        self._fail = fail
        self._pin_fail = pin_fail
        self.sent: list[dict] = []
        self.messages: list[FakeReferenceSent] = []

    async def send(self, **kwargs: object) -> FakeReferenceSent:
        if self._fail:
            raise discord.HTTPException(
                _FakeHTTPResponse(), {"message": "no", "code": 50013}
            )
        sent = FakeReferenceSent(pin_fail=self._pin_fail)
        self.sent.append(kwargs)
        self.messages.append(sent)
        return sent


class ReferencePayloadTests(unittest.TestCase):
    def test_content_only(self) -> None:
        payload = reference_service.reference_payload(
            FakeReferenceMessage(content="hi")
        )
        self.assertEqual(payload, {"content": "hi", "embeds": None})

    def test_content_and_embeds(self) -> None:
        embed = discord.Embed(title="t")
        payload = reference_service.reference_payload(
            FakeReferenceMessage(content="hi", embeds=[embed])
        )
        self.assertEqual(payload["content"], "hi")
        self.assertEqual(payload["embeds"], [embed])

    def test_empty_is_none(self) -> None:
        self.assertIsNone(
            reference_service.reference_payload(FakeReferenceMessage())
        )


class FetchReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_from_configured_ids(self) -> None:
        message = FakeReferenceMessage(content="hi")
        channel = FakeReferenceChannel(message)
        bot = FakeReferenceBot(
            channel,
            channel_id=1406126242192953404,
            message_id=1411841682940493904,
        )
        self.assertIs(await reference_service.fetch_reference(bot), message)
        self.assertEqual(channel.fetched, [1411841682940493904])

    async def test_disabled_when_ids_missing(self) -> None:
        bot = FakeReferenceBot(FakeReferenceChannel(), channel_id=0, message_id=0)
        self.assertIsNone(await reference_service.fetch_reference(bot))

    async def test_fetch_failure_is_none(self) -> None:
        bot = FakeReferenceBot(FakeReferenceChannel(fail=True))
        self.assertIsNone(await reference_service.fetch_reference(bot))

    async def test_unreachable_channel_is_none(self) -> None:
        bot = FakeReferenceBot(None)
        self.assertIsNone(await reference_service.fetch_reference(bot))


class PostReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_and_pins(self) -> None:
        embed = discord.Embed(title="t")
        bot = FakeReferenceBot(
            FakeReferenceChannel(FakeReferenceMessage(content="rules", embeds=[embed]))
        )
        target = FakeReferenceTarget()
        self.assertTrue(await reference_service.post_reference(bot, target))
        self.assertEqual(target.sent, [{"content": "rules", "embeds": [embed]}])
        self.assertTrue(target.messages[0].pinned)

    async def test_empty_reference_is_skipped(self) -> None:
        bot = FakeReferenceBot(FakeReferenceChannel(FakeReferenceMessage()))
        target = FakeReferenceTarget()
        self.assertFalse(await reference_service.post_reference(bot, target))
        self.assertEqual(target.sent, [])

    async def test_send_failure_is_swallowed(self) -> None:
        bot = FakeReferenceBot(
            FakeReferenceChannel(FakeReferenceMessage(content="rules"))
        )
        target = FakeReferenceTarget(fail=True)
        self.assertFalse(await reference_service.post_reference(bot, target))

    async def test_pin_failure_still_counts_as_posted(self) -> None:
        bot = FakeReferenceBot(
            FakeReferenceChannel(FakeReferenceMessage(content="rules"))
        )
        target = FakeReferenceTarget(pin_fail=True)
        self.assertTrue(await reference_service.post_reference(bot, target))
        self.assertFalse(target.messages[0].pinned)


class ParseMessageIdTests(unittest.TestCase):
    def test_bare_id(self) -> None:
        self.assertEqual(
            say_cmds.parse_message_id("1411841682940493904"),
            1411841682940493904,
        )

    def test_bare_id_with_whitespace(self) -> None:
        self.assertEqual(say_cmds.parse_message_id("  123  "), 123)

    def test_message_link(self) -> None:
        self.assertEqual(
            say_cmds.parse_message_id(
                "https://discord.com/channels/"
                "1405910078342565979/1406126242192953404/1411841682940493904"
            ),
            1411841682940493904,
        )

    def test_link_host_variants(self) -> None:
        for host in ("discordapp.com", "ptb.discord.com", "canary.discord.com"):
            self.assertEqual(
                say_cmds.parse_message_id(f"http://{host}/channels/1/2/345"), 345
            )

    def test_invalid_values(self) -> None:
        for value in ("", "abc", "123abc", "https://example.com/1/2/3"):
            self.assertIsNone(say_cmds.parse_message_id(value))


class ValidMessageTests(unittest.TestCase):
    def test_accepts_text_with_emojis(self) -> None:
        self.assertTrue(say_cmds.is_valid_message("hi <:Bunny:123> \U0001F600"))

    def test_rejects_blank(self) -> None:
        self.assertFalse(say_cmds.is_valid_message("   "))

    def test_rejects_too_long(self) -> None:
        self.assertFalse(
            say_cmds.is_valid_message("a" * (say_cmds.MESSAGE_LIMIT + 1))
        )

    def test_accepts_exactly_the_limit(self) -> None:
        self.assertTrue(say_cmds.is_valid_message("a" * say_cmds.MESSAGE_LIMIT))


class PingTextTests(unittest.TestCase):
    def test_response_includes_latency(self) -> None:
        self.assertIn("42", t("ping.response", latency=42))


if __name__ == "__main__":
    unittest.main()
