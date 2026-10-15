"""Pinned reference message for new forum threads.

When a thread (forum post) is created in the configured forum channel, the bot
reposts a fixed reference message into it and pins it. The reference message is
fetched fresh on every post so edits to it are always picked up.
"""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger("bh2.reference")


def reference_payload(message: discord.Message) -> dict | None:
    """Return the content/embeds to repost from the reference message.

    Returns ``None`` when the message has nothing to repost.
    """
    content = message.content or None
    embeds = list(message.embeds) or None
    if content is None and embeds is None:
        return None
    return {"content": content, "embeds": embeds}


async def _resolve_channel(
    bot: discord.Client, channel_id: int
) -> discord.abc.Messageable | None:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel


async def fetch_reference(bot: discord.Client) -> discord.Message | None:
    """Fetch the configured reference message, or ``None`` on failure."""
    channel_id = getattr(bot, "reference_channel_id", 0)
    message_id = getattr(bot, "reference_message_id", 0)
    if not channel_id or not message_id:
        return None

    channel = await _resolve_channel(bot, channel_id)
    if channel is None:
        logger.warning("Reference channel %s is not accessible", channel_id)
        return None

    try:
        return await channel.fetch_message(message_id)
    except discord.HTTPException:
        logger.warning(
            "Could not fetch reference message %s", message_id, exc_info=True
        )
        return None


async def post_reference(
    bot: discord.Client, target: discord.abc.Messageable
) -> bool:
    """Repost the reference message into ``target`` and pin it."""
    message = await fetch_reference(bot)
    if message is None:
        return False

    payload = reference_payload(message)
    if payload is None:
        logger.warning("Reference message %s has nothing to repost", message.id)
        return False

    try:
        sent = await target.send(**payload)
    except discord.HTTPException:
        logger.warning("Could not post reference in %s", target, exc_info=True)
        return False

    try:
        await sent.pin()
    except discord.HTTPException:
        logger.warning(
            "Could not pin reference message %s", sent.id, exc_info=True
        )
    return True
