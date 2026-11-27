"""Tests for the temporary draft level rewards (``/xp draft``)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import discord

from bot.commands import level as level_cmds
from bot.commands import level_reward_drafts as draft_cmds
from bot.services.level_reward_drafts import stale_role_ids

CLIENT = SimpleNamespace(default_embed_color=None, transparent_embed_image=None)


def _role(icon, colour: int = 0x123456) -> SimpleNamespace:
    return SimpleNamespace(
        display_icon=icon, mention="<@&5>", colour=discord.Colour(colour)
    )


class StaleRoleTests(unittest.TestCase):
    def test_only_roles_missing_from_the_new_list_are_stale(self) -> None:
        old = {5: 1, 10: 2, 20: 3}
        new = {5: 1, 15: 3, 30: 4}
        self.assertEqual(stale_role_ids(old, new), {2})


class DraftGroupTests(unittest.TestCase):
    def test_xp_hosts_the_draft_commands(self) -> None:
        group = next(
            c for c in level_cmds.xp_group.commands if c.name == "draft"
        )
        names = {command.name for command in group.commands}
        self.assertEqual(names, {"set", "remove", "list", "publish"})


class DraftEmbedTests(unittest.TestCase):
    def test_image_icon_becomes_the_thumbnail(self) -> None:
        icon = discord.Asset(
            None, url="https://cdn.example/icon.png", key="k", animated=False
        )
        embed = draft_cmds._draft_embed(CLIENT, 10, 5, _role(icon))
        self.assertEqual(embed.thumbnail.url, "https://cdn.example/icon.png")
        self.assertEqual(embed.description, "<@&5>")
        self.assertEqual(embed.colour.value, 0x123456)
        self.assertIn("10", embed.title)

    def test_unicode_icon_prefixes_the_mention(self) -> None:
        embed = draft_cmds._draft_embed(CLIENT, 1, 5, _role("⭐"))
        self.assertEqual(embed.description, "⭐ <@&5>")
        self.assertIsNone(embed.thumbnail.url)

    def test_default_colour_is_kept_for_uncoloured_roles(self) -> None:
        embed = draft_cmds._draft_embed(CLIENT, 1, 5, _role(None, colour=0))
        self.assertIsNone(embed.colour)

    def test_deleted_role_is_named_by_id(self) -> None:
        embed = draft_cmds._draft_embed(CLIENT, 1, 987, None)
        self.assertIn("987", embed.description)


if __name__ == "__main__":
    unittest.main()
