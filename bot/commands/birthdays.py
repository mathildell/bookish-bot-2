"""Birthday commands (``/birthday ...``).

Members set their own birthdays; admins configure the announcement channel,
the temporary role and the shared pools of messages and images.
"""

from __future__ import annotations

import discord
from discord import app_commands

from ..services import birthdays as birthday_service
from ..services.booster_role import resolve_notify_channel
from ..services.timezones import search_timezones
from ..texts import t
from .checks import is_admin, require_admin
from .listing import (
    EMBEDS_PER_PAGE,
    Paginator,
    new_embed,
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

messages_group = app_commands.Group(
    name="messages",
    description=t("birthday.messages.group_description"),
    parent=birthday_group,
)

images_group = app_commands.Group(
    name="images",
    description=t("birthday.images.group_description"),
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
        set_transparent_image(embed, client)
    else:
        embed.title = t("birthday.content.image_title", id=row.id)
        embed.set_image(url=row.url)
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
            style=discord.ButtonStyle.primary,
        )
        self.images_button = discord.ui.Button(
            label=t("birthday.content.images_button"),
            style=discord.ButtonStyle.secondary,
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
        self.messages_button.style = (
            discord.ButtonStyle.primary
            if self.kind == MESSAGE
            else discord.ButtonStyle.secondary
        )
        self.images_button.style = (
            discord.ButtonStyle.primary
            if self.kind == IMAGE
            else discord.ButtonStyle.secondary
        )

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


@messages_group.command(
    name="add", description=t("birthday.messages.add.description")
)
@app_commands.describe(
    message=t("birthday.messages.add.param_message"),
    user=t("birthday.messages.add.param_user"),
)
async def add_message(
    interaction: discord.Interaction,
    message: str,
    user: discord.User | None = None,
) -> None:
    if not await require_admin(interaction):
        return
    try:
        row = await _service(interaction).add_message(
            content=message, user_id=user.id if user is not None else None
        )
    except birthday_service.BirthdayError as exc:
        await interaction.response.send_message(
            _error_text(exc), ephemeral=True
        )
        return
    await interaction.response.send_message(
        t(
            "birthday.messages.add.success",
            id=row.id,
            user=(
                user.mention
                if user is not None
                else t("birthday.content.everyone")
            ),
        ),
        ephemeral=True,
    )


@messages_group.command(
    name="remove", description=t("birthday.messages.remove.description")
)
@app_commands.describe(message=t("birthday.messages.remove.param_message"))
async def remove_message(
    interaction: discord.Interaction, message: int
) -> None:
    if not await require_admin(interaction):
        return
    removed = await _service(interaction).remove_message(message)
    await interaction.response.send_message(
        t(
            "birthday.messages.remove.success"
            if removed
            else "birthday.messages.remove.not_found",
            id=message,
        ),
        ephemeral=True,
    )


@messages_group.command(
    name="list", description=t("birthday.messages.list.description")
)
@app_commands.describe(
    user=t("birthday.content.param_user"),
    search=t("birthday.content.param_search"),
)
async def list_messages(
    interaction: discord.Interaction,
    user: discord.User | None = None,
    search: str | None = None,
) -> None:
    await _open_content_list(
        interaction, kind=MESSAGE, user=user, search=search
    )


@images_group.command(
    name="add", description=t("birthday.images.add.description")
)
@app_commands.describe(
    url=t("birthday.images.add.param_url"),
    user=t("birthday.images.add.param_user"),
)
async def add_image(
    interaction: discord.Interaction,
    url: str,
    user: discord.User | None = None,
) -> None:
    if not await require_admin(interaction):
        return
    try:
        row = await _service(interaction).add_image(
            url=url, user_id=user.id if user is not None else None
        )
    except birthday_service.BirthdayError as exc:
        await interaction.response.send_message(
            _error_text(exc), ephemeral=True
        )
        return
    await interaction.response.send_message(
        t(
            "birthday.images.add.success",
            id=row.id,
            user=(
                user.mention
                if user is not None
                else t("birthday.content.everyone")
            ),
        ),
        ephemeral=True,
    )


@images_group.command(
    name="remove", description=t("birthday.images.remove.description")
)
@app_commands.describe(image=t("birthday.images.remove.param_image"))
async def remove_image(
    interaction: discord.Interaction, image: int
) -> None:
    if not await require_admin(interaction):
        return
    removed = await _service(interaction).remove_image(image)
    await interaction.response.send_message(
        t(
            "birthday.images.remove.success"
            if removed
            else "birthday.images.remove.not_found",
            id=image,
        ),
        ephemeral=True,
    )


@images_group.command(
    name="list", description=t("birthday.images.list.description")
)
@app_commands.describe(
    user=t("birthday.content.param_user"),
    search=t("birthday.content.param_search"),
)
async def list_images(
    interaction: discord.Interaction,
    user: discord.User | None = None,
    search: str | None = None,
) -> None:
    await _open_content_list(
        interaction, kind=IMAGE, user=user, search=search
    )


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


async def message_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    rows = await _service(interaction).list_messages()
    needle = (current or "").strip().lower()
    choices: list[app_commands.Choice[int]] = []
    for row in rows:
        label = f"#{row.id} {row.content}"
        if needle and needle not in label.lower():
            continue
        choices.append(
            app_commands.Choice(name=label[:100], value=row.id)
        )
        if len(choices) >= 25:
            break
    return choices


async def image_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    rows = await _service(interaction).list_images()
    needle = (current or "").strip().lower()
    choices: list[app_commands.Choice[int]] = []
    for row in rows:
        label = f"#{row.id} {row.url}"
        if needle and needle not in label.lower():
            continue
        choices.append(
            app_commands.Choice(name=label[:100], value=row.id)
        )
        if len(choices) >= 25:
            break
    return choices


remove_message.autocomplete("message")(message_autocomplete)
remove_image.autocomplete("image")(image_autocomplete)
