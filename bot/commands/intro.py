"""Introduction form commands (``/intro ...``) and the sticky button/modal.

Admins configure the intro channel and manage the questions. Members use the
sticky message's button to post or edit their own introduction.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..services.intro import (
    FIELD_VALUE_LIMIT,
    IntroService,
    build_intro_embed,
    select_modal_questions,
)
from ..services.level import utcnow
from ..texts import t
from .checks import require_admin
from .listing import new_embed, set_transparent_image

logger = logging.getLogger("bh2.intro")

intro_group = app_commands.Group(
    name="intro",
    description=t("intro.group_description"),
)

channel_group = app_commands.Group(
    name="channel",
    description=t("intro.channel.group_description"),
    parent=intro_group,
)

questions_group = app_commands.Group(
    name="questions",
    description=t("intro.questions.group_description"),
    parent=intro_group,
)

# Discord caps modal text inputs and field labels.
LABEL_LIMIT = 45
QUESTION_CONTENT_LIMIT = 200
PLACEHOLDER_LIMIT = 200
QUESTION_SELECT_LIMIT = 25


def _service(interaction: discord.Interaction) -> IntroService:
    return interaction.client.intros


async def resolve_intro_channel(
    client: discord.Client, channel_id: int | None
) -> discord.TextChannel | None:
    if not channel_id:
        return None
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


def sticky_embed(client: discord.Client) -> discord.Embed:
    """The recurring sticky message posted in the intro channel."""
    embed = new_embed(client)
    embed.description = t("intro.sticky.text")
    set_transparent_image(embed, client)
    return embed


class IntroduceView(discord.ui.View):
    """Persistent button on the sticky message that opens the intro modal."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label=t("intro.sticky.button"),
        style=discord.ButtonStyle.primary,
        custom_id="intro:open",
    )
    async def open_intro(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        service = _service(interaction)
        if not service.channel_id:
            await interaction.response.send_message(
                t("intro.errors.no_channel"), ephemeral=True
            )
            return
        questions = select_modal_questions(await service.active_questions())
        if not questions:
            await interaction.response.send_message(
                t("intro.errors.no_questions"), ephemeral=True
            )
            return
        post = await service.get_post(interaction.user.id)
        answers = dict(post.answers) if post is not None else {}
        await interaction.response.send_modal(
            IntroModal(service, questions, answers, post)
        )


class IntroModal(discord.ui.Modal):
    """The introduction form: one paragraph input per active question."""

    def __init__(
        self,
        service: IntroService,
        questions: list,
        answers: dict,
        post,
    ) -> None:
        super().__init__(title=t("intro.modal.title"))
        self.service = service
        self.questions = questions
        self.post = post
        for question in questions:
            existing = answers.get(str(question.id)) or ""
            self.add_item(
                discord.ui.TextInput(
                    label=question.content[:LABEL_LIMIT],
                    custom_id=str(question.id),
                    placeholder=(question.placeholder or None),
                    default=existing,
                    required=False,
                    style=discord.TextStyle.paragraph,
                    max_length=FIELD_VALUE_LIMIT,
                )
            )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        answers = {
            item.custom_id: (item.value or "").strip()
            for item in self.children
            if isinstance(item, discord.ui.TextInput)
        }
        channel = await resolve_intro_channel(
            interaction.client, self.service.channel_id
        )
        if channel is None:
            await interaction.followup.send(
                t("intro.errors.no_channel"), ephemeral=True
            )
            return

        now = utcnow()
        if self.post is not None:
            await self._edit(interaction, channel, answers, now)
        else:
            await self._create(interaction, channel, answers, now)

    async def _create(
        self, interaction, channel, answers, now
    ) -> None:
        embed = build_intro_embed(
            interaction.user, self.questions, answers, now, now, interaction.client
        )
        try:
            sent = await channel.send(embed=embed)
        except discord.HTTPException:
            logger.warning("Could not post introduction", exc_info=True)
            await interaction.followup.send(
                t("intro.post.failed"), ephemeral=True
            )
            return
        await self.service.create_post(
            interaction.user.id, sent.id, channel.id, answers, now
        )
        await repost_sticky(interaction.client, channel)
        await interaction.followup.send(t("intro.post.created"), ephemeral=True)

    async def _edit(self, interaction, channel, answers, now) -> None:
        embed = build_intro_embed(
            interaction.user,
            self.questions,
            answers,
            self.post.first_posted_at,
            now,
            interaction.client,
        )
        message_id = self.post.message_id
        recreated = False
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed)
        except discord.HTTPException:
            # The original post is gone: recreate it and remember the new id.
            try:
                sent = await channel.send(embed=embed)
            except discord.HTTPException:
                logger.warning("Could not edit introduction", exc_info=True)
                await interaction.followup.send(
                    t("intro.post.failed"), ephemeral=True
                )
                return
            message_id = sent.id
            recreated = True
        await self.service.update_post(
            self.post.id, answers, now, message_id=message_id
        )
        if recreated:
            await repost_sticky(interaction.client, channel)
        await interaction.followup.send(t("intro.post.edited"), ephemeral=True)


