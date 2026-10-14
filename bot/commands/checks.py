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


async def require_admin(interaction: discord.Interaction) -> bool:
    """Reply with a denial unless the member has Manage Server.

    Returns ``True`` when the member is allowed, ``False`` after sending the
    denial (the caller should then simply return).
    """
    if is_admin(interaction):
        return True
    await interaction.response.send_message(
        t("components.permissions.no_permission"), ephemeral=True
    )
    return False


def denial_message(
    interaction: discord.Interaction, target: discord.User, *, scope: str
) -> str | None:
    """Return the denial message, or ``None`` if the member may manage target.

    ``scope`` names the feature being managed (e.g. ``react``/``role``) and is
    only used to word the "manage your own" denial.
    """
    if is_admin(interaction):
        return None
    if is_booster(interaction):
        if target.id == interaction.user.id:
            return None
        return t("booster.permissions.cannot_manage_others", kind=scope)
    return t("components.permissions.no_permission")
