"""XP boosts for role- and channel-based message XP multipliers."""

from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import delete, select

from ..db import session_scope
from ..models import XpBoost

logger = logging.getLogger("bh2.xp_boosts")


def apply_boost(base: int, percent: int) -> int:
    if base <= 0 or percent <= 0:
        return base
    return base * (100 + percent) // 100


def boost_percent(
    role_boosts: dict[int, int],
    channel_boosts: dict[int, int],
    *,
    role_ids: Iterable[int],
    channel_ids: Iterable[int],
) -> int:
    total = 0
    seen_roles: set[int] = set()
    for role_id in role_ids:
        if role_id in seen_roles:
            continue
        seen_roles.add(role_id)
        total += role_boosts.get(role_id, 0)
    seen_channels: set[int] = set()
    for channel_id in channel_ids:
        if channel_id in seen_channels:
            continue
        seen_channels.add(channel_id)
        total += channel_boosts.get(channel_id, 0)
    return total


class XpBoostService:
    """CRUD for XP boosts plus in-memory registries."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self._role_boosts: dict[int, int] = {}
        self._channel_boosts: dict[int, int] = {}

    @property
    def role_boosts(self) -> dict[int, int]:
        return dict(self._role_boosts)

    @property
    def channel_boosts(self) -> dict[int, int]:
        return dict(self._channel_boosts)

    async def reload(self) -> None:
        async with session_scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(XpBoost).where(XpBoost.guild_id == self.guild_id)
                    )
                )
                .scalars()
                .all()
            )
        role_boosts: dict[int, int] = {}
        channel_boosts: dict[int, int] = {}
        for row in rows:
            if row.kind == "role":
                role_boosts[row.target_id] = row.percent
            elif row.kind == "channel":
                channel_boosts[row.target_id] = row.percent
        self._role_boosts = role_boosts
        self._channel_boosts = channel_boosts
        logger.info(
            "Loaded %d role boost(s) and %d channel boost(s)",
            len(self._role_boosts),
            len(self._channel_boosts),
        )

    async def set_role(self, role_id: int, percent: int) -> None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(XpBoost).where(
                        XpBoost.guild_id == self.guild_id,
                        XpBoost.kind == "role",
                        XpBoost.target_id == role_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    XpBoost(
                        guild_id=self.guild_id,
                        kind="role",
                        target_id=role_id,
                        percent=percent,
                    )
                )
            else:
                row.percent = percent
            await session.commit()
        self._role_boosts[role_id] = percent

    async def remove_role(self, role_id: int) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                delete(XpBoost).where(
                    XpBoost.guild_id == self.guild_id,
                    XpBoost.kind == "role",
                    XpBoost.target_id == role_id,
                )
            )
            await session.commit()
        existed = self._role_boosts.pop(role_id, None) is not None
        return existed or bool(result.rowcount)

    async def set_channel(self, channel_id: int, percent: int) -> None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(XpBoost).where(
                        XpBoost.guild_id == self.guild_id,
                        XpBoost.kind == "channel",
                        XpBoost.target_id == channel_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    XpBoost(
                        guild_id=self.guild_id,
                        kind="channel",
                        target_id=channel_id,
                        percent=percent,
                    )
                )
            else:
                row.percent = percent
            await session.commit()
        self._channel_boosts[channel_id] = percent

    async def remove_channel(self, channel_id: int) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                delete(XpBoost).where(
                    XpBoost.guild_id == self.guild_id,
                    XpBoost.kind == "channel",
                    XpBoost.target_id == channel_id,
                )
            )
            await session.commit()
        existed = self._channel_boosts.pop(channel_id, None) is not None
        return existed or bool(result.rowcount)
