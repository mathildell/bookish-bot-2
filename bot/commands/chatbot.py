"""Chatbot configuration commands (``/chatbot ...``)."""

from __future__ import annotations

import discord
from discord import app_commands

from ..services.members import resolve_members
from ..texts import t
from .checks import require_admin
from .listing import EMBEDS_PER_PAGE, chunked, new_embed, send_paginated, set_transparent_image

chatbot_group = app_commands.Group(
    name="chatbot",
    description=t("chatbot.group_description"),
    default_permissions=discord.Permissions(manage_guild=True),
)

model_group = app_commands.Group(
    name="model",
    description=t("chatbot.model.group_description"),
    parent=chatbot_group,
)

prompt_group = app_commands.Group(
    name="prompt",
    description=t("chatbot.prompt.group_description"),
    parent=chatbot_group,
)

userinfo_group = app_commands.Group(
    name="userinfo",
    description=t("chatbot.userinfo.group_description"),
    parent=chatbot_group,
)

PROMPT_TYPE_PERSONA = "persona"
PROMPT_TYPE_NAME = "name"
PROMPT_TYPE_INFO = "info"

MAX_CHOICES = 25


async def model_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    chat = getattr(interaction.client, "chat", None)
    if chat is None:
        return []
    try:
        detailed = await chat.list_models_detailed()
    except Exception:
        return []
    needle = (current or "").strip().lower()
    matches = []
    for display, value in detailed:
        if needle and needle not in display.lower() and needle not in value.lower():
            continue
        matches.append(app_commands.Choice(name=display[:100], value=value))
        if len(matches) >= MAX_CHOICES:
            break
    if not matches and detailed:
        for display, value in detailed[:MAX_CHOICES]:
            matches.append(app_commands.Choice(name=display[:100], value=value))
    return matches


@model_group.command(name="set", description=t("chatbot.model.set.description"))
@app_commands.describe(model=t("chatbot.model.set.param_model"))
@app_commands.autocomplete(model=model_autocomplete)
async def model_set(interaction: discord.Interaction, model: str) -> None:
    if not await require_admin(interaction):
        return
    model = (model or "").strip()
    if not model:
        await interaction.response.send_message(t("chatbot.model.set.missing"), ephemeral=True)
        return
    chat = getattr(interaction.client, "chat", None)
    if chat is not None:
        try:
            available = await chat.list_models()
            if model not in available:
                await interaction.response.send_message(t("chatbot.model.set.invalid"), ephemeral=True)
                return
        except Exception:
            pass
    await interaction.client.bot_settings.set_chat_model(model)
    if chat is not None:
        try:
            settings = getattr(interaction.client, "bot_settings", None)
            chat.configure(
                model,
                getattr(settings, "chat_persona", None) if settings else None,
                getattr(settings, "chat_prompt_name", None) if settings else None,
                getattr(settings, "chat_prompt_info", None) if settings else None,
            )
        except Exception:
            pass
    await interaction.response.send_message(t("chatbot.model.set.success", model=model), ephemeral=True)


