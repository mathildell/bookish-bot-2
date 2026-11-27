"""Draft level rewards: a level -> role list previewed before it goes live.

Temporary scaffolding for switching to a new reward list. Publishing replaces
``level_rewards`` with the draft; delete this module, its commands, texts and
the ``LevelRewardDraft`` model once the switch is done.
"""

from __future__ import annotations

from sqlalchemy import delete, select

from ..db import session_scope
from ..models import LevelReward, LevelRewardDraft


async def all_drafts(guild_id: int) -> dict[int, int]:
    """The draft as ``{level: role_id}``."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(LevelRewardDraft).where(LevelRewardDraft.guild_id == guild_id)
            )
        ).scalars()
        return {row.level: row.role_id for row in rows}


async def set_draft(guild_id: int, level: int, role_id: int) -> None:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(LevelRewardDraft).where(
                    LevelRewardDraft.guild_id == guild_id,
                    LevelRewardDraft.level == level,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                LevelRewardDraft(guild_id=guild_id, level=level, role_id=role_id)
            )
        else:
            row.role_id = role_id
        await session.commit()


async def remove_draft(guild_id: int, level: int) -> bool:
    async with session_scope() as session:
        result = await session.execute(
            delete(LevelRewardDraft).where(
                LevelRewardDraft.guild_id == guild_id,
                LevelRewardDraft.level == level,
            )
        )
        await session.commit()
    return bool(result.rowcount)


def stale_role_ids(old: dict[int, int], new: dict[int, int]) -> set[int]:
    """Live reward roles that the new list no longer grants."""
    return set(old.values()) - set(new.values())


async def publish(guild_id: int, draft: dict[int, int]) -> None:
    """Replace the guild's live level rewards with ``draft`` in one transaction."""
    async with session_scope() as session:
        await session.execute(
            delete(LevelReward).where(LevelReward.guild_id == guild_id)
        )
        session.add_all(
            LevelReward(guild_id=guild_id, level=level, role_id=role_id)
            for level, role_id in draft.items()
        )
        await session.commit()
