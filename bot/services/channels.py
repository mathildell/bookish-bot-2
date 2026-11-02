"""Channel lookups shared by commands and startup wiring."""

from __future__ import annotations

import discord


async def resolve_text_channel(
    client: discord.Client, channel_id: int | None
) -> discord.TextChannel | None:
    """The text channel with ``channel_id``, or None when it is not reachable."""
    if not channel_id:
        return None
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel if isinstance(channel, discord.TextChannel) else None
