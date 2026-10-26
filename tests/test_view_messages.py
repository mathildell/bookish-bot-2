"""Tests for persistent public views and their expiry bookkeeping."""

from __future__ import annotations

import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from bot.commands import level as level_cmds
from bot.commands import releases as release_cmds
from bot.commands import reminders as reminder_cmds
from bot.commands import streaks as streak_cmds
from bot.commands import views as view_cmds
from bot.services.level import utcnow
from bot.services.view_messages import ViewMessageService
from bot.texts import t


class _FakeResult:
    def __init__(self, *, value=None, rows=None, rowcount=0) -> None:
        self.value = value
        self.rows = rows or []
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return iter(self.rows)


class _FakeSession:
    def __init__(self, result: _FakeResult) -> None:
        self.result = result
        self.added = []
        self.committed = False
        self.refreshed = []

    async def execute(self, statement):
        return self.result

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, row) -> None:
        self.refreshed.append(row)


class _FakeScope:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class ViewMessageServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_creates_a_row(self) -> None:
        session = _FakeSession(_FakeResult())
        service = ViewMessageService(1)
        expires = utcnow() + timedelta(hours=2)

        with patch(
            "bot.services.view_messages.session_scope",
            lambda: _FakeScope(session),
        ):
            row = await service.register(
                channel_id=20,
                message_id=30,
                kind="reminder",
                expires_at=expires,
                state={"reminder_id": 4},
                author_id=5,
            )

        self.assertEqual(session.added, [row])
        self.assertTrue(session.committed)
        self.assertEqual(row.guild_id, 1)
        self.assertEqual(row.channel_id, 20)
        self.assertEqual(row.message_id, 30)
        self.assertEqual(row.kind, "reminder")
        self.assertEqual(row.author_id, 5)
        self.assertEqual(row.state, {"reminder_id": 4})
        self.assertEqual(row.expires_at, expires)

    async def test_register_updates_an_existing_row(self) -> None:
        existing = SimpleNamespace(
            channel_id=1,
            kind="old",
            author_id=None,
            state={},
            expires_at=utcnow(),
        )
        session = _FakeSession(_FakeResult(value=existing))
        service = ViewMessageService(1)
        expires = utcnow() + timedelta(hours=1)

        with patch(
            "bot.services.view_messages.session_scope",
            lambda: _FakeScope(session),
        ):
            row = await service.register(
                channel_id=2,
                message_id=3,
                kind="new",
                expires_at=expires,
                state={"page": 2},
                author_id=9,
            )

        self.assertIs(row, existing)
        self.assertEqual(session.added, [])
        self.assertEqual(row.channel_id, 2)
        self.assertEqual(row.kind, "new")
        self.assertEqual(row.author_id, 9)
        self.assertEqual(row.state, {"page": 2})
        self.assertEqual(row.expires_at, expires)

    async def test_expired_returns_the_due_rows(self) -> None:
        due = [SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]
        session = _FakeSession(_FakeResult(rows=due))
        service = ViewMessageService(1)

        with patch(
            "bot.services.view_messages.session_scope",
            lambda: _FakeScope(session),
        ):
            rows = await service.expired()

        self.assertEqual(rows, due)

    async def test_purge_before_returns_the_deleted_count(self) -> None:
        session = _FakeSession(_FakeResult(rowcount=3))
        service = ViewMessageService(1)

        with patch(
            "bot.services.view_messages.session_scope",
            lambda: _FakeScope(session),
        ):
            removed = await service.purge_before(utcnow())

        self.assertEqual(removed, 3)
        self.assertTrue(session.committed)


def _action_row() -> discord.components.ActionRow:
    return discord.components.ActionRow(
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": discord.ButtonStyle.secondary.value,
                    "custom_id": "test:button",
                    "label": "Test",
                }
            ],
        }
    )


class DisableViewTests(unittest.IsolatedAsyncioTestCase):
    def test_disabled_view_disables_every_child(self) -> None:
        message = SimpleNamespace(components=[_action_row()])

        view = view_cmds.disabled_view(message)

        self.assertIsNotNone(view)
        self.assertTrue(view.children)
        self.assertTrue(all(child.disabled for child in view.children))

    def test_message_without_components_has_no_disabled_view(self) -> None:
        self.assertIsNone(view_cmds.disabled_view(SimpleNamespace(components=[])))

    async def test_disable_view_message_edits_in_place(self) -> None:
        class Message:
            def __init__(self) -> None:
                self.components = [_action_row()]
                self.edits = []

            async def edit(self, **kwargs) -> None:
                self.edits.append(kwargs)

        message = Message()

        changed = await view_cmds.disable_view_message(message)

        self.assertTrue(changed)
        self.assertEqual(len(message.edits), 1)
        self.assertTrue(
            all(child.disabled for child in message.edits[0]["view"].children)
        )


