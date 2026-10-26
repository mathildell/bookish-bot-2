"""Persistent state and expiry for public timed views.

Fixed component custom IDs are shared by every message of the same family. The
row keyed by ``message_id`` carries the small amount of per-message state needed
after a restart (for example the current paginator page).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from ..db import session_scope
from ..models import ViewMessage
from .level import utcnow


class ViewMessageService:
    """CRUD and the expiry query behind the timed-view sweep."""

    def __init__(self, guild_id: int) -> None:
        self._guild_id = guild_id

    async def register(
        self,
        *,
        channel_id: int,
        message_id: int,
        kind: str,
        expires_at: datetime,
        state: dict | None = None,
        author_id: int | None = None,
    ) -> ViewMessage:
        """Create or replace the tracking row for one posted message."""
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(ViewMessage).where(
                        ViewMessage.message_id == message_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = ViewMessage(
                    guild_id=self._guild_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    kind=kind,
                    author_id=author_id,
                    state=state or {},
                    expires_at=expires_at,
                )
                session.add(row)
            else:
                row.channel_id = channel_id
                row.kind = kind
                row.author_id = author_id
                row.state = state or {}
                row.expires_at = expires_at
            await session.commit()
            await session.refresh(row)
        return row

    async def get(self, message_id: int) -> ViewMessage | None:
        async with session_scope() as session:
            return (
                await session.execute(
                    select(ViewMessage).where(
                        ViewMessage.guild_id == self._guild_id,
                        ViewMessage.message_id == message_id,
                    )
                )
            ).scalar_one_or_none()

    async def update_state(self, message_id: int, state: dict) -> None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(ViewMessage).where(
                        ViewMessage.guild_id == self._guild_id,
                        ViewMessage.message_id == message_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return
            row.state = state
            await session.commit()

    async def delete(self, message_id: int) -> None:
        async with session_scope() as session:
            await session.execute(
                sa_delete(ViewMessage).where(
                    ViewMessage.guild_id == self._guild_id,
                    ViewMessage.message_id == message_id,
                )
            )
            await session.commit()

    async def expired(
        self, now: datetime | None = None, limit: int | None = None
    ) -> list[ViewMessage]:
        statement = (
            select(ViewMessage)
            .where(
                ViewMessage.guild_id == self._guild_id,
                ViewMessage.expires_at <= (now or utcnow()),
            )
            .order_by(ViewMessage.expires_at)
        )
        if limit is not None:
            statement = statement.limit(limit)
        async with session_scope() as session:
            rows = (await session.execute(statement)).scalars()
        return list(rows)

    async def purge_before(self, cutoff: datetime) -> int:
        """Delete rows that are well past their expiry."""
        async with session_scope() as session:
            result = await session.execute(
                sa_delete(ViewMessage).where(
                    ViewMessage.guild_id == self._guild_id,
                    ViewMessage.expires_at < cutoff,
                )
            )
            await session.commit()
        return max(result.rowcount or 0, 0)
