"""Reminder commands (``/reminder ...``).

Members create one-off or recurring reminders in a channel; when one fires the
owner is pinged and can complete it or snooze it for an hour.
"""

from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands

from ..services import reminders as reminder_service
from ..services.members import resolve_members
from ..services.reminders import (
    KIND_DAILY,
    KIND_INTERVAL,
    KIND_MONTHLY,
    KIND_ONCE,
    KIND_WEEKLY,
    KINDS,
    SNOOZE_DELAY,
    WEEKDAY_NAMES,
    Reminder,
    ReminderService,
    utcnow,
)
from ..services.timezones import search_timezones
from ..texts import t
from .checks import is_admin, require_admin
from .listing import (
    EMBEDS_PER_PAGE,
    chunked,
    new_embed,
    send_paginated,
    set_author,
    set_transparent_image,
)
from .views import ExpiringView

BUTTON_LIFETIME_SECONDS = 2 * 60 * 60

reminders_group = app_commands.Group(
    name="reminder",
    description=t("reminders.group_description"),
)


def _service(interaction: discord.Interaction) -> ReminderService:
    return interaction.client.reminders


def _hour_value(hour: int) -> str:
    display = hour % 12 or 12
    key = (
        "reminders.shared.hour_value_am"
        if hour < 12
        else "reminders.shared.hour_value_pm"
    )
    return t(key, hour=display)


def _ordinal(day: int) -> str:
    remainder = day % 100
    if 11 <= remainder <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return t(f"reminders.shared.suffix.{suffix}")


def _weekday_name(weekday: int) -> str:
    return t(f"reminders.shared.weekday.{WEEKDAY_NAMES[weekday]}")


def _format_run(moment: datetime) -> str:
    return discord.utils.format_dt(moment.replace(tzinfo=timezone.utc), "f")


def schedule_summary(reminder: Reminder) -> str:
    """Human-readable schedule, used in confirmations and embed footers."""
    hour = _hour_value(reminder.hour)
    if reminder.kind == KIND_ONCE:
        return t(
            "reminders.schedule.once", when=_format_run(reminder.next_run_at)
        )
    if reminder.kind == KIND_DAILY:
        return t("reminders.schedule.daily", hour=hour)
    if reminder.kind == KIND_WEEKLY:
        return t(
            "reminders.schedule.weekly",
            weekday=_weekday_name(reminder.weekday),
            hour=hour,
        )
    if reminder.kind == KIND_MONTHLY:
        if reminder.month_end_offset is not None:
            offset = reminder.month_end_offset
            if offset == 0:
                return t("reminders.schedule.monthly_last", hour=hour)
            key = (
                "reminders.schedule.monthly_before"
                if offset == 1
                else "reminders.schedule.monthly_before_plural"
            )
            return t(key, days=offset, hour=hour)
        return t(
            "reminders.schedule.monthly",
            day=reminder.day_of_month,
            suffix=_ordinal(reminder.day_of_month or 1),
            hour=hour,
        )
    days = reminder.interval_days or 1
    key = (
        "reminders.schedule.interval"
        if days == 1
        else "reminders.schedule.interval_plural"
    )
    return t(key, days=days, hour=hour)


def schedule_footer(reminder: Reminder) -> str | None:
    """Footer for a posted reminder; one-offs carry none."""
    if reminder.kind == KIND_ONCE:
        return None
    return schedule_summary(reminder)


def reminder_embed(
    client: discord.Client, task_text: str, *, footer: str | None = None
) -> discord.Embed:
    embed = new_embed(client)
    embed.title = task_text
    if footer:
        embed.set_footer(text=footer)
    set_transparent_image(embed, client)
    return embed


def completed_embed(client: discord.Client, task_text: str) -> discord.Embed:
    embed = reminder_embed(
        client, t("reminders.post.completed_title", task=task_text)
    )
    embed.colour = discord.Colour.green()
    embed.set_footer(text=t("reminders.post.completed_footer"))
    return embed


def snoozed_embed(
    client: discord.Client, task_text: str, snoozed_until: datetime
) -> discord.Embed:
    when = discord.utils.format_dt(snoozed_until.replace(tzinfo=timezone.utc), "t")
    return reminder_embed(
        client, task_text, footer=t("reminders.post.snoozed_footer", time=when)
    )