class _Response:
    def __init__(self) -> None:
        self.edited = None
        self.deferred = False

    async def edit_message(self, **kwargs) -> None:
        self.edited = kwargs

    async def defer(self) -> None:
        self.deferred = True


class _Followup:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, content, **kwargs) -> None:
        self.sent.append((content, kwargs))


class TrackedRowTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_row_disables_the_message_and_deletes_it(self) -> None:
        expires = utcnow() - timedelta(seconds=1)
        delete = AsyncMock()
        service = SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(expires_at=expires)
            ),
            delete=delete,
        )
        response = _Response()
        followup = _Followup()
        message = SimpleNamespace(id=30, components=[_action_row()])
        interaction = SimpleNamespace(
            client=SimpleNamespace(view_messages=service),
            message=message,
            response=response,
            followup=followup,
        )

        allowed, row = await view_cmds.tracked_row(interaction)

        self.assertFalse(allowed)
        self.assertIsNone(row)
        self.assertTrue(
            all(child.disabled for child in response.edited["view"].children)
        )
        self.assertEqual(followup.sent[0][0], t("components.expired"))
        delete.assert_awaited_once_with(30)

    async def test_live_row_is_returned(self) -> None:
        live = SimpleNamespace(expires_at=utcnow() + timedelta(hours=1))
        service = SimpleNamespace(get=AsyncMock(return_value=live))
        interaction = SimpleNamespace(
            client=SimpleNamespace(view_messages=service),
            message=SimpleNamespace(id=30),
        )

        allowed, row = await view_cmds.tracked_row(interaction)

        self.assertTrue(allowed)
        self.assertIs(row, live)

    async def test_untracked_message_is_allowed_for_persistent_buttons(self) -> None:
        service = SimpleNamespace(get=AsyncMock(return_value=None))
        interaction = SimpleNamespace(
            client=SimpleNamespace(view_messages=service),
            message=SimpleNamespace(id=30),
        )

        allowed, row = await view_cmds.tracked_row_if_present(interaction)

        self.assertTrue(allowed)
        self.assertIsNone(row)


class PersistentViewTests(unittest.IsolatedAsyncioTestCase):
    def test_all_public_timed_views_have_persistent_fallbacks(self) -> None:
        views = [
            reminder_cmds.ReminderActionsFallbackView(),
            streak_cmds.StreakLogView(timeout=None),
            streak_cmds.StreakEditButtonView(),
            level_cmds.XpLeaderboardFallbackView(),
            streak_cmds.StreakLeaderboardFallbackView(),
            release_cmds.ReleaseListFallbackView(),
        ]

        for view in views:
            with self.subTest(view=type(view).__name__):
                self.assertTrue(view.is_persistent())

    def test_release_list_components_keep_stable_custom_ids(self) -> None:
        source = SimpleNamespace(page_count=2, total_results=10)
        view = release_cmds.ReleaseListView(source, 1)

        view.set_index(1)

        self.assertEqual(
            view.prev_button.custom_id, release_cmds.RELEASE_PREV_CUSTOM_ID
        )
        self.assertEqual(
            view.next_button.custom_id, release_cmds.RELEASE_NEXT_CUSTOM_ID
        )
        self.assertEqual(
            view.page_label.custom_id, release_cmds.RELEASE_PAGE_CUSTOM_ID
        )
        self.assertEqual(
            view.children[-1].custom_id, release_cmds.RELEASE_MONTH_CUSTOM_ID
        )

    async def test_fallback_state_is_derived_from_each_message(self) -> None:
        view = reminder_cmds.ReminderActionsFallbackView()

        async def state_for(owner_id: int, task: str):
            message = SimpleNamespace(
                content=f"<@{owner_id}>",
                embeds=[SimpleNamespace(title=task)],
            )
            interaction = SimpleNamespace(
                client=SimpleNamespace(), message=message
            )
            state, _row = await view._state(interaction)
            return state

        first = await state_for(10, "Water the plants")
        second = await state_for(20, "Read a chapter")

        self.assertEqual(first.owner_id, 10)
        self.assertEqual(first.task_text, "Water the plants")
        self.assertEqual(second.owner_id, 20)
        self.assertEqual(second.task_text, "Read a chapter")


if __name__ == "__main__":
    unittest.main()
