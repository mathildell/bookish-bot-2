"""XP and level reward commands (/xp ..., /xp rewards ...)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

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
    *,
    title: str | None = None,
    board_description: str | None = None,
    note=None,
    details=None,
) -> discord.Embed:
    embed = new_embed(client)
    embed.title = title or t("level.xp.leaderboard.title")
    lines = [
        board_description or t("level.xp.leaderboard.board_description")
    ]
    for index, ((user_xp, _rank), member) in enumerate(zip(rows, members)):
        place = PODIUM[index] if index < 3 else f"{index + 1}\."
        mention = member.mention if member else f"<@{user_xp.user_id}>"
        lines.append(
            t("level.xp.leaderboard.entry", place=place, mention=mention)
        )
        if details is not None:
            lines.append(details(user_xp))
            continue
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
    
    if note is not None:
        embed.description += f"\n\n{note}"
    
    if not rows:
        embed.description += f"\n{t('level.xp.leaderboard.weekly_empty')}"
    
    set_transparent_image(embed, client)
    return embed


def _weekly_details(row: level_service.WeeklyStanding) -> str:
    """Describe a member's weekly XP and the level progress it bought."""
    if row.level_start == row.level_end:
        return t(
            "level.xp.leaderboard.weekly_details_progress",
            xp=row.xp,
            n1=row.progress_start,
            n2=row.progress_end,
            needed=row.needed,
        )
    return t(
        "level.xp.leaderboard.weekly_details_levels",
        xp=row.xp,
        l1=row.level_start,
        l2=row.level_end,
        n1=row.progress_start,
        n2=row.progress_end,
        needed=row.needed,
    )


def weekly_leaderboard_embed(
    client: discord.Client,
    rows: list[tuple[level_service.WeeklyStanding, int]],
    members: list[discord.Member | None],
    *,
    week_start: date,
) -> discord.Embed:
    """Build the weekly board using the all-time leaderboard layout."""
    reset_at = datetime.combine(
        week_start + timedelta(days=7), time.min, tzinfo=timezone.utc
    )
    embed = _leaderboard_embed(
        client,
        rows,
        members,
        title=t("level.xp.leaderboard.weekly_title"),
        board_description=t('level.xp.leaderboard.weekly_board_description'),
        note=t(
            "level.xp.leaderboard.weekly_week_of",
            start=week_start.strftime("%b %d, %Y"),
            reset=discord.utils.format_dt(reset_at, "R"),
        ),
        details=_weekly_details,
    )
    return embed


class LeaderboardView(discord.ui.View):
    """Switch the leaderboard between all-time and the current week."""

    def __init__(
        self,
        all_time_embed: discord.Embed,
        weekly_embed: discord.Embed,
    ) -> None:
        super().__init__(timeout=180)
        self.embeds = {"all": all_time_embed, "weekly": weekly_embed}
        self.showing = "all"
        self.message: discord.Message | None = None
        self._sync_styles()

    def _sync_styles(self) -> None:
        self.all_time_button.disabled = self.showing == "all"
        self.weekly_button.disabled = self.showing == "weekly"

    async def _switch(self, interaction: discord.Interaction, which: str) -> None:
        self.showing = which
        self._sync_styles()
        await interaction.response.edit_message(
            embed=self.embeds[which], view=self
        )

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label=t("level.xp.leaderboard.button_all_time"),
        emoji=t("level.xp.leaderboard.button_all_time_emoji"),
        style=discord.ButtonStyle.secondary,
    )
    async def all_time_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._switch(interaction, "all")

    @discord.ui.button(
        label=t("level.xp.leaderboard.button_weekly"),
        emoji=t("level.xp.leaderboard.button_weekly_emoji"),
        style=discord.ButtonStyle.secondary,
    )
    async def weekly_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._switch(interaction, "weekly")


@xp_group.command(
    name="leaderboard", description=t("level.xp.leaderboard.description")
)
async def leaderboard(interaction: discord.Interaction) -> None:
    now = level_service.utcnow()
    week_start = level_service.week_start_for(now.date())
    async with session_scope() as session:
        rows = await level_service.page_of_user_xp(
            session,
            guild_id=interaction.guild_id,
            limit=LEADERBOARD_SIZE,
            offset=0,
        )
        weekly_rows = await level_service.page_of_weekly_xp(
            session,
            guild_id=interaction.guild_id,
            week_start=week_start,
            limit=LEADERBOARD_SIZE,
        )

    if not rows:
        await interaction.response.send_message(
            t("level.xp.leaderboard.empty"), ephemeral=True
        )
        return

    members = await resolve_members(
        interaction.guild, [user_xp.user_id for user_xp, _ in rows]
    )
    weekly_members = await resolve_members(
        interaction.guild, [row.user_id for row in weekly_rows]
    )
    weekly_ranked = [
        (row, index + 1) for index, row in enumerate(weekly_rows)
    ]
    all_time_embed = _leaderboard_embed(interaction.client, rows, members)
    weekly_embed = weekly_leaderboard_embed(
        interaction.client,
        weekly_ranked,
        weekly_members,
        week_start=week_start,
    )
    view = LeaderboardView(all_time_embed, weekly_embed)
    await interaction.response.send_message(embed=all_time_embed, view=view)
    view.message = await interaction.original_response()


@xp_group.command(name="weekly", description=t("level.xp.weekly.description"))
@app_commands.describe(channel=t("level.xp.weekly.param_channel"))
async def weekly_channel(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    if not await require_admin(interaction):
        return
    await interaction.client.weekly.set_channel(channel.id)
    await interaction.response.send_message(
        t("level.xp.weekly.success", channel=channel.mention)
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
