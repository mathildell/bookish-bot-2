"""Fun utility: pick a random item from a list (``/randomize``)."""

from __future__ import annotations

import asyncio
import random

import discord
from discord import app_commands

from ..texts import t


def parse_items(value: str) -> list[str]:
    """Split a comma-separated list, trimming blanks."""
    if value is None:
        return []
    items: list[str] = []
    for part in value.split(","):
        item = part.strip()
        if item:
            items.append(item)
    return items


_ROULETTE_FRAMES = [
    "🔳 🔳 ⬜\n🔳 ↗️ 🔳\n🔳 🔳 🔳",
    "🔳 🔳 🔳\n🔳 ➡️ ⬜\n🔳 🔳 🔳",
    "🔳 🔳 🔳\n🔳 ↘️ 🔳\n🔳 🔳 ⬜",
    "🔳 🔳 🔳\n🔳 ⬇️ 🔳\n🔳 ⬜ 🔳",
    "🔳 🔳 🔳\n🔳 ↙️ 🔳\n⬜ 🔳 🔳",
    "🔳 🔳 🔳\n⬜ ⬅️ 🔳\n🔳 🔳 🔳",
    "⬜ 🔳 🔳\n🔳 ↖️ 🔳\n🔳 🔳 🔳",
    "🔳 ⬜ 🔳\n🔳 ⬆️ 🔳\n🔳 🔳 🔳",
]


@app_commands.command(name="randomize", description=t("randomize.description"))
@app_commands.describe(items=t("randomize.param_items"))
async def randomize_command(interaction: discord.Interaction, items: str) -> None:
    parsed = parse_items(items)
    if len(parsed) < 2:
        await interaction.response.send_message(t("randomize.too_few"), ephemeral=True)
        return

    winner = random.choice(parsed)
    animation_frames = random.sample(_ROULETTE_FRAMES, k=3)
    await interaction.response.send_message(animation_frames[0])
    message = await interaction.original_response()

    await asyncio.sleep(1)
    await message.edit(content=animation_frames[1])

    await asyncio.sleep(1)
    await message.edit(content=animation_frames[2])

    await asyncio.sleep(1)
    final_frame = random.choice(_ROULETTE_FRAMES)
    await message.edit(content=f"{final_frame}\n\n{t('randomize.result', item=winner)}")
