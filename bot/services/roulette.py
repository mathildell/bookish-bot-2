"""The roulette animation shared by ``/randomize`` and the theme draw."""

from __future__ import annotations

import asyncio
import random

import discord

from ..texts import t, t_list

ROULETTE_FRAMES = t_list("randomize.roulette_frames")
FRAME_DELAY_SECONDS = 1


def roulette_content(
    items: list[str], winner: str | None = None
) -> tuple[list[str], str]:
    """Four consecutive animation frames and the chosen item.

    The item is random unless the caller already decided ``winner``.
    """
    frames = ROULETTE_FRAMES
    start = random.randint(0, len(frames) - 1)
    sequence = [frames[(start + offset) % len(frames)] for offset in range(4)]
    return sequence, winner if winner is not None else random.choice(items)


def spinning_content(frame: str) -> str:
    return f"{frame}\n\n{t('randomize.wheel_turning')}"


def result_content(frame: str, item: str) -> str:
    return f"{frame}\n\n{t('randomize.picking', item=item)}"


async def spin_roulette(
    channel: discord.abc.Messageable,
    items: list[str],
    *,
    winner: str | None = None,
) -> tuple[discord.Message, str]:
    """Post the animated wheel in ``channel`` and return it with the winner."""
    sequence, winner = roulette_content(items, winner)
    message = await channel.send(spinning_content(sequence[0]))
    for frame in sequence[1:3]:
        await asyncio.sleep(FRAME_DELAY_SECONDS)
        await message.edit(content=spinning_content(frame))
    await asyncio.sleep(FRAME_DELAY_SECONDS)
    await message.edit(content=result_content(sequence[3], winner))
    return message, winner
