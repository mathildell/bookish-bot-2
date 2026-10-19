"""XP and level reward commands (/xp ..., /xp rewards ...)."""

from __future__ import annotations

import discord
from discord import app_commands

from ..db import session_scope
from ..models import UserXP
from ..notifications import announce_level_up
from ..services import level as level_service
from ..services.level_rewards import LevelRewardService, sync_member_roles
from ..services.members import resolve_members
from ..texts import t
from .checks import require_admin
from .level_rewards import register_rewards_group
from .listing import PODIUM, new_embed, set_author, set_transparent_image

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


def _frozen_marker(user_xp) -> str:
    return t("level.frozen") if user_xp.xp_frozen else ""


def _progress_fields(
    user_xp, rank, service: LevelRewardService, guild: discord.Guild | None
) -> list[tuple[str, str]]:
    """Return the (name, value) fields describing a member's level progress."""
    current_level = user_xp.level
    current, needed = _progress_values(user_xp)
    role_id = service.role_for_level(current_level)
    role = guild.get_role(role_id) if guild and role_id else None
    progress = t(
        "level.xp.level.progress", 
        current=current, 
        needed=needed, 
        frozen=_frozen_marker(user_xp)
    )

    return [
        (t("level.progress"), progress),
        (t("level.rank"), f"#{str(rank)}"),
        (t("level.level"), str(current_level)),
        (t("level.reward_role"), role.mention if role else t("level.none")),
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

    set_author(embed, target, target.display_name)

    current, needed = _progress_values(user_xp)
    embed.description = f"# {_progress_bar(current, needed)}"

    for name, value in _progress_fields(
        user_xp, rank, _rewards(interaction), interaction.guild
    ):
        embed.add_field(name=name, value=value, inline=False)

    set_transparent_image(embed, interaction.client)
    await interaction.response.send_message(embed=embed)


LEADERBOARD_SIZE = 10


def _leaderboard_embed(
    client: discord.Client,
    rows: list[tuple[UserXP, int]],
    members: list[discord.Member | None],
) -> discord.Embed:
    embed = new_embed(client)
    embed.title = t("level.xp.leaderboard.title")
    lines = [t("level.xp.leaderboard.board_description")]
    for index, ((user_xp, _rank), member) in enumerate(zip(rows, members)):
        place = PODIUM[index] if index < 3 else f"{index + 1}\."
        mention = member.mention if member else f"<@{user_xp.user_id}>"
        lines.append(
            t("level.xp.leaderboard.entry", place=place, mention=mention)
        )
        current, needed = _progress_values(user_xp)
        lines.append(
            t(
                "level.xp.leaderboard.xp_details",
                level=user_xp.level,
                current=current,
                needed=needed,
                frozen=_frozen_marker(user_xp),
            )
        )
    embed.description = "\n".join(lines)
    set_transparent_image(embed, client)
    return embed


@xp_group.command(
    name="leaderboard", description=t("level.xp.leaderboard.description")
)
async def leaderboard(interaction: discord.Interaction) -> None:
    async with session_scope() as session:
        rows = await level_service.page_of_user_xp(
            session,
            guild_id=interaction.guild_id,
            limit=LEADERBOARD_SIZE,
            offset=0,
        )

    if not rows:
        await interaction.response.send_message(
            t("level.xp.leaderboard.empty"), ephemeral=True
        )
        return

    members = await resolve_members(
        interaction.guild, [user_xp.user_id for user_xp, _ in rows]
    )
    await interaction.response.send_message(
        embed=_leaderboard_embed(interaction.client, rows, members)
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
        current, needed = _progress_values(user_xp)

    if result.new_level != result.old_level and interaction.guild:
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
            current=current,
            needed=needed
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
        current, needed = _progress_values(user_xp)

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
            current=current,
            needed=needed
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
