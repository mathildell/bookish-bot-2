"""Reminder commands (``/reminder ...``).

Members create one-off or recurring reminders in a channel; when one fires the
owner is pinged and can complete it or snooze it by a chosen amount of time.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
    Reminder,
    ReminderService,
)
from ..services.timezones import search_timezones
from ..services.weekdays import WEEKDAY_NAMES
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
from .views import ExpiringView, delete_interaction_view, delete_view, tracked_row

logger = logging.getLogger("bh2.reminders")

BUTTON_LIFETIME_SECONDS = 2 * 60 * 60
REMINDER_VIEW_KIND = "reminder"
COMPLETE_CUSTOM_ID = "reminders:complete"
SNOOZE_CUSTOM_ID = "reminders:snooze"

_USER_MENTION = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION = re.compile(r"<@&(\d+)>")

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
    return t(f"components.weekday.{WEEKDAY_NAMES[weekday]}")


def _format_run(moment: datetime) -> str:
    return discord.utils.format_dt(moment.replace(tzinfo=timezone.utc), "f")


def schedule_summary(reminder: Reminder) -> str:
    """Human-readable schedule, used in confirmations and posted reminders."""
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


def schedule_description(reminder: Reminder) -> str | None:
    """Description for a posted reminder; one-offs carry none."""
    if reminder.kind == KIND_ONCE:
        return None
    return schedule_summary(reminder)


def reminder_embed(
    client: discord.Client, task_text: str, *, description: str | None = None
) -> discord.Embed:
    embed = new_embed(client)
    embed.title = task_text
    if description:
        embed.description = description
    set_transparent_image(embed, client)
    return embed


def completed_embed(client: discord.Client, task_text: str) -> discord.Embed:
    embed = reminder_embed(
        client, t("reminders.post.completed_title", task=task_text)
    )
    embed.colour = discord.Colour.green()
    embed.description = t("reminders.post.completed_description")
    return embed


def snoozed_title(task_text: str) -> str:
    """Prefix a snoozed reminder's title, without prefixing it twice."""
    prefix = t("reminders.post.snoozed_title", task="")
    if task_text.startswith(prefix):
        return task_text
    return t("reminders.post.snoozed_title", task=task_text)


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


@dataclass(frozen=True)
class _ReminderButtonState:
    owner_id: int | None
    role_id: int | None
    task_text: str


def _message_state(message: discord.Message) -> _ReminderButtonState:
    content = getattr(message, "content", "") or ""
    owner = _USER_MENTION.search(content)
    role = _ROLE_MENTION.search(content)
    embeds = getattr(message, "embeds", None) or []
    task_text = embeds[0].title if embeds and embeds[0].title else ""
    return _ReminderButtonState(
        owner_id=int(owner.group(1)) if owner else None,
        role_id=int(role.group(1)) if role else None,
        task_text=task_text,
    )


def _reminder_id(row: object | None) -> int | None:
    state = getattr(row, "state", None) if row is not None else None
    if isinstance(state, dict):
        value = state.get("reminder_id")
        if isinstance(value, int):
            return value
    return None


class _CompleteButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label=t("reminders.buttons.complete"),
            emoji=t("reminders.buttons.complete_emoji"),
            style=discord.ButtonStyle.success,
            custom_id=COMPLETE_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, _ReminderActions)
        await view.mark_completed(interaction)


class _SnoozeButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label=t("reminders.buttons.snooze"),
            emoji=t("reminders.buttons.snooze_emoji"),
            style=discord.ButtonStyle.secondary,
            custom_id=SNOOZE_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, _ReminderActions)
        await view.start_snooze(interaction)


SNOOZE_UNITS = ("minutes", "hours", "days")
SNOOZE_MAX_AMOUNT = 30
SNOOZE_MODAL_LIFETIME_SECONDS = 15 * 60
_SNOOZE_UNIT_TEXT = {
    "minutes": ("minute", "minutes"),
    "hours": ("hour", "hours"),
    "days": ("day", "days"),
}


def _has_role(user: discord.abc.User, role_id: int) -> bool:
    roles = getattr(user, "roles", None) or []
    return any(getattr(role, "id", None) == role_id for role in roles)


def _snooze_unit_name(unit: str, amount: int) -> str:
    singular, plural = _SNOOZE_UNIT_TEXT[unit]
    return t(f"reminders.snooze.unit.{singular if amount == 1 else plural}")


