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
    frames = _ROULETTE_FRAMES
    start = random.randint(0, len(frames) - 1)
    f1 = frames[start % len(frames)]
    f2 = frames[(start + 1) % len(frames)]
    f3 = frames[(start + 2) % len(frames)]
    f4 = frames[(start + 3) % len(frames)]

    await interaction.response.send_message(f"{f1}\n\n{t('randomize.wheel_turning')}")
    message = await interaction.original_response()

    await asyncio.sleep(1)
    await message.edit(content=f"{f2}\n\n{t('randomize.wheel_turning')}")

    await asyncio.sleep(1)
    await message.edit(content=f"{f3}\n\n{t('randomize.wheel_turning')}")

    await asyncio.sleep(1)
    await message.edit(content=f"{f4}\n\n{t('randomize.picking', item=winner)}")
