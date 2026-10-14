"""Booster roles: link boosters to the custom roles they own.

Port of the old Ruby "BoosterRole" feature, adapted to the bot's design:

- Admins create the Discord roles themselves and register them with the bot
  (``/booster role link``). The mapping table is the source of truth.
- Boosters (or admins) can then rename/recolour their own linked role.
- Because the bot runs without the privileged Members intent it cannot listen
  for unboost events, so ``reconcile`` polls the affected members on a schedule:
  anyone who is no longer boosting is *unlinked* (mapping removed) while their
  role is left in place, and the admins are notified so they can clean it up.
"""

from __future__ import annotations

import logging

import discord
from sqlalchemy import delete, select

from ..db import session_scope
from ..models import BoosterRole
from ..texts import t

logger = logging.getLogger("bh2.booster_role")


async def resolve_notify_channel(
    guild: discord.Guild, channel_id: int
) -> discord.abc.Messageable | None:
    """Return the configured notify channel, or ``None`` if unavailable."""
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel


class BoosterRoleService:
    """CRUD for the user <-> role mappings, plus the reconciliation poll."""

    async def all(self) -> list[BoosterRole]:
        async with session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(BoosterRole).order_by(BoosterRole.id)
                    )
                )
                .scalars()
                .all()
            )

    async def find_by_user(self, discord_user_id: int) -> BoosterRole | None:
        async with session_scope() as session:
            return (
                await session.execute(
                    select(BoosterRole).where(
                        BoosterRole.discord_user_id == discord_user_id
                    )
                )
            ).scalar_one_or_none()

    async def find_by_role(self, role_id: int) -> BoosterRole | None:
        async with session_scope() as session:
            return (
                await session.execute(
                    select(BoosterRole).where(BoosterRole.role_id == role_id)
                )
            ).scalar_one_or_none()

    async def upsert(
        self, discord_user_id: int, role_id: int
    ) -> BoosterRole:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(BoosterRole).where(
                        BoosterRole.discord_user_id == discord_user_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = BoosterRole(discord_user_id=discord_user_id)
                session.add(row)
            row.role_id = role_id
            await session.commit()
            await session.refresh(row)
        return row

    async def delete_by_user(self, discord_user_id: int) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                delete(BoosterRole).where(
                    BoosterRole.discord_user_id == discord_user_id
                )
            )
            await session.commit()
        return result.rowcount > 0

    async def delete_by_role(self, role_id: int) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                delete(BoosterRole).where(BoosterRole.role_id == role_id)
            )
            await session.commit()
        return result.rowcount > 0

    async def reconcile(
        self,
        guild: discord.Guild,
        notify_channel: discord.abc.Messageable | None = None,
    ) -> list[tuple[int, int]]:
        """Unlink boosters who no longer boost; notify admins. Returns unlinked.

        Fetches each linked member individually. The single-member endpoint does
        not require the privileged Members intent, unlike the bulk list.
        The Discord role is intentionally left in place.
        """
        unlinked: list[tuple[int, int]] = []

        for row in await self.all():
            try:
                member = await guild.fetch_member(row.discord_user_id)
            except discord.NotFound:
                still_boosting = False
            except discord.HTTPException as exc:
                logger.warning(
                    "Could not check member %s: %s", row.discord_user_id, exc
                )
                continue
            else:
                still_boosting = member.premium_since is not None

            if still_boosting:
                continue

            await self.delete_by_user(row.discord_user_id)
            unlinked.append((row.discord_user_id, row.role_id))

        if unlinked and notify_channel is not None:
            await self._notify(notify_channel, guild, unlinked)

        return unlinked

    async def _notify(
        self,
        channel: discord.abc.Messageable,
        guild: discord.Guild,
        unlinked: list[tuple[int, int]],
    ) -> None:
        for user_id, role_id in unlinked:
            role = guild.get_role(role_id)
            role_label = f"<@&{role_id}>" if role is not None else role_id
            try:
                await channel.send(
                    t(
                        "booster.role.cleanup.notice",
                        user=f"<@{user_id}>",
                        role=role_label,
                    )
                )
            except discord.HTTPException as exc:
                logger.warning("Could not post unboost notice: %s", exc)
