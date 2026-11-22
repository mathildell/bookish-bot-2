"""Autocomplete callbacks shared by several command groups."""

from __future__ import annotations

import discord
from discord import app_commands

from ..services.timezones import search_timezones


async def timezone_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=label, value=value)
        for value, label in search_timezones(current)
    ]
