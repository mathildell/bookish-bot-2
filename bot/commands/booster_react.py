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
from .checks import denial_message, is_admin
from .pagination import chunked, get_boosting_since, send_paginated

react_group = app_commands.Group(
    name="react",
    description=t("booster.react.group_description"),
)

PAGE_SIZE = 15


def _server_display_name(user: discord.User, interaction: discord.Interaction) -> str:
    if interaction.guild is not None:
        member = interaction.guild.get_member(user.id)
        if member is not None:
            if member.nick:
                return member.nick
            return member.display_name or member.name
    return user.global_name or user.name


def _username(user: discord.User) -> str:
    return user.name


def _nickname_extra(nicknames: str | None) -> str:
    """Render the extra nicknames as supplied, if any."""
    if not nicknames:
        return ""
    parts = [part.strip() for part in nicknames.split(",") if part.strip()]
    return f" ({', '.join(parts)})" if parts else ""


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
        display_name=_server_display_name(target, interaction),
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
            display_name=_server_display_name(target, interaction),
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


@react_group.command(name="list", description=t("booster.react.list.description"))
async def list_reacts(interaction: discord.Interaction) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message(
            t("booster.react.no_permission"), ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    records = await interaction.client.booster_reacts.all()
    if not records:
        await interaction.followup.send(
            t("booster.react.list.empty"), ephemeral=True
        )
        return

    lines = []
    for row in records:
        since = await get_boosting_since(interaction.guild, row.discord_user_id)
        boosting = (
            t("booster.react.list.boosting", ts=int(since.timestamp()))
            if since is not None
            else ""
        )
        rest = (row.identifiers or [])[1:]
        identifiers = ", ".join(rest) if rest else t("booster.react.list.no_aliases")
        lines.append(
            t(
                "booster.react.list.line",
                emoji=row.emoji,
                user=f"<@{row.discord_user_id}>",
                identifiers=identifiers,
                boosting=boosting,
            )
        )

    chunks = list(chunked(lines, PAGE_SIZE))
    total = len(chunks)
    embeds = []
    for index, chunk in enumerate(chunks):
        embed = discord.Embed(
            title=t("booster.react.list.title", count=len(records)),
            description="\n".join(chunk),
        )
        embed.set_footer(
            text=t("booster.pagination.page_footer", current=index + 1, total=total)
        )
        embeds.append(embed)

    await send_paginated(interaction, embeds)
