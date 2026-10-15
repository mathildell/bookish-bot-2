"""Upcoming book release commands (``/releases ...``).

Permissions:
- Anyone may add, edit and search releases.
- Removing a release, and setting the announcement channel/role, are admin-only.
"""

from __future__ import annotations

from datetime import date
from math import ceil

import discord
from discord import app_commands

from ..services.release_sources import (
    BookData,
    BookScrapeError,
    book_data_from_dict,
    parse_source_url,
)
from ..services.releases import ReleaseService, build_book_embed
from ..services.members import resolve_members
from ..texts import t
from .checks import require_admin
from .listing import EMBEDS_PER_PAGE, Paginator, chunked, send_paginated

releases_group = app_commands.Group(
    name="releases",
    description=t("releases.group_description"),
)

channel_group = app_commands.Group(
    name="channel",
    description=t("releases.channel.group_description"),
    parent=releases_group,
)

role_group = app_commands.Group(
    name="role",
    description=t("releases.role.group_description"),
    parent=releases_group,
)


def _service(interaction: discord.Interaction) -> ReleaseService:
    return interaction.client.releases


async def _scrape(
    interaction: discord.Interaction, url: str
) -> BookData | None:
    """Fetch book data, replying with a friendly error on failure."""
    try:
        return await interaction.client.release_scraper.fetch(url.strip())
    except BookScrapeError as exc:
        await interaction.followup.send(
            t(exc.key, **exc.variables), ephemeral=True
        )
        return None


def _month_options(today: date) -> list[discord.SelectOption]:
    """One option per month for the next year, jumping to its first page."""
    options: list[discord.SelectOption] = []
    year, month = today.year, today.month
    for _ in range(12):
        label = t(
            "releases.list.month_option",
            month=t(f"releases.months.{month}"),
            year=year,
        )
        options.append(
            discord.SelectOption(
                label=label, value=date(year, month, 1).isoformat()
            )
        )
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return options


class _ReleaseListSource:
    """Lazy page source over the upcoming releases."""

    def __init__(
        self,
        service: ReleaseService,
        guild: discord.Guild | None,
        client: discord.Client,
        total: int,
    ) -> None:
        self._service = service
        self._guild = guild
        self._client = client
        self.total_results = total
        self.page_count = max(1, ceil(total / EMBEDS_PER_PAGE))

    async def get_page(self, index: int) -> list[discord.Embed]:
        books = await self._service.upcoming_page(
            index * EMBEDS_PER_PAGE, EMBEDS_PER_PAGE
        )
        members = await resolve_members(
            self._guild, [book.discord_user_id for book in books]
        )
        return [
            build_book_embed(book, member, self._client)
            for book, member in zip(books, members)
        ]

    async def page_index_for_date(self, target: date, per_page: int) -> int:
        return await self._service.page_index_for_date(target, per_page)


