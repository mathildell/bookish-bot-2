"""Birthday commands (``/birthday ...``).

Members set their own birthdays; admins configure the announcement channel,
the temporary role and the shared pools of messages and images.
"""

from __future__ import annotations

from datetime import timezone

import discord
from discord import app_commands

from ..services import birthdays as birthday_service
from ..services.booster_role import resolve_notify_channel
from ..services.level import utcnow
from ..services.members import resolve_members
from ..services.timezones import search_timezones
from ..texts import t
from .checks import is_admin, require_admin
from .listing import (
    EMBEDS_PER_PAGE,
    Paginator,
    chunked,
    new_embed,
    send_paginated,
    set_transparent_image,
)

birthday_group = app_commands.Group(
    name="birthday",
    description=t("birthday.group_description"),
)

channel_group = app_commands.Group(
    name="channel",
    description=t("birthday.channel.group_description"),
    parent=birthday_group,
)

role_group = app_commands.Group(
    name="role",
    description=t("birthday.role.group_description"),
    parent=birthday_group,
)

content_group = app_commands.Group(
    name="content",
    description=t("birthday.content.group_description"),
    parent=birthday_group,
)

MESSAGE = "message"
IMAGE = "image"



def _service(interaction: discord.Interaction) -> birthday_service.BirthdayService:
    return interaction.client.birthdays


def _error_text(exc: birthday_service.BirthdayError) -> str:
    return t(exc.key, **exc.variables)


async def date_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=label, value=value)
        for value, label in birthday_service.search_birthday_dates(current)
    ]


async def timezone_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=label, value=value)
        for value, label in search_timezones(current)
    ]


# --------------------------------------------------------------------- views


def _content_embed(
    client: discord.Client, kind: str, row
) -> discord.Embed:
    embed = new_embed(client)
    if kind == MESSAGE:
        embed.title = t("birthday.content.message_title", id=row.id)
        embed.description = row.content
    else:
        embed.title = t("birthday.content.image_title", id=row.id)
        embed.set_thumbnail(url=row.url)
        embed.add_field(
            name=t("birthday.content.url_field"),
            value=row.url,
            inline=False,
        )
    embed.add_field(
        name=t("birthday.content.linked_field"),
        value=(
            f"<@{row.user_id}>"
            if row.user_id is not None
            else t("birthday.content.everyone")
        ),
        inline=True,
    )

    set_transparent_image(embed, client)
    return embed


def _birthday_list_embed(
    client: discord.Client,
    birthday,
    member: discord.Member | None,
    now,
) -> discord.Embed:
    embed = new_embed(client)
    name = (
        discord.utils.escape_markdown(member.display_name)
        if member is not None
        else t("birthday.list.unknown_member", id=birthday.user_id)
    )
    if member is not None:
        embed.set_thumbnail(url=member.display_avatar.url)

    embed.title(
        t("birthday.list.birthday_name", name=name)
    )
    embed.add_field(
        name=t("birthday.list.date_title"),
        value=birthday_service.date_label(birthday.month, birthday.day),
        inline=True,
    )
    moment = birthday_service.next_birthday_at(birthday, now)
    embed.add_field(
        name=t("birthday.list.next_title"),
        value=discord.utils.format_dt(
            moment.replace(tzinfo=timezone.utc), "R"
        ),
        inline=True,
    )
    set_transparent_image(embed, client)
    return embed


def _empty_embed(client: discord.Client, kind: str) -> discord.Embed:
    embed = new_embed(client)
    embed.description = t(
        "birthday.content.empty_messages"
        if kind == MESSAGE
        else "birthday.content.empty_images"
    )
    set_transparent_image(embed, client)
    return embed


class ContentListView(Paginator):
    """One paginated pool with buttons to switch between messages and images."""

    def __init__(
        self,
        *,
        client: discord.Client,
        author_id: int,
        messages: list,
        images: list,
        kind: str,
        timeout: float = 120.0,
    ) -> None:
        self.client = client
        self.items = {MESSAGE: messages, IMAGE: images}
        self.kind = kind
        self._pages = {
            name: self._build_pages(name) for name in (MESSAGE, IMAGE)
        }
        super().__init__(
            self._pages[kind], author_id=author_id, timeout=timeout
        )

        self.messages_button = discord.ui.Button(
            label=t("birthday.content.messages_button"),
            style=discord.ButtonStyle.secondary,
            emoji=t("birthday.content.messages_button_emoji"),
            disabled=True
        )
        self.images_button = discord.ui.Button(
            label=t("birthday.content.images_button"),
            style=discord.ButtonStyle.secondary,
            emoji=t("birthday.content.images_button_emoji"),
        )
        self.messages_button.callback = self.show_messages
        self.images_button.callback = self.show_images
        self.add_item(self.messages_button)
        self.add_item(self.images_button)
        self._refresh()

    def _build_pages(self, kind: str) -> list[list[discord.Embed]]:
        rows = self.items[kind]
        if not rows:
            return [[_empty_embed(self.client, kind)]]
        return [
            [
                _content_embed(self.client, kind, row)
                for row in rows[start : start + EMBEDS_PER_PAGE]
            ]
            for start in range(0, len(rows), EMBEDS_PER_PAGE)
        ]

    async def load_page(self, index: int) -> list[discord.Embed]:
        return self._pages[self.kind][index]

    def _refresh(self) -> None:
        self.page_count = len(self._pages[self.kind])
        self.total_results = len(self.items[self.kind])
        super()._refresh()
        if not hasattr(self, "messages_button"):
            return
        self.messages_button.disabled = self.kind == MESSAGE
        self.images_button.disabled   = self.kind == IMAGE

    async def show_messages(self, interaction: discord.Interaction) -> None:
        self.kind = MESSAGE
        self.index = 0
        await self._show(interaction)

    async def show_images(self, interaction: discord.Interaction) -> None:
        self.kind = IMAGE
        self.index = 0
        await self._show(interaction)


