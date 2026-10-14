"""Level-up announcements posted where the change happened."""

from __future__ import annotations

import logging

import discord

from .texts import t

logger = logging.getLogger("bh2")


async def announce_level_up(
    channel: discord.abc.Messageable | None,
    user: discord.abc.User,
    level: int,
) -> None:
    """Post the level-up notice, ignoring a missing channel or send failure."""
    if channel is None:
        return
    try:
        await channel.send(
            t("level.level_up", user=user.mention, level=level)
        )
    except discord.HTTPException as exc:
        logger.warning("Could not announce level up: %s", exc)
