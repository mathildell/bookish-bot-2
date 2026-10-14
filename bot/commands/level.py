"""XP and level reward commands (/xp ..., /xp rewards ...)."""

from __future__ import annotations

import discord
from discord import app_commands

from ..db import session_scope
from ..notifications import announce_level_up
from ..services import level as level_service
from ..services.level_rewards import LevelRewardService, sync_member_roles
from ..services.members import resolve_members
from ..texts import t
from .checks import require_admin
from .level_rewards import register_rewards_group
from .listing import (
    EMBEDS_PER_PAGE,
    new_embed,
    send_paginated,
    set_author,
    set_transparent_image,
)

xp_group = app_commands.Group(
    name="xp",
    description=t("level.xp_group_description"),
)


def _rewards(interaction: discord.Interaction) -> LevelRewardService:
    return interaction.client.level_rewards


PROGRESS_BAR_SEGMENTS = 12


def _progress_values(user_xp) -> tuple[int, int]:
    """XP earned since reaching the current level, and what this level needs."""
    current = user_xp.total_xp - level_service.total_xp_for_level(user_xp.level)
    needed = level_service.xp_increment_to_reach_level(user_xp.level)
    return current, needed


def _progress_bar(current: int, needed: int) -> str:
    """Build the emoji progress bar: a cap, middle segments, then a cap."""
    segments = PROGRESS_BAR_SEGMENTS
    if needed <= 0:
        filled = segments
    else:
        filled = max(0, min(segments, int(current / needed * segments)))
    middle_filled = max(0, min(segments - 2, filled - 1))
    middle_empty = (segments - 2) - middle_filled
    start = (
        t("level.xp.level.bar_start_filled")
        if filled > 0
        else t("level.xp.level.bar_start_empty")
    )
    end = (
        t("level.xp.level.bar_end_filled")
        if filled >= segments
        else t("level.xp.level.bar_end_empty")
    )
    return (
        start
        + t("level.xp.level.bar_middle_filled") * middle_filled
        + t("level.xp.level.bar_middle_empty") * middle_empty
        + end
    )


def _progress_fields(
    user_xp, rank, service: LevelRewardService, guild: discord.Guild | None
) -> list[tuple[str, str]]:
    """Return the (name, value) fields describing a member's level progress."""
    current_level = user_xp.level
    current, needed = _progress_values(user_xp)
    role_id = service.role_for_level(current_level)
    role = guild.get_role(role_id) if guild and role_id else None
    return [
        (t("level.progress"), t("level.xp.level.progress", current=current, needed=needed)),
        (t("level.rank"), f"#{str(rank)}"),
        (t("level.level"), str(current_level)),
        (t("level.reward_role"), role.mention if role else t("level.none")),
        (t("level.total_xp"), f"{str(user_xp.total_xp)} XP"),
    ]


@xp_group.command(name="level", description=t("level.xp.level.description"))
@app_commands.describe(user=t("level.xp.level.param_user"))
async def xp_level(
    interaction: discord.Interaction, user: discord.User | None = None
) -> None:
    target = user or interaction.user
    async with session_scope() as session:
        user_xp = await level_service.get_user_xp(
            session, guild_id=interaction.guild_id, user_id=target.id
        )
        if user_xp is None:
            await interaction.response.send_message(
                t("level.xp.level.no_xp", user=target.mention)
            )
            return
        rank = await level_service.get_rank(
            session, guild_id=interaction.guild_id, total_xp=user_xp.total_xp
        )

    embed = new_embed(interaction.client)
    embed.title = t(
        "level.xp.level.embed_title",
        user=discord.utils.escape_markdown(target.display_name),
    )

    set_author(
        embed,
        target,
        target.display_name if target is not None else t("booster.unknown_name"),
    )

    current, needed = _progress_values(user_xp)
    embed.description = f"# {_progress_bar(current, needed)}"

    for name, value in _progress_fields(
        user_xp, rank, _rewards(interaction), interaction.guild
    ):
        embed.add_field(name=name, value=value, inline=False)

    set_transparent_image(embed, interaction.client)
    await interaction.response.send_message(embed=embed)


