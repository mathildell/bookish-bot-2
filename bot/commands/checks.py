"""Shared permission checks for the ``/booster`` command groups.

Rules (same for reactions and roles):
- Members with Manage Server (admins/mods) may manage anyone.
- Members with the configured booster role may manage themselves only.
- Everyone else is denied.
"""

from __future__ import annotations

import discord

from ..texts import t


def is_admin(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction, "permissions", None)
    return bool(permissions and permissions.manage_guild)


def is_booster(interaction: discord.Interaction) -> bool:
    role_id = getattr(interaction.client, "booster_role_id", 0)
    if not role_id:
        return False
    roles = getattr(interaction.user, "roles", None) or []
    return any(role.id == role_id for role in roles)


def denial_reason(
    interaction: discord.Interaction, target: discord.User
) -> str | None:
    """Return a text-key suffix for the denial, or ``None`` if allowed."""
    if is_admin(interaction):
        return None
    if is_booster(interaction):
        if target.id == interaction.user.id:
            return None
        return "cannot_manage_others"
    return "no_permission"


def denial_message(
    interaction: discord.Interaction, target: discord.User, *, scope: str
) -> str | None:
    """Return the localized denial message, or ``None`` if allowed."""
    reason = denial_reason(interaction, target)
    if reason is None:
        return None
    return t(f"booster.{scope}.{reason}")
