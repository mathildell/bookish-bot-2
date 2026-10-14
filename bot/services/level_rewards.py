"""Level reward roles: level -> role mapping, persisted per guild.

The mapping is seeded with the server's defaults on first run and is fully
admin-editable afterwards. An in-memory registry mirrors the database so the
message listener can resolve a member's reward role without a query.
"""

from __future__ import annotations

import logging

import discord
from sqlalchemy import delete, select

from ..db import session_scope
from ..models import LevelReward, LevelSettings
from ..texts import t

logger = logging.getLogger("bh2.level_rewards")

DEFAULT_REWARD_ROLES: dict[int, int] = {
    1: 1406739488616615987,
    5: 1406739608158339162,
    10: 1406739687191875625,
    20: 1406739750475268226,
    30: 1410284676584833165,
    40: 1406739803159924737,
    50: 1406739837461069914,
    65: 1410284783229206570,
    80: 1406739925633859764,
    95: 1410284882113855602,
    110: 1406740011784732903,
    130: 1406740078964772904,
    150: 1406740133377605704,
    175: 1406740197596725370,
    200: 1406740273899376670,
}


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
            settings = (
                await session.execute(
                    select(LevelSettings).where(
                        LevelSettings.guild_id == self.guild_id
                    )
                )
            ).scalar_one_or_none()
            if settings is None:
                settings = LevelSettings(
                    guild_id=self.guild_id, rewards_seeded=False
                )
                session.add(settings)
            if not settings.rewards_seeded:
                for level, role_id in DEFAULT_REWARD_ROLES.items():
                    session.add(
                        LevelReward(
                            guild_id=self.guild_id, level=level, role_id=role_id
                        )
                    )
                settings.rewards_seeded = True
                await session.commit()
                self._rewards = dict(DEFAULT_REWARD_ROLES)
                logger.info(
                    "Seeded %d default level reward(s)", len(DEFAULT_REWARD_ROLES)
                )
                return
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


async def resolve_member(
    guild: discord.Guild, user_id: int
) -> discord.Member | None:
    """Return a guild member by id, fetching them if not in the cache."""
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


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
    remove_roles = [
        role
        for role_id in service.role_ids
        if role_id != target_id and (role := guild.get_role(role_id)) is not None
    ]
    reason = t("level.rewards.reason")
    try:
        if remove_roles:
            await member.remove_roles(*remove_roles, reason=reason)
        if add_role is not None and add_role not in member.roles:
            await member.add_roles(add_role, reason=reason)
    except discord.HTTPException:
        logger.warning("Could not sync level roles for %s", member.id, exc_info=True)
        return False
    return True


async def sync_user_roles(
    guild: discord.Guild,
    user_id: int,
    level: int,
    service: LevelRewardService,
) -> bool:
    """Resolve a member then sync their reward role; ``False`` if unresolved."""
    member = await resolve_member(guild, user_id)
    if member is None:
        return False
    return await sync_member_roles(guild, member, level, service)
