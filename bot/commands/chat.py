"""Chat commands (``/chat``) and mention handling."""

from __future__ import annotations

import logging
import re

import discord
from discord import app_commands

from ..services.chat import ChatCooldownError, ChatError, ChatTimeoutError, ChatUnavailableError
from ..texts import t

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@!?(\d+)>")


class ChatCog:
    def __init__(self, bot_id: int) -> None:
        self.bot_id = bot_id


def strip_bot_mentions(content: str, bot_id: int) -> str:
    content = content or ""
    # remove mentions to this bot
    def repl(m: re.Match[str]) -> str:
        try:
            if int(m.group(1)) == bot_id:
                return " "
        except Exception:
            pass
        return m.group(0)
    content = _MENTION_RE.sub(repl, content)
    return " ".join(content.split()).strip()


def add_metadata_to_message(username: str, content: str) -> str:
    content = content or ""
    username = username or ""
    return f"{{username: {username}, message: {content}}}"


def _configure_chat(client: discord.Client) -> None:
    chat_service = getattr(client, "chat", None)
    settings = getattr(client, "bot_settings", None)
    if chat_service is None or settings is None:
        return
    try:
        chat_service.configure(
            getattr(settings, "chat_model", None),
            getattr(settings, "chat_persona", None),
            getattr(settings, "chat_prompt_name", None),
            getattr(settings, "chat_prompt_info", None),
        )
    except Exception:
        pass


async def handle_mention(message: discord.Message, bot: discord.Client) -> None:
    if message.guild is None or bot.user is None:
        return
    chat_service = getattr(bot, "chat", None)
    profiles = getattr(bot, "member_profiles", None)
    if chat_service is None or profiles is None:
        return
    _configure_chat(bot)
    content = strip_bot_mentions(message.content, bot.user.id)

    if not content:
        try:
            await message.reply(t("chat.empty_ping"), mention_author=False)
        except Exception:
            pass
        return
    else:
        content = add_metadata_to_message(message.author.display_name, content)

    bio = None
    try:
        bio = await profiles.bio_for(message.author.id)
    except Exception:
        bio = None
    try:
        async with message.channel.typing():
            reply = await chat_service.chat(
                message.guild.id,
                message.author.id,
                message.author.display_name,
                bio,
                content,
            )
        if reply:
            await message.reply(reply, mention_author=False)
        else:
            try:
                await message.reply(t("chat.unavailable"), mention_author=False)
            except Exception:
                pass
    except ChatCooldownError:
        return
    except ChatTimeoutError:
        try:
            await message.reply(t("chat.timeout"), mention_author=False)
        except Exception:
            pass
    except ChatUnavailableError:
        try:
            await message.reply(t("chat.unavailable"), mention_author=False)
        except Exception:
            pass
    except Exception:
        logger.exception("Mention chat failed")
        try:
            await message.reply(t("chat.unavailable"), mention_author=False)
        except Exception:
            pass


@app_commands.command(name="chat", description=t("chat.description"))
@app_commands.describe(message=t("chat.param_message"))
async def chat_command(interaction: discord.Interaction, message: str) -> None:
    chat_service = getattr(interaction.client, "chat", None)
    profiles = getattr(interaction.client, "member_profiles", None)
    display_name = interaction.user.display_name
    message = add_metadata_to_message(display_name, message)
    if chat_service is None or profiles is None:
        await interaction.response.send_message(t("chat.unavailable"), ephemeral=True)
        return
    _configure_chat(interaction.client)
    bio = None
    try:
        bio = await profiles.bio_for(interaction.user.id)
    except Exception:
        bio = None
    try:
        await interaction.response.defer(thinking=True)
        reply = await chat_service.chat(
            interaction.guild.id if interaction.guild else 0,
            interaction.user.id,
            display_name,
            bio,
            message,
        )
        if reply:
            await interaction.followup.send(reply)
        else:
            await interaction.followup.send(t("chat.unavailable"))
    except ChatCooldownError:
        await interaction.response.send_message(t("chat.cooldown"), ephemeral=True)
    except ChatTimeoutError:
        try:
            await interaction.followup.send(t("chat.timeout"))
        except Exception:
            await interaction.response.send_message(t("chat.timeout"), ephemeral=True)
    except ChatUnavailableError:
        try:
            await interaction.followup.send(t("chat.unavailable"))
        except Exception:
            await interaction.response.send_message(t("chat.unavailable"), ephemeral=True)
    except Exception:
        logger.exception("Chat command failed")
        try:
            await interaction.followup.send(t("chat.unavailable"))
        except Exception:
            await interaction.response.send_message(t("chat.unavailable"), ephemeral=True)
