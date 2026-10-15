"""Guild member resolution shared by commands and services.

The bot runs without the privileged Members intent, so members that have not
been seen recently are not in the cache and must be fetched individually.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import discord

_FETCH_CONCURRENCY = 5


@dataclass(frozen=True)
class MemberLookup:
    """Outcome of resolving one member.

    ``absent`` is only set when Discord confirmed the member is not in the guild
    (``NotFound``). A failed lookup leaves it false, so callers can tell a member
    who left from one whose lookup was rejected.
    """

    member: discord.Member | None
    absent: bool = False


async def _lookup_member(
    guild: discord.Guild | None, user_id: int
) -> MemberLookup:
    if guild is None:
        return MemberLookup(None)
    member = guild.get_member(user_id)
    if member is not None:
        return MemberLookup(member)
    try:
        return MemberLookup(await guild.fetch_member(user_id))
    except discord.NotFound:
        return MemberLookup(None, absent=True)
    except discord.HTTPException:
        return MemberLookup(None)


async def resolve_members(
    guild: discord.Guild | None, user_ids: list[int]
) -> list[discord.Member | None]:
    """Resolve several members, capping concurrent API calls.

    Results line up positionally with ``user_ids``; ids that could not be
    resolved become ``None``.
    """
    return [
        lookup.member for lookup in await resolve_member_lookups(guild, user_ids)
    ]


async def resolve_member_lookups(
    guild: discord.Guild | None, user_ids: list[int]
) -> list[MemberLookup]:
    """Resolve several members, reporting why a lookup did not return one.

    Results line up positionally with ``user_ids``.
    """
    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def one(user_id: int) -> MemberLookup:
        async with semaphore:
            return await _lookup_member(guild, user_id)

    return await asyncio.gather(*(one(user_id) for user_id in user_ids))
