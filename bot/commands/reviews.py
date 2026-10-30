"""Goodreads review commands (``/reviews ...``).

Permissions:
- Members manage their own feed; admins (Manage Server) may set anyone's.
- The posting channel and forced reposts are admin-only.
"""

from __future__ import annotations

import discord
from discord import app_commands

from ..services.booster_role import resolve_notify_channel
from ..services.members import resolve_members
from ..services.reviews import (
    ReviewService,
    is_goodreads_profile_url,
    normalize_entry_id,
    normalize_profile_url,
)
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


def _service(interaction: discord.Interaction) -> ReviewService:
    return interaction.client.reviews


@feed_group.command(name="add", description=t("reviews.feed.add.description"))
@app_commands.describe(
    url=t("reviews.feed.add.param_url"),
    user=t("reviews.feed.add.param_user"),
)
async def add_feed(
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

    if not is_goodreads_profile_url(url):
        await interaction.response.send_message(
            t("reviews.feed.add.invalid_url"), ephemeral=True
        )
        return

    profile_url = normalize_profile_url(url) or url.strip()
    _, changed = await _service(interaction).set_feed(target.id, profile_url)
    key = (
        "reviews.feed.add.success_reset"
        if changed
        else "reviews.feed.add.success"
    )
    await interaction.response.send_message(t(key, user=target.mention))


@feed_group.command(name="remove", description=t("reviews.feed.remove.description"))
@app_commands.describe(user=t("reviews.feed.remove.param_user"))
async def remove_feed(
    interaction: discord.Interaction,
    user: discord.User | None = None,
) -> None:
    target = user if user is not None else interaction.user
    if target.id != interaction.user.id and not is_admin(interaction):
        await interaction.response.send_message(
            t("components.permissions.no_permission"), ephemeral=True
        )
        return

    if await _service(interaction).remove_feed(target.id):
        await interaction.response.send_message(
            t("reviews.feed.remove.success", user=target.mention)
        )
    else:
        await interaction.response.send_message(
            t("reviews.feed.remove.not_found", user=target.mention),
            ephemeral=True,
        )


def _feed_embed(feed, member: discord.Member | None, client: discord.Client):
    embed = new_embed(client)
    name = member.display_name if member is not None else str(feed.discord_user_id)
    set_author(embed, member, name)
    embed.add_field(
        name=t("reviews.feed.list.url_field"),
        value=feed.feed_url,
        inline=False,
    )
    set_transparent_image(embed, client)
    return embed


@feed_group.command(name="list", description=t("reviews.feed.list.description"))
async def list_feeds(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    feeds = await _service(interaction).all_feeds()
    if not feeds:
        await interaction.followup.send(
            t("reviews.feed.list.empty"), ephemeral=True
        )
        return

    members = await resolve_members(
        interaction.guild, [feed.discord_user_id for feed in feeds]
    )
    embeds = [
        _feed_embed(feed, member, interaction.client)
        for feed, member in zip(feeds, members)
    ]
    await send_paginated(
        interaction,
        chunked(embeds, EMBEDS_PER_PAGE),
        total_results=len(feeds),
    )


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


@reviews_group.command(name="repost", description=t("reviews.repost.description"))
@app_commands.describe(
    entry=t("reviews.repost.param_entry"),
    user=t("reviews.repost.param_user"),
)
async def repost(
    interaction: discord.Interaction,
    entry: str,
    user: discord.User | None = None,
) -> None:
    if not await require_admin(interaction):
        return

    target = user if user is not None else interaction.user
    entry_id = normalize_entry_id(entry)
    await interaction.response.defer(ephemeral=True)

    service = _service(interaction)
    if not service.goodreads_session_cookie:
        await interaction.followup.send(
            t("reviews.repost.no_cookie"), ephemeral=True
        )
        return

    if not await service.force_repost(target.id, entry_id):
        await interaction.followup.send(
            t("reviews.repost.not_found", entry=entry_id), ephemeral=True
        )
        return

    channel = await resolve_notify_channel(interaction.guild, service.channel_id)
    if channel is None:
        await interaction.followup.send(
            t("reviews.repost.no_channel", entry=entry_id), ephemeral=True
        )
        return

    result = await service.repost_entry(
        interaction.guild,
        channel,
        interaction.client,
        discord_user_id=target.id,
        entry_id=entry_id,
    )
    if result.auth_failed:
        await interaction.followup.send(
            t("reviews.repost.auth_failed"), ephemeral=True
        )
        return
    if not result.posted:
        await interaction.followup.send(
            t("reviews.repost.not_found", entry=entry_id), ephemeral=True
        )
        return

    await interaction.followup.send(
        t("reviews.repost.success", user=target.mention, entry=entry_id),
        ephemeral=True,
    )
