"""Upcoming book release commands (``/releases ...``).

Permissions:
- Anyone may add, edit and search releases.
- Removing a release, and setting the announcement channel/role, are admin-only.
"""

from __future__ import annotations

import logging
from datetime import date
from math import ceil

import discord
from discord import app_commands

from ..models import BookRelease
from ..services.release_sources import (
    BookData,
    BookScrapeError,
    book_data_from_dict,
    parse_source_url,
)
from ..services.releases import (
    ReleaseService,
    build_book_embed,
    build_confirm_embed,
    disambiguate_embed_urls,
    format_plain_date,
    today_utc,
)
from ..services.members import resolve_members
from ..texts import t
from .checks import require_admin
from .listing import Paginator, chunked, send_paginated
from .views import (
    ExpiringView,
    message_author_id,
    track_view,
    tracked_row,
    update_interaction_state,
)

logger = logging.getLogger("bh2.releases")

RELEASE_EMBEDS_PER_PAGE = 5
RELEASE_LIST_LIFETIME_SECONDS = 120
RELEASE_LIST_VIEW_KIND = "release_list"
RELEASE_PREV_CUSTOM_ID = "releases:list:prev"
RELEASE_NEXT_CUSTOM_ID = "releases:list:next"
RELEASE_MONTH_CUSTOM_ID = "releases:list:month"
RELEASE_PAGE_CUSTOM_ID = "releases:list:page"
RELEASE_SEARCH_EMBEDS_PER_PAGE = 3

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

force_group = app_commands.Group(
    name="force",
    description=t("releases.force.group_description"),
    parent=releases_group,
)


def _service(interaction: discord.Interaction) -> ReleaseService:
    return interaction.client.releases


async def _resolve_book(
    service: ReleaseService, value: str
) -> BookRelease | None:
    """Look a book up by numeric id or by its handle."""
    value = value.strip()
    if value.isdigit():
        return await service.get(int(value))
    return await service.get_by_reference(value.lower())


def _announcement_channel(
    interaction: discord.Interaction, service: ReleaseService
) -> discord.abc.Messageable | None:
    if not service.channel_id:
        return None
    if interaction.guild is not None:
        channel = interaction.guild.get_channel(service.channel_id)
        if channel is not None:
            return channel
    return interaction.client.get_channel(service.channel_id)


def _added_thanks(
    service: ReleaseService, release_date: date
) -> str:
    """The public thank-you note posted after a book is added or updated."""
    channel = (
        f"<#{service.channel_id}>"
        if service.channel_id
        else t("releases.channel.unset")
    )
    return t(
        "releases.added_thanks",
        channel=channel,
        date=format_plain_date(release_date),
    )


def _already_exists_message(existing: BookRelease, user_id: int) -> str:
    """Private error shown when the same book is added a second time."""
    title = discord.utils.escape_markdown(existing.title)
    if existing.discord_user_id == user_id:
        return t(
            "releases.errors.already_exists_self",
            title=title,
            date=format_plain_date(existing.created_at.date(), year=False),
        )
    return t(
        "releases.errors.already_exists_other",
        title=title,
        member=f"<@{existing.discord_user_id}>",
        date=format_plain_date(existing.created_at.date(), year=False),
    )


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
    except Exception:
        logger.exception("Unexpected error scraping %s", url)
        await interaction.followup.send(
            t("releases.errors.scrape_failed"), ephemeral=True
        )
        return None


