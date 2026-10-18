"""Fun admin tool: react to a message as the bot (``/bot react``).

Admins may add a single emoji reaction (custom or unicode) to a specific
message in a channel or thread.
"""

from __future__ import annotations

import discord
from discord import app_commands

from ..services.booster_react import parse_emoji
from ..texts import t
from .bot_say import parse_message_id
from .checks import require_admin


@app_commands.command(name="react", description=t("bot.react.description"))
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    channel=t("bot.react.param_channel"),
    message=t("bot.react.param_message"),
    emoji=t("bot.react.param_emoji"),
)
async def react_command(
    interaction: discord.Interaction,
    channel: discord.TextChannel | discord.Thread,
    message: str,
    emoji: str,
) -> None:
    if not await require_admin(interaction):
        return

    try:
        reaction = parse_emoji(emoji)
    except ValueError:
        await interaction.response.send_message(
            t("bot.react.invalid_emoji", emoji=emoji), ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    message_id = parse_message_id(message)
    target: discord.Message | None = None
    if message_id is not None:
        try:
            target = await channel.fetch_message(message_id)
        except discord.HTTPException:
            target = None
    if target is None:
        await interaction.followup.send(
            t("bot.react.invalid_message", channel=channel.mention), ephemeral=True
        )
        return

    try:
        await target.add_reaction(reaction)
    except discord.HTTPException:
        await interaction.followup.send(
            t("bot.react.react_failed", channel=channel.mention), ephemeral=True
        )
        return

    await interaction.followup.send(
        t("bot.react.success", emoji=emoji, link=target.jump_url), ephemeral=True
    )