class _MonthSelect(discord.ui.Select):
    """Jumps the list to the page holding the first release of a month."""

    def __init__(self, options: list[discord.SelectOption]) -> None:
        super().__init__(
            placeholder=t("releases.list.jump_to_month"),
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        target = date.fromisoformat(self.values[0])
        index = await view.source.page_index_for_date(target, EMBEDS_PER_PAGE)
        view.set_index(index)
        await interaction.response.defer()
        page = await view.load_page(view.index)
        await view.message.edit(embeds=page, view=view)


class ReleaseListView(Paginator):
    """Paginated upcoming releases with a "jump to month" select."""

    def __init__(self, source: _ReleaseListSource, author_id: int) -> None:
        super().__init__(source=source, author_id=author_id)
        self.add_item(_MonthSelect(_month_options(date.today())))


class ReleaseConfirmView(discord.ui.View):
    """Confirm/cancel buttons for a pending ``/releases edit``."""

    def __init__(self, draft_id: int, author_id: int) -> None:
        super().__init__(timeout=None)
        self.draft_id = draft_id
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                t("components.not_author"), ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label=t("releases.edit.confirm"), style=discord.ButtonStyle.success
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        service = _service(interaction)
        draft = await service.get_draft(self.draft_id)
        if draft is None:
            await interaction.response.send_message(
                t("releases.edit.expired"), ephemeral=True
            )
            return
        book = await service.update(
            draft.book_id, book_data_from_dict(draft.data)
        )
        await service.delete_draft(self.draft_id)
        if book is None:
            await interaction.response.send_message(
                t("releases.edit.not_found"), ephemeral=True
            )
            return
        embed = build_book_embed(book, interaction.user, interaction.client)
        await interaction.response.edit_message(
            content=t(
                "releases.edit.updated",
                title=discord.utils.escape_markdown(book.title),
            ),
            embeds=[embed],
            view=None,
        )

    @discord.ui.button(
        label=t("releases.edit.cancel"), style=discord.ButtonStyle.secondary
    )
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await _service(interaction).delete_draft(self.draft_id)
        await interaction.response.edit_message(
            content=t("releases.edit.cancelled"), embeds=[], view=None
        )


@releases_group.command(name="add", description=t("releases.add.description"))
@app_commands.describe(url=t("releases.add.param_url"))
async def add_release(interaction: discord.Interaction, url: str) -> None:
    try:
        parse_source_url(url)
    except BookScrapeError as exc:
        await interaction.response.send_message(
            t(exc.key, **exc.variables), ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    data = await _scrape(interaction, url)
    if data is None:
        return
    if data.release_date <= date.today():
        await interaction.followup.send(
            t("releases.errors.not_in_future"), ephemeral=True
        )
        return

    service = _service(interaction)
    existing = await service.find_by_source(data.source, data.source_id)
    if existing is not None:
        await interaction.followup.send(
            t(
                "releases.errors.already_exists",
                title=discord.utils.escape_markdown(existing.title),
                user=f"<@{existing.discord_user_id}>",
                id=existing.id,
            ),
            ephemeral=True,
        )
        return

    book = await service.add(data, interaction.user.id)
    embed = build_book_embed(book, interaction.user, interaction.client)
    await interaction.followup.send(
        content=t(
            "releases.add.success",
            title=discord.utils.escape_markdown(book.title),
            id=book.id,
        ),
        embed=embed,
        ephemeral=True,
    )


@releases_group.command(name="edit", description=t("releases.edit.description"))
@app_commands.describe(
    url=t("releases.edit.param_url"),
    book_id=t("releases.edit.param_book_id"),
)
async def edit_release(
    interaction: discord.Interaction, url: str, book_id: int | None = None
) -> None:
    try:
        parse_source_url(url)
    except BookScrapeError as exc:
        await interaction.response.send_message(
            t(exc.key, **exc.variables), ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    data = await _scrape(interaction, url)
    if data is None:
        return

    service = _service(interaction)
    if book_id is not None:
        book = await service.get(book_id)
    else:
        book = await service.find_by_source(data.source, data.source_id)
        if book is None:
            matches = await service.search(data.title)
            book = matches[0] if matches else None
    if book is None:
        await interaction.followup.send(
            t("releases.edit.not_found"), ephemeral=True
        )
        return

    draft = await service.create_draft(book.id, interaction.user.id, data)
    old_embed = build_book_embed(
        book, interaction.user, interaction.client, prefix=t("releases.edit.old_prefix")
    )
    new_embed = build_book_embed(
        data,
        interaction.user,
        interaction.client,
        prefix=t("releases.edit.new_prefix"),
        book_id=book.id,
    )
    view = ReleaseConfirmView(draft.id, interaction.user.id)
    await interaction.followup.send(
        embeds=[old_embed, new_embed], view=view, ephemeral=True
    )


@releases_group.command(name="find", description=t("releases.find.description"))
@app_commands.describe(title=t("releases.find.param_title"))
async def find_release(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer(ephemeral=True)
    books = await _service(interaction).search(title)
    if not books:
        await interaction.followup.send(
            t(
                "releases.find.not_found",
                title=discord.utils.escape_markdown(title),
            ),
            ephemeral=True,
        )
        return
    members = await resolve_members(
        interaction.guild, [book.discord_user_id for book in books]
    )
    embeds = [
        build_book_embed(book, member, interaction.client)
        for book, member in zip(books, members)
    ]
    await send_paginated(
        interaction, chunked(embeds, EMBEDS_PER_PAGE), total_results=len(books)
    )


@releases_group.command(name="list", description=t("releases.list.description"))
async def list_releases(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    service = _service(interaction)
    total = await service.count_upcoming()
    if total == 0:
        await interaction.followup.send(
            t("releases.list.empty"), ephemeral=True
        )
        return

    source = _ReleaseListSource(
        service, interaction.guild, interaction.client, total
    )
    if source.page_count == 1:
        await interaction.followup.send(
            embeds=await source.get_page(0), ephemeral=True
        )
        return
    view = ReleaseListView(source, interaction.user.id)
    view.message = await interaction.followup.send(
        embeds=await view.load_page(0), view=view, ephemeral=True
    )


@releases_group.command(name="view", description=t("releases.view.description"))
@app_commands.describe(book_id=t("releases.view.param_book_id"))
async def view_release(interaction: discord.Interaction, book_id: int) -> None:
    await interaction.response.defer(ephemeral=True)
    book = await _service(interaction).get(book_id)
    if book is None:
        await interaction.followup.send(
            t("releases.view.not_found", id=book_id), ephemeral=True
        )
        return
    member = (
        await resolve_members(interaction.guild, [book.discord_user_id])
    )[0]
    await interaction.followup.send(
        embed=build_book_embed(book, member, interaction.client),
        ephemeral=True,
    )


@releases_group.command(name="remove", description=t("releases.remove.description"))
@app_commands.describe(book_id=t("releases.remove.param_book_id"))
async def remove_release(interaction: discord.Interaction, book_id: int) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    service = _service(interaction)
    book = await service.get(book_id)
    if book is None:
        await interaction.followup.send(
            t("releases.remove.not_found", id=book_id), ephemeral=True
        )
        return
    title = discord.utils.escape_markdown(book.title)
    await service.remove(book_id)
    await interaction.followup.send(
        t("releases.remove.success", title=title), ephemeral=True
    )


@channel_group.command(
    name="set", description=t("releases.channel.set.description")
)
@app_commands.describe(channel=t("releases.channel.set.param_channel"))
async def set_channel(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_channel(channel.id)
    await interaction.response.send_message(
        t("releases.channel.set.success", channel=channel.mention)
    )


@channel_group.command(
    name="view", description=t("releases.channel.view.description")
)
async def view_channel(interaction: discord.Interaction) -> None:
    service = _service(interaction)
    if not service.channel_id:
        await interaction.response.send_message(
            t("releases.channel.view.none"), ephemeral=True
        )
        return
    await interaction.response.send_message(
        t(
            "releases.channel.view.current",
            channel=f"<#{service.channel_id}>",
        ),
        ephemeral=True,
    )


@role_group.command(name="set", description=t("releases.role.set.description"))
@app_commands.describe(role=t("releases.role.set.param_role"))
async def set_role(
    interaction: discord.Interaction, role: discord.Role
) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_ping_role(role.id)
    await interaction.response.send_message(
        t("releases.role.set.success", role=role.mention)
    )


@role_group.command(name="clear", description=t("releases.role.clear.description"))
async def clear_role(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_ping_role(None)
    await interaction.response.send_message(
        t("releases.role.clear.success")
    )


@role_group.command(name="view", description=t("releases.role.view.description"))
async def view_role(interaction: discord.Interaction) -> None:
    service = _service(interaction)
    if not service.ping_role_id:
        await interaction.response.send_message(
            t("releases.role.view.none"), ephemeral=True
        )
        return
    await interaction.response.send_message(
        t(
            "releases.role.view.current",
            role=f"<@&{service.ping_role_id}>",
        ),
        ephemeral=True,
    )
