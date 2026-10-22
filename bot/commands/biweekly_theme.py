"""Bi-weekly theme commands (``/biweekly-theme ...``).

Admins set where the theme is posted, when it goes out, which role is pinged
and what the deck of theme cards says.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..notifications import announce_theme_failed
from ..services.biweekly_theme import (
    BiweeklyThemeError,
    BiweeklyThemeService,
    ThemeImageError,
    format_posted_date,
)
from ..services.booster_role import resolve_notify_channel
from ..services.weekdays import WEEKDAY_NAMES
from ..texts import t
from .checks import require_admin
from .listing import (
    EMBEDS_PER_PAGE,
    chunked,
    new_embed,
    send_paginated,
    set_transparent_image,
)

logger = logging.getLogger("bh2.biweekly_theme")

biweekly_theme_group = app_commands.Group(
    name="biweekly-theme",
    description=t("biweekly_theme.group_description"),
)

channel_group = app_commands.Group(
    name="channel",
    description=t("biweekly_theme.channel.group_description"),
    parent=biweekly_theme_group,
)

role_group = app_commands.Group(
    name="role",
    description=t("biweekly_theme.role.group_description"),
    parent=biweekly_theme_group,
)

schedule_group = app_commands.Group(
    name="schedule",
    description=t("biweekly_theme.schedule.group_description"),
    parent=biweekly_theme_group,
)

cards_group = app_commands.Group(
    name="cards",
    description=t("biweekly_theme.cards.group_description"),
    parent=biweekly_theme_group,
)

DAY_CHOICES = [
    app_commands.Choice(name=t(f"components.weekday.{name}"), value=index)
    for index, name in enumerate(WEEKDAY_NAMES)
]


def _service(interaction: discord.Interaction) -> BiweeklyThemeService:
    return interaction.client.biweekly_theme


def _hour_value(hour: int) -> str:
    return f"{hour:02d}:00"


def _schedule_summary(service: BiweeklyThemeService) -> str:
    if service.weekday is None or service.hour is None:
        return t("biweekly_theme.status.schedule_unset")
    weekday = t(f"components.weekday.{WEEKDAY_NAMES[service.weekday]}")
    return t(
        "biweekly_theme.status.schedule_value",
        weekday=weekday,
        hour=_hour_value(service.hour),
    )


def _posting_channel(
    interaction: discord.Interaction, service: BiweeklyThemeService
) -> discord.abc.Messageable | None:
    """The channel the theme goes out in, if it can be reached."""
    if not service.channel_id:
        return None
    if interaction.guild is not None:
        channel = interaction.guild.get_channel(service.channel_id)
        if channel is not None:
            return channel
    return interaction.client.get_channel(service.channel_id)


async def _notify_failure(
    interaction: discord.Interaction, exc: ThemeImageError
) -> None:
    """Report an image failure to the admins' updates channel."""
    if interaction.guild is None:
        return
    channel = await resolve_notify_channel(
        interaction.guild,
        getattr(interaction.client, "bot_updates_channel_id", 0),
    )
    await announce_theme_failed(
        channel,
        title=exc.card.title,
        error=exc.cause,
        colour=getattr(interaction.client, "default_embed_color", None),
    )


def _status_embed(
    client: discord.Client, service: BiweeklyThemeService, cards
) -> discord.Embed:
    embed = new_embed(client)
    embed.title = t("biweekly_theme.status.title")
    embed.add_field(
        name=t("biweekly_theme.status.channel_field"),
        value=(
            f"<#{service.channel_id}>"
            if service.channel_id
            else t("biweekly_theme.status.channel_unset")
        ),
        inline=True,
    )
    embed.add_field(
        name=t("biweekly_theme.status.role_field"),
        value=(
            f"<@&{service.role_id}>"
            if service.role_id
            else t("biweekly_theme.status.role_unset")
        ),
        inline=True,
    )
    embed.add_field(
        name=t("biweekly_theme.status.schedule_field"),
        value=_schedule_summary(service),
        inline=True,
    )
    embed.add_field(
        name=t("biweekly_theme.status.state_field"),
        value=(
            t("biweekly_theme.status.state_active")
            if service.enabled
            else t("biweekly_theme.status.state_paused")
        ),
        inline=True,
    )
    pending = sum(1 for card in cards if not card.sent)
    embed.add_field(
        name=t("biweekly_theme.status.deck_field"),
        value=t(
            "biweekly_theme.status.deck_value",
            pending=pending,
            total=len(cards),
        ),
        inline=True,
    )
    embed.add_field(
        name=t("biweekly_theme.status.last_posted_field"),
        value=(
            format_posted_date(service.last_posted_on)
            if service.last_posted_on
            else t("biweekly_theme.status.never_posted")
        ),
        inline=True,
    )
    set_transparent_image(embed, client)
    return embed


