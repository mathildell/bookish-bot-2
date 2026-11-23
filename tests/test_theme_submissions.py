"""Unit tests for the bi-weekly theme submission rewards."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bot.services import level as level_service
from bot.services import theme_submissions as subs
from bot.services.biweekly_theme import ThemeCycle

EMOJI = "⭐"


def member(user_id: int, *, admin: bool = False, bot: bool = False):
    m = MagicMock(spec=discord.Member)
    m.id = user_id
    m.bot = bot
    m.guild_permissions = SimpleNamespace(manage_guild=admin)
    m.guild = MagicMock()
    m.guild.get_member.return_value = None
    return m


def reaction(emoji, users: list, me: bool = False):
    r = MagicMock()
    r.emoji = emoji
    r.me = me

    async def iterate():
        for user in users:
            yield user

    r.users = iterate
    return r


def message(author, reactions):
    return SimpleNamespace(author=author, reactions=reactions)


def channel_with(messages):
    async def history(**kwargs):
        for m in messages:
            yield m

    return SimpleNamespace(history=history, id=1, guild=member(0).guild)


class TestHelpers(unittest.TestCase):
    def test_unique_labels(self) -> None:
        self.assertEqual(
            subs.unique_labels(["Sam", "Ana", "Sam", "Sam"]),
            ["Sam", "Ana", "Sam (2)", "Sam (3)"],
        )

    def test_split_lines_respects_limit(self) -> None:
        blocks = subs.split_lines(["a" * 10] * 5, limit=25)
        self.assertEqual(len(blocks), 3)
        self.assertTrue(all(len(b) <= 25 for b in blocks))

    def test_progress_text_same_level_and_level_up(self) -> None:
        stay = level_service.preview_level_change(1, 200, 1000)
        text = subs.progress_text(stay, xp=1000)
        self.assertIn("1000", text)
        up = level_service.preview_level_change(0, 0, 5000)
        self.assertGreater(up.new_level, up.old_level)
        self.assertIn("→", subs.progress_text(up, xp=5000))


class TestFindParticipants(unittest.IsolatedAsyncioTestCase):
    async def test_requires_admin_reaction(self) -> None:
        admin = member(1, admin=True)
        regular = member(2)
        alice, bob, carol = member(10), member(11), member(12)
        channel = channel_with(
            [
                message(alice, [reaction(EMOJI, [admin])]),
                message(bob, [reaction(EMOJI, [regular])]),
                message(carol, [reaction("👍", [admin])]),
            ]
        )
        result = await subs.find_participants(
            channel, after_message_id=5, emoji=EMOJI
        )
        self.assertEqual([m.id for m in result], [10])

    async def test_bot_reaction_counts(self) -> None:
        alice, bob = member(10), member(11)
        channel = channel_with(
            [
                message(alice, [reaction(EMOJI, [], me=True)]),
                message(bob, [reaction(EMOJI, [member(2)])]),
            ]
        )
        result = await subs.find_participants(
            channel, after_message_id=5, emoji=EMOJI
        )
        self.assertEqual([m.id for m in result], [10])

    async def test_uncached_author_counts(self) -> None:
        stranger = SimpleNamespace(id=50, bot=False)
        channel = channel_with(
            [message(stranger, [reaction(EMOJI, [], me=True)])]
        )
        result = await subs.find_participants(
            channel, after_message_id=5, emoji=EMOJI
        )
        self.assertEqual([m.id for m in result], [50])

    async def test_author_counted_once_and_bots_skipped(self) -> None:
        admin = member(1, admin=True)
        alice = member(10)
        robot = member(99, bot=True)
        channel = channel_with(
            [
                message(alice, [reaction(EMOJI, [admin])]),
                message(alice, [reaction(EMOJI, [admin])]),
                message(robot, [reaction(EMOJI, [admin])]),
            ]
        )
        result = await subs.find_participants(
            channel, after_message_id=5, emoji=EMOJI
        )
        self.assertEqual([m.id for m in result], [10])


class TestReactionMatches(unittest.TestCase):
    def test_unicode_and_custom(self) -> None:
        self.assertTrue(subs.reaction_matches(reaction(EMOJI, []), EMOJI))
        self.assertFalse(subs.reaction_matches(reaction("👍", []), EMOJI))
        self.assertTrue(subs.reaction_matches(reaction("⭐", []), "⭐\ufe0f"))
        custom = reaction(SimpleNamespace(id=42), [])
        self.assertTrue(subs.reaction_matches(custom, "<:star:42>"))
        self.assertFalse(subs.reaction_matches(custom, "<:star:43>"))


class TestThemeCycle(unittest.TestCase):
    def test_round_trip(self) -> None:
        change = level_service.preview_level_change(1, 200, 1000)
        cycle = ThemeCycle(
            started_at=datetime(2026, 11, 22, 12),
            message_id=7,
            participants=[10, 11],
            winner=11,
            changes={10: change, 11: change},
            winner_change=change,
            announced=True,
            ready_at=datetime(2026, 11, 22, 12, 10),
        )
        self.assertEqual(ThemeCycle.from_dict(cycle.to_dict()), cycle)

    def test_stale(self) -> None:
        cycle = ThemeCycle(
            started_at=datetime(2026, 11, 22, 12),
            message_id=None,
            participants=[],
            winner=None,
            changes={},
        )
        self.assertFalse(subs.cycle_is_stale(cycle, datetime(2026, 11, 22, 13)))
        self.assertTrue(subs.cycle_is_stale(cycle, datetime(2026, 11, 23, 13)))


class FakeService:
    def __init__(self, pinned=7, settled=None, pinned_channel=1) -> None:
        self.pinned_message_id = pinned
        self.settled_message_id = settled
        self.pinned_channel_id = pinned_channel
        self.submission_emoji = EMOJI
        self.started = None

    async def start_cycle(self, **kwargs):
        self.started = kwargs
        return SimpleNamespace(**kwargs)


class TestStartCycle(unittest.IsolatedAsyncioTestCase):
    async def test_already_settled_pays_nobody(self) -> None:
        service = FakeService(settled=7)
        await subs.start_cycle(service, SimpleNamespace(id=1), datetime(2026, 1, 1))
        self.assertEqual(service.started["participants"], [])
        self.assertIsNone(service.started["winner"])

    async def test_winner_is_a_participant(self) -> None:
        admin = member(1, admin=True)
        alice, bob = member(10), member(11)
        channel = channel_with(
            [
                message(alice, [reaction(EMOJI, [admin])]),
                message(bob, [reaction(EMOJI, [admin])]),
            ]
        )
        service = FakeService()
        await subs.start_cycle(service, channel, datetime(2026, 1, 1))
        self.assertEqual(service.started["participants"], [10, 11])
        self.assertIn(service.started["winner"], (10, 11))


class TestDryRunPresentation(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_pings_and_rewards_nobody(self) -> None:
        change = level_service.preview_level_change(0, 0, 5000)
        cycle = ThemeCycle(
            started_at=datetime(2026, 1, 1),
            message_id=7,
            participants=[10],
            winner=10,
            changes={10: change},
            winner_change=change,
        )
        channel = SimpleNamespace(send=AsyncMock())
        client = SimpleNamespace(level_rewards=None, default_embed_color=None)
        with patch.object(subs, "resolve_members", AsyncMock(return_value=[None])), \
            patch.object(subs, "_grant_level_up", AsyncMock()) as grant:
            await subs.present_results(
                client, MagicMock(), channel, cycle, dry_run=True
            )
        grant.assert_not_called()
        last = channel.send.await_args_list[-1]
        self.assertIsInstance(
            last.kwargs["allowed_mentions"], discord.AllowedMentions
        )


if __name__ == "__main__":
    unittest.main()
