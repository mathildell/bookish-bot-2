"""Embed construction shared by commands, services and scheduled tasks."""

from __future__ import annotations

import discord


def new_embed(client: discord.Client, **kwargs) -> discord.Embed:
    """Create an embed using the configured default colour, if one is set."""
    return discord.Embed(
        colour=getattr(client, "default_embed_color", None), **kwargs
    )


def set_transparent_image(embed: discord.Embed, client: discord.Client) -> None:
    """Apply the configured transparent background image, if any."""
    url = getattr(client, "transparent_embed_image", None)
    if url:
        embed.set_image(url=url)
