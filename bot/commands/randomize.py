"""Fun utility: pick a random item from a list (``/randomize``)."""

from __future__ import annotations

import asyncio
import random

import discord
from discord import app_commands

from ..texts import t
from .views import ExpiringView, track_view

REROLL_CUSTOM_ID = "randomize:reroll"
REROLL_LIFETIME_SECONDS = 2 * 60 * 60
REROLL_VIEW_KIND = "randomize"


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


def _roulette_content(parsed: list[str]) -> tuple[list[str], str]:
    frames = _ROULETTE_FRAMES
    start = random.randint(0, len(frames) - 1)
    f1 = frames[start % len(frames)]
    f2 = frames[(start + 1) % len(frames)]
    f3 = frames[(start + 2) % len(frames)]
    f4 = frames[(start + 3) % len(frames)]
    winner = random.choice(parsed)
    return [f1, f2, f3, f4], winner


class RandomizeRerollButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label=t("randomize.reroll_label"),
            emoji=t("randomize.reroll_emoji"),
            style=discord.ButtonStyle.secondary,
            custom_id=REROLL_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RandomizeRerollView):
            await interaction.response.defer(ephemeral=True)
            return
        await view.reroll(interaction)


class RandomizeRerollView(ExpiringView):
    def __init__(self, items_text: str) -> None:
        super().__init__(timeout=REROLL_LIFETIME_SECONDS)
        self.items_text = items_text
        self.add_item(RandomizeRerollButton())

    async def reroll(self, interaction: discord.Interaction) -> None:
        parsed = parse_items(self.items_text)
        if len(parsed) < 2:
            await interaction.response.edit_message(
                content=t("randomize.too_few"), view=None
            )
            self.stop()
            return

        sequence, winner = _roulette_content(parsed)
        await interaction.response.edit_message(
            content=f"{sequence[0]}\n\n{t('randomize.wheel_turning')}", view=self
        )
        message = interaction.message
        if message is None:
            return

        await asyncio.sleep(1)
        await message.edit(content=f"{sequence[1]}\n\n{t('randomize.wheel_turning')}")

        await asyncio.sleep(1)
        await message.edit(content=f"{sequence[2]}\n\n{t('randomize.wheel_turning')}")

        await asyncio.sleep(1)
        await message.edit(
            content=f"{sequence[3]}\n\n{t('randomize.picking', item=winner)}"
        )


@app_commands.command(name="randomize", description=t("randomize.description"))
@app_commands.describe(items=t("randomize.param_items"))
async def randomize_command(interaction: discord.Interaction, items: str) -> None:
    parsed = parse_items(items)
    if len(parsed) < 2:
        await interaction.response.send_message(t("randomize.too_few"), ephemeral=True)
        return

    sequence, winner = _roulette_content(parsed)
    view = RandomizeRerollView(items)
    await interaction.response.send_message(
        f"{sequence[0]}\n\n{t('randomize.wheel_turning')}", view=view
    )
    message = await interaction.original_response()
    view.message = message
    await track_view(
        interaction.client,
        message,
        kind=REROLL_VIEW_KIND,
        lifetime=REROLL_LIFETIME_SECONDS,
        state={"items": items},
    )

    await asyncio.sleep(1)
    await message.edit(
        content=f"{sequence[1]}\n\n{t('randomize.wheel_turning')}", view=view
    )

    await asyncio.sleep(1)
    await message.edit(
        content=f"{sequence[2]}\n\n{t('randomize.wheel_turning')}", view=view
    )

    await asyncio.sleep(1)
    await message.edit(
        content=f"{sequence[3]}\n\n{t('randomize.picking', item=winner)}", view=view
    )
