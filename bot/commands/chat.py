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


async def handle_mention(message: discord.Message, bot: discord.Client) -> None:
    if message.guild is None or bot.user is None:
        return
    chat_service = getattr(bot, "chat", None)
    profiles = getattr(bot, "member_profiles", None)
    settings = getattr(bot, "bot_settings", None)
    if chat_service is None or profiles is None:
        return
    if settings is not None and chat_service is not None:
        try:
            chat_service.configure(settings.chat_model)
        except Exception:
            pass
    content = strip_bot_mentions(message.content, bot.user.id)
    if content:
        # Add the username for the bot
        content = "{" + f"username: {message.author.display_name}, message: {content}" + "}"
    else:
        try:
            await message.reply(t("chat.empty_ping"), mention_author=False)
        except Exception:
            pass
        return


    bio = None
    try:
        if profiles is not None:
            bio = await profiles.bio_for(message.author.id)
    except Exception:
        bio = None
    override = getattr(settings, "chat_system_prompt", None) if settings is not None else None
    try:
        async with message.channel.typing():
            reply = await chat_service.chat(message.guild.id, message.author.id, message.author.display_name, bio, content, system_prompt=override)
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
    settings = getattr(interaction.client, "bot_settings", None)
    if chat_service is None or profiles is None:
        await interaction.response.send_message(t("chat.unavailable"), ephemeral=True)
        return
    if settings is not None and chat_service is not None:
        try:
            chat_service.configure(settings.chat_model)
        except Exception:
            pass
    bio = None
    try:
        bio = await profiles.bio_for(interaction.user.id)
    except Exception:
        bio = None
    override = getattr(settings, "chat_system_prompt", None) if settings is not None else None
    try:
        await interaction.response.defer(thinking=True)
        reply = await chat_service.chat(interaction.guild.id if interaction.guild else 0, interaction.user.id, interaction.user.display_name, bio, message, system_prompt=override)
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