def _confirmation_embed(
    client: discord.Client, reminder: Reminder, title: str
) -> discord.Embed:
    embed = reminder_embed(client, reminder.message)
    embed.title = title
    embed.description = f"> {reminder.message}"
    embed.add_field(
        name=t("reminders.shared.summary_field"),
        value=schedule_summary(reminder),
        inline=True,
    )
    embed.add_field(
        name=t("reminders.shared.next_field"),
        value=_format_run(reminder.next_run_at),
        inline=True,
    )
    embed.add_field(
        name=t("reminders.shared.channel_field"),
        value=f"<#{reminder.channel_id}>",
        inline=True,
    )
    if reminder.role_id is not None:
        embed.add_field(
            name=t("reminders.shared.role_field"),
            value=f"<@&{reminder.role_id}>",
            inline=True,
        )
    return embed


# --------------------------------------------------------------------- views


class _CompleteButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label=t("reminders.buttons.complete"),
            emoji=t("reminders.buttons.complete_emoji"),
            style=discord.ButtonStyle.success,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, ReminderActionsView)
        await view.mark_completed(interaction)


class _SnoozeButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label=t("reminders.buttons.snooze"),
            emoji=t("reminders.buttons.snooze_emoji"),
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, ReminderActionsView)
        await view.snooze_for_an_hour(interaction)


