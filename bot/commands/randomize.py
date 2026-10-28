"""Fun utility: pick a random item from a list (``/randomize``)."""

from __future__ import annotations

import asyncio
import random

import discord
from discord import app_commands

from ..texts import t, t_list
from .views import ExpiringView, message_author_id, track_view, tracked_row

REROLL_CUSTOM_ID = "randomize:reroll"
REROLL_LIFETIME_SECONDS = 2 * 60 * 60
REROLL_VIEW_KIND = "randomize"

_ROULETTE_FRAMES = t_list("randomize.roulette_frames")


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
    def __init__(self, *, disabled: bool = False) -> None:
        super().__init__(
            label=t("randomize.reroll_label"),
            emoji=t("randomize.reroll_emoji"),
            style=discord.ButtonStyle.secondary,
            custom_id=REROLL_CUSTOM_ID,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, RandomizeRerollView):
            await view.reroll(interaction)
            return
        allowed, row = await tracked_row(interaction)
        if not allowed or row is None:
            return
        state = getattr(row, "state", None) or {}
        restored = RandomizeRerollView(
            state.get("items", ""),
            disabled=True,
            owner_id=state.get("owner_id"),
        )
        await restored.reroll(interaction)


class RandomizeRerollView(ExpiringView):
    def __init__(self, items_text: str = "", *, disabled: bool = False, owner_id: int | None = None) -> None:
        super().__init__(timeout=REROLL_LIFETIME_SECONDS)
        self.items_text = items_text
        self.owner_id = owner_id
        self.add_item(RandomizeRerollButton(disabled=disabled))

    def _set_reroll_disabled(self, disabled: bool) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button) and getattr(child, "custom_id") == REROLL_CUSTOM_ID:
                child.disabled = disabled

    async def reroll(self, interaction: discord.Interaction) -> None:
        client = getattr(interaction, "client", None)
        owner_id = getattr(self, "owner_id", None)
        if owner_id is None and interaction.message is not None:
            owner_id = message_author_id(interaction.message)
        if owner_id is not None and interaction.user.id != owner_id:
            await interaction.response.send_message(
                t("randomize.reroll_not_author"), ephemeral=True
            )
            return
        parsed = parse_items(self.items_text)
        if len(parsed) < 2:
            self._set_reroll_disabled(True)
            await interaction.response.edit_message(
                content=t("randomize.too_few"), view=self
            )
            return

        sequence, winner = _roulette_content(parsed)
        self._set_reroll_disabled(True)
        await interaction.response.edit_message(
            content=f"{sequence[0]}\n\n{t('randomize.wheel_turning')}", view=self
        )
        message = interaction.message
        if message is None:
            return
        try:
            await track_view(
                client,
                message,
                kind=REROLL_VIEW_KIND,
                lifetime=REROLL_LIFETIME_SECONDS,
                state={"items": self.items_text, "owner_id": owner_id},
            )
        except Exception:
            pass

        await asyncio.sleep(1)
        await message.edit(content=f"{sequence[1]}\n\n{t('randomize.wheel_turning')}")

        await asyncio.sleep(1)
        await message.edit(content=f"{sequence[2]}\n\n{t('randomize.wheel_turning')}")

        await asyncio.sleep(1)
        self._set_reroll_disabled(False)
        await message.edit(
            content=f"{sequence[3]}\n\n{t('randomize.picking', item=winner)}",
            view=self,
        )
        try:
            await track_view(
                client,
                message,
                kind=REROLL_VIEW_KIND,
                lifetime=REROLL_LIFETIME_SECONDS,
                state={"items": self.items_text, "owner_id": owner_id},
            )
        except Exception:
            pass


class RandomizeRerollFallbackView(discord.ui.View):
    """Persistent fallback for reroll button after a restart."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(RandomizeRerollButton(disabled=False))



@app_commands.command(name="randomize", description=t("randomize.description"))
@app_commands.describe(items=t("randomize.param_items"))
async def randomize_command(interaction: discord.Interaction, items: str) -> None:
    parsed = parse_items(items)
    if len(parsed) < 2:
        await interaction.response.send_message(t("randomize.too_few"), ephemeral=True)
        return

    owner_id = interaction.user.id
    sequence, winner = _roulette_content(parsed)
    view = RandomizeRerollView(items, disabled=True, owner_id=owner_id)
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
        state={"items": items, "owner_id": owner_id},
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
    view._set_reroll_disabled(False)
    await message.edit(
        content=f"{sequence[3]}\n\n{t('randomize.picking', item=winner)}", view=view
    )
    await track_view(
        interaction.client,
        message,
        kind=REROLL_VIEW_KIND,
        lifetime=REROLL_LIFETIME_SECONDS,
        state={"items": items, "owner_id": owner_id},
    )
