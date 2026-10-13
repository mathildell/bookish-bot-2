"""Reusable button pagination and listing helpers.

Used by the admin-only ``/booster react list`` and ``/booster role list``
commands. There is no ``discord.ext.pages`` in this discord.py build, so a small
button view is implemented here instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator, Sequence

import discord

from ..texts import t


def chunked(items: Sequence, size: int) -> Iterator[list]:
    """Yield ``items`` in lists of at most ``size`` elements."""
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


async def get_boosting_since(
    guild: discord.Guild | None, user_id: int
) -> datetime | None:
    """Return when ``user_id`` started boosting, or ``None`` if not boosting.

    Uses the single-member fetch, which does not require the privileged Members
    intent. Best-effort: returns ``None`` on any API failure.
    """
    if guild is None:
        return None
    try:
        member = await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None
    return member.premium_since


class Paginator(discord.ui.View):
    """Button pagination over a fixed list of embeds."""

    def __init__(
        self, pages: Sequence[discord.Embed], *, author_id: int, timeout: float = 120.0
    ) -> None:
        super().__init__(timeout=timeout)
        self.pages = list(pages)
        self.author_id = author_id
        self.index = 0
        self.message: discord.Message | None = None
        self._refresh()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                t("booster.pagination.not_author"), ephemeral=True
            )
            return False
        return True

    def _refresh(self) -> None:
        last = len(self.pages) - 1
        single = last <= 0
        self.first_page.disabled = single or self.index == 0
        self.previous_page.disabled = single or self.index == 0
        self.next_page.disabled = single or self.index == last
        self.last_page.disabled = single or self.index == last
        self.page_indicator.label = f"{self.index + 1}/{len(self.pages)}"

    async def _show(self, interaction: discord.Interaction) -> None:
        self._refresh()
        await interaction.response.edit_message(
            embed=self.pages[self.index], view=self
        )

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="«", style=discord.ButtonStyle.secondary)
    async def first_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = 0
        await self._show(interaction)

    @discord.ui.button(label="‹", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def page_indicator(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        pass

    @discord.ui.button(label="›", style=discord.ButtonStyle.secondary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._show(interaction)

    @discord.ui.button(label="»", style=discord.ButtonStyle.secondary)
    async def last_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = len(self.pages) - 1
        await self._show(interaction)


async def send_paginated(
    interaction: discord.Interaction, pages: Sequence[discord.Embed]
) -> None:
    """Send the first page (ephemeral) with pagination buttons if needed."""
    if len(pages) <= 1:
        await interaction.followup.send(embed=pages[0], ephemeral=True)
        return
    view = Paginator(pages, author_id=interaction.user.id)
    view.message = await interaction.followup.send(
        embed=pages[0], view=view, ephemeral=True
    )
