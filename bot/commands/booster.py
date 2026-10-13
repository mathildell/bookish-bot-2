"""Booster reaction commands (``/booster react ...``).

Permissions:
- Admins/mods (Manage Server) can manage anyone.
- Members with the configured booster role can manage themselves only.
- Everyone can view anyone's registration.
"""

from __future__ import annotations

import discord
from discord import app_commands

from ..services.booster_react import build_identifiers, parse_emoji
from ..texts import t

booster_group = app_commands.Group(
    name="booster",
    description=t("booster.group_description"),
)
react_group = app_commands.Group(
    name="react",
    description=t("booster.react.group_description"),
)
booster_group.add_command(react_group)


def _display_name_for(user: discord.User, interaction: discord.Interaction) -> str:
    member = (
        interaction.guild.get_member(user.id) if interaction.guild else None
    )
    if member is not None:
        return member.display_name or member.name
    return user.global_name or user.name


def _nickname_extra(nicknames: str | None) -> str:
    """Render the extra nicknames as supplied, if any."""
    if not nicknames:
        return ""
    parts = [part.strip() for part in nicknames.split(",") if part.strip()]
    return f" ({', '.join(parts)})" if parts else ""


def _is_admin(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction, "permissions", None)
    return bool(permissions and permissions.manage_guild)


def _is_booster(interaction: discord.Interaction) -> bool:
    role_id = getattr(interaction.client, "booster_role_id", 0)
    if not role_id:
        return False
    roles = getattr(interaction.user, "roles", None) or []
    return any(role.id == role_id for role in roles)


def _denial(interaction: discord.Interaction, target: discord.User) -> str | None:
    """Return a denial message, or ``None`` if the actor may manage ``target``."""
    if _is_admin(interaction):
        return None
    if _is_booster(interaction):
        if target.id == interaction.user.id:
            return None
        return t("booster.react.cannot_manage_others")
    return t("booster.react.no_permission")


@react_group.command(name="add", description=t("booster.react.add.description"))
@app_commands.describe(
    emoji=t("booster.react.add.param_emoji"),
    user=t("booster.react.add.param_user"),
    nicknames=t("booster.react.add.param_nicknames"),
)
async def add(
    interaction: discord.Interaction,
    emoji: str,
    user: discord.User | None = None,
    nicknames: str | None = None,
) -> None:
    target = user if user is not None else interaction.user
    denial = _denial(interaction, target)
    if denial is not None:
        await interaction.response.send_message(denial, ephemeral=True)
        return

    await interaction.response.defer()

    try:
        parse_emoji(emoji)
    except ValueError:
        await interaction.followup.send(
            t("booster.react.add.invalid_emoji", emoji=emoji), ephemeral=True
        )
        return

    identifiers = build_identifiers(
        discord_user_id=target.id,
        username=target.name,
        display_name=_display_name_for(target, interaction),
        nicknames=nicknames,
    )

    service = interaction.client.booster_reacts
    await service.upsert(
        discord_user_id=target.id,
        identifiers=identifiers,
        emoji=emoji,
    )

    await interaction.followup.send(
        t(
            "booster.react.add.success",
            emoji=emoji,
            user=target.mention,
            extra=_nickname_extra(nicknames),
        )
    )


@react_group.command(name="edit", description=t("booster.react.edit.description"))
@app_commands.describe(
    user=t("booster.react.edit.param_user"),
    emoji=t("booster.react.edit.param_emoji"),
    nicknames=t("booster.react.edit.param_nicknames"),
)
async def edit(
    interaction: discord.Interaction,
    user: discord.User | None = None,
    emoji: str | None = None,
    nicknames: str | None = None,
) -> None:
    target = user if user is not None else interaction.user
    denial = _denial(interaction, target)
    if denial is not None:
        await interaction.response.send_message(denial, ephemeral=True)
        return

    await interaction.response.defer()

    if emoji is None and nicknames is None:
        await interaction.followup.send(
            t("booster.react.edit.nothing_to_change"), ephemeral=True
        )
        return

    service = interaction.client.booster_reacts
    record = await service.find(target.id)
    if record is None:
        await interaction.followup.send(
            t("booster.react.edit.not_found", user=target.mention),
            ephemeral=True,
        )
        return

    new_emoji = record.emoji
    if emoji is not None:
        try:
            parse_emoji(emoji)
        except ValueError:
            await interaction.followup.send(
                t("booster.react.edit.invalid_emoji", emoji=emoji),
                ephemeral=True,
            )
            return
        new_emoji = emoji

    identifiers = list(record.identifiers or [])
    if nicknames is not None:
        identifiers = build_identifiers(
            discord_user_id=target.id,
            username=target.name,
            display_name=_display_name_for(target, interaction),
            nicknames=nicknames,
        )

    await service.upsert(
        discord_user_id=target.id,
        identifiers=identifiers,
        emoji=new_emoji,
    )

    await interaction.followup.send(
        t(
            "booster.react.edit.success",
            emoji=new_emoji,
            user=target.mention,
            extra=_nickname_extra(nicknames),
        )
    )


@react_group.command(name="view", description=t("booster.react.view.description"))
@app_commands.describe(user=t("booster.react.view.param_user"))
async def view(
    interaction: discord.Interaction,
    user: discord.User | None = None,
) -> None:
    target = user if user is not None else interaction.user

    await interaction.response.defer()

    service = interaction.client.booster_reacts
    record = await service.find(target.id)
    if record is None:
        await interaction.followup.send(
            t("booster.react.view.not_found", user=target.mention),
            ephemeral=True,
        )
        return

    identifiers = record.identifiers or []
    await interaction.followup.send(
        t(
            "booster.react.view.success",
            emoji=record.emoji,
            user=target.mention,
            identifiers=", ".join(identifiers[1:]),
        )
    )


@react_group.command(name="remove", description=t("booster.react.remove.description"))
@app_commands.describe(user=t("booster.react.remove.param_user"))
async def remove(
    interaction: discord.Interaction,
    user: discord.User | None = None,
) -> None:
    target = user if user is not None else interaction.user
    denial = _denial(interaction, target)
    if denial is not None:
        await interaction.response.send_message(denial, ephemeral=True)
        return

    await interaction.response.defer()

    service = interaction.client.booster_reacts
    removed = await service.delete(target.id)
    if removed:
        await interaction.followup.send(
            t("booster.react.remove.success", user=target.mention)
        )
    else:
        await interaction.followup.send(
            t("booster.react.remove.not_found", user=target.mention),
            ephemeral=True,
        )


def register_commands(bot: discord.ext.commands.Bot, guild_id: int) -> None:
    """Bind the booster group to the configured guild and register it."""
    bot.tree.add_command(booster_group, guild=discord.Object(id=guild_id))
