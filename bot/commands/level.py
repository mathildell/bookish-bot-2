"""Level commands (/level ...)."""

from __future__ import annotations

import discord
from discord import app_commands

from ..db import session_scope
from ..services import level as level_service
from ..services.level_rewards import LevelRewardService, sync_user_roles
from ..texts import t
from .checks import require_admin
from .level_rewards import register_rewards_group
from .listing import send_paginated

level_group = app_commands.Group(
    name="level",
    description=t("level.group_description"),
)


def _rewards(interaction: discord.Interaction) -> LevelRewardService:
    return interaction.client.level_rewards


def _progress_fields(
    user_xp, service: LevelRewardService, guild: discord.Guild | None
) -> list[tuple[str, str]]:
    """Return the (name, value) fields describing a member's level progress."""
    current_level = user_xp.level
    # XP earned since reaching the current level, out of what this level needs.
    progress = user_xp.total_xp - level_service.total_xp_for_level(current_level)
    needed = level_service.xp_increment_to_reach_level(current_level)
    role_id = service.role_for_level(current_level)
    role = guild.get_role(role_id) if guild and role_id else None
    return [
        (t("level.level"), str(current_level)),
        (t("level.total_xp"), str(user_xp.total_xp)),
        (t("level.progress"), f"{progress}/{needed}"),
        (t("level.reward_role"), role.name if role else t("level.none")),
    ]


@level_group.command(name="me", description=t("level.me.description"))
async def me(interaction: discord.Interaction) -> None:
    async with session_scope() as session:
        user_xp = await level_service.get_user_xp(
            session, guild_id=interaction.guild_id, user_id=interaction.user.id
        )
        if user_xp is None:
            await interaction.response.send_message(
                t("level.you_have_no_xp"), ephemeral=True
            )
            return
        rank = await level_service.get_rank(
            session, guild_id=interaction.guild_id, total_xp=user_xp.total_xp
        )

    embed = discord.Embed(title=t("level.me.embed_title"))
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.add_field(name=t("level.rank"), value=str(rank))
    for name, value in _progress_fields(
        user_xp, _rewards(interaction), interaction.guild
    ):
        embed.add_field(name=name, value=value)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@level_group.command(
    name="leaderboard", description=t("level.leaderboard.description")
)
async def leaderboard(interaction: discord.Interaction) -> None:
    async with session_scope() as session:
        rows = await level_service.all_user_xp(
            session, guild_id=interaction.guild_id
        )
    ordered = sorted(rows, key=lambda r: (-r.total_xp, r.user_id))

    if not ordered:
        await interaction.response.send_message(
            t("level.leaderboard.empty"), ephemeral=True
        )
        return

    service = _rewards(interaction)
    pages = []
    previous_xp = None
    rank = 0
    for position, row in enumerate(ordered, start=1):
        if row.total_xp != previous_xp:
            rank = position
            previous_xp = row.total_xp
        member = interaction.guild.get_member(row.user_id) if interaction.guild else None
        name = member.display_name if member else str(row.user_id)
        embed = discord.Embed(title=f"#{rank} {name}")
        embed.add_field(name=t("level.rank"), value=str(rank))
        for field_name, value in _progress_fields(row, service, interaction.guild):
            embed.add_field(name=field_name, value=value)
        pages.append([embed])

    await interaction.response.defer()
    await send_paginated(interaction, pages, total_results=len(ordered))


admin_group = app_commands.Group(
    name="admin",
    description=t("level.admin_group_description"),
    parent=level_group,
)

register_rewards_group(level_group)


@admin_group.command(name="add_xp", description=t("level.admin.add_xp.description"))
@app_commands.describe(
    user=t("level.admin.add_xp.param_user"),
    amount=t("level.admin.add_xp.param_amount"),
)
async def add_xp(
    interaction: discord.Interaction, user: discord.User, amount: int
) -> None:
    if not await require_admin(interaction):
        return
    amount = max(0, amount)
    async with session_scope() as session:
        result = await level_service.add_xp(
            session, guild_id=interaction.guild_id, user_id=user.id, amount=amount
        )
        await session.commit()
        user_xp = await level_service.get_user_xp(
            session, guild_id=interaction.guild_id, user_id=user.id
        )
    if result.new_level != result.old_level and interaction.guild:
        await sync_user_roles(
            interaction.guild, user.id, result.new_level, _rewards(interaction)
        )
    await interaction.response.send_message(
        t(
            "level.xp_added",
            amount=amount,
            user=user.mention,
            level=user_xp.level,
            total_xp=user_xp.total_xp,
        ),
        ephemeral=True,
    )


@admin_group.command(
    name="remove_xp", description=t("level.admin.remove_xp.description")
)
@app_commands.describe(
    user=t("level.admin.remove_xp.param_user"),
    amount=t("level.admin.remove_xp.param_amount"),
)
async def remove_xp(
    interaction: discord.Interaction, user: discord.User, amount: int
) -> None:
    if not await require_admin(interaction):
        return
    amount = max(0, amount)
    async with session_scope() as session:
        user_xp = await level_service.get_user_xp(
            session, guild_id=interaction.guild_id, user_id=user.id
        )
        if user_xp is None or user_xp.total_xp < amount:
            await interaction.response.send_message(
                t("level.cannot_remove_more", user=user.mention), ephemeral=True
            )
            return
        result = await level_service.remove_xp(
            session, guild_id=interaction.guild_id, user_id=user.id, amount=amount
        )
        await session.commit()
        user_xp = await level_service.get_user_xp(
            session, guild_id=interaction.guild_id, user_id=user.id
        )
    if result.new_level != result.old_level and interaction.guild:
        await sync_user_roles(
            interaction.guild, user.id, result.new_level, _rewards(interaction)
        )
    await interaction.response.send_message(
        t(
            "level.xp_removed",
            amount=amount,
            user=user.mention,
            level=user_xp.level,
            total_xp=user_xp.total_xp,
        ),
        ephemeral=True,
    )


@admin_group.command(name="xp_freeze", description=t("level.admin.freeze.description"))
@app_commands.describe(user=t("level.admin.freeze.param_user"))
async def xp_freeze(interaction: discord.Interaction, user: discord.User) -> None:
    if not await require_admin(interaction):
        return
    async with session_scope() as session:
        await level_service.set_frozen(
            session, guild_id=interaction.guild_id, user_id=user.id, frozen=True
        )
        await session.commit()
    await interaction.response.send_message(
        t("level.admin.frozen", user=user.mention), ephemeral=True
    )


@admin_group.command(
    name="xp_unfreeze", description=t("level.admin.unfreeze.description")
)
@app_commands.describe(user=t("level.admin.unfreeze.param_user"))
async def xp_unfreeze(interaction: discord.Interaction, user: discord.User) -> None:
    if not await require_admin(interaction):
        return
    async with session_scope() as session:
        await level_service.set_frozen(
            session, guild_id=interaction.guild_id, user_id=user.id, frozen=False
        )
        await session.commit()
    await interaction.response.send_message(
        t("level.admin.unfrozen", user=user.mention), ephemeral=True
    )
