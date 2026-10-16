"""Reading streak commands (``/streaks ...``).

Members register a daily goal, log their reading, and keep a streak alive.
Admins can point the feature at a channel and backfill missed days.
"""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime, time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from ..notifications import announce_level_up
from ..services import streaks as streak_service
from ..services.level_rewards import sync_member_roles
from ..services.members import resolve_members
from ..services.streaks import (
    LEVEL_THRESHOLD,
    Streak,
    StreakGoal,
    StreakLog,
    search_timezones,
    valid_timezone,
)
from ..texts import t
from .checks import require_admin
from .listing import new_embed, set_author, set_transparent_image

RESCUE_CUSTOM_ID = "streaks:rescue"
PODIUM = ("🥇", "🥈", "🥉")

streaks_group = app_commands.Group(
    name="streaks",
    description=t("streaks.group_description"),
)

channel_group = app_commands.Group(
    name="channel",
    description=t("streaks.channel.group_description"),
    parent=streaks_group,
)

admin_group = app_commands.Group(
    name="admin",
    description=t("streaks.admin.group_description"),
    parent=streaks_group,
)


def _service(interaction: discord.Interaction) -> streak_service.StreakService:
    return interaction.client.streaks


def _icons() -> dict[str, str]:
    return {key: t(f"streaks.icons.{key}") for key in (
        "upcoming",
        "success",
        "success_today",
        "partial",
        "zero",
        "rescue",
        "golden",
    )}


