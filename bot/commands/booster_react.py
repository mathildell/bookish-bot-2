"""Booster reaction commands (``/booster react ...``).

Permissions:
- Admins/mods (Manage Server) can manage anyone.
- Members with the configured booster role can manage themselves only.
- Everyone can view anyone's registration.
"""

from __future__ import annotations

import discord
from discord import app_commands, PartialEmoji

from ..services.booster_react import build_identifiers, parse_emoji
from ..texts import t
from .checks import denial_message, require_admin
from .listing import (
    EMBEDS_PER_PAGE,
    boost_value,
    chunked,
    new_embed,
    resolve_member,
    send_paginated,
    set_author,
    set_transparent_image,
)

react_group = app_commands.Group(
    name="react",
    description=t("booster.react.group_description"),
)


def _username(user: discord.User) -> str:
    return user.name


def _display_name(user: discord.User, interaction: discord.Interaction) -> str:
    """Return the server-visible display name, falling back to the global name."""
    member = user if isinstance(user, discord.Member) else None
    if member is None and interaction.guild is not None:
        member = interaction.guild.get_member(user.id)
    if member is not None:
        return member.display_name
    return user.global_name or user.name


def _member_name(
    member: discord.Member | None, identifiers: list[str] | None
) -> str:
    """Name for a listed react: live member, else stored identifier, else label."""
    if member is not None:
        return member.display_name
    parts = identifiers or []
    for index in (1, 2):
        if len(parts) > index:
            return parts[index]
    return t("booster.unknown_name")


def _nickname_extra(nicknames: str | None) -> str:
    """Render the extra nicknames as supplied, if any."""
    if not nicknames:
        return ""
    parts = [part.strip() for part in nicknames.split(",") if part.strip()]
    return f" ({', '.join(parts)})" if parts else ""


def _react_embed(row, member: discord.Member | None, client) -> discord.Embed:
    embed = new_embed(client)
    partial = PartialEmoji.from_str(row.emoji)
    if partial.is_custom_emoji() and partial.url:
        embed.set_thumbnail(url=partial.url)
        inline = False
    else:
        embed.description = f"# {row.emoji}"
        inline = True

    set_author(embed, member, _member_name(member, row.identifiers))
    names = (row.identifiers or [])[1:]
    aliases = (
        ", ".join(discord.utils.escape_markdown(name) for name in names)
        if names
        else t("booster.react.list.no_aliases")
    )
    embed.add_field(
        name=t("booster.react.list.names_field"), value=aliases, inline=inline
    )
    embed.add_field(
        name=t("booster.boost.date_field"),
        value=boost_value(member),
        inline=inline,
    )
    set_transparent_image(embed, client)
    return embed


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
    denial = denial_message(interaction, target, scope="react")
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
        username=_username(target),
        display_name=_display_name(target, interaction),
        nicknames=nicknames,
    )

    await interaction.client.booster_reacts.upsert(
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
    denial = denial_message(interaction, target, scope="react")
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
            username=_username(target),
            display_name=_display_name(target, interaction),
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

    record = await interaction.client.booster_reacts.find(target.id)
    if record is None:
        await interaction.followup.send(
            t("booster.react.view.not_found", user=target.mention),
            ephemeral=True,
        )
        return

    identifiers = (record.identifiers or [])[1:]
    await interaction.followup.send(
        t(
            "booster.react.view.success",
            emoji=record.emoji,
            user=target.mention,
            identifiers=", ".join(
                discord.utils.escape_markdown(name) for name in identifiers
            ),
        )
    )


@react_group.command(
    name="remove", description=t("booster.react.remove.description")
)
@app_commands.describe(user=t("booster.react.remove.param_user"))
async def remove(
    interaction: discord.Interaction,
    user: discord.User | None = None,
) -> None:
    target = user if user is not None else interaction.user
    denial = denial_message(interaction, target, scope="react")
    if denial is not None:
        await interaction.response.send_message(denial, ephemeral=True)
        return

    await interaction.response.defer()

    removed = await interaction.client.booster_reacts.delete(target.id)
    if removed:
        await interaction.followup.send(
            t("booster.react.remove.success", user=target.mention)
        )
    else:
        await interaction.followup.send(
            t("booster.react.remove.not_found", user=target.mention),
            ephemeral=True,
        )


@react_group.command(name="list", description=t("booster.react.list.description"))
async def list_reacts(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    records = await interaction.client.booster_reacts.all()
    if not records:
        await interaction.followup.send(
            t("booster.react.list.empty"), ephemeral=True
        )
        return

    embeds = []
    for row in records:
        member = await resolve_member(interaction.guild, row.discord_user_id)
        embeds.append(_react_embed(row, member, interaction.client))

    await send_paginated(
        interaction,
        chunked(embeds, EMBEDS_PER_PAGE),
        total_results=len(records),
    )
