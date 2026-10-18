"""Level-up and admin notices posted outside the triggering interaction."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import discord

from .services.streaks import ClosedStreak, format_date
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


async def announce_streak_closed(
    channel: discord.abc.Messageable | None,
    *,
    closed: Sequence[ClosedStreak],
    colour: discord.Colour | None = None,
) -> None:
    """Tell the admins' updates channel which members' streaks just closed."""
    if channel is None or not closed:
        return
    lines = [
        t(
            "streaks.closed.entry",
            user=f"<@{entry.user_id}>",
            id=entry.user_id,
            date=format_date(entry.start_date),
        )
        for entry in closed
    ]
    embed = discord.Embed(
        title=t("streaks.closed.title"),
        description="\n".join(lines),
        colour=colour,
    )
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as exc:
        logger.warning("Could not announce closed streak: %s", exc)
