"""Weekly reading prompt commands (``/weekly-reading ...``).

Admins set where the prompt is posted, when it goes out, which role is pinged
and what the deck of prompts says.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..services.weekdays import WEEKDAY_NAMES
from ..services.weekly_reading import (
    WeeklyReadingError,
    WeeklyReadingService,
    format_posted_date,
)
from ..texts import t
from .checks import require_admin
from .listing import (
    EMBEDS_PER_PAGE,
    chunked,
    new_embed,
    send_paginated,
    set_transparent_image,
)

logger = logging.getLogger("bh2.weekly_reading")

weekly_reading_group = app_commands.Group(
    name="weekly-reading",
    description=t("weekly_reading.group_description"),
)

channel_group = app_commands.Group(
    name="channel",
    description=t("weekly_reading.channel.group_description"),
    parent=weekly_reading_group,
)

role_group = app_commands.Group(
    name="role",
    description=t("weekly_reading.role.group_description"),
    parent=weekly_reading_group,
)

schedule_group = app_commands.Group(
    name="schedule",
    description=t("weekly_reading.schedule.group_description"),
    parent=weekly_reading_group,
)

cards_group = app_commands.Group(
    name="cards",
    description=t("weekly_reading.cards.group_description"),
    parent=weekly_reading_group,
)

DAY_CHOICES = [
    app_commands.Choice(name=t(f"components.weekday.{name}"), value=index)
    for index, name in enumerate(WEEKDAY_NAMES)
]


def _service(interaction: discord.Interaction) -> WeeklyReadingService:
    return interaction.client.weekly_reading


def _hour_value(hour: int) -> str:
    return f"{hour:02d}:00"


def _schedule_summary(service: WeeklyReadingService) -> str:
    if service.weekday is None or service.hour is None:
        return t("weekly_reading.status.schedule_unset")
    weekday = t(f"components.weekday.{WEEKDAY_NAMES[service.weekday]}")
    return t(
        "weekly_reading.status.schedule_value",
        weekday=weekday,
        hour=_hour_value(service.hour),
    )


def _posting_channel(
    interaction: discord.Interaction, service: WeeklyReadingService
) -> discord.abc.Messageable | None:
    """The channel the prompt goes out in, if it can be reached."""
    if not service.channel_id:
        return None
    if interaction.guild is not None:
        channel = interaction.guild.get_channel(service.channel_id)
        if channel is not None:
            return channel
    return interaction.client.get_channel(service.channel_id)


def _status_embed(
    client: discord.Client, service: WeeklyReadingService, cards
) -> discord.Embed:
    embed = new_embed(client)
    embed.title = t("weekly_reading.status.title")
    embed.add_field(
        name=t("weekly_reading.status.channel_field"),
        value=(
            f"<#{service.channel_id}>"
            if service.channel_id
            else t("weekly_reading.status.channel_unset")
        ),
        inline=True,
    )
    embed.add_field(
        name=t("weekly_reading.status.role_field"),
        value=(
            f"<@&{service.role_id}>"
            if service.role_id
            else t("weekly_reading.status.role_unset")
        ),
        inline=True,
    )
    embed.add_field(
        name=t("weekly_reading.status.schedule_field"),
        value=_schedule_summary(service),
        inline=True,
    )
    embed.add_field(
        name=t("weekly_reading.status.state_field"),
        value=(
            t("weekly_reading.status.state_active")
            if service.enabled
            else t("weekly_reading.status.state_paused")
        ),
        inline=True,
    )
    pending = sum(1 for card in cards if not card.sent)
    embed.add_field(
        name=t("weekly_reading.status.deck_field"),
        value=t(
            "weekly_reading.status.deck_value",
            pending=pending,
            total=len(cards),
        ),
        inline=True,
    )
    embed.add_field(
        name=t("weekly_reading.status.last_posted_field"),
        value=(
            format_posted_date(service.last_posted_on)
            if service.last_posted_on
            else t("weekly_reading.status.never_posted")
        ),
        inline=True,
    )
    set_transparent_image(embed, client)
    return embed


def _card_embed(client: discord.Client, card) -> discord.Embed:
    embed = new_embed(client)
    embed.title = t("weekly_reading.cards.list.title", id=card.id)
    if card.description:
        embed.description = card.description
    embed.add_field(
        name=t("weekly_reading.status.state_field"),
        value=(
            t("weekly_reading.cards.list.pending")
            if not card.sent
            else t(
                "weekly_reading.cards.list.posted",
                when=format_posted_date(card.sent_at.date())
                if card.sent_at
                else t("weekly_reading.status.never_posted"),
            )
        ),
        inline=True,
    )
    set_transparent_image(embed, client)
    return embed


async def _card_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    cards = await _service(interaction).list_cards()
    needle = (current or "").strip().lower()
    choices: list[app_commands.Choice[int]] = []
    for card in cards:
        label = f"#{card.id} {card.title}"
        if needle and needle not in label.lower():
            continue
        choices.append(
            app_commands.Choice(name=label[:100], value=card.id)
        )
        if len(choices) >= 25:
            break
    return choices


# ------------------------------------------------------------------ commands


@channel_group.command(
    name="set", description=t("weekly_reading.channel.set.description")
)
@app_commands.describe(channel=t("weekly_reading.channel.set.param_channel"))
async def set_channel(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_channel(channel.id)
    await interaction.response.send_message(
        t("weekly_reading.channel.set.success", channel=channel.mention)
    )


@role_group.command(
    name="set", description=t("weekly_reading.role.set.description")
)
@app_commands.describe(role=t("weekly_reading.role.set.param_role"))
async def set_role(
    interaction: discord.Interaction, role: discord.Role
) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_role(role.id)
    await interaction.response.send_message(
        t("weekly_reading.role.set.success", role=role.mention)
    )


@role_group.command(
    name="remove", description=t("weekly_reading.role.remove.description")
)
async def remove_role(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_role(None)
    await interaction.response.send_message(
        t("weekly_reading.role.remove.success")
    )


@schedule_group.command(
    name="set", description=t("weekly_reading.schedule.set.description")
)
@app_commands.describe(
    day=t("weekly_reading.schedule.set.param_day"),
    hour=t("weekly_reading.schedule.set.param_hour"),
)
@app_commands.choices(day=DAY_CHOICES)
async def set_schedule(
    interaction: discord.Interaction, day: int, hour: int
) -> None:
    if not await require_admin(interaction):
        return
    service = _service(interaction)
    try:
        await service.set_schedule(day, hour)
    except WeeklyReadingError as exc:
        await interaction.response.send_message(
            t(exc.key, **exc.variables), ephemeral=True
        )
        return
    await interaction.response.send_message(
        t(
            "weekly_reading.schedule.set.success",
            weekday=t(f"components.weekday.{WEEKDAY_NAMES[day]}"),
            hour=_hour_value(hour),
        )
    )


@weekly_reading_group.command(
    name="pause", description=t("weekly_reading.pause.description")
)
async def pause(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_enabled(False)
    await interaction.response.send_message(t("weekly_reading.pause.success"))


@weekly_reading_group.command(
    name="resume", description=t("weekly_reading.resume.description")
)
async def resume(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_enabled(True)
    await interaction.response.send_message(t("weekly_reading.resume.success"))


@weekly_reading_group.command(
    name="force", description=t("weekly_reading.force.description")
)
async def force(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    service = _service(interaction)
    channel = _posting_channel(interaction, service)
    if channel is None:
        await interaction.followup.send(
            t("weekly_reading.force.no_channel"), ephemeral=True
        )
        return
    try:
        card = await service.post_card(channel, interaction.client)
    except discord.HTTPException as exc:
        logger.warning("Could not post the reading prompt", exc_info=True)
        await interaction.followup.send(
            t("weekly_reading.force.failed", error=exc), ephemeral=True
        )
        return
    if card is None:
        await interaction.followup.send(
            t("weekly_reading.force.no_cards"), ephemeral=True
        )
        return
    await interaction.followup.send(
        t(
            "weekly_reading.force.success",
            channel=f"<#{service.channel_id}>",
        ),
        ephemeral=True,
    )


@weekly_reading_group.command(
    name="status", description=t("weekly_reading.status.description")
)
async def status(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    service = _service(interaction)
    cards = await service.list_cards()
    await interaction.response.send_message(
        embed=_status_embed(interaction.client, service, cards),
        ephemeral=True,
    )


@cards_group.command(
    name="add", description=t("weekly_reading.cards.add.description")
)
@app_commands.describe(
    title=t("weekly_reading.cards.add.param_title"),
    description=t("weekly_reading.cards.add.param_description"),
)
async def add_card(
    interaction: discord.Interaction,
    title: str,
    description: str | None = None,
) -> None:
    if not await require_admin(interaction):
        return
    service = _service(interaction)
    try:
        card = await service.add_card(title, description)
    except WeeklyReadingError as exc:
        await interaction.response.send_message(
            t(exc.key, **exc.variables), ephemeral=True
        )
        return
    await interaction.response.send_message(
        t("weekly_reading.cards.add.success", title=card.title)
    )


@cards_group.command(
    name="edit", description=t("weekly_reading.cards.edit.description")
)
@app_commands.describe(
    card=t("weekly_reading.cards.edit.param_card"),
    title=t("weekly_reading.cards.edit.param_title"),
    description=t("weekly_reading.cards.edit.param_description"),
)
async def edit_card(
    interaction: discord.Interaction,
    card: int,
    title: str | None = None,
    description: str | None = None,
) -> None:
    if not await require_admin(interaction):
        return
    service = _service(interaction)
    try:
        updated = await service.edit_card(
            card, title=title, description=description
        )
    except WeeklyReadingError as exc:
        await interaction.response.send_message(
            t(exc.key, **exc.variables), ephemeral=True
        )
        return
    await interaction.response.send_message(
        t("weekly_reading.cards.edit.success", title=updated.title)
    )


@cards_group.command(
    name="remove", description=t("weekly_reading.cards.remove.description")
)
@app_commands.describe(card=t("weekly_reading.cards.remove.param_card"))
async def remove_card(interaction: discord.Interaction, card: int) -> None:
    if not await require_admin(interaction):
        return
    service = _service(interaction)
    existing = await service.get_card(card)
    if existing is None:
        await interaction.response.send_message(
            t("weekly_reading.errors.card_not_found", id=card), ephemeral=True
        )
        return
    await service.remove_card(card)
    await interaction.response.send_message(
        t("weekly_reading.cards.remove.success", title=existing.title)
    )


@cards_group.command(
    name="list", description=t("weekly_reading.cards.list.description")
)
async def list_cards(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    cards = await _service(interaction).list_cards()
    if not cards:
        await interaction.followup.send(
            t("weekly_reading.cards.list.empty"), ephemeral=True
        )
        return
    embeds = [
        _card_embed(interaction.client, card) for card in cards
    ]
    await send_paginated(
        interaction,
        chunked(embeds, EMBEDS_PER_PAGE),
        total_results=len(cards),
    )


@edit_card.autocomplete("card")
@remove_card.autocomplete("card")
async def card_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    return await _card_autocomplete(interaction, current)