async def repost_sticky(
    bot: discord.Client, channel: discord.TextChannel
) -> None:
    """Delete the stored sticky in ``channel`` and post a fresh one."""
    service = bot.intros
    old_id = service.sticky_message_id
    if old_id:
        try:
            old = await channel.fetch_message(old_id)
            await old.delete()
        except discord.HTTPException:
            pass
    sent = await channel.send(embed=sticky_embed(bot), view=IntroduceView())
    await service.set_sticky(sent.id)


# ------------------------------------------------------------- admin: channel


@channel_group.command(name="set", description=t("intro.channel.set.description"))
@app_commands.describe(channel=t("intro.channel.set.param_channel"))
async def set_channel(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)

    service = _service(interaction)
    old_channel_id, old_sticky_id = await service.set_channel(channel.id)

    if old_sticky_id:
        old_channel = (
            await resolve_intro_channel(interaction.client, old_channel_id)
            if old_channel_id
            else channel
        )
        if old_channel is not None:
            try:
                old = await old_channel.fetch_message(old_sticky_id)
                await old.delete()
            except discord.HTTPException:
                pass

    try:
        sent = await channel.send(
            embed=sticky_embed(interaction.client), view=IntroduceView()
        )
    except discord.HTTPException:
        logger.warning("Could not post intro sticky", exc_info=True)
        await interaction.followup.send(
            t("intro.channel.set.failed", channel=channel.mention), ephemeral=True
        )
        return
    await service.set_sticky(sent.id)
    await interaction.followup.send(
        t("intro.channel.set.success", channel=channel.mention), ephemeral=True
    )


