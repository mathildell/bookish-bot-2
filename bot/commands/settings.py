"""Cleanup settings commands (``/settings ...``).

Admins view and edit how long the scheduled cleanup jobs keep data before
deleting it. The jobs themselves keep running daily; these values are their
retention windows, expressed in days.
"""

from __future__ import annotations

import discord
from discord import app_commands

from ..texts import t
from .checks import require_admin
from .listing import new_embed

settings_group = app_commands.Group(
    name="settings",
    description=t("settings.group_description"),
    default_permissions=discord.Permissions(manage_guild=True),
)

PURGE_BOOK_RELEASES = "purge_book_releases"
PURGE_STREAK_LOGS = "purge_streak_logs"

SETTING_KEYS = (PURGE_BOOK_RELEASES, PURGE_STREAK_LOGS)

MIN_DAYS = 1
MAX_DAYS = 3650


def setting_label(key: str) -> str:
    """Human-readable name for a setting key."""
    return t(f"settings.keys.{key}")


def current_value(interaction: discord.Interaction, key: str) -> int:
    """Read the current number of days for a setting key."""
    if key == PURGE_BOOK_RELEASES:
        return interaction.client.releases.purge_days
    return interaction.client.streaks.log_retention_days


async def apply_value(
    interaction: discord.Interaction, key: str, days: int
) -> None:
    """Persist a setting key to a new number of days."""
    if key == PURGE_BOOK_RELEASES:
        await interaction.client.releases.set_purge_days(days)
    else:
        await interaction.client.streaks.set_log_retention_days(days)


def parse_days(raw: str) -> int:
    """Parse a whole number of days within the allowed range."""
    text = raw.strip()
    if not text.isdigit():
        raise ValueError(raw)
    days = int(text)
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise ValueError(raw)
    return days


@settings_group.command(name="list", description=t("settings.list.description"))
async def list_settings(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    embed = new_embed(interaction.client)
    embed.title = t("settings.list.title")
    embed.description = t("settings.list.hint")
    for key in SETTING_KEYS:
        embed.add_field(
            name=setting_label(key),
            value=t("settings.list.value", days=current_value(interaction, key)),
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@settings_group.command(name="edit", description=t("settings.edit.description"))
@app_commands.choices(
    key=[
        app_commands.Choice(name=setting_label(key), value=key)
        for key in SETTING_KEYS
    ]
)
@app_commands.describe(
    key=t("settings.edit.param_key"),
    value=t("settings.edit.param_value"),
)
async def edit(
    interaction: discord.Interaction, key: str, value: str
) -> None:
    if not await require_admin(interaction):
        return
    try:
        days = parse_days(value)
    except ValueError:
        await interaction.response.send_message(
            t(
                "settings.edit.invalid_value",
                value=value,
                min=MIN_DAYS,
                max=MAX_DAYS,
            ),
            ephemeral=True,
        )
        return
    await apply_value(interaction, key, days)
    await interaction.response.send_message(
        t("settings.edit.success", key=setting_label(key), days=days)
    )
