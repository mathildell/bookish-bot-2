"""Goodreads review commands (``/reviews ...``).

Permissions:
- Members manage their own feed; admins (Manage Server) may set anyone's.
- The posting channel and cooldown resets are admin-only.
"""

from __future__ import annotations

import discord
from discord import app_commands

from ..services.reviews import (
    ReviewService,
    is_goodreads_feed_url,
    normalize_entry_id,
)
from ..texts import t
from .checks import is_admin, require_admin

reviews_group = app_commands.Group(
    name="reviews",
    description=t("reviews.group_description"),
)

feed_group = app_commands.Group(
    name="feed",
    description=t("reviews.feed.group_description"),
    parent=reviews_group,
)

channel_group = app_commands.Group(
    name="channel",
    description=t("reviews.channel.group_description"),
    parent=reviews_group,
)

cooldown_group = app_commands.Group(
    name="cooldown",
    description=t("reviews.cooldown.group_description"),
    parent=reviews_group,
)


def _service(interaction: discord.Interaction) -> ReviewService:
    return interaction.client.reviews


@feed_group.command(name="set", description=t("reviews.feed.set.description"))
@app_commands.describe(
    url=t("reviews.feed.set.param_url"),
    user=t("reviews.feed.set.param_user"),
)
async def set_feed(
    interaction: discord.Interaction,
    url: str,
    user: discord.User | None = None,
) -> None:
    target = user if user is not None else interaction.user
    if target.id != interaction.user.id and not is_admin(interaction):
        await interaction.response.send_message(
            t("components.permissions.no_permission"), ephemeral=True
        )
        return

    if not is_goodreads_feed_url(url):
        await interaction.response.send_message(
            t("reviews.feed.set.invalid_url"), ephemeral=True
        )
        return

    _, changed = await _service(interaction).set_feed(target.id, url.strip())
    key = (
        "reviews.feed.set.success_reset"
        if changed
        else "reviews.feed.set.success"
    )
    await interaction.response.send_message(t(key, user=target.mention))


@channel_group.command(name="set", description=t("reviews.channel.set.description"))
@app_commands.describe(channel=t("reviews.channel.set.param_channel"))
async def set_channel(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_channel(channel.id)
    await interaction.response.send_message(
        t("reviews.channel.set.success", channel=channel.mention)
    )


@cooldown_group.command(name="reset", description=t("reviews.cooldown.reset.description"))
@app_commands.describe(
    entry=t("reviews.cooldown.reset.param_entry"),
    user=t("reviews.cooldown.reset.param_user"),
)
async def reset_cooldown(
    interaction: discord.Interaction,
    entry: str,
    user: discord.User | None = None,
) -> None:
    if not await require_admin(interaction):
        return

    target = user if user is not None else interaction.user
    entry_id = normalize_entry_id(entry)
    if await _service(interaction).reset_cooldown(target.id, entry_id):
        await interaction.response.send_message(
            t(
                "reviews.cooldown.reset.success",
                user=target.mention,
                entry=entry_id,
            ),
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            t("reviews.cooldown.reset.not_found", entry=entry_id),
            ephemeral=True,
        )