def _format_hour(hour: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:00 {suffix}"


def _reminder_text(goal: StreakGoal) -> str:
    hour = _format_hour(goal.custom_hour) if goal.custom_hour else ""
    return t(f"streaks.shared.reminder.{goal.reminder}", hour=hour)


def _day_timestamp(day: _date, tz_name: str) -> int:
    return int(datetime.combine(day, time(0, 0), ZoneInfo(tz_name)).timestamp())


def _slots(
    streak: Streak, logs: list[StreakLog], goal: StreakGoal, *, highlight: bool
) -> str:
    today = streak_service.local_date(streak_service.utcnow(), goal.timezone)
    return "".join(
        streak_service.window_slots(
            logs,
            days_logged=streak.days_logged,
            highlight_today=highlight,
            today=today,
            icons=_icons(),
        )
    )


def _window_successes(streak: Streak, logs: list[StreakLog]) -> int:
    remainder = streak.days_logged % LEVEL_THRESHOLD
    window = logs[-remainder:] if remainder else []
    return sum(1 for entry in window if entry.outcome == "success")


def _add_goal_fields(embed: discord.Embed, goal: StreakGoal) -> None:
    embed.add_field(
        name=t("streaks.shared.goal_field"),
        value=t("streaks.shared.per_day", amount=goal.amount, unit=goal.unit),
        inline=True,
    )
    embed.add_field(
        name=t("streaks.shared.timezone_field"),
        value=goal.timezone,
        inline=True,
    )
    embed.add_field(
        name=t("streaks.shared.notification_field"),
        value=_reminder_text(goal),
        inline=True,
    )


async def _channel_guard(interaction: discord.Interaction) -> bool:
    channel_id = _service(interaction).channel_id
    if channel_id is None or interaction.channel_id == channel_id:
        return True
    await interaction.response.send_message(
        t("streaks.errors.wrong_channel", channel=f"<#{channel_id}>"),
        ephemeral=True,
    )
    return False


async def _handle_progress(
    interaction: discord.Interaction,
    progress: streak_service.LevelProgress,
    member: discord.Member | discord.User,
) -> None:
    """Apply XP reward roles and celebrate an anniversary, if any."""
    if not progress.leveled:
        return
    if (
        progress.xp_new_level is not None
        and progress.xp_new_level != progress.xp_old_level
        and interaction.guild is not None
    ):
        await sync_member_roles(
            interaction.guild,
            member,
            progress.xp_new_level,
            interaction.client.level_rewards,
        )
        await announce_level_up(
            interaction.channel, member, progress.xp_new_level
        )
    if progress.anniversary:
        embed = new_embed(interaction.client)
        embed.title = t("streaks.anniversary.title")
        embed.description = t(
            "streaks.anniversary.content",
            user_id=member.id,
            days=progress.level * LEVEL_THRESHOLD,
        )
        embed.set_footer(text=t("streaks.anniversary.footer"))
        set_transparent_image(embed, interaction.client)
        await interaction.followup.send(embed=embed)


# ------------------------------------------------------------------ embeds


def _register_step_embed(
    client: discord.Client, amount: int, unit: str, timezone: str
) -> discord.Embed:
    embed = new_embed(client)
    embed.title = t("streaks.register.step_title")
    embed.description = t(
        "streaks.register.step_description",
        amount=amount,
        unit=unit,
        timezone=timezone,
    )
    set_transparent_image(embed, client)
    return embed


def _register_confirmed_embed(
    client: discord.Client, goal: StreakGoal
) -> discord.Embed:
    embed = new_embed(client)
    embed.title = t("streaks.register.confirmed_title")
    embed.description = t("streaks.register.confirmed_description")
    _add_goal_fields(embed, goal)
    set_transparent_image(embed, client)
    return embed


def _hour_embed(client: discord.Client) -> discord.Embed:
    embed = new_embed(client)
    embed.title = t("streaks.register.custom_title")
    embed.description = t("streaks.register.custom_description")
    set_transparent_image(embed, client)
    return embed


def _log_embed(
    client: discord.Client,
    member: discord.abc.User,
    result: streak_service.LogResult,
) -> discord.Embed:
    goal = result.goal
    log = result.log
    embed = new_embed(client)
    set_author(embed, member, member.display_name)
    embed.title = t("streaks.log.title")
    embed.description = _slots(result.streak, result.logs, goal, highlight=True)
    embed.add_field(
        name=t("streaks.shared.date_field"),
        value=f"<t:{_day_timestamp(log.log_date, goal.timezone)}:D>",
        inline=True,
    )
    embed.add_field(
        name=t("streaks.log.amount_field"),
        value=t("streaks.log.amount_value", amount=log.amount, unit=goal.unit),
        inline=True,
    )
    embed.add_field(
        name=t("streaks.log.goal_field"),
        value=t("streaks.shared.per_day", amount=goal.amount, unit=goal.unit),
        inline=True,
    )
    if log.outcome == "success":
        percent = t("streaks.log.percent_success", percent=log.percent)
    elif log.outcome == "zero":
        percent = t("streaks.log.percent_zero")
    else:
        percent = t("streaks.log.percent_failed", percent=log.percent)
    embed.add_field(
        name=t("streaks.log.percent_field"), value=percent, inline=False
    )
    embed.add_field(
        name=t("streaks.shared.level_field"),
        value=str(result.streak.level),
        inline=True,
    )
    if result.progress.leveled:
        content = t(
            "streaks.log.level_up_content",
            level=result.progress.level,
            xp=result.progress.xp_awarded,
        )
        if result.progress.anniversary:
            content += t("streaks.log.level_up_anniversary")
        embed.add_field(
            name=t("streaks.log.level_up_title"), value=content, inline=False
        )
    set_transparent_image(embed, client)
    return embed


def _view_embed(
    client: discord.Client,
    member: discord.abc.User,
    goal: StreakGoal,
    streak: Streak | None,
    logs: list[StreakLog],
) -> discord.Embed:
    embed = new_embed(client)
    set_author(embed, member, member.display_name)
    embed.title = t(
        "streaks.view.title",
        user=discord.utils.escape_markdown(member.display_name),
    )
    if streak is None:
        embed.description = t("streaks.errors.not_registered")
        _add_goal_fields(embed, goal)
        set_transparent_image(embed, client)
        return embed

    tz = goal.timezone
    embed.description = _slots(streak, logs, goal, highlight=False)
    _add_goal_fields(embed, goal)
    embed.add_field(
        name=t("streaks.info.started_field"),
        value=t(
            "streaks.info.started_value",
            ts=_day_timestamp(streak.start_date, tz),
            days=streak.days_logged,
        ),
        inline=True,
    )
    embed.add_field(
        name=t("streaks.shared.level_field"),
        value=str(streak.level),
        inline=True,
    )
    today = streak_service.local_date(streak_service.utcnow(), tz)
    embed.add_field(
        name=t("streaks.info.logged_field"),
        value=t(
            "streaks.info.logged_yes"
            if streak.last_log_date == today
            else "streaks.info.logged_no"
        ),
        inline=True,
    )
    embed.add_field(
        name=t("streaks.info.rescues_field"),
        value=t(
            "streaks.info.rescues_value",
            normal=streak.rescues_available,
            golden=streak.golden_available,
            normal_used=streak.rescues_used,
            golden_used=streak.golden_used,
        ),
        inline=False,
    )
    if streak.status == streak_service.STATUS_ONGOING and streak.last_log_date:
        closes = streak_service.streak_deadline(streak.last_log_date, tz)
        embed.add_field(
            name=t("streaks.info.closes_field"),
            value=t("streaks.info.closes_value", ts=int(closes.timestamp())),
            inline=False,
        )
    elif streak.end_date is not None:
        embed.add_field(
            name=t("streaks.info.closed_field"),
            value=t("streaks.info.closed_value", ts=_day_timestamp(streak.end_date, tz)),
            inline=True,
        )
    if streak.last_xp_at is not None:
        embed.add_field(
            name=t("streaks.info.coupon_field"),
            value=t(
                "streaks.info.coupon_value",
                xp=streak.last_xp_amount,
                ts=int(streak.last_xp_at.replace(tzinfo=ZoneInfo("UTC")).timestamp()),
            ),
            inline=True,
        )
    else:
        embed.add_field(
            name=t("streaks.info.coupon_field"),
            value=t("streaks.info.coupon_none"),
            inline=True,
        )
    embed.add_field(
        name=t("streaks.info.total_xp_field"),
        value=t("streaks.info.total_xp_value", xp=streak.total_xp_awarded),
        inline=True,
    )
    if streak.status == streak_service.STATUS_ONGOING:
        embed.add_field(
            name=t("streaks.info.on_track_field"),
            value=t(
                "streaks.info.on_track_value",
                xp=streak_service.on_track_xp(
                    streak.level, _window_successes(streak, logs)
                ),
            ),
            inline=True,
        )
    set_transparent_image(embed, client)
    return embed


def _leaderboard_embed(
    client: discord.Client,
    rows: list[tuple[Streak, StreakGoal]],
    members: list[discord.Member | None],
    *,
    ongoing: bool,
) -> discord.Embed:
    embed = new_embed(client)
    embed.title = t(
        "streaks.leaderboard.ongoing_title"
        if ongoing
        else "streaks.leaderboard.all_time_title"
    )
    lines = [
        t(
            "streaks.leaderboard.ongoing_desc"
            if ongoing
            else "streaks.leaderboard.all_time_desc"
        ),
        "",
    ]
    for index, ((streak, _goal), member) in enumerate(zip(rows, members)):
        place = PODIUM[index] if index < 3 else f"{index + 1}."
        mention = member.mention if member else f"<@{streak.user_id}>"
        latest = streak.last_log_date or streak.start_date
        lines.append(
            t("streaks.leaderboard.entry", place=place, mention=mention)
        )
        lines.append(
            "\n-# "
            + t(
                "streaks.leaderboard.days_details",
                days=streak.days_logged,
                level=streak.level,
                date=f"<t:{int(datetime.combine(latest, time(0, 0), ZoneInfo('UTC')).timestamp())}:D>",
            )
        )
    embed.description = "\n".join(lines)
    set_transparent_image(embed, client)
    return embed


# ------------------------------------------------------------------- views


class _HourSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label=_format_hour(hour), value=str(hour))
            for hour in range(
                streak_service.CUSTOM_HOUR_MIN,
                streak_service.CUSTOM_HOUR_MAX + 1,
            )
        ]
        super().__init__(
            placeholder=t("streaks.register.custom_placeholder"),
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.save(interaction, int(self.values[0]))  # type: ignore[attr-defined]


class _HourSelectView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        amount: int,
        unit: str,
        timezone: str,
        reminder: str,
    ) -> None:
        super().__init__(timeout=180)
        self.author_id = author_id
        self.amount = amount
        self.unit = unit
        self.timezone = timezone
        self.reminder = reminder
        self.add_item(_HourSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _author_check(interaction, self.author_id)

    async def save(self, interaction: discord.Interaction, hour: int) -> None:
        goal = await _service(interaction).save_goal(
            user_id=interaction.user.id,
            amount=self.amount,
            unit=self.unit,
            timezone=self.timezone,
            reminder=self.reminder,
            custom_hour=hour,
        )
        await interaction.response.edit_message(
            embed=_register_confirmed_embed(interaction.client, goal), view=None
        )


class _RegisterReminderView(discord.ui.View):
    def __init__(
        self, author_id: int, amount: int, unit: str, timezone: str
    ) -> None:
        super().__init__(timeout=180)
        self.author_id = author_id
        self.amount = amount
        self.unit = unit
        self.timezone = timezone

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _author_check(interaction, self.author_id)

    async def _save(self, interaction: discord.Interaction, reminder: str) -> None:
        goal = await _service(interaction).save_goal(
            user_id=interaction.user.id,
            amount=self.amount,
            unit=self.unit,
            timezone=self.timezone,
            reminder=reminder,
            custom_hour=None,
        )
        await interaction.response.edit_message(
            embed=_register_confirmed_embed(interaction.client, goal), view=None
        )

    @discord.ui.button(emoji="❌", style=discord.ButtonStyle.secondary)
    async def none(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._save(interaction, "none")

    @discord.ui.button(emoji="🕑", style=discord.ButtonStyle.secondary)
    async def custom(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            embed=_hour_embed(interaction.client),
            view=_HourSelectView(
                self.author_id, self.amount, self.unit, self.timezone, "custom"
            ),
        )

    @discord.ui.button(emoji="🌙", style=discord.ButtonStyle.secondary)
    async def last_chance(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._save(interaction, "last_chance")

    @discord.ui.button(emoji="2️⃣", style=discord.ButtonStyle.secondary)
    async def both(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            embed=_hour_embed(interaction.client),
            view=_HourSelectView(
                self.author_id, self.amount, self.unit, self.timezone, "both"
            ),
        )


async def _author_check(interaction: discord.Interaction, author_id: int) -> bool:
    if interaction.user.id == author_id:
        return True
    await interaction.response.send_message(
        t("components.not_author"), ephemeral=True
    )
    return False


class StreakRescueView(discord.ui.View):
    """Persistent "repair streak" button; works after restarts."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label=t("streaks.shared.rescue_button"),
        emoji=t("streaks.shared.rescue_emoji"),
        style=discord.ButtonStyle.secondary,
        custom_id=RESCUE_CUSTOM_ID,
    )
    async def rescue(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        service = _service(interaction)
        found = await service.rescuable_for_user(interaction.user.id)
        if found is None:
            await interaction.response.send_message(
                t("streaks.errors.cant_rescue"), ephemeral=True
            )
            return
        streak, _goal = found
        try:
            result = await service.revive(
                streak_id=streak.id, user_id=interaction.user.id
            )
        except streak_service.StreakError:
            await interaction.response.send_message(
                t("streaks.errors.cant_rescue"), ephemeral=True
            )
            return
        embed = _view_embed(
            interaction.client,
            interaction.user,
            result.goal,
            result.streak,
            result.logs,
        )
        await interaction.response.edit_message(embed=embed, view=None)
        await _handle_progress(interaction, result.progress, interaction.user)


class _LeaderboardView(discord.ui.View):
    def __init__(self, author_id: int, ongoing: bool = True) -> None:
        super().__init__(timeout=120)
        self.author_id = author_id
        self.ongoing = ongoing
        self._refresh()

    def _refresh(self) -> None:
        self.ongoing_button.disabled = self.ongoing
        self.all_time_button.disabled = not self.ongoing

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _author_check(interaction, self.author_id)

    async def _show(self, interaction: discord.Interaction, ongoing: bool) -> None:
        self.ongoing = ongoing
        self._refresh()
        rows = await _service(interaction).leaderboard(ongoing=ongoing)
        members = await resolve_members(
            interaction.guild, [streak.user_id for streak, _ in rows]
        )
        embed = _leaderboard_embed(
            interaction.client, rows, members, ongoing=ongoing
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(
        label=t("streaks.leaderboard.ongoing_button"),
        emoji=t("streaks.leaderboard.ongoing_emoji"),
        style=discord.ButtonStyle.secondary,
    )
    async def ongoing_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._show(interaction, True)

    @discord.ui.button(
        label=t("streaks.leaderboard.all_time_button"),
        emoji=t("streaks.leaderboard.all_time_emoji"),
        style=discord.ButtonStyle.secondary,
    )
    async def all_time_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._show(interaction, False)


# ---------------------------------------------------------------- commands


@streaks_group.command(
    name="register", description=t("streaks.register.description")
)
@app_commands.describe(
    amount=t("streaks.register.param_amount"),
    unit=t("streaks.register.param_unit"),
    timezone=t("streaks.register.param_timezone"),
)
async def register(
    interaction: discord.Interaction, amount: int, unit: str, timezone: str
) -> None:
    if not await _channel_guard(interaction):
        return
    if amount <= 0:
        await interaction.response.send_message(
            t("streaks.errors.amount_positive"), ephemeral=True
        )
        return
    zone = valid_timezone(timezone)
    if zone is None:
        await interaction.response.send_message(
            t("streaks.errors.invalid_timezone", timezone=timezone), ephemeral=True
        )
        return
    view = _RegisterReminderView(
        interaction.user.id, amount, unit.strip(), zone
    )
    await interaction.response.send_message(
        embed=_register_step_embed(interaction.client, amount, unit.strip(), zone),
        view=view,
        ephemeral=True,
    )


@register.autocomplete("timezone")
async def _timezone_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=label, value=value)
        for value, label in search_timezones(current)
    ]


@streaks_group.command(name="log", description=t("streaks.log.description"))
@app_commands.describe(amount=t("streaks.log.param_amount"))
async def log(interaction: discord.Interaction, amount: str) -> None:
    if not await _channel_guard(interaction):
        return
    try:
        result = await _service(interaction).log_daily(
            user_id=interaction.user.id, amount_raw=amount
        )
    except streak_service.StreakError as exc:
        await interaction.response.send_message(
            t(exc.key, **exc.variables), ephemeral=True
        )
        return

    view = StreakRescueView() if result.rescuable else None
    await interaction.response.send_message(
        embed=_log_embed(interaction.client, interaction.user, result),
        view=view,
    )
    await _handle_progress(interaction, result.progress, interaction.user)


@streaks_group.command(name="today", description=t("streaks.today.description"))
async def today(interaction: discord.Interaction) -> None:
    if not await _channel_guard(interaction):
        return
    try:
        logged = await _service(interaction).logged_today(interaction.user.id)
    except streak_service.StreakError:
        await interaction.response.send_message(
            t("streaks.today.not_registered"), ephemeral=True
        )
        return
    await interaction.response.send_message(
        t("streaks.today.yes" if logged else "streaks.today.no"), ephemeral=True
    )


@streaks_group.command(name="view", description=t("streaks.view.description"))
@app_commands.describe(user=t("streaks.view.param_user"))
async def view(
    interaction: discord.Interaction, user: discord.User | None = None
) -> None:
    if not await _channel_guard(interaction):
        return
    target = user or interaction.user
    try:
        goal, streak, logs = await _service(interaction).view(target.id)
    except streak_service.StreakError:
        await interaction.response.send_message(
            t("streaks.errors.not_registered"), ephemeral=True
        )
        return
    resc = (
        streak is not None
        and target.id == interaction.user.id
        and streak_service.is_rescuable(
            streak, goal.timezone, streak_service.utcnow()
        )
    )
    await interaction.response.send_message(
        embed=_view_embed(interaction.client, target, goal, streak, logs),
        view=StreakRescueView() if resc else None,
    )


@streaks_group.command(
    name="leaderboard", description=t("streaks.leaderboard.description")
)
async def leaderboard(interaction: discord.Interaction) -> None:
    if not await _channel_guard(interaction):
        return
    rows = await _service(interaction).leaderboard(ongoing=True)
    if not rows:
        await interaction.response.send_message(
            t("streaks.leaderboard.empty"), ephemeral=True
        )
        return
    members = await resolve_members(
        interaction.guild, [streak.user_id for streak, _ in rows]
    )
    await interaction.response.send_message(
        embed=_leaderboard_embed(
            interaction.client, rows, members, ongoing=True
        ),
        view=_LeaderboardView(interaction.user.id),
    )


@channel_group.command(
    name="set", description=t("streaks.channel.set.description")
)
@app_commands.describe(channel=t("streaks.channel.set.param_channel"))
async def set_channel(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_channel(channel.id)
    await interaction.response.send_message(
        t("streaks.channel.set.success", channel=channel.mention)
    )


@admin_group.command(
    name="log", description=t("streaks.admin.log.description")
)
@app_commands.describe(
    user=t("streaks.admin.log.param_user"),
    amount=t("streaks.admin.log.param_amount"),
    date=t("streaks.admin.log.param_date"),
)
async def admin_log(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: str,
    date: str | None = None,
) -> None:
    if not await require_admin(interaction):
        return
    when: _date | None = None
    if date is not None:
        try:
            when = _date.fromisoformat(date)
        except ValueError:
            await interaction.response.send_message(
                t("streaks.errors.invalid_date", raw=date), ephemeral=True
            )
            return
    try:
        result = await _service(interaction).admin_log(
            user_id=user.id, amount_raw=amount, log_date=when
        )
    except streak_service.StreakError as exc:
        await interaction.response.send_message(
            t(exc.key, **exc.variables), ephemeral=True
        )
        return
    await interaction.response.send_message(
        t(
            "streaks.admin.log.success_reopened"
            if result.reopened
            else "streaks.admin.log.success",
            amount=result.log.amount,
            user=user.mention,
            date=result.log.log_date.isoformat(),
        )
    )
    await _handle_progress(interaction, result.progress, user)
