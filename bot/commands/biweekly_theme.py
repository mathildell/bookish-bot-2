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
    CardNotFound,
    ThemeImageError,
    credit_description,
    format_posted_date,
    validate_description,
    validate_title,
)
from ..services.booster_role import resolve_notify_channel
from ..services.weekdays import WEEKDAY_NAMES
from ..texts import t
from .checks import is_admin, require_admin
from .listing import (
    EMBEDS_PER_PAGE,
    chunked,
    new_embed,
    send_paginated,
    set_transparent_image,
)
from .views import ExpiringView

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

    embed.add_field(
        name=t("biweekly_theme.status.title_field"),
        value=card.title,
        inline=False,
    )
    if card.description:
        embed.add_field(
            name=t("biweekly_theme.status.description_field"),
            value=card.description,
            inline=False,
        )
    set_transparent_image(embed, client)
    return embed


class ThemeSuggestView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label=t("biweekly_theme.cards.suggest.button_label"),
        emoji=t("biweekly_theme.cards.suggest.button_emoji"),
        style=discord.ButtonStyle.secondary,
        custom_id="biweekly_theme:suggest",
    )
    async def suggest(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        service = _service(interaction)
        updates_id = getattr(interaction.client, "bot_updates_channel_id", 0)
        if not updates_id:
            await interaction.response.send_message(
                t("biweekly_theme.cards.review.no_feed"), ephemeral=True
            )
            return
        await interaction.response.send_modal(ThemeSuggestModal(service))


class ThemeSuggestModal(discord.ui.Modal):
    def __init__(self, service: BiweeklyThemeService) -> None:
        super().__init__(title=t("biweekly_theme.cards.suggest.modal_title"))
        self.service = service
        self.add_item(
            discord.ui.TextInput(
                label=t("biweekly_theme.cards.suggest.modal_prompt_label")[:45],
                style=discord.TextStyle.short,
                max_length=256,
                required=True,
            )
        )
        self.add_item(
            discord.ui.TextInput(
                label=t("biweekly_theme.cards.suggest.modal_description_label")[:45],
                style=discord.TextStyle.paragraph,
                max_length=2000,
                required=False,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        prompt = (self.children[0].value or "").strip()
        description = (self.children[1].value or "").strip() or None
        if not prompt:
            await interaction.response.send_message(
                t("biweekly_theme.cards.suggest.no_text"), ephemeral=True
            )
            return
        try:
            clean_prompt = validate_title(prompt)
            clean_description = validate_description(description)
        except BiweeklyThemeError as exc:
            await interaction.response.send_message(
                t(exc.key, **exc.variables), ephemeral=True
            )
            return
        updates_id = getattr(interaction.client, "bot_updates_channel_id", 0)
        if not updates_id:
            await interaction.response.send_message(
                t("biweekly_theme.cards.review.no_feed"), ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                t("biweekly_theme.cards.suggest.closed"), ephemeral=True
            )
            return
        channel = await resolve_notify_channel(guild, updates_id)
        if channel is None:
            await interaction.response.send_message(
                t("biweekly_theme.cards.review.no_feed"), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        embed = new_embed(interaction.client)
        embed.title = t("biweekly_theme.cards.review.title")
        embed.add_field(
            name=t("biweekly_theme.cards.review.prompt_field"),
            value=clean_prompt,
            inline=False,
        )
        if clean_description:
            embed.add_field(
                name=t("biweekly_theme.cards.review.description_field"),
                value=clean_description,
                inline=False,
            )
        embed.add_field(
            name=t("biweekly_theme.cards.review.suggester_field"),
            value=interaction.user.mention,
            inline=False,
        )
        set_transparent_image(embed, interaction.client)
        view = ThemeSuggestionReviewView()
        message = await channel.send(embed=embed, view=view)
        await self.service.add_suggestion(
            interaction.user.id,
            clean_prompt,
            clean_description,
            message.id,
        )
        await interaction.followup.send(
            t("biweekly_theme.cards.suggest.success"), ephemeral=True
        )


class ThemeSuggestionReviewView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label=t("biweekly_theme.cards.review.approve"),
        emoji=t("biweekly_theme.cards.review.approve_emoji"),
        style=discord.ButtonStyle.success,
        custom_id="biweekly_theme:suggest_approve",
    )
    async def approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message(
                t("components.permissions.no_permission"), ephemeral=True
            )
            return
        service = _service(interaction)
        suggestion = await service.get_suggestion_by_message(interaction.message.id)
        if suggestion is None:
            await interaction.response.send_message(
                t("biweekly_theme.cards.review.already_handled"), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        name = None
        if guild is not None:
            member = guild.get_member(suggestion.user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(suggestion.user_id)
                except Exception:
                    member = None
            if member is not None:
                name = member.display_name
        if name is None:
            user = interaction.client.get_user(suggestion.user_id)
            if user is None:
                try:
                    user = await interaction.client.fetch_user(suggestion.user_id)
                except Exception:
                    user = None
            if user is not None:
                name = getattr(user, "display_name", None) or user.name
        if name is None:
            name = f"User {suggestion.user_id}"
        try:
            await service.add_card(suggestion.title, credit_description)
        except BiweeklyThemeError as exc:
            await interaction.followup.send(
                t(exc.key, **exc.variables), ephemeral=True
            )
            return
        await service.remove_suggestion(suggestion.id)
        embed = interaction.message.embeds[0] if interaction.message.embeds else new_embed(interaction.client)
        embed.add_field(
            name=t("biweekly_theme.cards.review.approb_status"),
            value=t("biweekly_theme.cards.review.approved", user=interaction.user.mention),
            inline=False,
        )
        await interaction.message.edit(embed=embed, view=None)
        await interaction.followup.send(
            t("biweekly_theme.cards.review.approve_success"), ephemeral=True
        )

    @discord.ui.button(
        label=t("biweekly_theme.cards.review.refuse"),
        emoji=t("biweekly_theme.cards.review.refuse_emoji"),
        style=discord.ButtonStyle.danger,
        custom_id="biweekly_theme:suggest_refuse",
    )
    async def refuse(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not is_admin(interaction):
            await interaction.response.send_message(
                t("components.permissions.no_permission"), ephemeral=True
            )
            return
        service = _service(interaction)
        suggestion = await service.get_suggestion_by_message(interaction.message.id)
        if suggestion is None:
            await interaction.response.send_message(
                t("biweekly_theme.cards.review.already_handled"), ephemeral=True
            )
            return
        await service.remove_suggestion(suggestion.id)
        embed = interaction.message.embeds[0] if interaction.message.embeds else new_embed(interaction.client)
        embed.add_field(
            name=t("biweekly_theme.cards.review.approb_status"),
            value=t("biweekly_theme.cards.review.refused", user=interaction.user.mention),            inline=False,
        )
        await interaction.response.edit_message(embed=embed, view=None)


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
    name="toggle", description=t("biweekly_theme.toggle.description")
)
async def toggle(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    service = _service(interaction)
    await service.set_enabled(not service.enabled)
    if service.enabled:
        await interaction.response.send_message(t("biweekly_theme.toggle.success_active"))
    else:
        await interaction.response.send_message(t("biweekly_theme.toggle.success_paused"))


@biweekly_theme_group.command(
    name="force", description=t("biweekly_theme.force.description")
)
@app_commands.describe(card=t("biweekly_theme.force.param_card"))
@app_commands.autocomplete(card=_card_autocomplete)
async def force(interaction: discord.Interaction, card: int | None = None) -> None:
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
        posted = await service.post_card(
            channel, interaction.client, card_id=card, view=ThemeSuggestView()
        )
    except CardNotFound:
        await interaction.followup.send(
            t("biweekly_theme.errors.card_not_found", id=card), ephemeral=True
        )
        return
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
    if posted is None:
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
    await interaction.response.defer(ephemeral=True)
    cards = await _service(interaction).list_cards()
    if not cards:
        await interaction.followup.send(
            t("biweekly_theme.cards.list.empty"), ephemeral=True
        )
        return
    embeds = [_card_embed(interaction.client, card) for card in cards]
    await send_paginated(
        interaction,
        chunked(embeds, EMBEDS_PER_PAGE),
        total_results=len(cards),
    )


@cards_group.command(
    name="search", description=t("biweekly_theme.cards.search.description")
)
@app_commands.describe(keyword=t("biweekly_theme.cards.search.param_keyword"))
async def search_cards(interaction: discord.Interaction, keyword: str) -> None:
    await interaction.response.defer(ephemeral=True)
    service = _service(interaction)
    cards = await service.search_cards(keyword)
    if not cards:
        await interaction.followup.send(
            t(
                "biweekly_theme.cards.search.not_found",
                keyword=discord.utils.escape_markdown(keyword),
            ),
            ephemeral=True,
        )
        return
    embeds = [_card_embed(interaction.client, card) for card in cards]
    await interaction.followup.send(
        t("biweekly_theme.cards.search.found"), ephemeral=True
    )
    await send_paginated(
        interaction,
        chunked(embeds, EMBEDS_PER_PAGE),
        total_results=len(cards),
    )


@cards_group.command(
    name="suggest", description=t("biweekly_theme.cards.suggest.description")
)
@app_commands.describe(
    prompt=t("biweekly_theme.cards.suggest.param_prompt"),
    description=t("biweekly_theme.cards.suggest.param_description"),
)
async def suggest_card(
    interaction: discord.Interaction,
    prompt: str,
    description: str | None = None,
) -> None:
    service = _service(interaction)
    updates_id = getattr(interaction.client, "bot_updates_channel_id", 0)
    if not updates_id:
        await interaction.response.send_message(
            t("biweekly_theme.cards.review.no_feed"), ephemeral=True
        )
        return
    try:
        clean_prompt = validate_title(prompt)
        clean_description = validate_description(description)
    except BiweeklyThemeError as exc:
        await interaction.response.send_message(
            t(exc.key, **exc.variables), ephemeral=True
        )
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            t("biweekly_theme.cards.suggest.closed"), ephemeral=True
        )
        return
    channel = await resolve_notify_channel(guild, updates_id)
    if channel is None:
        await interaction.response.send_message(
            t("biweekly_theme.cards.review.no_feed"), ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    embed = new_embed(interaction.client)
    embed.title = t("biweekly_theme.cards.review.title")
    embed.add_field(
        name=t("biweekly_theme.cards.review.prompt_field"),
        value=clean_prompt,
        inline=False,
    )
    if clean_description:
        embed.add_field(
            name=t("biweekly_theme.cards.review.description_field"),
            value=clean_description,
            inline=False,
        )
    embed.add_field(
        name=t("biweekly_theme.cards.review.suggester_field"),
        value=interaction.user.mention,
        inline=False,
    )
    set_transparent_image(embed, interaction.client)
    view = ThemeSuggestionReviewView()
    message = await channel.send(embed=embed, view=view)
    await service.add_suggestion(
        interaction.user.id,
        clean_prompt,
        clean_description,
        message.id,
    )
    await interaction.followup.send(
        t("biweekly_theme.cards.suggest.success"), ephemeral=True
    )


@edit_card.autocomplete("card")
@remove_card.autocomplete("card")
@force.autocomplete("card")
async def card_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    return await _card_autocomplete(interaction, current)
