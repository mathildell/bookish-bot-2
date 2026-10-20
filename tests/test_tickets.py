"""Unit tests for the ticketing embeds, views and pure helpers."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

import discord

from bot.commands import tickets as ticket_cmds
from bot.services import tickets as ticket_service
from bot.texts import t


class FakeClient:
    transparent_embed_image = "https://example.com/bg.png"
    default_embed_color = discord.Colour(0x97A97C)


class TicketViewTests(unittest.TestCase):
    def test_panel_button_is_persistent(self) -> None:
        view = ticket_cmds.OpenTicketView()
        self.assertIsNone(view.timeout)
        self.assertEqual(view.children[0].custom_id, ticket_cmds.OPEN_CUSTOM_ID)
        self.assertEqual(view.children[0].label, t("tickets.sticky.button"))

    def test_close_button_is_persistent(self) -> None:
        view = ticket_cmds.TicketCloseView()
        self.assertIsNone(view.timeout)
        self.assertEqual(view.children[0].custom_id, ticket_cmds.CLOSE_CUSTOM_ID)
        self.assertEqual(
            view.children[0].label, t("tickets.embed.close_button")
        )

    def test_reopen_button_is_persistent(self) -> None:
        view = ticket_cmds.TicketReopenView()
        self.assertIsNone(view.timeout)
        self.assertEqual(view.children[0].custom_id, ticket_cmds.REOPEN_CUSTOM_ID)
        self.assertEqual(
            view.children[0].label, t("tickets.embed.reopen_button")
        )


class TicketCommandTests(unittest.TestCase):
    def test_group_hosts_admin_and_thread_commands(self) -> None:
        names = {command.name for command in ticket_cmds.tickets_group.commands}
        self.assertEqual(names, {"channel", "role", "close", "reopen"})

    def test_subgroups_host_set(self) -> None:
        self.assertEqual(
            {command.name for command in ticket_cmds.channel_group.commands},
            {"set"},
        )
        self.assertEqual(
            {command.name for command in ticket_cmds.role_group.commands},
            {"set"},
        )


class ThreadNameTests(unittest.TestCase):
    def test_name_uses_display_name_and_number(self) -> None:
        self.assertEqual(
            ticket_service.thread_name("Alice", 3), "Alice's ticket #3"
        )

    def test_long_name_is_truncated(self) -> None:
        name = ticket_service.thread_name("A" * 200, 12)
        self.assertLessEqual(len(name), ticket_service.THREAD_NAME_LIMIT)
        self.assertTrue(name.endswith("…"))

    def test_resolved_name_keeps_the_base(self) -> None:
        self.assertEqual(
            ticket_service.resolved_thread_name("Alice's ticket #3"),
            "[Resolved] Alice's ticket #3",
        )

    def test_resolved_name_is_truncated(self) -> None:
        name = ticket_service.resolved_thread_name("A" * 200)
        self.assertLessEqual(len(name), ticket_service.THREAD_NAME_LIMIT)


class ReopenWindowTests(unittest.TestCase):
    def ticket(self, **kwargs):
        defaults = {
            "status": ticket_service.STATUS_CLOSED,
            "button_removed": False,
            "closed_at": datetime(2026, 10, 20, 12, 0, 0),
        }
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_within_two_hours(self) -> None:
        ticket = self.ticket()
        self.assertTrue(
            ticket_service.can_reopen_with_button(
                ticket, datetime(2026, 10, 20, 13, 59)
            )
        )

    def test_exactly_two_hours_is_expired(self) -> None:
        ticket = self.ticket()
        self.assertFalse(
            ticket_service.can_reopen_with_button(
                ticket, ticket.closed_at + timedelta(hours=2)
            )
        )

    def test_open_ticket_cannot_use_reopen(self) -> None:
        ticket = self.ticket(status=ticket_service.STATUS_OPEN)
        self.assertFalse(
            ticket_service.can_reopen_with_button(
                ticket, datetime(2026, 10, 20, 12, 30)
            )
        )

    def test_removed_button_is_expired(self) -> None:
        ticket = self.ticket(button_removed=True)
        self.assertFalse(
            ticket_service.can_reopen_with_button(
                ticket, datetime(2026, 10, 20, 12, 30)
            )
        )


class TicketEmbedTests(unittest.TestCase):
    def test_sticky_embed(self) -> None:
        embed = ticket_service.sticky_embed(FakeClient())
        self.assertEqual(embed.title, t("tickets.sticky.title"))
        self.assertEqual(embed.description, t("tickets.sticky.description"))
        self.assertEqual(embed.colour, FakeClient.default_embed_color)
        self.assertEqual(embed.image.url, FakeClient.transparent_embed_image)

    def test_ticket_embed(self) -> None:
        embed = ticket_service.ticket_embed(FakeClient(), 7)
        self.assertEqual(embed.title, t("tickets.embed.title", number=7))
        self.assertEqual(embed.description, t("tickets.embed.description"))

    def test_summary_embed(self) -> None:
        ticket = SimpleNamespace(
            name="Alice's ticket #7",
            opener_id=42,
            opened_at=datetime(2026, 10, 20, 10, 0, 0),
            closed_at=datetime(2026, 10, 20, 12, 0, 0),
        )
        embed = ticket_service.summary_embed(FakeClient(), ticket)
        self.assertEqual(embed.title, t("tickets.summary.title"))
        self.assertIn("<@42>", embed.description)
        self.assertIn("Alice's ticket #7", embed.description)
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(
            fields[t("tickets.summary.opened_field")],
            ticket_service.format_timestamp(ticket.opened_at),
        )
        self.assertEqual(
            fields[t("tickets.summary.closed_field")],
            ticket_service.format_timestamp(ticket.closed_at),
        )
        self.assertTrue(
            fields[t("tickets.summary.opened_field")].startswith("<t:")
        )

    def test_unknown_timestamp_falls_back(self) -> None:
        self.assertEqual(
            ticket_service.format_timestamp(None),
            t("tickets.summary.unknown_time"),
        )


class TicketAdminTests(unittest.TestCase):
    def interaction(self, *, manage_guild: bool, role_ids: list[int]):
        return SimpleNamespace(
            permissions=SimpleNamespace(manage_guild=manage_guild),
            user=SimpleNamespace(
                roles=[SimpleNamespace(id=role_id) for role_id in role_ids]
            ),
        )

    def test_manage_guild_is_admin(self) -> None:
        service = SimpleNamespace(admin_role_id=None)
        interaction = self.interaction(manage_guild=True, role_ids=[])
        self.assertTrue(ticket_cmds._has_ticket_admin(interaction, service))

    def test_configured_role_is_admin(self) -> None:
        service = SimpleNamespace(admin_role_id=55)
        interaction = self.interaction(manage_guild=False, role_ids=[55])
        self.assertTrue(ticket_cmds._has_ticket_admin(interaction, service))

    def test_other_member_is_not_admin(self) -> None:
        service = SimpleNamespace(admin_role_id=55)
        interaction = self.interaction(manage_guild=False, role_ids=[99])
        self.assertFalse(ticket_cmds._has_ticket_admin(interaction, service))


class FakeMessage:
    def __init__(self) -> None:
        self.id = 777
        self.pinned = False

    async def pin(self, *, reason: str | None = None) -> None:
        self.pinned = True


class FakeThread:
    id = 900
    mention = "<#900>"

    def __init__(self) -> None:
        self.added = []
        self.sent = []
        self.message = None
        self.deleted = False

    async def add_user(self, user) -> None:
        self.added.append(user)

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        self.message = FakeMessage()
        return self.message

    async def delete(self) -> None:
        self.deleted = True


class FakeTicketChannel(discord.TextChannel):
    @property
    def mention(self) -> str:
        return "<#5>"

    def __init__(self) -> None:
        self.id = 5
        self.thread = FakeThread()
        self.created_kwargs = None

    async def create_thread(self, **kwargs):
        self.created_kwargs = kwargs
        return self.thread


class FakeResponse:
    def __init__(self) -> None:
        self.done = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, message, *, ephemeral: bool = False) -> None:
        self.messages.append({"message": message, "ephemeral": ephemeral})


class FakeTicketService:
    channel_id = 5
    admin_role_id = 55

    def __init__(self) -> None:
        self.open_lock = asyncio.Lock()
        self.created = None

    async def next_number(self) -> int:
        return 3

    async def create(self, **kwargs):
        self.created = kwargs
        return SimpleNamespace(**kwargs)


class OpenTicketTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_adds_the_opener_and_posts_both_mentions(self) -> None:
        thread = FakeThread()
        channel = FakeTicketChannel()
        channel.thread = thread
        role = SimpleNamespace(id=55, mention="<@&55>")
        opener = SimpleNamespace(id=42, display_name="Alice", mention="<@42>")
        service = FakeTicketService()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(
                get_role=lambda role_id: role if role_id == 55 else None
            ),
            channel=channel,
            user=opener,
            client=SimpleNamespace(tickets=service),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )

        await ticket_cmds._handle_open(interaction)

        self.assertEqual(thread.added, [opener])
        self.assertEqual(len(thread.sent), 1)
        payload = thread.sent[0]
        self.assertIn(opener.mention, payload["content"])
        self.assertIn(role.mention, payload["content"])
        self.assertIsInstance(payload["view"], ticket_cmds.TicketCloseView)
        self.assertTrue(payload["allowed_mentions"].users)
        self.assertTrue(payload["allowed_mentions"].roles)
        self.assertEqual(service.created["opener_id"], 42)
        self.assertEqual(service.created["thread_id"], 900)
        self.assertEqual(service.created["message_id"], 777)
        self.assertTrue(service.created["name"].startswith("Alice's ticket #3"))
        self.assertTrue(thread.message.pinned)
        self.assertEqual(
            channel.created_kwargs["type"], discord.ChannelType.private_thread
        )
        self.assertFalse(thread.deleted)
        self.assertEqual(
            interaction.followup.messages[-1]["message"],
            t("tickets.open.created", thread=thread.mention),
        )