@prompt_group.command(name="list", description=t("chatbot.prompt.list.description"))
async def prompt_list(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    settings = interaction.client.bot_settings
    persona = getattr(settings, "chat_persona", None)
    prompt_name = getattr(settings, "chat_prompt_name", None)
    prompt_info = getattr(settings, "chat_prompt_info", None)
    embed = new_embed(interaction.client)
    embed.title = t("chatbot.prompt.list.title")
    embed.add_field(name=t("chatbot.prompt.type_persona"), value=persona or t("chatbot.prompt.list.builtin"), inline=False)
    embed.add_field(name=t("chatbot.prompt.type_name"), value=prompt_name or t("chatbot.prompt.list.builtin"), inline=False)
    embed.add_field(name=t("chatbot.prompt.type_info"), value=prompt_info or t("chatbot.prompt.list.builtin"), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@prompt_group.command(name="set", description=t("chatbot.prompt.set.description"))
@app_commands.choices(
    type=[
        app_commands.Choice(name=t("chatbot.prompt.type_persona"), value=PROMPT_TYPE_PERSONA),
        app_commands.Choice(name=t("chatbot.prompt.type_name"), value=PROMPT_TYPE_NAME),
        app_commands.Choice(name=t("chatbot.prompt.type_info"), value=PROMPT_TYPE_INFO),
    ]
)
@app_commands.describe(
    type=t("chatbot.prompt.set.param_type"),
    content=t("chatbot.prompt.set.param_content"),
)
async def prompt_set(interaction: discord.Interaction, type: str, content: str) -> None:
    if not await require_admin(interaction):
        return
    content = content or ""
    if not content.strip():
        await interaction.response.send_message(t("chatbot.prompt.set.missing"), ephemeral=True)
        return
    settings = interaction.client.bot_settings
    chat = getattr(interaction.client, "chat", None)
    if type == PROMPT_TYPE_PERSONA:
        await settings.set_chat_persona(content)
        if chat is not None:
            try:
                chat.configure(
                    getattr(settings, "chat_model", None),
                    content,
                    getattr(settings, "chat_prompt_name", None),
                    getattr(settings, "chat_prompt_info", None),
                )
            except Exception:
                pass
        await interaction.response.send_message(t("chatbot.prompt.set.success_persona"), ephemeral=True)
        return
    if type == PROMPT_TYPE_NAME:
        await settings.set_chat_prompt_name(content)
        if chat is not None:
            try:
                chat.configure(
                    getattr(settings, "chat_model", None),
                    getattr(settings, "chat_persona", None),
                    content,
                    getattr(settings, "chat_prompt_info", None),
                )
            except Exception:
                pass
        await interaction.response.send_message(t("chatbot.prompt.set.success_name"), ephemeral=True)
        return
    if type == PROMPT_TYPE_INFO:
        await settings.set_chat_prompt_info(content)
        if chat is not None:
            try:
                chat.configure(
                    getattr(settings, "chat_model", None),
                    getattr(settings, "chat_persona", None),
                    getattr(settings, "chat_prompt_name", None),
                    content,
                )
            except Exception:
                pass
        await interaction.response.send_message(t("chatbot.prompt.set.success_info"), ephemeral=True)
        return
    await interaction.response.send_message(t("chatbot.prompt.set.invalid_type"), ephemeral=True)


@userinfo_group.command(name="set", description=t("chatbot.userinfo.set.description"))
@app_commands.describe(
    user=t("chatbot.userinfo.set.param_user"),
    content=t("chatbot.userinfo.set.param_content"),
)
async def userinfo_set(interaction: discord.Interaction, user: discord.Member, content: str) -> None:
    if not await require_admin(interaction):
        return
    content = (content or "").strip()
    if content.lower() == "none":
        deleted = await interaction.client.member_profiles.delete(user.id)
        if deleted:
            await interaction.response.send_message(t("chatbot.userinfo.set.cleared", member=user.mention), ephemeral=True)
        else:
            await interaction.response.send_message(t("chatbot.userinfo.set.no_bio", member=user.mention), ephemeral=True)
        return
    if len(content) > 500:
        await interaction.response.send_message(t("chatbot.userinfo.set.too_long"), ephemeral=True)
        return
    await interaction.client.member_profiles.upsert(user.id, content, updated_by=interaction.user.id)
    await interaction.response.send_message(t("chatbot.userinfo.set.success", member=user.mention), ephemeral=True)


@userinfo_group.command(name="list", description=t("chatbot.userinfo.list.description"))
async def userinfo_list(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    rows = await interaction.client.member_profiles.all()
    if not rows:
        embed = new_embed(interaction.client)
        embed.title = t("chatbot.userinfo.list.title")
        embed.description = t("chatbot.userinfo.list.empty")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    members = await resolve_members(interaction.guild, [row.user_id for row in rows])
    member_map = {m.id: m for m in members if m is not None}
    blocks = []
    for row in rows:
        member = member_map.get(row.user_id)
        name = member.display_name if member is not None else f"Unknown ({row.user_id})"
        blocks.append(f"### {name}\n{row.bio}")
    pages = []
    for chunk in chunked(blocks, EMBEDS_PER_PAGE):
        embed = new_embed(interaction.client)
        embed.title = t("chatbot.userinfo.list.title")
        embed.description = "\n".join(chunk)
        pages.append([embed])

    embed.colour = getattr(interaction.client, "default_embed_color", None)
    set_transparent_image(embed, interaction.client)
    await send_paginated(interaction, pages=pages, total_results=len(blocks))