@intro_group.command(name="sticky", description=t("intro.sticky.description"))
async def sticky_command(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    channel = await resolve_intro_channel(
        interaction.client, _service(interaction).channel_id
    )
    if channel is None:
        await interaction.response.send_message(
            t("intro.errors.no_channel"), ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    await repost_sticky(interaction.client, channel)
    await interaction.followup.send(t("intro.sticky.reposted"), ephemeral=True)


# ----------------------------------------------------------- admin: questions


@questions_group.command(
    name="add", description=t("intro.questions.add.description")
)
@app_commands.describe(
    content=t("intro.questions.add.param_content"),
    placeholder=t("intro.questions.add.param_placeholder"),
    active=t("intro.questions.add.param_active"),
)
async def add_question(
    interaction: discord.Interaction,
    content: str,
    placeholder: str | None = None,
    active: bool = True,
) -> None:
    if not await require_admin(interaction):
        return
    row = await _service(interaction).add_question(
        content.strip(),
        (placeholder or "").strip() or None,
        active,
    )
    if row.placeholder:
        message = t(
            "intro.questions.add.success_placeholder",
            position=row.position,
            content=row.content,
            placeholder=row.placeholder,
        )
    else:
        message = t(
            "intro.questions.add.success",
            position=row.position,
            content=row.content,
        )
    await interaction.response.send_message(message, ephemeral=True)


def _question_embed(view: "QuestionsView", client: discord.Client) -> discord.Embed:
    embed = new_embed(client)
    embed.title = t("intro.questions.list.title")
    if not view.questions:
        embed.description = t("intro.questions.list.empty")
        set_transparent_image(embed, client)
        return embed

    embed.description = t("intro.questions.list.disclaimer")
    posted_ids = {q.id for q in select_modal_questions(view.questions)}
    for index, question in enumerate(view.questions, start=1):
        status = (
            t("intro.questions.list.posted")
            if question.id in posted_ids
            else t("intro.questions.list.not_posted")
        )
        active = (
            t("intro.questions.list.active")
            if question.active
            else t("intro.questions.list.inactive")
        )
        lines = [
            t(
                "intro.questions.list.line",
                position=index,
                active=active,
                status=status,
            )
        ]
        if question.placeholder:
            lines.append(
                t(
                    "intro.questions.list.placeholder",
                    placeholder=question.placeholder,
                )
            )
        embed.add_field(
            name=question.content[:256],
            value="\n".join(lines)[:1024],
            inline=False,
        )
    set_transparent_image(embed, client)
    return embed


class QuestionsView(discord.ui.View):
    """Interactive question manager: select a question, then act on it."""

    def __init__(
        self, service: IntroService, author_id: int, questions: list
    ) -> None:
        super().__init__(timeout=180)
        self.service = service
        self.author_id = author_id
        self.questions = questions
        self.selected_id: int | None = None
        self.message: discord.Message | None = None

        self.select = discord.ui.Select(
            placeholder=t("intro.questions.select_placeholder"), row=0
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        self.edit_button = self._button(
            "edit",
            t("intro.questions.button.edit"),
            discord.ButtonStyle.primary,
            1,
            self._on_edit,
        )
        self.toggle_button = self._button(
            "toggle",
            t("intro.questions.button.toggle_active"),
            discord.ButtonStyle.secondary,
            1,
            self._on_toggle,
        )
        self.up_button = self._button(
            "up",
            t("intro.questions.button.move_up"),
            discord.ButtonStyle.secondary,
            2,
            self._on_up,
        )
        self.down_button = self._button(
            "down",
            t("intro.questions.button.move_down"),
            discord.ButtonStyle.secondary,
            2,
            self._on_down,
        )
        self.remove_button = self._button(
            "remove",
            t("intro.questions.button.remove"),
            discord.ButtonStyle.danger,
            2,
            self._on_remove,
        )
        for button in (
            self.edit_button,
            self.toggle_button,
            self.up_button,
            self.down_button,
            self.remove_button,
        ):
            self.add_item(button)
        self.sync()

    @staticmethod
    def _button(custom_id, label, style, row, callback) -> discord.ui.Button:
        button = discord.ui.Button(
            label=label, style=style, row=row, custom_id=custom_id
        )
        button.callback = callback
        return button

    def _selected(self):
        return next(
            (q for q in self.questions if q.id == self.selected_id), None
        )

    def sync(self) -> None:
        self.select.options = [
            discord.SelectOption(
                label=question.content[:100],
                value=str(question.id),
                description=(question.placeholder or "")[:100] or None,
                default=(question.id == self.selected_id),
            )
            for question in self.questions[:QUESTION_SELECT_LIMIT]
        ]
        self.select.disabled = not self.questions
        index = next(
            (
                i
                for i, question in enumerate(self.questions)
                if question.id == self.selected_id
            ),
            None,
        )
        selected = index is not None
        self.edit_button.disabled = not selected
        self.toggle_button.disabled = not selected
        self.remove_button.disabled = not selected
        self.up_button.disabled = not selected or index == 0
        self.down_button.disabled = (
            not selected or index == len(self.questions) - 1
        )

    async def reload(self) -> None:
        self.questions = await self.service.all_questions()
        if self.selected_id is not None and self._selected() is None:
            self.selected_id = None
        self.sync()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                t("components.not_author"), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def _embed(self, client: discord.Client) -> discord.Embed:
        return _question_embed(self, client)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await self.reload()
        if not self.questions:
            # A select menu with no options is rejected; drop the components.
            self.stop()
            await interaction.response.edit_message(
                embed=self._embed(interaction.client), view=None
            )
            return
        await interaction.response.edit_message(
            embed=self._embed(interaction.client), view=self
        )

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.selected_id = int(self.select.values[0])
        await self._refresh(interaction)

    async def _on_edit(self, interaction: discord.Interaction) -> None:
        question = self._selected()
        if question is None:
            await interaction.response.send_message(
                t("intro.questions.select_placeholder"), ephemeral=True
            )
            return
        await interaction.response.send_modal(QuestionModal(self, question))

    async def _on_toggle(self, interaction: discord.Interaction) -> None:
        question = self._selected()
        if question is None:
            return
        updated = await self.service.set_question_active(
            question.id, not question.active
        )
        await self._refresh(interaction)
        if updated is not None:
            key = (
                "intro.questions.disabled_active"
                if updated.active
                else "intro.questions.disabled_now"
            )
            await interaction.followup.send(t(key), ephemeral=True)

    async def _on_up(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, -1)

    async def _on_down(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, 1)

    async def _move(self, interaction: discord.Interaction, delta: int) -> None:
        if self.selected_id is None:
            return
        await self.service.move_question(self.selected_id, delta)
        await self._refresh(interaction)

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        if self.selected_id is None:
            return
        await self.service.remove_question(self.selected_id)
        self.selected_id = None
        await self._refresh(interaction)
        await interaction.followup.send(
            t("intro.questions.remove.success"), ephemeral=True
        )


class QuestionModal(discord.ui.Modal):
    """Edit a question's content and placeholder."""

    def __init__(self, view: QuestionsView, question) -> None:
        super().__init__(title=t("intro.questions.edit.modal_title"))
        self.view = view
        self.question_id = question.id
        self.add_item(
            discord.ui.TextInput(
                label=t("intro.questions.edit.param_content")[:LABEL_LIMIT],
                default=question.content,
                max_length=QUESTION_CONTENT_LIMIT,
            )
        )
        self.add_item(
            discord.ui.TextInput(
                label=t("intro.questions.edit.param_placeholder")[:LABEL_LIMIT],
                default=question.placeholder or "",
                required=False,
                style=discord.TextStyle.paragraph,
                max_length=PLACEHOLDER_LIMIT,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        content = self.children[0].value.strip()
        placeholder = self.children[1].value.strip() or None
        await self.view.service.update_question(
            self.question_id, content=content, placeholder=placeholder
        )
        if self.view.message is not None:
            await self.view.reload()
            await self.view.message.edit(
                embed=self.view._embed(interaction.client), view=self.view
            )
        await interaction.response.send_message(
            t("intro.questions.edit.success"), ephemeral=True
        )


@questions_group.command(
    name="list", description=t("intro.questions.list.description")
)
async def list_questions(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    service = _service(interaction)
    questions = await service.all_questions()
    if not questions:
        embed = new_embed(interaction.client)
        embed.title = t("intro.questions.list.title")
        embed.description = t("intro.questions.list.empty")
        set_transparent_image(embed, interaction.client)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    view = QuestionsView(service, interaction.user.id, questions)
    await interaction.response.send_message(
        embed=view._embed(interaction.client), view=view, ephemeral=True
    )
    view.message = await interaction.original_response()
