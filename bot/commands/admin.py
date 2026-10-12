"""Admin slash command root (``/admin ...``)."""

from __future__ import annotations

import discord
from discord import app_commands

from ..texts import t
from .boosters import boosters_group

admin_group = app_commands.Group(
    name="admin",
    description=t("admin.group_description"),
    default_permissions=discord.Permissions(manage_guild=True),
)
admin_group.add_command(boosters_group)


def register_commands(bot: discord.ext.commands.Bot, guild_id: int) -> None:
    """Bind the admin group to the configured guild and register it."""
    bot.tree.add_command(admin_group, guild=discord.Object(id=guild_id))
