"""Private support tickets opened from a panel in a read-only channel.

Members click the panel button to get a private thread; the opener and the
configured admin role can close it, and it can be reopened for two hours after
closing. The service owns storage, embeds and the expiry of the Reopen button.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from sqlalchemy import func, select

from ..db import session_scope
from ..models import Ticket, TicketSetting
from ..texts import t
from .level import utcnow

logger = logging.getLogger("bh2.tickets")

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"

REOPEN_WINDOW = timedelta(hours=2)
THREAD_NAME_LIMIT = 100


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def format_timestamp(value: datetime | None) -> str:
    """Render a stored naive-UTC datetime as a Discord timestamp."""
    if value is None:
        return t("tickets.summary.unknown_time")
    return discord.utils.format_dt(_utc(value), "f")


def _limited(value: str, limit: int = THREAD_NAME_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def thread_name(display_name: str, number: int) -> str:
    """The base thread name, e.g. ``Alice's ticket #3``."""
    return _limited(
        t("tickets.thread.name", display_name=display_name, number=number)
    )


def resolved_thread_name(name: str) -> str:
    """The closed thread name, keeping the base name and adding the prefix."""
    return _limited(t("tickets.thread.resolved_prefix") + name)


def can_reopen_with_button(ticket: Ticket, now: datetime) -> bool:
    """Whether the Reopen button is still within its two-hour window."""
    return (
        ticket.status == STATUS_CLOSED
        and not ticket.button_removed
        and ticket.closed_at is not None
        and now < ticket.closed_at + REOPEN_WINDOW
    )


def _apply_transparent_image(
    embed: discord.Embed, client: discord.Client
) -> None:
    image = getattr(client, "transparent_embed_image", None)
    if image:
        embed.set_image(url=image)


def sticky_embed(client: discord.Client) -> discord.Embed:
    """The single panel message posted in the ticketing channel."""
    embed = discord.Embed(
        title=t("tickets.sticky.title"),
        description=t("tickets.sticky.description"),
        colour=getattr(client, "default_embed_color", None),
    )
    # _apply_transparent_image(embed, client)
    return embed


def ticket_embed(client: discord.Client, number: int) -> discord.Embed:
    """The embed pinned in a new ticket thread."""
    embed = discord.Embed(
        title=t("tickets.embed.title", number=number),
        description=t("tickets.embed.description"),
        colour=getattr(client, "default_embed_color", None),
    )
    _apply_transparent_image(embed, client)
    return embed


def summary_embed(client: discord.Client, ticket: Ticket) -> discord.Embed:
    """The close summary posted in the admins' updates channel."""
    embed = discord.Embed(
        title=t("tickets.summary.title"),
        description=t(
            "tickets.summary.description",
            name=discord.utils.escape_markdown(ticket.name),
            user=f"<@{ticket.opener_id}>",
        ),
        colour=getattr(client, "default_embed_color", None),
    )
    embed.add_field(
        name=t("tickets.summary.opened_field"),
        value=format_timestamp(ticket.opened_at),
        inline=True,
    )
    embed.add_field(
        name=t("tickets.summary.closed_field"),
        value=format_timestamp(ticket.closed_at),
        inline=True,
    )
    return embed


