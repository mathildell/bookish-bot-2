"""Ticketing commands and the persistent panel/ticket buttons.

Admins configure a read-only channel and an admin role. Members open private
threads from the panel; the opener or a ticket admin can close and reopen them.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..services.booster_role import resolve_notify_channel
from ..services.level import utcnow
from ..services.tickets import (
    STATUS_CLOSED,
    STATUS_OPEN,
    Ticket,
    TicketService,
    can_reopen_with_button,
    resolved_thread_name,
    sticky_embed,
    summary_embed,
    thread_name,
    ticket_embed,
)
from ..texts import t
from .checks import is_admin, require_admin

logger = logging.getLogger("bh2.tickets")

OPEN_CUSTOM_ID = "tickets:open"
CLOSE_CUSTOM_ID = "tickets:close"
REOPEN_CUSTOM_ID = "tickets:reopen"

AUTO_ARCHIVE_MINUTES = 7 * 24 * 60

tickets_group = app_commands.Group(
    name="tickets",
    description=t("tickets.group_description"),
)

channel_group = app_commands.Group(
    name="channel",
    description=t("tickets.channel.group_description"),
    parent=tickets_group,
)

role_group = app_commands.Group(
    name="role",
    description=t("tickets.role.group_description"),
    parent=tickets_group,
)


def _service(interaction: discord.Interaction) -> TicketService:
    return interaction.client.tickets


async def resolve_ticket_channel(
    client: discord.Client, channel_id: int | None
) -> discord.TextChannel | None:
    if not channel_id:
        return None
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


async def ensure_sticky(
    client: discord.Client, channel: discord.TextChannel
) -> None:
    """Post the panel when the stored one is missing; never duplicate it."""
    service = client.tickets
    if service.sticky_message_id:
        try:
            await channel.fetch_message(service.sticky_message_id)
            return
        except discord.NotFound:
            pass
        except discord.HTTPException:
            return
    sent = await channel.send(embed=sticky_embed(client), view=OpenTicketView())
    await service.set_sticky(sent.id)


def _has_ticket_admin(
    interaction: discord.Interaction, service: TicketService
) -> bool:
    if is_admin(interaction):
        return True
    role_id = service.admin_role_id
    if not role_id:
        return False
    roles = getattr(interaction.user, "roles", None) or []
    return any(role.id == role_id for role in roles)


def _thread(interaction: discord.Interaction) -> discord.Thread | None:
    return (
        interaction.channel
        if isinstance(interaction.channel, discord.Thread)
        else None
    )


async def _respond(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def _defer(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)


async def _clear_command_progress(interaction: discord.Interaction) -> None:
    if interaction.type is not discord.InteractionType.application_command:
        return
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        logger.debug("Could not clear the ticket command response", exc_info=True)


async def _refresh_ticket_message(
    thread: discord.Thread, ticket: Ticket, view: discord.ui.View
) -> None:
    try:
        message = await thread.fetch_message(ticket.message_id)
        await message.edit(view=view)
    except discord.HTTPException:
        logger.warning(
            "Could not refresh ticket message %s", ticket.message_id, exc_info=True
        )


async def _announce_closed(
    client: discord.Client,
    guild: discord.Guild | None,
    ticket: Ticket,
    thread: discord.Thread,
) -> None:
    if guild is None:
        return
    channel = await resolve_notify_channel(
        guild, getattr(client, "bot_updates_channel_id", 0)
    )
    if channel is None:
        return
    try:
        await channel.send(embed=summary_embed(client, ticket, thread.jump_url))
    except discord.HTTPException:
        logger.warning("Could not announce closed ticket", exc_info=True)


async def _handle_open(interaction: discord.Interaction) -> None:
    service = _service(interaction)
    guild = interaction.guild
    channel = interaction.channel
    if (
        guild is None
        or not service.channel_id
        or not service.admin_role_id
        or not isinstance(channel, discord.TextChannel)
    ):
        await _respond(interaction, t("tickets.errors.not_configured"))
        return
    role = guild.get_role(service.admin_role_id)
    if role is None:
        await _respond(interaction, t("tickets.errors.not_configured"))
        return

    await _defer(interaction)
    async with service.open_lock:
        number = await service.next_number()
        name = thread_name(interaction.user.display_name, number)
        try:
            thread = await channel.create_thread(
                name=name,
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=AUTO_ARCHIVE_MINUTES,
                reason=t(
                    "tickets.audit.open",
                    number=number,
                    user=interaction.user.mention,
                ),
            )
        except discord.HTTPException:
            logger.warning("Could not create a ticket thread", exc_info=True)
            await interaction.followup.send(
                t("tickets.open.failed"), ephemeral=True
            )
            return

        try:
            message = await thread.send(
                content=f"{interaction.user.mention} {role.mention}",
                embed=ticket_embed(interaction.client, number),
                view=TicketCloseView(),
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=True
                ),
            )
        except discord.HTTPException:
            logger.warning("Could not post in the ticket thread", exc_info=True)
            try:
                await thread.delete()
            except discord.HTTPException:
                pass
            await interaction.followup.send(
                t("tickets.open.failed"), ephemeral=True
            )
            return

        try:
            await message.pin(reason=t("tickets.audit.pin", number=number))
        except discord.HTTPException:
            logger.warning(
                "Could not pin ticket message %s", message.id, exc_info=True
            )

        try:
            await service.create(
                number=number,
                opener_id=interaction.user.id,
                name=name,
                channel_id=channel.id,
                thread_id=thread.id,
                message_id=message.id,
                opened_at=utcnow(),
            )
        except Exception:
            logger.exception("Could not store the ticket")
            try:
                await thread.delete()
            except discord.HTTPException:
                pass
            await interaction.followup.send(
                t("tickets.open.failed"), ephemeral=True
            )
            return


async def _handle_close(
    interaction: discord.Interaction, *, allow_opener: bool
) -> None:
    service = _service(interaction)
    thread = _thread(interaction)
    if thread is None:
        await _respond(interaction, t("tickets.errors.not_thread"))
        return
    await _defer(interaction)
    ticket = await service.get_by_thread(thread.id)
    if ticket is None:
        await _respond(interaction, t("tickets.errors.not_ticket"))
        return
    allowed = _has_ticket_admin(interaction, service) or (
        allow_opener and interaction.user.id == ticket.opener_id
    )
    if not allowed:
        await _respond(interaction, t("tickets.errors.no_permission"))
        return
    if ticket.status == STATUS_CLOSED and thread.archived:
        await _respond(interaction, t("tickets.close.already_closed"))
        return

    reason = t("tickets.audit.close", number=ticket.number)
    try:
        await thread.edit(
            name=resolved_thread_name(ticket.name), locked=True, reason=reason
        )
    except discord.HTTPException:
        logger.warning("Could not lock ticket thread", exc_info=True)
        await interaction.followup.send(t("tickets.close.failed"), ephemeral=True)
        return

    updated = await service.close(ticket.id, utcnow())
    if updated is None:
        await interaction.followup.send(t("tickets.close.failed"), ephemeral=True)
        return

    await _refresh_ticket_message(thread, updated, TicketReopenView())
    try:
        await thread.edit(archived=True, reason=reason)
    except discord.HTTPException:
        logger.warning("Could not archive ticket thread", exc_info=True)
    await _announce_closed(interaction.client, interaction.guild, updated, thread)
    await _clear_command_progress(interaction)


async def _handle_reopen(
    interaction: discord.Interaction,
    *,
    allow_opener: bool,
    from_button: bool,
) -> None:
    service = _service(interaction)
    thread = _thread(interaction)
    if thread is None:
        await _respond(interaction, t("tickets.errors.not_thread"))
        return
    await _defer(interaction)
    ticket = await service.get_by_thread(thread.id)
    if ticket is None:
        await _respond(interaction, t("tickets.errors.not_ticket"))
        return
    allowed = _has_ticket_admin(interaction, service) or (
        allow_opener and interaction.user.id == ticket.opener_id
    )
    if not allowed:
        await _respond(interaction, t("tickets.errors.no_permission"))
        return
    if ticket.status == STATUS_OPEN:
        await _respond(interaction, t("tickets.reopen.already_open"))
        return
    if from_button and not can_reopen_with_button(ticket, utcnow()):
        await _respond(interaction, t("tickets.reopen.expired"))
        return

    reason = t("tickets.audit.reopen", number=ticket.number)
    try:
        await thread.edit(archived=False, reason=reason)
        await thread.edit(name=ticket.name, locked=False, reason=reason)
    except discord.HTTPException:
        logger.warning("Could not reopen ticket thread", exc_info=True)
        await interaction.followup.send(
            t("tickets.reopen.failed"), ephemeral=True
        )
        return

    updated = await service.reopen(ticket.id)
    if updated is None:
        await interaction.followup.send(
            t("tickets.reopen.failed"), ephemeral=True
        )
        return
    await _refresh_ticket_message(thread, updated, TicketCloseView())
    await _clear_command_progress(interaction)


class OpenTicketView(discord.ui.View):
    """Persistent button on the ticket panel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label=t("tickets.sticky.button"),
        emoji=t("tickets.sticky.button_emoji"),
        style=discord.ButtonStyle.secondary,
        custom_id=OPEN_CUSTOM_ID,
    )
    async def open_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await _handle_open(interaction)


