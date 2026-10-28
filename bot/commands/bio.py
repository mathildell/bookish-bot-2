"""Admin commands for member bios (``/bio``)."""

from __future__ import annotations

import discord
from discord import app_commands

from ..services.members import resolve_members
from ..texts import t
from .checks import require_admin
from .listing import EMBEDS_PER_PAGE, chunked, new_embed, send_paginated

bio_group = app_commands.Group(
    name="bio",
    description=t("bio.group_description"),
    default_permissions=discord.Permissions(manage_guild=True),
)


@bio_group.command(name="edit", description=t("bio.edit.description"))
@app_commands.describe(
    member=t("bio.edit.param_member"),
    bio=t("bio.edit.param_bio"),
)
async def bio_edit(interaction: discord.Interaction, member: discord.Member, bio: str) -> None:
    if not await require_admin(interaction):
        return
    bio = (bio or "").strip()
    if bio.lower() == "none":
        deleted = await interaction.client.member_profiles.delete(member.id)
        if deleted:
            await interaction.response.send_message(t("bio.edit.cleared", member=member.mention), ephemeral=True)
        else:
            await interaction.response.send_message(t("bio.edit.no_bio", member=member.mention), ephemeral=True)
        return
    if len(bio) > 500:
        await interaction.response.send_message(t("bio.edit.too_long"), ephemeral=True)
        return
    await interaction.client.member_profiles.upsert(member.id, bio, updated_by=interaction.user.id)
    await interaction.response.send_message(t("bio.edit.success", member=member.mention), ephemeral=True)


@bio_group.command(name="list", description=t("bio.list.description"))
async def bio_list(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    rows = await interaction.client.member_profiles.all()
    if not rows:
        await interaction.followup.send(t("bio.list.empty"), ephemeral=True)
        return
    members = await resolve_members(interaction.guild, [row.user_id for row in rows])
    member_map = {m.id: m for m in members if m is not None}
    embeds: list[discord.Embed] = []
    for row in rows:
        member = member_map.get(row.user_id)
        embed = new_embed(interaction.client)
        name = member.display_name if member is not None else f"Unknown ({row.user_id})"
        embed.title = name
        embed.description = row.bio
        embeds.append(embed)
    pages = list(chunked(embeds, EMBEDS_PER_PAGE))
    await send_paginated(interaction, pages=pages, total_results=len(embeds))
