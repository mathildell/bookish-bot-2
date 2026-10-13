"""Booster role commands (``/booster role ...``).

Admins create the Discord roles themselves and register them with
``/booster role link``. Boosters may then ``edit`` their own role; admins may do
so for anyone. ``link``/``remove``/``sync`` are admin-only.
"""

from __future__ import annotations

import re

import discord
from discord import app_commands

from ..services.booster_react import CUSTOM_EMOJI_RE
from ..services.booster_role import resolve_notify_channel
from ..texts import t
from .checks import denial_message, is_admin
from .pagination import chunked, resolve_member, send_paginated
from ..config import load_config

role_group = app_commands.Group(
    name="role",
    description=t("booster.role.group_description"),
)

_HEX_RE = re.compile(r"^#?(?P<hex>[0-9a-fA-F]{6})$")
_NONE_VALUES = {"none", "clear"}
PAGE_SIZE = 15


EMBEDS_PER_PAGE = 6

config = load_config()

def _is_none(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _NONE_VALUES


def _parse_hex(value: str) -> discord.Colour:
    match = _HEX_RE.match(value.strip())
    if not match:
        raise ValueError(value)
    return discord.Colour(int(match.group("hex"), 16))


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


@role_group.command(name="list", description=t("booster.role.list.description"))
async def list_roles(interaction: discord.Interaction) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message(
            t("booster.role.no_permission"), ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    records = await interaction.client.booster_roles.all()
    if not records:
        await interaction.followup.send(
            t("booster.role.list.empty"), ephemeral=True
        )
        return

    embeds = []
    for row in records:
        member = await resolve_member(interaction.guild, row.discord_user_id)
        role = interaction.guild.get_role(row.role_id)
        embeds.append(_role_embed(row, member, role))

    pages = list(chunked(embeds, EMBEDS_PER_PAGE))
    total = len(pages)
    for index, page in enumerate(pages):
        page[-1].set_footer(
            text=t(
                "booster.role.list.footer",
                current=index + 1,
                total=total,
                count=len(records),
            )
        )

    await send_paginated(interaction, pages)


def _role_embed(row, member: discord.Member | None, role) -> discord.Embed:
    embed = discord.Embed()
    if role is not None:
        embed.description = f"# {role.mention}"
    if member is not None:
        embed.set_author(
            name=discord.utils.escape_markdown(member.display_name),
            icon_url=member.display_avatar.url,
        )

    if role is not None:
        fields = []
        # fields.append(("Role", role.mention, True))
        fields.append(("Color", f"#{role.colour.value:06X}" if role.colour.value else "None", True))
        fields.append(
            (
                "Gradient",
                f"#{role.secondary_colour.value:06X}" if getattr(role, "secondary_colour", None) and role.secondary_colour.value else "None",
                True,
            )
        )
        icon_val = role.display_icon
        if icon_val is None:
            icon_text = "None"
        elif isinstance(icon_val, discord.Asset):
            icon_text = "Custom icon"
            embed.set_thumbnail(url=icon_val.url)
        else:
            icon_text = str(icon_val)
        fields.append(("Emoji/Icon", icon_text, True))
        # image field - if it's an asset with url, show it; otherwise maybe indicate image
        if isinstance(icon_val, discord.Asset) and icon_val.url:
            # put image link or just note - but embed can have image; maybe also add field
            pass
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    return embed
