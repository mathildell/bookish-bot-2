"""Guild member resolution shared by commands and services.

The bot runs without the privileged Members intent, so members that have not
been seen recently are not in the cache and must be fetched individually.
"""

from __future__ import annotations

import asyncio

import discord

_FETCH_CONCURRENCY = 5


async def resolve_member(
    guild: discord.Guild | None, user_id: int
) -> discord.Member | None:
    """Return a guild member by id, fetching them if not cached.

    Returns ``None`` when the guild is unavailable or Discord rejects the fetch.
    """
    if guild is None:
        return None
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


async def resolve_members(
    guild: discord.Guild | None, user_ids: list[int]
) -> list[discord.Member | None]:
    """Resolve several members, capping concurrent API calls.

    Results line up positionally with ``user_ids``.
    """
    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def one(user_id: int) -> discord.Member | None:
        async with semaphore:
            return await resolve_member(guild, user_id)

    return await asyncio.gather(*(one(user_id) for user_id in user_ids))
