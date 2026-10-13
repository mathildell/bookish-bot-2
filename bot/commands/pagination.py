"""Reusable button pagination and listing helpers.

Used by the admin-only ``/booster react list`` and ``/booster role list``
commands. There is no ``discord.ext.pages`` in this discord.py build, so a small
button view is implemented here instead.
"""

from __future__ import annotations

from typing import Iterator, Sequence

import discord

from ..texts import t


def chunked(items: Sequence, size: int) -> Iterator[list]:
    """Yield ``items`` in lists of at most ``size`` elements."""
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


async def resolve_member(
    guild: discord.Guild | None, user_id: int
) -> discord.Member | None:
    """Return ``user_id``'s member object, or ``None`` if it can't be resolved.

    Prefers the local cache (populated when the Members intent is on) and falls
    back to the single-member fetch, which is not gated by the privileged
    Members intent. Best-effort: returns ``None`` on any API failure.
    """
    if guild is None:
        return None
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


class Paginator(discord.ui.View):
    """Button pagination over a fixed list of pages, each a list of embeds."""

    def __init__(
        self,
        pages: Sequence[Sequence[discord.Embed]],
        *,
        author_id: int,
        timeout: float = 120.0,
        total_results: int | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.pages = [list(page) for page in pages]
        self.author_id = author_id
        self.index = 0
        self.message: discord.Message | None = None
        self.total_results = total_results or sum(len(p) for p in self.pages)
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
        self.prev_button.disabled = single or self.index == 0
        self.next_button.disabled = single or self.index == last
        self.page_label.disabled = True
        self.page_label.label = t("components.pagination.page_x_of_y", n=f"{self.index + 1}", t=f"{len(self.pages)}", r=f"{self.total_results}")

    async def _show(self, interaction: discord.Interaction) -> None:
        self._refresh()
        await interaction.response.edit_message(
            embeds=self.pages[self.index], view=self
        )

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label=t("components.pagination.prev"), emoji="<:chevronPrev:1559682407987413145>", style=discord.ButtonStyle.secondary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(label=t("components.pagination.next"), emoji="<:chevronNext:1559682424940797992>", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._show(interaction)

    @discord.ui.button(
        label=t("components.pagination.page_x_of_y", n="1", t="1", r="0"),
        style=discord.ButtonStyle.secondary,
        disabled=True,
    )
    async def page_label(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        pass


async def send_paginated(
    interaction: discord.Interaction,
    pages: Sequence[Sequence[discord.Embed]],
    total_results: int | None = None,
) -> None:
    """Send the first page (ephemeral) with pagination buttons if needed."""
    if len(pages) <= 1:
        await interaction.followup.send(embeds=list(pages[0]), ephemeral=True)
        return
    view = Paginator(pages, author_id=interaction.user.id, total_results=total_results)
    view.message = await interaction.followup.send(
        embeds=list(pages[0]), view=view, ephemeral=True
    )