def _card_embed(client: discord.Client, card) -> discord.Embed:
    embed = new_embed(client)
    embed.title = t("biweekly_theme.cards.list.title", id=card.id)
    embed.description = f"### {card.title}"
    if card.description:
        embed.description += f"\n{card.description}"
    embed.add_field(
        name=t("biweekly_theme.status.state_field"),
        value=(
            t("biweekly_theme.cards.list.pending")
            if not card.sent
            else t(
                "biweekly_theme.cards.list.posted",
                when=format_posted_date(card.sent_at.date())
                if card.sent_at
                else t("biweekly_theme.status.never_posted"),
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
    name="set", description=t("biweekly_theme.channel.set.description")
)
@app_commands.describe(channel=t("biweekly_theme.channel.set.param_channel"))
async def set_channel(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_channel(channel.id)
    await interaction.response.send_message(
        t("biweekly_theme.channel.set.success", channel=channel.mention)
    )


@role_group.command(
    name="set", description=t("biweekly_theme.role.set.description")
)
@app_commands.describe(role=t("biweekly_theme.role.set.param_role"))
async def set_role(
    interaction: discord.Interaction, role: discord.Role
) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_role(role.id)
    await interaction.response.send_message(
        t("biweekly_theme.role.set.success", role=role.mention)
    )


@role_group.command(
    name="remove", description=t("biweekly_theme.role.remove.description")
)
async def remove_role(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_role(None)
    await interaction.response.send_message(
        t("biweekly_theme.role.remove.success")
    )


@schedule_group.command(
    name="set", description=t("biweekly_theme.schedule.set.description")
)
@app_commands.describe(
    day=t("biweekly_theme.schedule.set.param_day"),
    hour=t("biweekly_theme.schedule.set.param_hour"),
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
    except BiweeklyThemeError as exc:
        await interaction.response.send_message(
            t(exc.key, **exc.variables), ephemeral=True
        )
        return
    await interaction.response.send_message(
        t(
            "biweekly_theme.schedule.set.success",
            weekday=t(f"components.weekday.{WEEKDAY_NAMES[day]}"),
            hour=_hour_value(hour),
        )
    )


@biweekly_theme_group.command(
    name="pause", description=t("biweekly_theme.pause.description")
)
async def pause(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_enabled(False)
    await interaction.response.send_message(t("biweekly_theme.pause.success"))


@biweekly_theme_group.command(
    name="resume", description=t("biweekly_theme.resume.description")
)
async def resume(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_enabled(True)
    await interaction.response.send_message(t("biweekly_theme.resume.success"))


@biweekly_theme_group.command(
    name="force", description=t("biweekly_theme.force.description")
)
async def force(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    service = _service(interaction)
    channel = _posting_channel(interaction, service)
    if channel is None:
        await interaction.followup.send(
            t("biweekly_theme.force.no_channel"), ephemeral=True
        )
        return
    try:
        card = await service.post_card(channel, interaction.client)
    except ThemeImageError as exc:
        logger.warning("Could not create the theme image", exc_info=True)
        await _notify_failure(interaction, exc)
        await interaction.followup.send(
            t(exc.key, **exc.variables), ephemeral=True
        )
        return
    except discord.HTTPException as exc:
        logger.warning("Could not post the theme", exc_info=True)
        await interaction.followup.send(
            t("biweekly_theme.force.failed", error=exc), ephemeral=True
        )
        return
    if card is None:
        await interaction.followup.send(
            t("biweekly_theme.force.no_cards"), ephemeral=True
        )
        return
    await interaction.followup.send(
        t(
            "biweekly_theme.force.success",
            channel=f"<#{service.channel_id}>",
        ),
        ephemeral=True,
    )


@biweekly_theme_group.command(
    name="status", description=t("biweekly_theme.status.description")
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
    name="add", description=t("biweekly_theme.cards.add.description")
)
@app_commands.describe(
    title=t("biweekly_theme.cards.add.param_title"),
    description=t("biweekly_theme.cards.add.param_description"),
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
    except BiweeklyThemeError as exc:
        await interaction.response.send_message(
            t(exc.key, **exc.variables), ephemeral=True
        )
        return
    await interaction.response.send_message(
        t("biweekly_theme.cards.add.success", title=card.title)
    )


@cards_group.command(
    name="edit", description=t("biweekly_theme.cards.edit.description")
)
@app_commands.describe(
    card=t("biweekly_theme.cards.edit.param_card"),
    title=t("biweekly_theme.cards.edit.param_title"),
    description=t("biweekly_theme.cards.edit.param_description"),
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
    except BiweeklyThemeError as exc:
        await interaction.response.send_message(
            t(exc.key, **exc.variables), ephemeral=True
        )
        return
    await interaction.response.send_message(
        t("biweekly_theme.cards.edit.success", title=updated.title)
    )


@cards_group.command(
    name="remove", description=t("biweekly_theme.cards.remove.description")
)
@app_commands.describe(card=t("biweekly_theme.cards.remove.param_card"))
async def remove_card(interaction: discord.Interaction, card: int) -> None:
    if not await require_admin(interaction):
        return
    service = _service(interaction)
    existing = await service.get_card(card)
    if existing is None:
        await interaction.response.send_message(
            t("biweekly_theme.errors.card_not_found", id=card), ephemeral=True
        )
        return
    await service.remove_card(card)
    await interaction.response.send_message(
        t("biweekly_theme.cards.remove.success", title=existing.title)
    )


@cards_group.command(
    name="list", description=t("biweekly_theme.cards.list.description")
)
async def list_cards(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    cards = await _service(interaction).list_cards()
    if not cards:
        await interaction.followup.send(
            t("biweekly_theme.cards.list.empty"), ephemeral=True
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
