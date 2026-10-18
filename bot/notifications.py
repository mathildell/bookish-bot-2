"""Level-up announcements posted where the change happened."""

from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import TYPE_CHECKING

import discord

from .texts import t

if TYPE_CHECKING:
    from .services.streaks import ClosedStreak

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


async def announce_streak_closed(
    channel: discord.abc.Messageable | None,
    *,
    closed: ClosedStreak,
    colour: discord.Colour | None = None,
) -> None:
    """Tell the admins' updates channel that a member's streak just closed."""
    if channel is None:
        return
    started = int(
        datetime.combine(closed.start_date, time.min, tzinfo=timezone.utc).timestamp()
    )
    embed = discord.Embed(
        title=t("streaks.closed.title"),
        description=t("streaks.closed.description", user=f"<@{closed.user_id}>"),
        colour=colour,
    )
    embed.add_field(
        name=t("streaks.closed.user_id_field"),
        value=f"`{closed.user_id}`",
        inline=True,
    )
    embed.add_field(
        name=t("streaks.closed.started_field"),
        value=t("streaks.closed.started_value", ts=started),
        inline=True,
    )
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as exc:
        logger.warning("Could not announce closed streak: %s", exc)
