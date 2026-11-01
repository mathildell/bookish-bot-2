"""Chat commands (``/chat``) and mention handling."""

from __future__ import annotations

import logging
import re

import discord
from discord import app_commands

from ..services.chat import (
    ChatCooldownError,
    ChatTimeoutError,
    ChatUnavailableError,
    configure_chat,
)
from ..texts import t

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@!?(\d+)>")


def strip_bot_mentions(content: str, bot_id: int) -> str:
    """Drop mentions of ``bot_id`` from ``content`` and collapse the spaces."""

    def repl(match: re.Match[str]) -> str:
        return " " if int(match.group(1)) == bot_id else match.group(0)

    return " ".join(_MENTION_RE.sub(repl, content or "").split()).strip()


def mentions_user(content: str, user_id: int) -> bool:
    """Whether ``content`` pings ``user_id`` explicitly.

    A reply's target is not part of the content, so this distinguishes a member
    typing the ping from Discord's reply mechanism.
    """
    return any(
        int(match) == user_id for match in _MENTION_RE.findall(content or "")
    )


def add_metadata_to_message(username: str, content: str) -> str:
    content = content or ""
    username = username or ""
    return f"{{username: {username}, message: {content}}}"


def is_utility_message(message: discord.Message) -> bool:
    """Whether a message is bot output rather than a conversation to join.

    Members reply to streak reminders, leaderboards, level-up notices, coupons,
    ticket messages and announcements without meaning to chat with the bot, so
    a reply to one of those is ignored. Plain-text messages from the bot carry
    none of these markers and stay conversational, so replies to her own
    answers (including ``/chat`` output) still reach her.
    """
    return bool(
        message.embeds
        or message.attachments
        or message.components
        or message.mentions
        or message.role_mentions
        or message.mention_everyone
        or message.pinned
    )


async def _resolve_referenced_message(
    message: discord.Message,
) -> discord.Message | None:
    """Return the message ``message`` replies to, or None when unavailable."""
    reference = message.reference
    if reference is None or reference.message_id is None:
        return None
    resolved = reference.resolved
    if isinstance(resolved, discord.Message):
        return resolved
    try:
        return await message.channel.fetch_message(reference.message_id)
    except discord.NotFound:
        return None
    except discord.HTTPException:
        logger.exception("Failed to fetch referenced message %s", reference.message_id)
        return None


async def handle_mention(message: discord.Message, bot: discord.Client) -> None:
    if message.guild is None or bot.user is None:
        return
    chat_service = getattr(bot, "chat", None)
    profiles = getattr(bot, "member_profiles", None)
    if chat_service is None or profiles is None:
        return
    configure_chat(bot)

    parent = None
    if message.reference is not None:
        try:
            parent = await _resolve_referenced_message(message)
        except Exception:
            logger.exception("Failed to resolve referenced message")
            parent = None
    if (
        parent is not None
        and parent.author.id == bot.user.id
        and is_utility_message(parent)
        and not mentions_user(message.content, bot.user.id)
    ):
        logger.debug(
            "Ignoring reply to utility message %s in channel %s",
            parent.id,
            message.channel.id,
        )
        return

    content = strip_bot_mentions(message.content, bot.user.id)

    if not content:
        try:
            await message.reply(t("chat.empty_ping"), mention_author=False)
        except Exception:
            pass
        return
    else:
        content = add_metadata_to_message(message.author.display_name, content)

    previous = None
    if parent is not None:
        ref_content = strip_bot_mentions(parent.content, bot.user.id)
        previous = {
            "username": parent.author.display_name,
            "message": add_metadata_to_message(
                parent.author.display_name, ref_content
            ),
            "author_id": parent.author.id,
        }

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
                previous_message=previous,
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
    configure_chat(interaction.client)
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
            message
        )
        if reply:
            await interaction.followup.send(reply)
        else:
            await interaction.followup.send(t("chat.unavailable"))
    except ChatCooldownError:
        try:
            await interaction.followup.send(t("chat.cooldown"), ephemeral=True)
        except Exception:
            await interaction.response.send_message(
                t("chat.cooldown"), ephemeral=True
            )
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
