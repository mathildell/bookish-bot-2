"""Fun admin tool: edit a message the bot already sent (``/bot edit``).

Only the message content is replaced; its embeds, attachments and components
stay exactly as they are.
"""

from __future__ import annotations

import discord
from discord import app_commands

from ..texts import t
from .bot_say import (
    MESSAGE_LIMIT,
    fetch_linked_message,
    is_valid_message,
    render_message,
)
from .checks import require_admin


@app_commands.command(name="edit", description=t("bot.edit.description"))
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    link=t("bot.edit.param_link"),
    content=t("bot.edit.param_content"),
)
async def edit_command(
    interaction: discord.Interaction,
    link: str,
    content: str,
) -> None:
    if not await require_admin(interaction):
        return

    content = render_message(content)
    if not is_valid_message(content):
        await interaction.response.send_message(
            t("bot.edit.invalid_message", limit=MESSAGE_LIMIT), ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    target = await fetch_linked_message(interaction.guild, link)
    if target is None:
        await interaction.followup.send(t("bot.invalid_link"), ephemeral=True)
        return

    bot_user = interaction.client.user
    if bot_user is None or target.author.id != bot_user.id:
        await interaction.followup.send(
            t("bot.edit.not_own_message"), ephemeral=True
        )
        return

    try:
        await target.edit(content=content)
    except discord.HTTPException:
        await interaction.followup.send(
            t("bot.edit.edit_failed"), ephemeral=True
        )
        return

    await interaction.followup.send(
        t("bot.edit.success", link=target.jump_url), ephemeral=True
    )
