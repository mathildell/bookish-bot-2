"""Shared plumbing for the paginated booster list commands.

``/booster react list`` and ``/booster role list`` both resolve members, render
one embed per row, then chunk and paginate the result. This module owns that
shared logic along with the embed fragments (author header, boost date,
transparent background) that both listings display.
"""

from __future__ import annotations

import re
from typing import Iterable, Iterator, Protocol, Sequence

import discord

from ..texts import t

EMBEDS_PER_PAGE = 6
_HEX_RE = re.compile(r"^#?(?P<hex>[0-9a-fA-F]{6})$")


class PaginatedSource(Protocol):
    """Supplies embed pages on demand so the paginator can load lazily."""

    page_count: int
    total_results: int

    async def get_page(self, index: int) -> list[discord.Embed]:
        ...


def parse_hex_colour(value: str) -> discord.Colour:
    """Parse a hex colour string (``#rrggbb`` or ``rrggbb``)."""
    match = _HEX_RE.match(value.strip())
    if not match:
        raise ValueError(value)
    return discord.Colour(int(match.group("hex"), 16))


def new_embed(client: discord.Client) -> discord.Embed:
    """Create an embed using the configured default colour, if one is set."""
    return discord.Embed(colour=getattr(client, "default_embed_color", None))


def chunked(items: Sequence, size: int) -> Iterator[list]:
    """Yield ``items`` in lists of at most ``size`` elements."""
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


async def resolve_member(
    guild: discord.Guild | None, user_id: int
) -> discord.Member | None:
    """Return ``user_id``'s member object, or ``None`` if it can't be resolved.

    Prefers the local cache and falls back to the single-member fetch, which is
    not gated by the privileged Members intent. Best-effort: returns ``None`` on
    any API failure.
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


def set_author(
    embed: discord.Embed, member: discord.Member | None, name: str
) -> None:
    """Set the embed author to ``name``, adding the member's avatar if known."""
    escaped = discord.utils.escape_markdown(name)
    if member is not None:
        embed.set_author(name=escaped, icon_url=member.display_avatar.url)
    else:
        embed.set_author(name=escaped)


def boost_value(member: discord.Member | None) -> str:
    """Human-readable boost start date, or the "not boosting" text."""
    if member is not None and member.premium_since is not None:
        return f"<t:{int(member.premium_since.timestamp())}:f>"
    return t("booster.boost.not_boosting")


def set_transparent_image(embed: discord.Embed, client: discord.Client) -> None:
    """Apply the configured transparent background image, if any."""
    url = getattr(client, "transparent_embed_image", None)
    if url:
        embed.set_image(url=url)


class Paginator(discord.ui.View):
    """Button pagination over a fixed list of pages or a lazy page source.

    Pass ``pages`` (each a list of embeds) when everything is already built, or
    ``source`` to load each page only when the viewer navigates to it.
    """

    def __init__(
        self,
        pages: Sequence[Sequence[discord.Embed]] | None = None,
        *,
        source: PaginatedSource | None = None,
        author_id: int,
        timeout: float = 120.0,
        total_results: int | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.source = source
        self.author_id = author_id
        self.index = 0
        self.message: discord.Message | None = None
        self._cache: dict[int, list[discord.Embed]] = {}
        if source is not None:
            self.page_count = source.page_count
            self.total_results = source.total_results
        else:
            eager = [list(page) for page in pages or []]
            self._cache = dict(enumerate(eager))
            self.page_count = len(eager)
            self.total_results = total_results or sum(len(p) for p in eager)
        self._refresh()

    async def load_page(self, index: int) -> list[discord.Embed]:
        """Return a page, fetching it from the source the first time."""
        if index not in self._cache:
            self._cache[index] = list(await self.source.get_page(index))
        return self._cache[index]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                t("components.pagination.not_author"), ephemeral=True
            )
            return False
        return True

    def _refresh(self) -> None:
        last = self.page_count - 1
        single = last <= 0
        self.prev_button.disabled = single or self.index == 0
        self.next_button.disabled = single or self.index == last
        self.page_label.label = t(
            "components.pagination.page_x_of_y",
            current=self.index + 1,
            total=self.page_count,
            results=self.total_results,
        )

    async def _show(self, interaction: discord.Interaction) -> None:
        self._refresh()
        if self.source is not None and self.message is not None:
            await interaction.response.defer()
            page = await self.load_page(self.index)
            await self.message.edit(embeds=page, view=self)
        else:
            page = await self.load_page(self.index)
            await interaction.response.edit_message(embeds=page, view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label=t("components.pagination.prev"),
        emoji=t("components.pagination.prev_emoji"),
        style=discord.ButtonStyle.secondary,
    )
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(
        label=t("components.pagination.next"),
        emoji=t("components.pagination.next_emoji"),
        style=discord.ButtonStyle.secondary,
    )
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._show(interaction)

    @discord.ui.button(
        label=t("components.pagination.page_x_of_y", current=1, total=1, results=0),
        style=discord.ButtonStyle.secondary,
        disabled=True,
    )
    async def page_label(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        pass


async def send_paginated(
    interaction: discord.Interaction,
    pages: Iterable[Sequence[discord.Embed]] | None = None,
    total_results: int | None = None,
    *,
    source: PaginatedSource | None = None,
) -> None:
    """Send the first page (ephemeral), with pagination buttons if needed.

    Pass ``pages`` for fully-built pages, or ``source`` to load pages lazily.
    """
    if source is not None:
        if source.page_count == 1:
            await interaction.followup.send(
                embeds=await source.get_page(0), ephemeral=True
            )
            return
        view = Paginator(source=source, author_id=interaction.user.id)
        view.message = await interaction.followup.send(
            embeds=await view.load_page(0), view=view, ephemeral=True
        )
        return

    pages = [list(page) for page in pages or []]
    if not pages:
        return
    if len(pages) == 1:
        await interaction.followup.send(embeds=pages[0], ephemeral=True)
        return
    view = Paginator(
        pages, author_id=interaction.user.id, total_results=total_results
    )
    view.message = await interaction.followup.send(
        embeds=pages[0], view=view, ephemeral=True
    )