async def _open_content_list(
    interaction: discord.Interaction,
    *,
    kind: str,
    user: discord.User | None,
    search: str | None,
) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    service = _service(interaction)
    user_id = user.id if user is not None else None
    messages = await service.list_messages(user_id=user_id, search=search)
    images = await service.list_images(user_id=user_id, search=search)
    view = ContentListView(
        client=interaction.client,
        author_id=interaction.user.id,
        messages=messages,
        images=images,
        kind=kind,
    )
    view.message = await interaction.followup.send(
        embeds=await view.load_page(0), view=view, ephemeral=True
    )


# ------------------------------------------------------------------ commands


@birthday_group.command(
    name="set", description=t("birthday.set.description")
)
@app_commands.describe(
    date=t("birthday.set.param_date"),
    timezone=t("birthday.set.param_timezone"),
    user=t("birthday.set.param_user"),
)
@app_commands.autocomplete(
    date=date_autocomplete, timezone=timezone_autocomplete
)
async def set_birthday(
    interaction: discord.Interaction,
    date: str,
    timezone: str | None = None,
    user: discord.User | None = None,
) -> None:
    if user is not None and not is_admin(interaction):
        await interaction.response.send_message(
            t("components.permissions.no_permission"), ephemeral=True
        )
        return
    target = user if user is not None else interaction.user
    try:
        month, day = birthday_service.parse_date_value(date)
        birthday = await _service(interaction).save_birthday(
            user_id=target.id,
            month=month,
            day=day,
            timezone=timezone,
        )
    except birthday_service.BirthdayError as exc:
        await interaction.response.send_message(
            _error_text(exc), ephemeral=True
        )
        return
    await interaction.response.send_message(
        t(
            "birthday.set.success",
            user=target.mention,
            date=birthday_service.date_label(
                birthday.month, birthday.day
            ),
            timezone=birthday.timezone,
        ),
        ephemeral=True,
    )


@birthday_group.command(
    name="remove", description=t("birthday.remove.description")
)
@app_commands.describe(user=t("birthday.remove.param_user"))
async def remove_birthday(
    interaction: discord.Interaction, user: discord.User | None = None
) -> None:
    if user is not None and not is_admin(interaction):
        await interaction.response.send_message(
            t("components.permissions.no_permission"), ephemeral=True
        )
        return
    target = user if user is not None else interaction.user
    removed = await _service(interaction).remove_birthday(target.id)
    await interaction.response.send_message(
        t(
            "birthday.remove.success"
            if removed
            else "birthday.remove.not_found",
            user=target.mention,
        ),
        ephemeral=True,
    )