def _month_options(today: date) -> list[discord.SelectOption]:
    """One option per month for the next year, jumping to its first page."""
    options: list[discord.SelectOption] = []
    year, month = today.year, today.month
    for _ in range(12):
        label = t(
            "releases.list.month_option",
            month=t(f"components.months.{month}"),
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
        self.page_count = max(1, ceil(total / RELEASE_EMBEDS_PER_PAGE))

    async def get_page(self, index: int) -> list[discord.Embed]:
        books = await self._service.upcoming_page(
            index * RELEASE_EMBEDS_PER_PAGE, RELEASE_EMBEDS_PER_PAGE
        )
        members = await resolve_members(
            self._guild, [book.discord_user_id for book in books]
        )
        return [
            build_book_embed(book, member, self._client, include_blurb=False)
            for book, member in zip(books, members)
        ]

    async def page_index_for_date(self, target: date, per_page: int) -> int:
        return await self._service.page_index_for_date(target, per_page)


class _MonthSelect(discord.ui.Select):
    """Jumps the list to the page holding the first release of a month."""

    def __init__(
        self, options: list[discord.SelectOption], *, custom_id: str | None = None
    ) -> None:
        super().__init__(
            placeholder=t("releases.list.jump_to_month"),
            options=options,
            row=1,
            custom_id=custom_id,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        target = date.fromisoformat(self.values[0])
        jump = getattr(view, "jump_to_month", None)
        if jump is not None:
            await jump(interaction, target)
            return
        index = await view.source.page_index_for_date(
            target, RELEASE_EMBEDS_PER_PAGE
        )
        view.set_index(index)
        await interaction.response.defer()
        page = await view.load_page(view.index)
        await view.message.edit(embeds=page, view=view)
        persist = getattr(view, "_persist_page", None)
        if persist is not None:
            await persist(interaction)


class ReleaseListView(Paginator):
    """Paginated upcoming releases with a "jump to month" select."""

    def __init__(self, source: _ReleaseListSource, author_id: int) -> None:
        super().__init__(source=source, author_id=author_id)
        self.prev_button.custom_id = RELEASE_PREV_CUSTOM_ID
        self.next_button.custom_id = RELEASE_NEXT_CUSTOM_ID
        self.page_label.custom_id = RELEASE_PAGE_CUSTOM_ID
        self.add_item(
            _MonthSelect(
                _month_options(today_utc()), custom_id=RELEASE_MONTH_CUSTOM_ID
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed, row = await tracked_row(interaction)
        if not allowed:
            return False
        author_id = (
            getattr(row, "author_id", None)
            or message_author_id(interaction.message)
            or self.author_id
        )
        if interaction.user.id != author_id:
            await interaction.response.send_message(
                t("components.not_author"), ephemeral=True
            )
            return False
        return True

    async def _persist_page(self, interaction: discord.Interaction) -> None:
        await update_interaction_state(interaction, {"page": self.index})

    async def _show(self, interaction: discord.Interaction) -> None:
        await super()._show(interaction)
        await self._persist_page(interaction)


class ReleaseListFallbackView(discord.ui.View):
    """Persistent fallback for the release list's navigation controls."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            _MonthSelect(
                _month_options(today_utc()), custom_id=RELEASE_MONTH_CUSTOM_ID
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed, row = await tracked_row(interaction)
        if not allowed:
            return False
        author_id = (
            getattr(row, "author_id", None)
            or message_author_id(interaction.message)
        )
        if author_id is not None and interaction.user.id != author_id:
            await interaction.response.send_message(
                t("components.not_author"), ephemeral=True
            )
            return False
        return True

    async def _row_state(self, interaction: discord.Interaction) -> dict:
        client = getattr(interaction, "client", None)
        service = getattr(client, "view_messages", None)
        message = getattr(interaction, "message", None)
        row = (
            await service.get(message.id)
            if service is not None and message is not None
            else None
        )
        state = getattr(row, "state", None) if row is not None else None
        return state if isinstance(state, dict) else {}

    async def _render(self, interaction: discord.Interaction, index: int) -> None:
        service = _service(interaction)
        total = await service.count_upcoming()
        if total == 0:
            await interaction.response.defer()
            return
        source = _ReleaseListSource(
            service, interaction.guild, interaction.client, total
        )
        view = ReleaseListView(source, interaction.user.id)
        view.set_index(index)
        view.message = interaction.message
        page = await view.load_page(view.index)
        await update_interaction_state(interaction, {"page": view.index})
        await interaction.response.edit_message(embeds=page, view=view)

    async def _jump(self, interaction: discord.Interaction, index: int) -> None:
        await self._render(interaction, index)

    async def jump_to_month(
        self, interaction: discord.Interaction, target: date
    ) -> None:
        index = await _service(interaction).page_index_for_date(
            target, RELEASE_EMBEDS_PER_PAGE
        )
        await self._render(interaction, index)

    @discord.ui.button(
        label=t("components.pagination.prev"),
        emoji=t("components.pagination.prev_emoji"),
        style=discord.ButtonStyle.secondary,
        custom_id=RELEASE_PREV_CUSTOM_ID,
    )
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = await self._row_state(interaction)
        page = int(state.get("page", 0))
        await self._jump(interaction, max(0, page - 1))

    @discord.ui.button(
        label=t("components.pagination.next"),
        emoji=t("components.pagination.next_emoji"),
        style=discord.ButtonStyle.secondary,
        custom_id=RELEASE_NEXT_CUSTOM_ID,
    )
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = await self._row_state(interaction)
        page = int(state.get("page", 0))
        total = await _service(interaction).count_upcoming()
        page_count = max(1, ceil(total / RELEASE_EMBEDS_PER_PAGE))
        await self._jump(interaction, min(page_count - 1, page + 1))


class ReleaseConfirmView(ExpiringView):
    """Confirm/cancel buttons for a pending ``/releases update``."""

    def __init__(self, draft_id: int, author_id: int) -> None:
        super().__init__(timeout=15 * 60)
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
        label=t("releases.update.confirm"),
        emoji=t("releases.update.confirm_emoji"),
        style=discord.ButtonStyle.success,
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        service = _service(interaction)
        draft = await service.get_draft(self.draft_id)
        if draft is None:
            await interaction.response.send_message(
                t("releases.update.expired"), ephemeral=True
            )
            return
        book = await service.update(
            draft.book_id, book_data_from_dict(draft.data)
        )
        await service.delete_draft(self.draft_id)
        if book is None:
            await interaction.response.send_message(
                t("releases.update.not_found"), ephemeral=True
            )
            return
        embed = build_book_embed(book, interaction.user, interaction.client)
        # Keep the confirmation prompt private, but post the updated book for
        # everyone to see.
        await interaction.response.edit_message(
            content=t("releases.update.confirmed"), embeds=[], view=None
        )
        await interaction.followup.send(
            content=_added_thanks(service, book.release_date), embeds=[embed]
        )
        self.stop()

    @discord.ui.button(
        label=t("releases.update.cancel"), style=discord.ButtonStyle.secondary
    )
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await _service(interaction).delete_draft(self.draft_id)
        await interaction.response.edit_message(
            content=t("releases.update.cancelled"), embeds=[], view=None
        )
        self.stop()


@releases_group.command(name="add", description=t("releases.add.description"))
@app_commands.describe(url=t("releases.add.param_url"))
async def add_release(interaction: discord.Interaction, url: str) -> None:
    try:
        source, source_id = parse_source_url(url)
    except BookScrapeError as exc:
        await interaction.response.send_message(
            t(exc.key, **exc.variables), ephemeral=True
        )
        return

    service = _service(interaction)
    existing = await service.find_by_source(source, source_id)
    if existing is not None:
        await interaction.response.send_message(
            _already_exists_message(existing, interaction.user.id),
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    data = await _scrape(interaction, url)
    if data is None:
        return
    if data.release_date <= today_utc():
        await interaction.followup.send(
            t("releases.errors.not_in_future"), ephemeral=True
        )
        return

    book = await service.add(data, interaction.user.id)
    embed = build_book_embed(book, interaction.user, interaction.client)
    await interaction.followup.send(
        content=_added_thanks(service, book.release_date), embed=embed
    )


@releases_group.command(
    name="update", description=t("releases.update.description")
)
@app_commands.describe(
    url=t("releases.update.param_url"),
    book=t("releases.update.param_book"),
)
async def update_release(
    interaction: discord.Interaction, url: str, book: str | None = None
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
    if data.release_date <= today_utc():
        await interaction.followup.send(
            t("releases.errors.not_in_future"), ephemeral=True
        )
        return

    service = _service(interaction)
    if book is not None:
        target = await _resolve_book(service, book)
    else:
        target = await service.find_by_source(data.source, data.source_id)
        if target is None:
            matches = await service.search(data.title)
            target = matches[0] if matches else None
    if target is None:
        await interaction.followup.send(
            t("releases.update.not_found"), ephemeral=True
        )
        return

    draft = await service.create_draft(target.id, interaction.user.id, data)
    old_embed = build_book_embed(
        target,
        interaction.user,
        interaction.client,
        prefix=t("releases.update.old_prefix"),
    )
    new_embed = build_book_embed(
        data,
        interaction.user,
        interaction.client,
        prefix=t("releases.update.new_prefix"),
    )
    disambiguate_embed_urls([old_embed, new_embed])
    instruction_embed = build_confirm_embed(interaction.client)
    view = ReleaseConfirmView(draft.id, interaction.user.id)
    view.message = await interaction.followup.send(
        embeds=[old_embed, new_embed, instruction_embed],
        view=view,
        ephemeral=True,
    )


@releases_group.command(
    name="search", description=t("releases.search.description")
)
@app_commands.describe(keyword=t("releases.search.param_keyword"))
async def search_release(
    interaction: discord.Interaction, keyword: str
) -> None:
    await interaction.response.defer(ephemeral=True)
    books = await _service(interaction).search(keyword)
    if not books:
        await interaction.followup.send(
            t(
                "releases.search.not_found",
                keyword=discord.utils.escape_markdown(keyword),
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
        interaction,
        chunked(embeds, RELEASE_SEARCH_EMBEDS_PER_PAGE),
        total_results=len(books),
        content=t("releases.search.found"),
    )


@releases_group.command(name="list", description=t("releases.list.description"))
async def list_releases(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    service = _service(interaction)
    total = await service.count_upcoming()
    if total == 0:
        await interaction.followup.send(t("releases.list.empty"))
        return

    source = _ReleaseListSource(
        service, interaction.guild, interaction.client, total
    )
    view = ReleaseListView(source, interaction.user.id)
    view.message = await interaction.followup.send(
        embeds=await view.load_page(0), view=view
    )
    await track_view(
        interaction.client,
        view.message,
        kind=RELEASE_LIST_VIEW_KIND,
        lifetime=RELEASE_LIST_LIFETIME_SECONDS,
        state={"page": 0},
        author_id=interaction.user.id,
    )


@releases_group.command(name="view", description=t("releases.view.description"))
@app_commands.describe(book=t("releases.view.param_book"))
async def view_release(interaction: discord.Interaction, book: str) -> None:
    await interaction.response.defer(ephemeral=True)
    target = await _resolve_book(_service(interaction), book)
    if target is None:
        await interaction.followup.send(
            t("releases.view.not_found", query=book), ephemeral=True
        )
        return
    member = (
        await resolve_members(interaction.guild, [target.discord_user_id])
    )[0]
    await interaction.followup.send(
        embed=build_book_embed(target, member, interaction.client),
        ephemeral=True,
    )


@releases_group.command(name="remove", description=t("releases.remove.description"))
@app_commands.describe(book=t("releases.remove.param_book"))
async def remove_release(interaction: discord.Interaction, book: str) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    service = _service(interaction)
    target = await _resolve_book(service, book)
    if target is None:
        await interaction.followup.send(
            t("releases.remove.not_found", query=book), ephemeral=True
        )
        return
    title = discord.utils.escape_markdown(target.title)
    await service.remove(target.id)
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


@force_group.command(
    name="daily", description=t("releases.force.daily.description")
)
async def force_daily(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    service = _service(interaction)
    channel = _announcement_channel(interaction, service)
    if channel is None:
        await interaction.followup.send(
            t("releases.force.no_channel"), ephemeral=True
        )
        return
    count = await service.force_post_daily(
        interaction.guild, channel, interaction.client
    )
    await interaction.followup.send(
        t(
            "releases.force.daily.success"
            if count
            else "releases.force.daily.none",
            count=count,
        ),
        ephemeral=True,
    )


@force_group.command(
    name="monthly", description=t("releases.force.monthly.description")
)
async def force_monthly(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    service = _service(interaction)
    channel = _announcement_channel(interaction, service)
    if channel is None:
        await interaction.followup.send(
            t("releases.force.no_channel"), ephemeral=True
        )
        return
    count = await service.force_post_monthly(
        interaction.guild, channel, interaction.client
    )
    await interaction.followup.send(
        t(
            "releases.force.monthly.success"
            if count
            else "releases.force.monthly.none",
            count=count,
        ),
        ephemeral=True,
    )
