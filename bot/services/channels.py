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


async def resolve_updates_channel(
    client: discord.Client, guild: discord.Guild
) -> discord.abc.Messageable | None:
    """The admins' updates channel from the bot settings, if reachable."""
    return await resolve_notify_channel(
        guild, getattr(client, "bot_updates_channel_id", 0)
    )