@birthday_group.command(
    name="list", description=t("birthday.list.description")
)
async def list_birthdays(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            t("birthday.errors.no_guild"), ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    service = _service(interaction)
    rows = await service.list_birthdays()
    if not rows:
        await interaction.followup.send(
            t("birthday.list.empty"), ephemeral=True
        )
        return
    now = utcnow()
    rows.sort(
        key=lambda row: (
            birthday_service.next_birthday_at(row, now),
            row.month,
            row.day,
            row.user_id,
        )
    )
    members = await resolve_members(
        interaction.guild, [row.user_id for row in rows]
    )
    embeds = [
        _birthday_list_embed(interaction.client, row, member, now)
        for row, member in zip(rows, members)
    ]
    await send_paginated(
        interaction,
        chunked(embeds, EMBEDS_PER_PAGE),
        total_results=len(rows),
    )


@channel_group.command(
    name="set", description=t("birthday.channel.set.description")
)
@app_commands.describe(channel=t("birthday.channel.set.param_channel"))
async def set_channel(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_channel(channel.id)
    await interaction.response.send_message(
        t("birthday.channel.set.success", channel=channel.mention)
    )


@role_group.command(
    name="set", description=t("birthday.role.set.description")
)
@app_commands.describe(role=t("birthday.role.set.param_role"))
async def set_role(
    interaction: discord.Interaction, role: discord.Role
) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_role(role.id)
    await interaction.response.send_message(
        t("birthday.role.set.success", role=role.mention)
    )


@role_group.command(
    name="remove", description=t("birthday.role.remove.description")
)
async def remove_role(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await _service(interaction).set_role(None)
    await interaction.response.send_message(
        t("birthday.role.remove.success")
    )


@content_group.command(
    name="add", description=t("birthday.content.add.description")
)
@app_commands.describe(
    content=t("birthday.content.add.param_content"),
    user=t("birthday.content.add.param_user"),
)
async def add_content_cmd(
    interaction: discord.Interaction,
    content: str,
    user: discord.User | None = None,
) -> None:
    if not await require_admin(interaction):
        return
    try:
        row = await _service(interaction).add_content(
            content=content, user_id=user.id if user is not None else None
        )
    except birthday_service.BirthdayError as exc:
        await interaction.response.send_message(
            _error_text(exc), ephemeral=True
        )
        return
    # Determine type - BirthdayImage has url, BirthdayMessage has content
    is_img = hasattr(row, "url")
    if is_img:
        await interaction.response.send_message(
            t(
                "birthday.content.add.success_image",
                id=row.id,
                user=(
                    user.mention
                    if user is not None
                    else t("birthday.content.everyone")
                ),
            ),
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            t(
                "birthday.content.add.success_message",
                id=row.id,
                user=(
                    user.mention
                    if user is not None
                    else t("birthday.content.everyone")
                ),
            ),
            ephemeral=True,
        )



@content_group.command(
    name="remove", description=t("birthday.content.remove.description")
)
@app_commands.describe(id=t("birthday.content.remove.param_id"))
async def remove_content_cmd(
    interaction: discord.Interaction, id: int
) -> None:
    if not await require_admin(interaction):
        return
    removed = await _service(interaction).remove_content(id)
    await interaction.response.send_message(
        t(
            "birthday.content.remove.success"
            if removed
            else "birthday.content.remove.not_found",
            id=id,
        ),
        ephemeral=True,
    )


@content_group.command(
    name="list", description=t("birthday.content.list.description")
)
@app_commands.describe(
    user=t("birthday.content.param_user"),
    search=t("birthday.content.param_search"),
)
async def list_content_cmd(
    interaction: discord.Interaction,
    user: discord.User | None = None,
    search: str | None = None,
) -> None:
    # Default to showing both; reuse list view logic by showing messages first or just open combined - but existing view shows both via buttons
    await _open_content_list(
        interaction, kind=MESSAGE, user=user, search=search
    )


@content_group.command(
    name="edit", description=t("birthday.content.edit.description")
)
@app_commands.describe(
    id=t("birthday.content.edit.param_id"),
    content=t("birthday.content.edit.param_content"),
    user=t("birthday.content.edit.param_user"),
)
async def edit_content_cmd(
    interaction: discord.Interaction,
    id: int,
    content: str,
    user: discord.User | None = None,
) -> None:
    if not await require_admin(interaction):
        return
    try:
        result = await _service(interaction).edit_content(
            id, content=content, user_id=user.id if user is not None else None, clear_user=False
        )
        if result is None:
            await interaction.response.send_message(
                t("birthday.content.edit.not_found", id=id), ephemeral=True
            )
            return
        kind, row = result
        if kind == "image":
            await interaction.response.send_message(
                t("birthday.content.edit.success_image", id=row.id), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                t("birthday.content.edit.success_message", id=row.id), ephemeral=True
            )
    except birthday_service.BirthdayError as exc:
        await interaction.response.send_message(
            _error_text(exc), ephemeral=True
        )
        return



@birthday_group.command(
    name="force", description=t("birthday.force.description")
)
@app_commands.describe(user=t("birthday.force.param_user"))
async def force(
    interaction: discord.Interaction, user: discord.User
) -> None:
    if not await require_admin(interaction):
        return
    if interaction.guild is None:
        await interaction.response.send_message(
            t("birthday.errors.no_channel"), ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    service = _service(interaction)
    channel = await resolve_notify_channel(
        interaction.guild, service.channel_id or 0
    )
    if channel is None:
        await interaction.followup.send(
            t("birthday.errors.no_channel"), ephemeral=True
        )
        return
    try:
        await service.force(
            user_id=user.id,
            guild=interaction.guild,
            channel=channel,
            client=interaction.client,
        )
    except birthday_service.BirthdayError as exc:
        await interaction.followup.send(
            _error_text(exc), ephemeral=True
        )
        return
    await interaction.followup.send(
        t("birthday.force.success", user=user.mention), ephemeral=True
    )


# -------------------------------------------------------------- autocomplete


async def content_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    service = _service(interaction)
    messages = await service.list_messages()
    images = await service.list_images()
    rows = []
    # combine
    for r in messages:
        rows.append((r.id, "msg", getattr(r, "content", "")))
    for r in images:
        rows.append((r.id, "img", getattr(r, "url", "")))
    needle = (current or "").strip().lower()
    choices: list[app_commands.Choice[int]] = []
    for row_id, kind, content in rows:
        label = f"#{row_id} [{kind}] {content}"
        if needle and needle not in label.lower():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=row_id))
        if len(choices) >= 25:
            break
    return choices


remove_content_cmd.autocomplete("id")(content_autocomplete)
edit_content_cmd.autocomplete("id")(content_autocomplete)