class _LeaderboardSource:
    """Lazy page source: one query and one member lookup batch per page."""

    def __init__(self, interaction: discord.Interaction, total: int) -> None:
        self._interaction = interaction
        self.page_count = max(1, -(-total // EMBEDS_PER_PAGE))
        self.total_results = total

    async def get_page(self, index: int) -> list[discord.Embed]:
        interaction = self._interaction
        async with session_scope() as session:
            rows = await level_service.page_of_user_xp(
                session,
                guild_id=interaction.guild_id,
                limit=EMBEDS_PER_PAGE,
                offset=index * EMBEDS_PER_PAGE,
            )
        members = await resolve_members(
            interaction.guild, [row.user_id for row, _ in rows]
        )
        service = _rewards(interaction)
        embeds = []
        for (row, rank), member in zip(rows, members):
            name = member.display_name if member else str(row.user_id)
            embed = new_embed(interaction.client)
            set_author(embed, member, name)
            for field_name, value in _progress_fields(
                row, rank, service, interaction.guild
            ):
                embed.add_field(name=field_name, value=value, inline=True)
            # Add an empty field to make the layout prettier
            embed.add_field(name=" ‎ ", value=" ‎ ", inline=True)
            set_transparent_image(embed, interaction.client)
            embeds.append(embed)
        return embeds


@xp_group.command(
    name="leaderboard", description=t("level.xp.leaderboard.description")
)
async def leaderboard(interaction: discord.Interaction) -> None:
    async with session_scope() as session:
        total = await level_service.count_user_xp(
            session, guild_id=interaction.guild_id
        )

    if total == 0:
        await interaction.response.send_message(
            t("level.xp.leaderboard.empty"), ephemeral=True
        )
        return

    await interaction.response.defer()
    await send_paginated(
        interaction, source=_LeaderboardSource(interaction, total)
    )


register_rewards_group(xp_group)


@xp_group.command(name="add", description=t("level.xp.add.description"))
@app_commands.describe(
    user=t("level.xp.add.param_user"),
    amount=t("level.xp.add.param_amount"),
)
async def add_xp(
    interaction: discord.Interaction, user: discord.Member, amount: int
) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer()
    amount = max(0, amount)
    async with session_scope() as session:
        result = await level_service.add_xp(
            session, guild_id=interaction.guild_id, user_id=user.id, amount=amount
        )
        await session.commit()
        user_xp = await level_service.get_user_xp(
            session, guild_id=interaction.guild_id, user_id=user.id
        )
    if result.new_level != result.old_level:
        if interaction.guild:
            await sync_member_roles(
                interaction.guild, user, result.new_level, _rewards(interaction)
            )
        await announce_level_up(interaction.channel, user, result.new_level)
    await interaction.followup.send(
        t(
            "level.xp_added",
            amount=amount,
            user=user.mention,
            level=user_xp.level,
            total_xp=user_xp.total_xp,
        )
    )


@xp_group.command(name="remove", description=t("level.xp.remove.description"))
@app_commands.describe(
    user=t("level.xp.remove.param_user"),
    amount=t("level.xp.remove.param_amount"),
)
async def remove_xp(
    interaction: discord.Interaction, user: discord.Member, amount: int
) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer()
    amount = max(0, amount)
    async with session_scope() as session:
        user_xp = await level_service.get_user_xp(
            session, guild_id=interaction.guild_id, user_id=user.id
        )
        if user_xp is None or user_xp.total_xp < amount:
            await interaction.followup.send(
                t("level.cannot_remove_more", user=user.mention)
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
        await sync_member_roles(
            interaction.guild, user, result.new_level, _rewards(interaction)
        )
    await interaction.followup.send(
        t(
            "level.xp_removed",
            amount=amount,
            user=user.mention,
            level=user_xp.level,
            total_xp=user_xp.total_xp,
        )
    )


@xp_group.command(name="freeze", description=t("level.xp.freeze.description"))
@app_commands.describe(user=t("level.xp.freeze.param_user"))
async def freeze_xp(interaction: discord.Interaction, user: discord.User) -> None:
    if not await require_admin(interaction):
        return
    async with session_scope() as session:
        await level_service.set_frozen(
            session, guild_id=interaction.guild_id, user_id=user.id, frozen=True
        )
        await session.commit()
    await interaction.response.send_message(
        t("level.xp.frozen", user=user.mention), ephemeral=True
    )


@xp_group.command(name="unfreeze", description=t("level.xp.unfreeze.description"))
@app_commands.describe(user=t("level.xp.unfreeze.param_user"))
async def unfreeze_xp(interaction: discord.Interaction, user: discord.User) -> None:
    if not await require_admin(interaction):
        return
    async with session_scope() as session:
        await level_service.set_frozen(
            session, guild_id=interaction.guild_id, user_id=user.id, frozen=False
        )
        await session.commit()
    await interaction.response.send_message(
        t("level.xp.unfrozen", user=user.mention), ephemeral=True
    )
