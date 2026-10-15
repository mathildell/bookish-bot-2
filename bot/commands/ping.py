"""Fun utility: check that the bot is awake (``/ping``)."""

from __future__ import annotations

import discord
from discord import app_commands

from ..texts import t


@app_commands.command(name="ping", description=t("ping.description"))
async def ping_command(interaction: discord.Interaction) -> None:
    latency = round(interaction.client.latency * 1000)
    await interaction.response.send_message(t("ping.response", latency=latency))
