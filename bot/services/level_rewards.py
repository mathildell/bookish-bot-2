"""Level reward roles: level -> role mapping, persisted per guild.

The mapping starts empty and is fully admin-editable. An in-memory registry
mirrors the database so the message listener can resolve a member's reward role
without a query.
"""

from __future__ import annotations

import logging

import discord
from sqlalchemy import delete, select

from ..db import session_scope
from ..models import LevelReward
from ..texts import t

logger = logging.getLogger("bh2.level_rewards")


class LevelRewardService:
    """CRUD for level rewards plus the in-memory registry."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self._rewards: dict[int, int] = {}

    @property
    def rewards(self) -> dict[int, int]:
        return dict(self._rewards)

    @property
    def role_ids(self) -> set[int]:
        return set(self._rewards.values())

    def role_for_level(self, level: int) -> int | None:
        """The reward role id for the highest reward level <= ``level``."""
        target: int | None = None
        for reward_level in sorted(self._rewards):
            if level >= reward_level:
                target = reward_level
            else:
                break
        return self._rewards.get(target) if target is not None else None

    async def reload(self) -> None:
        async with session_scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(LevelReward).where(
                            LevelReward.guild_id == self.guild_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        self._rewards = {row.level: row.role_id for row in rows}
        logger.info("Loaded %d level reward(s)", len(self._rewards))

    async def set(self, level: int, role_id: int) -> None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(LevelReward).where(
                        LevelReward.guild_id == self.guild_id,
                        LevelReward.level == level,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    LevelReward(
                        guild_id=self.guild_id, level=level, role_id=role_id
                    )
                )
            else:
                row.role_id = role_id
            await session.commit()
        self._rewards[level] = role_id

    async def remove(self, level: int) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                delete(LevelReward).where(
                    LevelReward.guild_id == self.guild_id,
                    LevelReward.level == level,
                )
            )
            await session.commit()
        existed = self._rewards.pop(level, None) is not None
        return existed or bool(result.rowcount)


async def sync_member_roles(
    guild: discord.Guild,
    member: discord.Member,
    level: int,
    service: LevelRewardService,
) -> bool:
    """Give ``member`` the reward role for ``level``, removing any other reward.

    Returns ``False`` when Discord rejects the update, ``True`` otherwise.
    """
    target_id = service.role_for_level(level)
    add_role = guild.get_role(target_id) if target_id is not None else None
    held = {role.id for role in member.roles}
    remove_roles = [
        role
        for role_id in service.role_ids
        if role_id != target_id
        and role_id in held
        and (role := guild.get_role(role_id)) is not None
    ]
    reason = t("level.rewards.reason")
    try:
        if remove_roles:
            await member.remove_roles(*remove_roles, atomic=False, reason=reason)
        if add_role is not None and add_role not in member.roles:
            await member.add_roles(add_role, atomic=False, reason=reason)
    except discord.HTTPException:
        logger.warning("Could not sync level roles for %s", member.id, exc_info=True)
        return False
    return True