class _ReminderActions:
    """Shared callbacks for sent and persistent reminder action views."""

    reminder_id: int | None = None
    owner_id: int | None = None
    task_text: str | None = None
    role_id: int | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed, _ = await tracked_row(interaction)
        if not allowed:
            return False

        state = _message_state(interaction.message)
        owner_id = self.owner_id or state.owner_id
        role_id = self.role_id if self.role_id is not None else state.role_id
        if owner_id is not None and interaction.user.id == owner_id:
            return True
        if role_id is not None and _has_role(interaction.user, role_id):
            return True
        await interaction.response.send_message(
            t("reminders.errors.not_allowed"), ephemeral=True
        )
        return False

    async def _state(
        self, interaction: discord.Interaction
    ) -> tuple[_ReminderButtonState, object | None]:
        client = getattr(interaction, "client", None)
        service = getattr(client, "view_messages", None)
        message = getattr(interaction, "message", None)
        row = (
            await service.get(message.id)
            if service is not None and message is not None
            else None
        )
        state = _message_state(interaction.message)
        return (
            _ReminderButtonState(
                owner_id=self.owner_id or state.owner_id,
                role_id=(
                    self.role_id
                    if self.role_id is not None
                    else state.role_id
                ),
                task_text=self.task_text or state.task_text,
            ),
            row,
        )

    async def mark_completed(self, interaction: discord.Interaction) -> None:
        allowed, _ = await tracked_row(interaction)
        if not allowed:
            return
        state, _ = await self._state(interaction)
        await interaction.response.edit_message(
            embed=completed_embed(interaction.client, state.task_text),
            view=None,
        )
        if self.timeout is not None:
            self.stop()
        await delete_interaction_view(interaction)

    async def start_snooze(self, interaction: discord.Interaction) -> None:
        allowed, _ = await tracked_row(interaction)
        if not allowed:
            return
        state, row = await self._state(interaction)
        message = interaction.message
        modal = SnoozeModal(
            reminder_id=self.reminder_id or _reminder_id(row),
            owner_id=state.owner_id or interaction.user.id,
            role_id=state.role_id,
            task_text=state.task_text,
            channel_id=(
                message.channel.id
                if message is not None
                else interaction.channel_id
            ),
            original_message=message,
        )
        await interaction.response.send_modal(modal)


class SnoozeModal(discord.ui.Modal):
    """Modal choosing how long to snooze a reminder for."""

    def __init__(
        self,
        *,
        reminder_id: int | None,
        owner_id: int,
        role_id: int | None,
        task_text: str,
        channel_id: int,
        original_message: discord.Message | None,
    ) -> None:
        super().__init__(
            title=t("reminders.snooze.modal_title"),
            timeout=SNOOZE_MODAL_LIFETIME_SECONDS,
        )
        self.reminder_id = reminder_id
        self.owner_id = owner_id
        self.role_id = role_id
        self.task_text = task_text
        self.channel_id = channel_id
        self.original_message = original_message
        self.amount_input = discord.ui.Label(
            text=t(
                "reminders.snooze.amount_label",
                max=SNOOZE_MAX_AMOUNT,
            ),
            component=discord.ui.TextInput(
                placeholder=t("reminders.snooze.amount_placeholder"),
                default="1",
                max_length=2,
            ),
        )
        self.unit_input = discord.ui.Label(
            text=t("reminders.snooze.unit_label"),
            component=discord.ui.Select(
                placeholder=t("reminders.snooze.unit_placeholder"),
                options=[
                    discord.SelectOption(
                        label=t(f"reminders.snooze.units.{unit}"),
                        value=unit,
                        default=unit == "minutes",
                    )
                    for unit in SNOOZE_UNITS
                ],
            ),
        )
        self.add_item(self.amount_input)
        self.add_item(self.unit_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.amount_input.component.value.strip()
        try:
            amount = int(raw)
        except ValueError:
            amount = 0
        if not 1 <= amount <= SNOOZE_MAX_AMOUNT:
            await interaction.response.send_message(
                t("reminders.snooze.invalid_amount", max=SNOOZE_MAX_AMOUNT),
                ephemeral=True,
            )
            return

        unit = self.unit_input.component.values[0]
        await _service(interaction).snooze(
            reminder_id=self.reminder_id,
            user_id=self.owner_id,
            channel_id=self.channel_id,
            message=self.task_text,
            delay=timedelta(**{unit: amount}),
            role_id=self.role_id,
        )
        await interaction.response.send_message(
            t(
                "reminders.snooze.success",
                amount=amount,
                unit=_snooze_unit_name(unit, amount),
            ),
            ephemeral=True,
        )
        if self.original_message is not None:
            await delete_view(interaction.client, self.original_message.id)
            try:
                await self.original_message.delete()
            except discord.HTTPException:
                logger.debug(
                    "Could not delete the snoozed reminder message",
                    exc_info=True,
                )


class ReminderActionsView(_ReminderActions, ExpiringView):
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


class ReminderActionsFallbackView(_ReminderActions, discord.ui.View):
    """Persistent fallback for reminder action buttons after a restart."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(_CompleteButton())
        self.add_item(_SnoozeButton())


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
            reschedule=parsed is not None or timezone is not None,
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
async def list_reminders(
    interaction: discord.Interaction, user: discord.User | None = None
) -> None:
    if user is not None and not await require_admin(interaction):
        return
    if user is not None:
        target: discord.User | None = user
        user_id: int | None = user.id
    elif is_admin(interaction):
        target = None
        user_id = None
    else:
        target = interaction.user
        user_id = interaction.user.id

    await interaction.response.defer(ephemeral=True)
    rows = await _service(interaction).list_for(user_id=user_id)
    if not rows:
        if target is None:
            await interaction.followup.send(
                t("reminders.list.empty_all"), ephemeral=True
            )
        elif target.id == interaction.user.id:
            await interaction.followup.send(
                t("reminders.list.empty"), ephemeral=True
            )
        else:
            await interaction.followup.send(
                t("reminders.list.empty_user", user=target.mention),
                ephemeral=True,
            )
        return

    members = await resolve_members(
        interaction.guild, [reminder.user_id for reminder in rows]
    )
    embeds = [
        _list_embed(interaction.client, reminder, member)
        for reminder, member in zip(rows, members)
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
