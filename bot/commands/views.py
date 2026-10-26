from __future__ import annotations

import logging
from datetime import timedelta

import discord

from ..models import ViewMessage
from ..services.level import utcnow
from ..texts import t

logger = logging.getLogger(__name__)


class ExpiringView(discord.ui.View):
    """A timed view that disables its components when it stops listening."""

    message: discord.Message | None = None

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                logger.debug("Could not disable an expired view", exc_info=True)


def disabled_view(message: discord.Message) -> discord.ui.View | None:
    """A copy of ``message``'s components with every input disabled."""
    try:
        view = discord.ui.View.from_message(message, timeout=None)
    except (TypeError, ValueError):
        return None
    if not view.children:
        return None
    for child in view.children:
        child.disabled = True
    return view


async def disable_view_message(message: discord.Message) -> bool:
    """Disable the components on an already-fetched message."""
    view = disabled_view(message)
    if view is None:
        return False
    await message.edit(view=view)
    return True


async def expire_interaction(interaction: discord.Interaction) -> None:
    """Disable the clicked message and tell the user its buttons expired."""
    view = disabled_view(interaction.message)
    if view is not None:
        await interaction.response.edit_message(view=view)
    else:
        await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(t("components.expired"), ephemeral=True)


async def tracked_row(
    interaction: discord.Interaction,
) -> tuple[bool, ViewMessage | None]:
    """Return whether the clicked message is still tracked and unexpired."""
    client = getattr(interaction, "client", None)
    service = getattr(client, "view_messages", None)
    message = getattr(interaction, "message", None)
    if service is None or message is None:
        return True, None

    row = await service.get(message.id)
    if row is None or row.expires_at <= utcnow():
        await expire_interaction(interaction)
        if row is not None:
            await service.delete(message.id)
        return False, None
    return True, row


async def tracked_row_if_present(
    interaction: discord.Interaction,
) -> tuple[bool, ViewMessage | None]:
    """Like :func:`tracked_row`, but untracked messages are allowed.

    Persistent buttons such as streak rescue also appear on messages that are
    not part of a timed view, so a missing row must not disable them.
    """
    client = getattr(interaction, "client", None)
    service = getattr(client, "view_messages", None)
    message = getattr(interaction, "message", None)
    if service is None or message is None:
        return True, None

    row = await service.get(message.id)
    if row is not None and row.expires_at <= utcnow():
        await expire_interaction(interaction)
        await service.delete(message.id)
        return False, None
    return True, row


async def track_view(
    client: discord.Client,
    message: discord.Message,
    *,
    kind: str,
    lifetime: float,
    state: dict | None = None,
    author_id: int | None = None,
) -> None:
    """Record a public view so it can be restored and swept after a restart."""
    service = getattr(client, "view_messages", None)
    if service is None:
        return
    await service.register(
        channel_id=message.channel.id,
        message_id=message.id,
        kind=kind,
        expires_at=utcnow() + timedelta(seconds=lifetime),
        state=state,
        author_id=author_id,
    )


async def update_view_state(
    client: discord.Client | None, message_id: int, state: dict
) -> None:
    service = getattr(client, "view_messages", None)
    if service is not None:
        await service.update_state(message_id, state)


async def update_interaction_state(
    interaction: discord.Interaction, state: dict
) -> None:
    client = getattr(interaction, "client", None)
    message = getattr(interaction, "message", None)
    if client is None or message is None:
        return
    await update_view_state(client, message.id, state)


async def delete_view(
    client: discord.Client | None, message_id: int
) -> None:
    service = getattr(client, "view_messages", None)
    if service is not None:
        await service.delete(message_id)


async def delete_interaction_view(interaction: discord.Interaction) -> None:
    client = getattr(interaction, "client", None)
    message = getattr(interaction, "message", None)
    if client is None or message is None:
        return
    await delete_view(client, message.id)


def message_author_id(message: discord.Message) -> int | None:
    """The user who invoked the command that produced ``message``, if known."""
    metadata = getattr(message, "interaction_metadata", None)
    user = getattr(metadata, "user", None)
    if user is not None:
        return getattr(user, "id", None)
    interaction = getattr(message, "interaction", None)
    user = getattr(interaction, "user", None)
    if user is not None:
        return getattr(user, "id", None)
    return None
