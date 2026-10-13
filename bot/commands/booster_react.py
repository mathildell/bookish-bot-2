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
from .checks import denial_message, is_admin
from .pagination import chunked, resolve_member, send_paginated
from ..config import load_config

react_group = app_commands.Group(
    name="react",
    description=t("booster.react.group_description"),
)

EMBEDS_PER_PAGE = 6

config = load_config()


def _member_name(
    member: discord.Member | None, identifiers: list[str] | None
) -> str:
    if member is not None:
        return member.display_name
    parts = identifiers or []
    if len(parts) > 1:
        return parts[1]
    if len(parts) > 2:
        return parts[2]
    return "Unknown"


def _boost_value(member: discord.Member | None) -> str:
    if member is not None and member.premium_since is not None:
        return f"<t:{int(member.premium_since.timestamp())}:f>"
    return t("booster.pagination.not_boosting")


def _authorize_embed(
    embed: discord.Embed, member: discord.Member | None, name: str
) -> None:
    escaped = discord.utils.escape_markdown(name)
    if member is not None:
        embed.set_author(name=escaped, icon_url=member.display_avatar.url)
    else:
        embed.set_author(name=escaped)


def _react_embed(row, member: discord.Member | None) -> discord.Embed:
    embed = discord.Embed()
    try:
        partial_emoji = PartialEmoji.from_str(row.emoji)
        if partial_emoji.is_custom_emoji() and partial_emoji.url:
            embed.set_thumbnail(url=partial_emoji.url)
            inline=False
        else:
            embed.description = f"# {row.emoji}"
            inline=True
    except Exception:
        embed.description = f"# {row.emoji}"

    _authorize_embed(embed, member, _member_name(member, row.identifiers))
    names = (row.identifiers or [])[1:]
    value = (
        ", ".join(discord.utils.escape_markdown(name) for name in names)
        if names
        else t("booster.react.list.no_aliases")
    )
    embed.add_field(
        name=t("booster.react.list.names_field"), 
        value=value, 
        inline=inline
    )
    embed.add_field(
        name=t("booster.pagination.boost_field"),
        value=_boost_value(member),
        inline=inline,
    )
    embed.set_image(
        url=config.get("transparent_embed_image")
    )

    return embed


def _server_display_name(user: discord.User, interaction: discord.Interaction) -> str:
    member = None
    if interaction.guild is not None:
        member = interaction.guild.get_member(user.id)
    if isinstance(user, discord.Member):
        member = user
    if member is None and interaction.guild is not None:
        try:
            member = interaction.guild.get_member(user.id)
        except Exception:
            member = None
    if member is None and interaction.guild is not None:
        try:
            import asyncio

            # but we're in sync context? no, we're in async command
            # so can't await directly; but easier to just try fetch via client? maybe better to handle in caller differently
            # simpler: rely on get_member first
            pass
        except Exception:
            pass
    if member is not None:
        # Return the server-visible display name in full: prefer nick if set,
        # otherwise member.display_name (which falls back to global)
        if getattr(member, "nick", None):
            return str(member.nick)
        dn = getattr(member, "display_name", None) or getattr(member, "name", None)
        if dn:
            return str(dn)
    return str(getattr(user, "global_name", None) or getattr(user, "name", ""))


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
            identifiers=", ".join(
                discord.utils.escape_markdown(n) for n in identifiers[1:]
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

    embeds = []
    for row in records:
        member = await resolve_member(interaction.guild, row.discord_user_id)
        embeds.append(_react_embed(row, member))

    pages = list(chunked(embeds, EMBEDS_PER_PAGE))
    await send_paginated(interaction, pages, total_results=len(records))
