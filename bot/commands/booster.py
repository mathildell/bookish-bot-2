"""Booster commands (``/booster ...``).

Root group that hosts the ``react`` and ``role`` sub-groups.
"""

from __future__ import annotations

import discord
from discord import app_commands

from ..texts import t
from .booster_react import react_group
from .booster_role import role_group

booster_group = app_commands.Group(
    name="booster",
    description=t("booster.group_description"),
)
booster_group.add_command(react_group)
booster_group.add_command(role_group)


def register_commands(bot: discord.ext.commands.Bot, guild_id: int) -> None:
    """Bind the booster group to the configured guild and register it."""
    bot.tree.add_command(booster_group, guild=discord.Object(id=guild_id))