class TicketCloseView(discord.ui.View):
    """Persistent Close button on an open ticket's message."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label=t("tickets.embed.close_button"),
        emoji=t("tickets.embed.close_button_emoji"),
        style=discord.ButtonStyle.danger,
        custom_id=CLOSE_CUSTOM_ID,
    )
    async def close_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await _handle_close(interaction, allow_opener=True)


class TicketReopenView(discord.ui.View):
    """Persistent Reopen button shown after a ticket is closed."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label=t("tickets.embed.reopen_button"),
        emoji=t("tickets.embed.reopen_button_emoji"),
        style=discord.ButtonStyle.success,
        custom_id=REOPEN_CUSTOM_ID,
    )
    async def reopen_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await _handle_reopen(
            interaction, allow_opener=True, from_button=True
        )


@channel_group.command(
    name="set", description=t("tickets.channel.set.description")
)
@app_commands.describe(channel=t("tickets.channel.set.param_channel"))
async def set_channel(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)

    service = _service(interaction)
    old_channel_id, old_sticky_id = await service.set_channel(channel.id)
    if old_sticky_id:
        old_channel = await resolve_ticket_channel(
            interaction.client, old_channel_id
        )
        if old_channel is not None:
            try:
                old = await old_channel.fetch_message(old_sticky_id)
                await old.delete()
            except discord.HTTPException:
                pass

    try:
        sent = await channel.send(
            embed=sticky_embed(interaction.client), view=OpenTicketView()
        )
    except discord.HTTPException:
        logger.warning("Could not post the ticket panel", exc_info=True)
        await interaction.followup.send(
            t("tickets.channel.set.failed", channel=channel.mention),
            ephemeral=True,
        )
        return
    await service.set_sticky(sent.id)
    await interaction.followup.send(
        t("tickets.channel.set.success", channel=channel.mention),
        ephemeral=True,
    )


@role_group.command(name="set", description=t("tickets.role.set.description"))
@app_commands.describe(role=t("tickets.role.set.param_role"))
async def set_role(
    interaction: discord.Interaction, role: discord.Role
) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_admin_role(role.id)
    await interaction.response.send_message(
        t("tickets.role.set.success", role=role.mention), ephemeral=True
    )


@tickets_group.command(name="close", description=t("tickets.close.description"))
async def close_ticket(interaction: discord.Interaction) -> None:
    await _handle_close(interaction, allow_opener=True)


@tickets_group.command(
    name="reopen", description=t("tickets.reopen.description")
)
async def reopen_ticket(interaction: discord.Interaction) -> None:
    await _handle_reopen(
        interaction, allow_opener=False, from_button=False
    )
