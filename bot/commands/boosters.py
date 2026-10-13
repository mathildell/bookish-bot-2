"""Booster reaction admin commands (``/admin boosters ...``)."""

from __future__ import annotations

import discord
from discord import app_commands

from ..texts import t
from ..services.booster_react import build_identifiers, parse_emoji

boosters_group = app_commands.Group(
    name="boosters",
    description=t("boosters.group_description"),
)


def _display_name_for(user: discord.User, interaction: discord.Interaction) -> str:
    member = (
        interaction.guild.get_member(user.id) if interaction.guild else None
    )
    if member is not None:
        return member.display_name or member.name
    return user.global_name or user.name


def _success(content: str) -> discord.Embed:
    return discord.Embed(description=content, color=0x57F287)


def _nickname_extra(nicknames: str | None) -> str:
    """Render the extra nicknames as supplied by the admin, if any."""
    if not nicknames:
        return ""
    parts = [part.strip() for part in nicknames.split(",") if part.strip()]
    return f" ({', '.join(parts)})" if parts else ""


@boosters_group.command(
    name="add", description=t("boosters.add.description")
)
@app_commands.describe(
    user=t("boosters.add.param_user"),
    emoji=t("boosters.add.param_emoji"),
    nicknames=t("boosters.add.param_nicknames"),
)
async def add(
    interaction: discord.Interaction,
    user: discord.User,
    emoji: str,
    nicknames: str | None = None,
) -> None:
    await interaction.response.defer()

    try:
        parse_emoji(emoji)
    except ValueError:
        await interaction.followup.send(
            t("boosters.add.invalid_emoji", emoji=emoji), ephemeral=True
        )
        return

    identifiers = build_identifiers(
        discord_user_id=user.id,
        username=user.name,
        display_name=_display_name_for(user, interaction),
        nicknames=nicknames,
    )

    service = interaction.client.booster_reacts
    record = await service.upsert(
        discord_user_id=user.id,
        identifiers=identifiers,
        emoji=emoji,
    )

    await interaction.followup.send(
        embed=_success(
            t(
                "boosters.add.success",
                emoji=emoji,
                user=user.mention,
                id=record.id,
                extra=_nickname_extra(nicknames),
            )
        )
    )


@boosters_group.command(
    name="edit", description=t("boosters.edit.description")
)
@app_commands.describe(
    user=t("boosters.edit.param_user"),
    emoji=t("boosters.edit.param_emoji"),
    nicknames=t("boosters.edit.param_nicknames"),
)
async def edit(
    interaction: discord.Interaction,
    user: discord.User,
    emoji: str | None = None,
    nicknames: str | None = None,
) -> None:
    await interaction.response.defer()

    if emoji is None and nicknames is None:
        await interaction.followup.send(
            t("boosters.edit.nothing_to_change"), ephemeral=True
        )
        return

    service = interaction.client.booster_reacts
    record = await service.find(user.id)
    if record is None:
        await interaction.followup.send(
            t("boosters.edit.not_found", user=user.mention), ephemeral=True
        )
        return

    new_emoji = record.emoji
    if emoji is not None:
        try:
            parse_emoji(emoji)
        except ValueError:
            await interaction.followup.send(
                t("boosters.edit.invalid_emoji", emoji=emoji), ephemeral=True
            )
            return
        new_emoji = emoji

    identifiers = list(record.identifiers or [])
    if nicknames is not None:
        identifiers = build_identifiers(
            discord_user_id=user.id,
            username=user.name,
            display_name=_display_name_for(user, interaction),
            nicknames=nicknames,
        )

    record = await service.upsert(
        discord_user_id=user.id,
        identifiers=identifiers,
        emoji=new_emoji,
    )

    await interaction.followup.send(
        embed=_success(
            t(
                "boosters.edit.success",
                emoji=new_emoji,
                user=user.mention,
                id=record.id,
                extra=_nickname_extra(nicknames),
            )
        )
    )


@boosters_group.command(
    name="view", description=t("boosters.view.description")
)
@app_commands.describe(user=t("boosters.view.param_user"))
async def view(
    interaction: discord.Interaction, user: discord.User
) -> None:
    await interaction.response.defer()

    service = interaction.client.booster_reacts
    record = await service.find(user.id)
    if record is None:
        await interaction.followup.send(
            t("boosters.view.not_found", user=user.mention), ephemeral=True
        )
        return

    identifiers = record.identifiers or []
    listed = identifiers[1:]
    await interaction.followup.send(
        embed=_success(
            t(
                "boosters.view.success",
                emoji=record.emoji,
                user=user.mention,
                identifiers=", ".join(listed),
            )
        )
    )


@boosters_group.command(
    name="remove", description=t("boosters.remove.description")
)
@app_commands.describe(user=t("boosters.remove.param_user"))
async def remove(
    interaction: discord.Interaction, user: discord.User
) -> None:
    await interaction.response.defer()

    service = interaction.client.booster_reacts
    removed = await service.delete(user.id)
    if removed:
        await interaction.followup.send(
            embed=_success(
                t("boosters.remove.success", user=user.mention)
            )
        )
    else:
        await interaction.followup.send(
            t("boosters.remove.not_found", user=user.mention),
            ephemeral=True,
        )
