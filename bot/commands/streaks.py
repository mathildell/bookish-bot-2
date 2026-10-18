"""Reading streak commands (``/streak ...``).

Members register a daily goal, log their reading, and keep a streak alive.
Admins can point the feature at a channel and backfill missed days.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date as _date, datetime, timedelta
from typing import Literal

import discord
from discord import app_commands

from ..notifications import announce_level_up, announce_streak_closed
from ..services import streaks as streak_service
from ..services.booster_role import resolve_notify_channel
from ..services.level_rewards import sync_member_roles
from ..services.members import resolve_members
from ..services.streaks import (
    LEVEL_THRESHOLD,
    RESCUE_GOLDEN,
    STATUS_CLOSED,
    Streak,
    StreakGoal,
    StreakLog,
    display_level,
    search_timezones,
    valid_timezone,
)
from ..texts import t, t_choice
from .checks import require_admin
from .listing import new_embed, set_author, set_transparent_image

RESCUE_CUSTOM_ID = "streaks:rescue"
PODIUM = ("🥇", "🥈", "🥉")

streaks_group = app_commands.Group(
    name="streak",
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


def _format_date(day: _date) -> str:
    return streak_service.format_date(day)


def _format_date_short(day: _date) -> str:
    return day.strftime("%b %d, %Y")


def _format_local_datetime(now: datetime) -> str:
    hour = now.hour % 12 or 12
    suffix = "AM" if now.hour < 12 else "PM"
    return f"{now.strftime('%b %d, %Y')} {hour}:{now.minute:02d}{suffix}"


def _format_local_now(timezone: str) -> str:
    return _format_local_datetime(
        streak_service.local_now(streak_service.utcnow(), timezone)
    )


def _dry_marker(client: discord.Client) -> str:
    service = getattr(client, "streaks", None)
    if service is not None and service.dry_run:
        return t("streaks.info.dry_icon") + " ‎ "
    return ""


def _slots(
    streak: Streak, logs: list[StreakLog], goal: StreakGoal, *, highlight: bool
) -> str:
    today = streak_service.local_date(streak_service.utcnow(), goal.timezone)
    slots = streak_service.window_slots(
        logs,
        days_logged=streak.days_logged,
        highlight_today=highlight,
        today=today,
        icons=_icons(),
    )
    return "# " + " \u200e ".join(slots)


def _window_successes(streak: Streak, logs: list[StreakLog]) -> int:
    remainder = streak.days_logged % LEVEL_THRESHOLD
    window = logs[-remainder:] if remainder else []
    return sum(1 for entry in window if entry.outcome == "success")


def _add_goal_fields(embed: discord.Embed, goal: StreakGoal) -> None:
    embed.add_field(
        name=t("streaks.shared.timezone_field"),
        value=t(
            "streaks.shared.timezone_value",
            timezone=goal.timezone,
            current=_format_local_now(goal.timezone),
        ),
        inline=True,
    )
    embed.add_field(
        name=t("streaks.shared.notification_field"),
        value=_reminder_text(goal),
        inline=True,
    )
    embed.add_field(
        name=t("streaks.shared.goal_field"),
        value=t("streaks.shared.per_day", amount=goal.amount, unit=goal.unit),
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
    if progress.anniversary:
        embed = new_embed(interaction.client)
        embed.title = t("streaks.anniversary.title")
        embed.description = t(
            "streaks.anniversary.content",
            user_id=member.id,
            days=progress.level * LEVEL_THRESHOLD,
        )
        embed.set_footer(text=t("streaks.anniversary.footer"))
        embed.set_image(url=t_choice("streaks.anniversary.images"))
        await interaction.followup.send(embed=embed)
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
        options=t("streaks.shared.reminder_options"),
    )
    set_transparent_image(embed, client)
    return embed


def _edit_menu_embed(client: discord.Client) -> discord.Embed:
    embed = new_embed(client)
    embed.title = t("streaks.edit.title")
    embed.description = t("streaks.edit.menu_description")
    set_transparent_image(embed, client)
    return embed


def _edit_confirmed_embed(
    client: discord.Client, goal: StreakGoal
) -> discord.Embed:
    embed = new_embed(client)
    embed.title = t("streaks.edit.confirmed_title")
    embed.description = t("streaks.edit.confirmed_description")
    _add_goal_fields(embed, goal)
    set_transparent_image(embed, client)
    return embed


def _reminders_prompt_embed(client: discord.Client) -> discord.Embed:
    embed = new_embed(client)
    embed.title = t("streaks.edit.reminders_title")
    embed.description = t(
        "streaks.edit.reminders_description",
        options=t("streaks.shared.reminder_options"),
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
        value=_format_date(log.log_date),
        inline=True,
    )
    embed.add_field(
        name=t("streaks.log.amount_field"),
        value=t("streaks.log.amount_value", amount=log.amount, unit=goal.unit),
        inline=True,
    )
    embed.add_field(
        name=t("streaks.shared.level_field"),
        value=t("streaks.shared.level_value", level=str(display_level(result.streak.days_logged))),
        inline=True,
    )

    if log.outcome == streak_service.OUTCOME_SUCCESS:
        achievement_value = t(
            "streaks.log.achievement_success_value",
            percent=log.percent,
            amount=goal.amount,
            unit=goal.unit
        )
    else:
        achievement_value = t("streaks.log.achievement_failure_value")
    
    embed.add_field(
        name=t("streaks.log.achievement_field"),
        value=achievement_value,
        inline=False,
    )

    if result.progress.leveled:
        content = t(
            "streaks.log.level_up_content",
            days=result.streak.days_logged,
            level=result.progress.level,
            rescue_emoji=t("streaks.icons.rescue"),
            xp=result.progress.xp_awarded,
        )

        if result.progress.anniversary:
            content += t("streaks.log.level_up_anniversary")
        elif result.progress.golden_granted > 0:
            content += t("streaks.log.level_up_50th", rescue_emoji=t("streaks.icons.golden"))

        embed.add_field(
            name=t("streaks.log.level_up_title"), value=content, inline=False
        )
    if result.progress.top_up and result.progress.xp_awarded > 0:
        embed.add_field(
            name=t("streaks.log.top_up_title"),
            value=t("streaks.log.top_up_content", xp=result.progress.xp_awarded),
            inline=False,
        )
    if result.progress.dry_run and result.progress.xp_awarded > 0:
        embed.add_field(
            name=t("streaks.info.dry_run_field"),
            value=t("streaks.log.dry_run", xp=result.progress.xp_awarded),
            inline=False,
        )
    if result.progress.anniversary:
        embed.set_image(url=t_choice("streaks.anniversary.images"))
    else:
        set_transparent_image(embed, client)
    return embed


def _view_embed(
    client: discord.Client,
    member: discord.abc.User,
    goal: StreakGoal,
    streak: Streak | None,
    logs: list[StreakLog],
    *,
    rescuable: bool = False,
    rescued: streak_service.ReviveResult | None = None,
) -> discord.Embed:
    embed = new_embed(client)
    set_author(embed, member, member.display_name)
    embed.title = t(
        "streaks.view.title",
        user=discord.utils.escape_markdown(member.display_name),
    )
    if streak is None:
        embed.description = t("streaks.view.no_streak")
        _add_goal_fields(embed, goal)
        set_transparent_image(embed, client)
        return embed

    if streak.status == STATUS_CLOSED:
        if not rescuable:
            # The streak is beyond saving: show the goal but hide the streak.
            embed.description = t("streaks.view.streak_gone")
            _add_goal_fields(embed, goal)
            set_transparent_image(embed, client)
            return embed
        # A rescuable closed streak: no progress, just the facts.
        _add_goal_fields(embed, goal)
        embed.add_field(
            name=t("streaks.info.started_field"),
            value=t(
                "streaks.info.started_value",
                date=_format_date_short(streak.start_date),
                days=streak.days_logged,
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
                rescue_emoji=t("streaks.icons.rescue"),
                golden_emoji=t("streaks.icons.golden"),
            ),
            inline=True,
        )
        if streak.end_date is not None:
            embed.add_field(
                name=t("streaks.info.closed_field"),
                value=t(
                    "streaks.info.closed_rescuable_value",
                    date=_format_date(streak.end_date),
                ),
                inline=True,
            )
        last_log = streak.last_log_date or streak.start_date
        embed.add_field(
            name=t("streaks.info.logged_field"),
            value=_format_date_short(last_log),
            inline=True,
        )
        embed.add_field(
            name=t("streaks.shared.level_field"),
            value=t(
                "streaks.shared.level_value",
                level=str(display_level(streak.days_logged)),
            ),
            inline=True,
        )
        set_transparent_image(embed, client)
        return embed

    tz = goal.timezone
    embed.description = _slots(streak, logs, goal, highlight=False)
    _add_goal_fields(embed, goal)
    embed.add_field(
        name=t("streaks.info.started_field"),
        value=t(
            "streaks.info.started_value",
            date=_format_date_short(streak.start_date),
            days=streak.days_logged,
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
            rescue_emoji=t("streaks.icons.rescue"),
            golden_emoji=t("streaks.icons.golden"),
        ),
        inline=True,
    )
    if streak.last_log_date:
        closes = streak_service.streak_deadline(streak.last_log_date, tz)
        embed.add_field(
            name=t("streaks.info.closes_field"),
            value=t("streaks.info.closes_value", ts=int(closes.timestamp())),
            inline=True,
        )
    today = streak_service.local_date(streak_service.utcnow(), tz)
    last_log = streak.last_log_date or streak.start_date
    embed.add_field(
        name=t("streaks.info.logged_field"),
        value=t(
            "streaks.info.logged_yes"
            if streak.last_log_date == today
            else "streaks.info.logged_no",
            date=_format_date_short(last_log),
        ),
        inline=True,
    )
    yesterday_log = next(
        (entry for entry in logs if entry.log_date == today - timedelta(days=1)),
        None,
    )
    if yesterday_log is not None:
        value = t(
            "streaks.info.yesterday_value",
            amount=yesterday_log.amount,
            unit=goal.unit,
        )
        if yesterday_log.outcome == streak_service.OUTCOME_SUCCESS:
            value += "\n" + t(
                "streaks.info.yesterday_percent", percent=yesterday_log.percent
            )
        embed.add_field(
            name=t("streaks.info.yesterday_field"),
            value=value,
            inline=True,
        )

    embed.add_field(
        name=t("streaks.shared.level_field"),
        value=t("streaks.shared.level_value", level=str(display_level(streak.days_logged))),
        inline=True,
    )
    if streak.last_xp_at is not None:
        embed.add_field(
            name=t("streaks.info.coupon_field"),
            value=t(
                "streaks.info.coupon_value",
                date=_format_date(streak_service.local_date(streak.last_xp_at, tz)),
            ),
            inline=True,
        )
    else:
        embed.add_field(
            name=t("streaks.info.coupon_field"),
            value=t("streaks.info.coupon_none"),
            inline=True,
        )
    marker = _dry_marker(client)
    embed.add_field(
        name=t("streaks.info.total_xp_field"),
        value=t(
            "streaks.info.total_xp_value",
            xp=f"{marker}{streak.last_xp_amount}",
        ),
        inline=True,
    )
    if streak.status == streak_service.STATUS_ONGOING:
        projected = streak_service.on_track_xp(
            streak.level, streak.days_logged, _window_successes(streak, logs)
        )
        embed.add_field(
            name=t("streaks.info.on_track_field"),
            value=t("streaks.info.on_track_value", xp=f"{marker}{projected}"),
            inline=True,
        )
    if rescued is not None and rescued.progress.leveled:
        emoji = (
            t("streaks.icons.golden")
            if rescued.rescue_type == RESCUE_GOLDEN
            else t("streaks.icons.rescue")
        )
        key = (
            "streaks.rescue.content_one"
            if rescued.patched_days == 1
            else "streaks.rescue.content"
        )
        embed.add_field(
            name=t("streaks.rescue.title"),
            value=t(
                key,
                emoji=emoji,
                days=rescued.patched_days,
                level=rescued.progress.level,
                xp=rescued.progress.xp_awarded,
            ),
            inline=False,
        )
        if rescued.progress.dry_run and rescued.progress.xp_awarded > 0:
            embed.add_field(
                name=t("streaks.info.dry_run_field"),
                value=t("streaks.log.dry_run", xp=rescued.progress.xp_awarded),
                inline=False,
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
                level=display_level(streak.days_logged),
                date=_format_date(latest),
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


async def _author_check(interaction: discord.Interaction, author_id: int) -> bool:
    if interaction.user.id == author_id:
        return True
    await interaction.response.send_message(
        t("components.not_author"), ephemeral=True
    )
    return False


ReminderSaver = Callable[
    [discord.Interaction, str, int | None], Awaitable[StreakGoal]
]
ConfirmBuilder = Callable[[discord.Client, StreakGoal], discord.Embed]


class _HourSelectView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        reminder: str,
        save_reminder: ReminderSaver,
        confirm_builder: ConfirmBuilder,
    ) -> None:
        super().__init__(timeout=180)
        self.author_id = author_id
        self.reminder = reminder
        self.save_reminder = save_reminder
        self.confirm_builder = confirm_builder
        self.add_item(_HourSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _author_check(interaction, self.author_id)

    async def save(self, interaction: discord.Interaction, hour: int) -> None:
        goal = await self.save_reminder(interaction, self.reminder, hour)
        await interaction.response.edit_message(
            embed=self.confirm_builder(interaction.client, goal), view=None
        )


class _ReminderChoiceView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        save_reminder: ReminderSaver,
        confirm_builder: ConfirmBuilder,
    ) -> None:
        super().__init__(timeout=180)
        self.author_id = author_id
        self.save_reminder = save_reminder
        self.confirm_builder = confirm_builder

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _author_check(interaction, self.author_id)

    async def _save(self, interaction: discord.Interaction, reminder: str) -> None:
        goal = await self.save_reminder(interaction, reminder, None)
        await interaction.response.edit_message(
            embed=self.confirm_builder(interaction.client, goal), view=None
        )

    def _hour_view(self, reminder: str) -> _HourSelectView:
        return _HourSelectView(
            self.author_id, reminder, self.save_reminder, self.confirm_builder
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
            embed=_hour_embed(interaction.client), view=self._hour_view("custom")
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
            embed=_hour_embed(interaction.client), view=self._hour_view("both")
        )


async def _edit_save_reminder(
    interaction: discord.Interaction, reminder: str, custom_hour: int | None
) -> StreakGoal:
    service = _service(interaction)
    goal = await service.get_goal(interaction.user.id)
    if goal is None:
        raise streak_service.NotRegistered()
    return await service.save_goal(
        user_id=interaction.user.id,
        amount=goal.amount,
        unit=goal.unit,
        timezone=goal.timezone,
        reminder=reminder,
        custom_hour=custom_hour,
    )


class _AmountUnitModal(discord.ui.Modal):
    def __init__(self, goal: StreakGoal) -> None:
        super().__init__(title=t("streaks.edit.amount_modal_title"))
        self.amount = discord.ui.TextInput(
            label=t("streaks.edit.amount_param"), default=str(goal.amount)
        )
        self.unit = discord.ui.TextInput(
            label=t("streaks.edit.unit_param"), default=goal.unit
        )
        self.add_item(self.amount)
        self.add_item(self.unit)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.amount.value).strip()
        try:
            amount = int(raw)
        except ValueError:
            await interaction.response.send_message(
                t("streaks.errors.invalid_amount", raw=raw), ephemeral=True
            )
            return
        if amount <= 0:
            await interaction.response.send_message(
                t("streaks.errors.amount_positive"), ephemeral=True
            )
            return
        unit = str(self.unit.value).strip()
        if not unit:
            await interaction.response.send_message(
                t("streaks.errors.invalid_amount", raw=unit), ephemeral=True
            )
            return
        service = _service(interaction)
        goal = await service.get_goal(interaction.user.id)
        if goal is None:
            await interaction.response.send_message(
                t("streaks.errors.not_registered"), ephemeral=True
            )
            return
        goal = await service.save_goal(
            user_id=interaction.user.id,
            amount=amount,
            unit=unit,
            timezone=goal.timezone,
            reminder=goal.reminder,
            custom_hour=goal.custom_hour,
        )
        await interaction.response.edit_message(
            embed=_edit_confirmed_embed(interaction.client, goal), view=None
        )


class _TimezoneModal(discord.ui.Modal):
    def __init__(self, goal: StreakGoal) -> None:
        super().__init__(title=t("streaks.edit.timezone_modal_title"))
        self.timezone = discord.ui.TextInput(
            label=t("streaks.edit.timezone_param"), default=goal.timezone
        )
        self.add_item(self.timezone)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        zone = valid_timezone(str(self.timezone.value))
        if zone is None:
            await interaction.response.send_message(
                t(
                    "streaks.errors.invalid_timezone",
                    timezone=str(self.timezone.value),
                ),
                ephemeral=True,
            )
            return
        service = _service(interaction)
        goal = await service.get_goal(interaction.user.id)
        if goal is None:
            await interaction.response.send_message(
                t("streaks.errors.not_registered"), ephemeral=True
            )
            return
        goal = await service.save_goal(
            user_id=interaction.user.id,
            amount=goal.amount,
            unit=goal.unit,
            timezone=zone,
            reminder=goal.reminder,
            custom_hour=goal.custom_hour,
        )
        await interaction.response.edit_message(
            embed=_edit_confirmed_embed(interaction.client, goal), view=None
        )


class StreakEditView(discord.ui.View):
    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=180)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _author_check(interaction, self.author_id)

    async def _goal(self, interaction: discord.Interaction) -> StreakGoal | None:
        goal = await _service(interaction).get_goal(interaction.user.id)
        if goal is None:
            await interaction.response.send_message(
                t("streaks.errors.not_registered"), ephemeral=True
            )
        return goal

    @discord.ui.button(
        label=t("streaks.edit.amount_unit_button"),
        style=discord.ButtonStyle.primary,
    )
    async def amount_unit(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        goal = await self._goal(interaction)
        if goal is None:
            return
        await interaction.response.send_modal(_AmountUnitModal(goal))

    @discord.ui.button(
        label=t("streaks.edit.timezone_button"),
        style=discord.ButtonStyle.secondary,
    )
    async def timezone(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        goal = await self._goal(interaction)
        if goal is None:
            return
        await interaction.response.send_modal(_TimezoneModal(goal))

    @discord.ui.button(
        label=t("streaks.edit.reminders_button"),
        style=discord.ButtonStyle.secondary,
    )
    async def reminders(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            embed=_reminders_prompt_embed(interaction.client),
            view=_ReminderChoiceView(
                self.author_id, _edit_save_reminder, _edit_confirmed_embed
            ),
        )


async def _do_rescue(interaction: discord.Interaction) -> None:
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
        rescuable=False,
        rescued=result,
    )
    view = _ViewActions(rescue=False, edit=True)
    view.message = interaction.message
    await interaction.response.edit_message(embed=embed, view=view)
    await _handle_progress(interaction, result.progress, interaction.user)


class _RescueButton(discord.ui.Button):
    def __init__(self, kind: str = streak_service.RESCUE_NORMAL) -> None:
        icon = (
            t("streaks.icons.golden")
            if kind == RESCUE_GOLDEN
            else t("streaks.icons.rescue")
        )
        super().__init__(
            label=t("streaks.shared.rescue_button"),
            emoji=icon,
            style=discord.ButtonStyle.secondary,
            custom_id=RESCUE_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await _do_rescue(interaction)


class StreakRescueView(discord.ui.View):
    """Persistent "repair streak" button; works after restarts."""

    def __init__(self, kind: str = streak_service.RESCUE_NORMAL) -> None:
        super().__init__(timeout=None)
        self.add_item(_RescueButton(kind))


class _EditButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label=t("streaks.edit.button_on_view"),
            emoji="✏️",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=_edit_menu_embed(interaction.client),
            view=StreakEditView(interaction.user.id),
            ephemeral=True,
        )


class _ExpiringView(discord.ui.View):
    """A timed view whose buttons grey out once it can no longer respond."""

    message: discord.Message | None = None

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class _ViewActions(_ExpiringView):
    def __init__(
        self,
        *,
        rescue: bool,
        edit: bool,
        rescue_kind: str = streak_service.RESCUE_NORMAL,
    ) -> None:
        super().__init__(timeout=300)
        if rescue:
            self.add_item(_RescueButton(rescue_kind))
        if edit:
            self.add_item(_EditButton())


class _LeaderboardView(_ExpiringView):
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
    if await _service(interaction).get_goal(interaction.user.id) is not None:
        await interaction.response.send_message(
            t("streaks.errors.already_registered"), ephemeral=True
        )
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
    clean_unit = unit.strip()

    async def save_reminder(
        inner: discord.Interaction, reminder: str, custom_hour: int | None
    ) -> StreakGoal:
        return await _service(inner).save_goal(
            user_id=inner.user.id,
            amount=amount,
            unit=clean_unit,
            timezone=zone,
            reminder=reminder,
            custom_hour=custom_hour,
        )

    view = _ReminderChoiceView(
        interaction.user.id, save_reminder, _register_confirmed_embed
    )
    await interaction.response.send_message(
        embed=_register_step_embed(interaction.client, amount, clean_unit, zone),
        view=view,
        ephemeral=False,
    )


@register.autocomplete("timezone")
async def _timezone_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=label, value=value)
        for value, label in search_timezones(current)
    ]


@streaks_group.command(name="edit", description=t("streaks.edit.description"))
async def edit(interaction: discord.Interaction) -> None:
    if not await _channel_guard(interaction):
        return
    if await _service(interaction).get_goal(interaction.user.id) is None:
        await interaction.response.send_message(
            t("streaks.errors.not_registered"), ephemeral=True
        )
        return
    await interaction.response.send_message(
        embed=_edit_menu_embed(interaction.client),
        view=StreakEditView(interaction.user.id),
        ephemeral=True,
    )


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

    view = None
    if result.rescuable is not None:
        kind = streak_service.rescue_kind_for_closed(
            result.rescuable.end_date,
            result.goal.timezone,
            streak_service.utcnow(),
        )
        view = StreakRescueView(kind or streak_service.RESCUE_NORMAL)
    await interaction.response.send_message(
        embed=_log_embed(interaction.client, interaction.user, result),
        view=view,
    )
    await _handle_progress(interaction, result.progress, interaction.user)
    if result.closed is not None:
        channel = await resolve_notify_channel(
            interaction.guild,
            getattr(interaction.client, "bot_updates_channel_id", 0),
        )
        await announce_streak_closed(
            channel,
            closed=[result.closed],
            colour=getattr(interaction.client, "default_embed_color", None),
        )


@streaks_group.command(name="today", description=t("streaks.today.description"))
async def today(interaction: discord.Interaction) -> None:
    if not await _channel_guard(interaction):
        return
    try:
        status = await _service(interaction).today_status(interaction.user.id)
    except streak_service.StreakError:
        await interaction.response.send_message(
            t("streaks.today.not_registered"), ephemeral=True
        )
        return

    goal = status.goal
    if status.logged:
        message = t(
            "streaks.today.yes",
            amount=status.amount,
            unit=goal.unit,
            date=_format_date(status.last_date),
        )
    # elif status.ever_logged and status.last_date is not None:
    #     message = t(
    #         "streaks.today.no",
    #         date=_format_date(status.last_date),
    #     )
    else:
        message = t("streaks.today.no_first")

    after_five = streak_service.local_hour(
        streak_service.utcnow(), goal.timezone
    ) >= 17
    if not status.logged and after_five and status.closes_at is not None:
        if goal.amount > 1:
            this=t("streaks.today.these")
        else:
            this=t("streaks.today.this")

        message += "\n" + t(
            "streaks.today.hurry",
            this=this,
            amount=goal.amount,
            unit=goal.unit,
            closes=f"<t:{int(status.closes_at.timestamp())}:R>",
        )

    await interaction.response.send_message(message, ephemeral=True)


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
    is_self = target.id == interaction.user.id
    now = streak_service.utcnow()
    can_rescue = streak is not None and streak_service.is_rescuable(
        streak, goal.timezone, now
    )
    kind = (
        streak_service.rescue_kind_for_closed(streak.end_date, goal.timezone, now)
        if can_rescue
        else None
    )
    show_rescue = can_rescue and is_self
    actions = (
        _ViewActions(
            rescue=show_rescue,
            edit=is_self,
            rescue_kind=kind or streak_service.RESCUE_NORMAL,
        )
        if (show_rescue or is_self)
        else None
    )
    await interaction.response.send_message(
        embed=_view_embed(
            interaction.client, target, goal, streak, logs, rescuable=can_rescue
        ),
        view=actions,
    )
    if actions is not None:
        actions.message = await interaction.original_response()


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
    view = _LeaderboardView(interaction.user.id)
    await interaction.response.send_message(
        embed=_leaderboard_embed(
            interaction.client, rows, members, ongoing=True
        ),
        view=view,
    )
    view.message = await interaction.original_response()


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
    name="dryrun", description=t("streaks.admin.dryrun.description")
)
@app_commands.describe(state=t("streaks.admin.dryrun.param_state"))
async def dryrun(
    interaction: discord.Interaction, state: Literal["on", "off"]
) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_dry_run(state == "on")
    await interaction.response.send_message(
        t(f"streaks.admin.dryrun.{state}")
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