class ReminderActionsView(ExpiringView):
    """The complete/snooze buttons attached to a posted reminder."""

    def __init__(
        self,
        reminder_id: int | None,
        owner_id: int,
        task_text: str,
        *,
        role_id: int | None = None,
        timeout: float = BUTTON_LIFETIME_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self.reminder_id = reminder_id
        self.owner_id = owner_id
        self.task_text = task_text
        self.role_id = role_id
        self.add_item(_CompleteButton())
        self.add_item(_SnoozeButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            t("components.not_author"), ephemeral=True
        )
        return False

    async def mark_completed(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=completed_embed(interaction.client, self.task_text),
            view=None,
        )

    async def snooze_for_an_hour(self, interaction: discord.Interaction) -> None:
        now = utcnow()
        await _service(interaction).snooze(
            reminder_id=self.reminder_id,
            user_id=self.owner_id,
            channel_id=interaction.channel_id or 0,
            message=self.task_text,
            role_id=self.role_id,
            now=now,
        )
        self.stop()
        await interaction.response.edit_message(
            embed=snoozed_embed(
                interaction.client, self.task_text, now + SNOOZE_DELAY
            ),
            view=None,
        )


# ------------------------------------------------------------------ choices

REPEAT_CHOICES = [
    app_commands.Choice(name=t(f"reminders.repeat.{kind}"), value=kind)
    for kind in KINDS
]

SCHEDULE_EXAMPLES = {
    KIND_ONCE: (
        "2026-10-29 at 14:00",
        "thursday at 2pm",
        "tomorrow at 9am",
        "in 30 minutes",
    ),
    KIND_DAILY: ("at 2pm", "every day at 2pm"),
    KIND_WEEKLY: ("on monday at 2pm", "every monday at 2pm"),
    KIND_MONTHLY: (
        "on the 15th at 2pm",
        "on the last day of the month at 2pm",
        "2 days before the end of the month at 2pm",
        "3 days before the end of the month at 2pm",
    ),
    KIND_INTERVAL: ("every 2 days at 2pm",),
}


def _option_value(interaction: discord.Interaction, name: str):
    for option in (interaction.data or {}).get("options", []):
        if option.get("name") == name:
            return option.get("value")
    return None


def _error_text(exc: reminder_service.ReminderError) -> str:
    variables = dict(exc.variables)
    if isinstance(exc, reminder_service.ScheduleError):
        kind = variables["kind"]
        variables["repeat"] = t(f"reminders.repeat.{kind}")
        variables["examples"] = t(f"reminders.schedule_help.{kind}")
    return t(exc.key, **variables)


async def schedule_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    kind = _option_value(interaction, "repeat")
    options = (interaction.data or {}).get("options", [])
    editing = any(option.get("name") == "reminder" for option in options)
    if kind in SCHEDULE_EXAMPLES:
        examples = SCHEDULE_EXAMPLES[kind]
    elif editing:
        examples = [
            example
            for group in SCHEDULE_EXAMPLES.values()
            for example in group
        ]
    else:
        examples = SCHEDULE_EXAMPLES[KIND_ONCE]
    needle = (current or "").strip().lower()
    return [
        app_commands.Choice(name=example, value=example)
        for example in examples
        if needle in example.lower()
    ][:25]


async def _reminder_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    service = _service(interaction)
    user_id = None if is_admin(interaction) else interaction.user.id
    rows = await service.list_for(user_id=user_id)
    needle = (current or "").strip().lower()
    choices: list[app_commands.Choice[int]] = []
    for reminder in rows:
        label = f"#{reminder.id} {reminder.message}"
        if needle and needle not in label.lower():
            continue
        choices.append(
            app_commands.Choice(name=label[:100], value=reminder.id)
        )
        if len(choices) >= 25:
            break
    return choices


# ------------------------------------------------------------------ commands


@reminders_group.command(name="add", description=t("reminders.add.description"))
@app_commands.describe(
    message=t("reminders.shared.param_message"),
    schedule=t("reminders.shared.param_schedule"),
    repeat=t("reminders.shared.param_repeat"),
    timezone=t("reminders.shared.param_timezone"),
    channel=t("reminders.shared.param_channel"),
    role=t("reminders.shared.param_role"),
)
@app_commands.choices(repeat=REPEAT_CHOICES)
@app_commands.autocomplete(schedule=schedule_autocomplete)
async def add(
    interaction: discord.Interaction,
    message: str,
    schedule: str,
    repeat: str = KIND_ONCE,
    timezone: str | None = None,
    channel: discord.TextChannel | None = None,
    role: discord.Role | None = None,
) -> None:
    if channel is not None and not is_admin(interaction):
        await interaction.response.send_message(
            t("reminders.errors.admin_only_channel"), ephemeral=True
        )
        return
    if role is not None and not is_admin(interaction):
        await interaction.response.send_message(
            t("reminders.errors.admin_only_role"), ephemeral=True
        )
        return
    service = _service(interaction)
    zone = timezone or await service.default_timezone(interaction.user.id)
    try:
        parsed = reminder_service.parse_schedule(
            repeat, schedule, timezone=zone
        )
        reminder = await service.add(
            user_id=interaction.user.id,
            channel_id=(
                channel.id
                if channel is not None
                else interaction.channel_id or 0
            ),
            message=message,
            timezone=zone,
            kind=repeat,
            hour=parsed.hour,
            weekday=parsed.weekday,
            day_of_month=parsed.day_of_month,
            month_end_offset=parsed.month_end_offset,
            interval_days=parsed.interval_days,
            date_raw=(
                parsed.run_date.isoformat() if parsed.run_date else None
            ),
            run_at_raw=(
                parsed.run_at.isoformat() if parsed.run_at else None
            ),
            role_id=role.id if role is not None else None,
        )
    except reminder_service.ReminderError as exc:
        await interaction.response.send_message(
            _error_text(exc), ephemeral=True
        )
        return
    await interaction.response.send_message(
        embed=_confirmation_embed(
            interaction.client, reminder, t("reminders.add.title")
        ),
    )


@reminders_group.command(
    name="edit", description=t("reminders.edit.description")
)
@app_commands.describe(
    reminder=t("reminders.shared.param_reminder"),
    message=t("reminders.shared.param_message"),
    schedule=t("reminders.shared.param_schedule"),
    repeat=t("reminders.shared.param_repeat"),
    timezone=t("reminders.shared.param_timezone"),
    channel=t("reminders.shared.param_channel"),
    role=t("reminders.shared.param_role"),
)
@app_commands.choices(repeat=REPEAT_CHOICES)
@app_commands.autocomplete(schedule=schedule_autocomplete)
async def edit(
    interaction: discord.Interaction,
    reminder: int,
    message: str | None = None,
    schedule: str | None = None,
    repeat: str | None = None,
    timezone: str | None = None,
    channel: discord.TextChannel | None = None,
    role: discord.Role | None = None,
) -> None:
    service = _service(interaction)
    existing = await service.get(reminder)
    if existing is None:
        await interaction.response.send_message(
            t("reminders.remove.not_found", id=reminder), ephemeral=True
        )
        return
    if existing.user_id != interaction.user.id and not is_admin(interaction):
        await interaction.response.send_message(
            t("components.permissions.no_permission"), ephemeral=True
        )
        return
    if channel is not None and not is_admin(interaction):
        await interaction.response.send_message(
            t("reminders.errors.admin_only_channel"), ephemeral=True
        )
        return
    if role is not None and not is_admin(interaction):
        await interaction.response.send_message(
            t("reminders.errors.admin_only_role"), ephemeral=True
        )
        return

    try:
        kind = repeat or existing.kind
        if schedule is None and repeat is not None and repeat != existing.kind:
            raise reminder_service.MissingSchedule(kind=kind)
        parsed = None
        if schedule is not None:
            parsed = reminder_service.parse_schedule(
                kind, schedule, timezone=timezone or existing.timezone
            )
        updated = await service.edit(
            reminder,
            message=message,
            timezone=timezone,
            kind=repeat,
            hour=parsed.hour if parsed else None,
            weekday=parsed.weekday if parsed else None,
            day_of_month=parsed.day_of_month if parsed else None,
            month_end_offset=parsed.month_end_offset if parsed else None,
            interval_days=parsed.interval_days if parsed else None,
            date_raw=(
                parsed.run_date.isoformat()
                if parsed and parsed.run_date
                else None
            ),
            run_at_raw=(
                parsed.run_at.isoformat()
                if parsed and parsed.run_at
                else None
            ),
            role_id=role.id if role is not None else None,
            channel_id=channel.id if channel is not None else None,
        )
    except reminder_service.ReminderError as exc:
        await interaction.response.send_message(
            _error_text(exc), ephemeral=True
        )
        return
    await interaction.response.send_message(
        embed=_confirmation_embed(
            interaction.client, updated, t("reminders.edit.title")
        ),
    )


@reminders_group.command(
    name="remove", description=t("reminders.remove.description")
)
@app_commands.describe(reminder=t("reminders.shared.param_reminder"))
async def remove(
    interaction: discord.Interaction, reminder: int
) -> None:
    service = _service(interaction)
    existing = await service.get(reminder)
    if existing is None:
        await interaction.response.send_message(
            t("reminders.remove.not_found", id=reminder), ephemeral=True
        )
        return
    if existing.user_id != interaction.user.id and not is_admin(interaction):
        await interaction.response.send_message(
            t("components.permissions.no_permission"), ephemeral=True
        )
        return
    await service.remove(reminder)
    await interaction.response.send_message(
        t("reminders.remove.success", id=reminder), ephemeral=True
    )


@edit.autocomplete("reminder")
@remove.autocomplete("reminder")
async def reminder_id_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    return await _reminder_autocomplete(interaction, current)


def _list_embed(
    client: discord.Client,
    reminder: Reminder,
    member: discord.Member | None,
) -> discord.Embed:
    embed = reminder_embed(client, reminder.message)
    name = member.display_name if member is not None else str(reminder.user_id)
    set_author(embed, member, name)
    embed.add_field(
        name=t("reminders.shared.summary_field"),
        value=schedule_summary(reminder),
        inline=True,
    )
    embed.add_field(
        name=t("reminders.shared.next_field"),
        value=_format_run(reminder.next_run_at),
        inline=True,
    )
    embed.add_field(
        name=t("reminders.shared.channel_field"),
        value=f"<#{reminder.channel_id}>",
        inline=True,
    )
    if reminder.role_id is not None:
        embed.add_field(
            name=t("reminders.shared.role_field"),
            value=f"<@&{reminder.role_id}>",
            inline=True,
        )
    return embed


@reminders_group.command(
    name="list", description=t("reminders.list.description")
)
@app_commands.describe(user=t("reminders.shared.param_user"))
@app_commands.default_permissions(manage_guild=True)
async def list_reminders(
    interaction: discord.Interaction, user: discord.User | None = None
) -> None:
    if not await require_admin(interaction):
        return
    target = user if user is not None else interaction.user

    await interaction.response.defer(ephemeral=True)
    rows = await _service(interaction).list_for(user_id=target.id)
    if not rows:
        key = (
            "reminders.list.empty"
            if target.id == interaction.user.id
            else "reminders.list.empty_user"
        )
        await interaction.followup.send(
            t(key, user=target.mention), ephemeral=True
        )
        return

    members = await resolve_members(interaction.guild, [target.id])
    member = members[0]
    embeds = [
        _list_embed(interaction.client, reminder, member) for reminder in rows
    ]
    await send_paginated(
        interaction,
        chunked(embeds, EMBEDS_PER_PAGE),
        total_results=len(rows),
    )


@add.autocomplete("timezone")
@edit.autocomplete("timezone")
async def timezone_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=label, value=value)
        for value, label in search_timezones(current)
    ]
