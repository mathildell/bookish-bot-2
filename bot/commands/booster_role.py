"""Booster role commands (``/booster role ...``).

Admins create the Discord roles themselves and register them with
``/booster role link``. Boosters may then ``view``/``edit`` their own role;
admins may do so for anyone. ``link``/``remove``/``sync`` are admin-only.
"""

from __future__ import annotations

import re

import discord
from discord import app_commands

from ..services.booster_react import CUSTOM_EMOJI_RE
from ..services.booster_role import resolve_notify_channel
from ..texts import t
from .checks import denial_message, is_admin

role_group = app_commands.Group(
    name="role",
    description=t("booster.role.group_description"),
)

_HEX_RE = re.compile(r"^#?(?P<hex>[0-9a-fA-F]{6})$")
_NONE_VALUES = {"none", "clear"}


def _is_none(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _NONE_VALUES


def _parse_hex(value: str) -> discord.Colour:
    match = _HEX_RE.match(value.strip())
    if not match:
        raise ValueError(value)
    return discord.Colour(int(match.group("hex"), 16))


def _color_label(value: int | None) -> str:
    if value is None or value == 0:
        return t("booster.role.none_label")
    return f"#{value:06x}"


def _icon_label(role: discord.Role) -> str:
    display = role.display_icon
    if display is None:
        return t("booster.role.none_label")
    if isinstance(display, str):
        return display
    return t("booster.role.image_label")


@role_group.command(name="view", description=t("booster.role.view.description"))
@app_commands.describe(user=t("booster.role.view.param_user"))
async def view(
    interaction: discord.Interaction,
    user: discord.User | None = None,
) -> None:
    target = user if user is not None else interaction.user
    denial = denial_message(interaction, target, scope="role")
    if denial is not None:
        await interaction.response.send_message(denial, ephemeral=True)
        return

    await interaction.response.defer()

    record = await interaction.client.booster_roles.find_by_user(target.id)
    role = interaction.guild.get_role(record.role_id) if record else None
    if record is None or role is None:
        await interaction.followup.send(
            t("booster.role.view.not_found", user=target.mention),
            ephemeral=True,
        )
        return

    secondary = role.secondary_colour
    await interaction.followup.send(
        t(
            "booster.role.view.success",
            user=target.mention,
            role=role.mention,
            name=role.name,
            color=_color_label(role.colour.value),
            gradient=_color_label(secondary.value if secondary else None),
            emoji=_icon_label(role),
        )
    )


@role_group.command(name="edit", description=t("booster.role.edit.description"))
@app_commands.describe(
    user=t("booster.role.edit.param_user"),
    name=t("booster.role.edit.param_name"),
    color=t("booster.role.edit.param_color"),
    gradient=t("booster.role.edit.param_gradient"),
    emoji=t("booster.role.edit.param_emoji"),
    image=t("booster.role.edit.param_image"),
)
async def edit(
    interaction: discord.Interaction,
    user: discord.User | None = None,
    name: str | None = None,
    color: str | None = None,
    gradient: str | None = None,
    emoji: str | None = None,
    image: discord.Attachment | None = None,
) -> None:
    target = user if user is not None else interaction.user
    denial = denial_message(interaction, target, scope="role")
    if denial is not None:
        await interaction.response.send_message(denial, ephemeral=True)
        return

    await interaction.response.defer()

    if emoji is not None and image is not None:
        await interaction.followup.send(
            t("booster.role.edit.icon_conflict"), ephemeral=True
        )
        return

    if all(v is None for v in (name, color, gradient, emoji, image)):
        await interaction.followup.send(
            t("booster.role.edit.nothing_to_change"), ephemeral=True
        )
        return

    record = await interaction.client.booster_roles.find_by_user(target.id)
    role = interaction.guild.get_role(record.role_id) if record else None
    if record is None or role is None:
        await interaction.followup.send(
            t("booster.role.edit.not_found", user=target.mention),
            ephemeral=True,
        )
        return

    changes: dict = {}

    if name is not None:
        new_name = name.strip()
        if not new_name:
            await interaction.followup.send(
                t("booster.role.edit.invalid_name"), ephemeral=True
            )
            return
        changes["name"] = new_name[:100]

    if color is not None:
        if _is_none(color):
            changes["colour"] = 0
        else:
            try:
                changes["colour"] = _parse_hex(color)
            except ValueError:
                await interaction.followup.send(
                    t("booster.role.edit.invalid_color", color=color),
                    ephemeral=True,
                )
                return

    if gradient is not None:
        if _is_none(gradient):
            changes["secondary_colour"] = None
        else:
            primary = changes.get("colour")
            primary_value = (
                primary.value if isinstance(primary, discord.Colour)
                else (primary if isinstance(primary, int) else role.colour.value)
            )
            if not primary_value:
                await interaction.followup.send(
                    t("booster.role.edit.gradient_needs_color"), ephemeral=True
                )
                return
            try:
                changes["secondary_colour"] = _parse_hex(gradient)
            except ValueError:
                await interaction.followup.send(
                    t("booster.role.edit.invalid_color", color=gradient),
                    ephemeral=True,
                )
                return

    if emoji is not None:
        if _is_none(emoji):
            changes["display_icon"] = None
        elif CUSTOM_EMOJI_RE.match(emoji.strip()):
            await interaction.followup.send(
                t("booster.role.edit.invalid_emoji", emoji=emoji), ephemeral=True
            )
            return
        else:
            changes["display_icon"] = emoji.strip()

    if image is not None:
        changes["display_icon"] = await image.read()

    try:
        await role.edit(**changes, reason=f"Booster role edit by {interaction.user}")
    except discord.Forbidden:
        await interaction.followup.send(
            t("booster.role.edit.hierarchy_error"), ephemeral=True
        )
        return
    except discord.HTTPException as exc:
        await interaction.followup.send(
            t("booster.role.edit.failed", error=str(exc)), ephemeral=True
        )
        return

    await interaction.followup.send(
        t("booster.role.edit.success", user=target.mention, role=role.mention)
    )


@role_group.command(name="link", description=t("booster.role.link.description"))
@app_commands.describe(
    user=t("booster.role.link.param_user"),
    role=t("booster.role.link.param_role"),
)
async def link(
    interaction: discord.Interaction,
    user: discord.Member,
    role: discord.Role,
) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message(
            t("booster.role.no_permission"), ephemeral=True
        )
        return

    await interaction.response.defer()

    owner = await interaction.client.booster_roles.find_by_role(role.id)
    if owner is not None and owner.discord_user_id != user.id:
        await interaction.followup.send(
            t(
                "booster.role.link.role_taken",
                role=role.mention,
                user=f"<@{owner.discord_user_id}>",
            ),
            ephemeral=True,
        )
        return

    existing = await interaction.client.booster_roles.find_by_user(user.id)
    if existing is not None and existing.role_id != role.id:
        await interaction.followup.send(
            t(
                "booster.role.link.user_taken",
                user=user.mention,
                role=f"<@&{existing.role_id}>",
            ),
            ephemeral=True,
        )
        return

    bot_member = interaction.guild.me
    if bot_member is None or role >= bot_member.top_role:
        await interaction.followup.send(
            t("booster.role.link.hierarchy_error", role=role.mention),
            ephemeral=True,
        )
        return

    try:
        if role not in user.roles:
            await user.add_roles(role, reason=f"Booster role link by {interaction.user}")
    except discord.Forbidden:
        await interaction.followup.send(
            t("booster.role.link.hierarchy_error", role=role.mention),
            ephemeral=True,
        )
        return

    await interaction.client.booster_roles.upsert(user.id, role.id)

    await interaction.followup.send(
        t("booster.role.link.success", user=user.mention, role=role.mention)
    )


@role_group.command(name="remove", description=t("booster.role.remove.description"))
@app_commands.describe(user=t("booster.role.remove.param_user"))
async def remove(
    interaction: discord.Interaction,
    user: discord.User,
) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message(
            t("booster.role.no_permission"), ephemeral=True
        )
        return

    await interaction.response.defer()

    record = await interaction.client.booster_roles.find_by_user(user.id)
    if record is None:
        await interaction.followup.send(
            t("booster.role.remove.not_found", user=user.mention),
            ephemeral=True,
        )
        return

    role = interaction.guild.get_role(record.role_id)
    if role is not None:
        try:
            await role.delete(reason=f"Booster role removed by {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send(
                t("booster.role.remove.hierarchy_error", role=role.mention),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                t("booster.role.remove.failed", error=str(exc)), ephemeral=True
            )
            return

    await interaction.client.booster_roles.delete_by_user(user.id)

    await interaction.followup.send(
        t("booster.role.remove.success", user=user.mention)
    )


@role_group.command(name="sync", description=t("booster.role.sync.description"))
async def sync(interaction: discord.Interaction) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message(
            t("booster.role.no_permission"), ephemeral=True
        )
        return

    await interaction.response.defer()

    channel = await resolve_notify_channel(
        interaction.guild,
        getattr(interaction.client, "bot_updates_channel_id", 0),
    )
    unlinked = await interaction.client.booster_roles.reconcile(
        interaction.guild, channel
    )

    await interaction.followup.send(
        t("booster.role.sync.success", count=len(unlinked))
    )
