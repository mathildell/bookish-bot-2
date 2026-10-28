"""Per-member bios for the chatbot."""

from __future__ import annotations

from sqlalchemy import delete, select

from ..db import session_scope
from ..models import MemberProfile


class MemberProfileService:
    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id

    async def get(self, user_id: int) -> MemberProfile | None:
        async with session_scope() as session:
            return (
                await session.execute(
                    select(MemberProfile).where(
                        MemberProfile.guild_id == self.guild_id,
                        MemberProfile.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()

    async def bio_for(self, user_id: int) -> str | None:
        row = await self.get(user_id)
        if row is None:
            return None
        bio = (row.bio or "").strip()
        return bio or None

    async def upsert(self, user_id: int, bio: str, updated_by: int | None = None) -> MemberProfile:
        bio = (bio or "").strip()
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(MemberProfile).where(
                        MemberProfile.guild_id == self.guild_id,
                        MemberProfile.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = MemberProfile(
                    guild_id=self.guild_id,
                    user_id=user_id,
                    bio=bio,
                    updated_by=updated_by,
                )
                session.add(row)
            else:
                row.bio = bio
                row.updated_by = updated_by
            await session.commit()
            await session.refresh(row)
            return row

    async def delete(self, user_id: int) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                delete(MemberProfile).where(
                    MemberProfile.guild_id == self.guild_id,
                    MemberProfile.user_id == user_id,
                )
            )
            await session.commit()
            return result.rowcount > 0

    async def delete_for_member(self, user_id: int) -> bool:
        return await self.delete(user_id)

    async def all(self) -> list[MemberProfile]:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(MemberProfile)
                    .where(MemberProfile.guild_id == self.guild_id)
                    .order_by(MemberProfile.user_id)
                )
            ).scalars().all()
            return list(rows)