class TicketService:
    """CRUD for the ticket settings/tickets and the Reopen button expiry."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self._channel_id: int | None = None
        self._admin_role_id: int | None = None
        self._sticky_message_id: int | None = None
        self.open_lock = asyncio.Lock()

    @property
    def channel_id(self) -> int | None:
        return self._channel_id

    @property
    def admin_role_id(self) -> int | None:
        return self._admin_role_id

    @property
    def sticky_message_id(self) -> int | None:
        return self._sticky_message_id

    async def reload(self) -> None:
        async with session_scope() as session:
            row = await self._find_setting(session)
        self._channel_id = row.channel_id if row is not None else None
        self._admin_role_id = row.admin_role_id if row is not None else None
        self._sticky_message_id = (
            row.sticky_message_id if row is not None else None
        )

    async def set_channel(
        self, channel_id: int
    ) -> tuple[int | None, int | None]:
        """Point the panel at ``channel_id``; return the previous setting."""
        async with session_scope() as session:
            row = await self._find_setting(session)
            previous = (
                (row.channel_id, row.sticky_message_id)
                if row is not None
                else (None, None)
            )
            if row is None:
                session.add(
                    TicketSetting(
                        guild_id=self.guild_id,
                        channel_id=channel_id,
                        admin_role_id=self._admin_role_id,
                    )
                )
            else:
                row.channel_id = channel_id
                row.sticky_message_id = None
            await session.commit()
        self._channel_id = channel_id
        self._sticky_message_id = None
        return previous

    async def set_sticky(self, message_id: int) -> None:
        async with session_scope() as session:
            row = await self._find_setting(session)
            if row is None:
                row = TicketSetting(
                    guild_id=self.guild_id,
                    channel_id=self._channel_id,
                    admin_role_id=self._admin_role_id,
                    sticky_message_id=message_id,
                )
                session.add(row)
            else:
                row.sticky_message_id = message_id
            await session.commit()
        self._sticky_message_id = message_id

    async def set_admin_role(self, role_id: int) -> None:
        async with session_scope() as session:
            row = await self._find_setting(session)
            if row is None:
                session.add(
                    TicketSetting(
                        guild_id=self.guild_id,
                        channel_id=self._channel_id,
                        admin_role_id=role_id,
                    )
                )
            else:
                row.admin_role_id = role_id
            await session.commit()
        self._admin_role_id = role_id

    async def _find_setting(self, session) -> TicketSetting | None:
        return (
            await session.execute(
                select(TicketSetting).where(
                    TicketSetting.guild_id == self.guild_id
                )
            )
        ).scalar_one_or_none()

    async def next_number(self) -> int:
        async with session_scope() as session:
            highest = (
                await session.execute(
                    select(func.max(Ticket.number)).where(
                        Ticket.guild_id == self.guild_id
                    )
                )
            ).scalar()
        return (highest or 0) + 1

    async def create(
        self,
        *,
        number: int,
        opener_id: int,
        name: str,
        channel_id: int,
        thread_id: int,
        message_id: int,
        opened_at: datetime,
    ) -> Ticket:
        async with session_scope() as session:
            row = Ticket(
                guild_id=self.guild_id,
                number=number,
                opener_id=opener_id,
                name=name,
                channel_id=channel_id,
                thread_id=thread_id,
                message_id=message_id,
                status=STATUS_OPEN,
                opened_at=opened_at,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def get_by_thread(self, thread_id: int) -> Ticket | None:
        async with session_scope() as session:
            return (
                await session.execute(
                    select(Ticket).where(
                        Ticket.guild_id == self.guild_id,
                        Ticket.thread_id == thread_id,
                    )
                )
            ).scalar_one_or_none()

    async def close(self, ticket_id: int, now: datetime) -> Ticket | None:
        async with session_scope() as session:
            row = await session.get(Ticket, ticket_id)
            if row is None or row.guild_id != self.guild_id:
                return None
            row.status = STATUS_CLOSED
            row.closed_at = now
            row.button_removed = False
            await session.commit()
            await session.refresh(row)
        return row

    async def reopen(self, ticket_id: int) -> Ticket | None:
        async with session_scope() as session:
            row = await session.get(Ticket, ticket_id)
            if row is None or row.guild_id != self.guild_id:
                return None
            row.status = STATUS_OPEN
            row.closed_at = None
            row.button_removed = False
            await session.commit()
            await session.refresh(row)
        return row

    async def expired_reopen_buttons(self, now: datetime) -> list[Ticket]:
        cutoff = now - REOPEN_WINDOW
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Ticket)
                    .where(
                        Ticket.guild_id == self.guild_id,
                        Ticket.status == STATUS_CLOSED,
                        Ticket.button_removed.is_(False),
                        Ticket.closed_at.is_not(None),
                        Ticket.closed_at <= cutoff,
                    )
                    .order_by(Ticket.id)
                )
            ).scalars()
        return list(rows)

    async def mark_button_removed(self, ticket_id: int) -> None:
        async with session_scope() as session:
            row = await session.get(Ticket, ticket_id)
            if row is None or row.guild_id != self.guild_id:
                return
            row.button_removed = True
            await session.commit()

    async def expire_reopen_buttons(
        self, guild: discord.Guild, now: datetime | None = None
    ) -> int:
        """Remove the Reopen button from tickets whose two hours are up."""
        removed = 0
        for ticket in await self.expired_reopen_buttons(now or utcnow()):
            thread = guild.get_thread(ticket.thread_id)
            if thread is None:
                try:
                    thread = await guild.fetch_channel(ticket.thread_id)
                except discord.HTTPException:
                    thread = None
            if not isinstance(thread, discord.Thread):
                await self.mark_button_removed(ticket.id)
                removed += 1
                continue

            was_archived = thread.archived
            if was_archived:
                try:
                    await thread.edit(
                        archived=False,
                        reason=t("tickets.audit.expire", number=ticket.number),
                    )
                except discord.HTTPException:
                    logger.warning(
                        "Could not unarchive ticket thread %s",
                        ticket.thread_id,
                        exc_info=True,
                    )
                    continue

            cleared = False
            try:
                message = await thread.fetch_message(ticket.message_id)
            except discord.NotFound:
                cleared = True
            except discord.HTTPException:
                logger.warning(
                    "Could not fetch ticket message %s",
                    ticket.message_id,
                    exc_info=True,
                )
            else:
                try:
                    await message.edit(view=None)
                    cleared = True
                except discord.HTTPException:
                    logger.warning(
                        "Could not remove the Reopen button from %s",
                        ticket.message_id,
                        exc_info=True,
                    )
            finally:
                if was_archived:
                    try:
                        await thread.edit(locked=True, archived=True)
                    except discord.HTTPException:
                        logger.warning(
                            "Could not re-archive ticket thread %s",
                            ticket.thread_id,
                            exc_info=True,
                        )

            if cleared:
                await self.mark_button_removed(ticket.id)
                removed += 1
        return removed
