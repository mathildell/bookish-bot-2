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


def chatbot_enabled(client: discord.Client) -> bool:
    """Whether admins have left the chatbot switched on."""
    settings = getattr(client, "bot_settings", None)
    if settings is None:
        return True
    return bool(getattr(settings, "chat_enabled", True))


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


async def _reply_target(message: discord.Message) -> discord.Message | None:
    """The message ``message`` replies to, logging and swallowing failures."""
    try:
        return await _resolve_referenced_message(message)
    except Exception:
        logger.exception("Failed to resolve referenced message")
        return None


async def _reply_chain(
    message: discord.Message, depth: int
) -> list[discord.Message]:
    """The messages ``message`` replies to, nearest first, up to ``depth``."""
    chain: list[discord.Message] = []
    current = message
    while len(chain) < depth:
        parent = await _reply_target(current)
        if parent is None:
            break
        chain.append(parent)
        current = parent
    return chain


def _utility_reply_in_chain(
    message: discord.Message, chain: list[discord.Message], bot_id: int
) -> discord.Message | None:
    """The bot utility message that makes ``message``'s reply a non-chat reply."""
    child = message
    for parent in chain:
        if (
            parent.author.id == bot_id
            and is_utility_message(parent)
            and not mentions_user(child.content, bot_id)
        ):
            return parent
        child = parent
    return None


def _history_entry(message: discord.Message, bot_id: int) -> dict:
    """A message as the chat service expects it in its conversation history."""
    content = strip_bot_mentions(message.content, bot_id)
    return {
        "username": message.author.display_name,
        "message": add_metadata_to_message(message.author.display_name, content),
        "author_id": message.author.id,
    }


async def _bio_for(profiles, user_id: int) -> str | None:
    """The member's stored bio, or None when there is none."""
    try:
        return await profiles.bio_for(user_id)
    except Exception:
        return None


async def _reply_quietly(message: discord.Message, content: str) -> None:
    """Reply without a mention; a failed reply is not worth reporting."""
    try:
        await message.reply(content, mention_author=False)
    except Exception:
        logger.debug("Could not send chat reply", exc_info=True)


async def _respond(
    interaction: discord.Interaction, content: str, *, ephemeral: bool = False
) -> None:
    """Answer through the followup once acknowledged, otherwise directly."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except Exception:
        logger.debug("Could not send chat response", exc_info=True)


async def handle_mention(message: discord.Message, bot: discord.Client) -> None:
    if message.guild is None or bot.user is None:
        return
    chat_service = getattr(bot, "chat", None)
    profiles = getattr(bot, "member_profiles", None)
    if chat_service is None or profiles is None:
        return
    configure_chat(bot)
    bot_id = bot.user.id

    chain = await _reply_chain(message, chat_service.history_depth)
    ignored = _utility_reply_in_chain(message, chain, bot_id)
    if ignored is not None:
        logger.debug(
            "Ignoring reply to utility message %s in channel %s",
            ignored.id,
            message.channel.id,
        )
        return

    if not chatbot_enabled(bot):
        await _reply_quietly(message, t("chat.disabled"))
        return

    content = strip_bot_mentions(message.content, bot_id)
    if not content:
        await _reply_quietly(message, t("chat.empty_ping"))
        return
    content = add_metadata_to_message(message.author.display_name, content)

    bio = await _bio_for(profiles, message.author.id)
    try:
        async with message.channel.typing():
            reply = await chat_service.chat(
                message.guild.id,
                message.author.id,
                message.author.display_name,
                bio,
                content,
                history=[_history_entry(entry, bot_id) for entry in reversed(chain)],
            )
    except ChatCooldownError:
        return
    except ChatTimeoutError:
        await _reply_quietly(message, t("chat.timeout"))
        return
    except ChatUnavailableError:
        await _reply_quietly(message, t("chat.unavailable"))
        return
    except Exception:
        logger.exception("Mention chat failed")
        await _reply_quietly(message, t("chat.unavailable"))
        return

    await _reply_quietly(message, reply or t("chat.unavailable"))


@app_commands.command(name="chat", description=t("chat.description"))
@app_commands.describe(message=t("chat.param_message"))
async def chat_command(interaction: discord.Interaction, message: str) -> None:
    if not chatbot_enabled(interaction.client):
        await _respond(interaction, t("chat.disabled"), ephemeral=True)
        return

    chat_service = getattr(interaction.client, "chat", None)
    profiles = getattr(interaction.client, "member_profiles", None)
    if chat_service is None or profiles is None:
        await _respond(interaction, t("chat.unavailable"), ephemeral=True)
        return

    configure_chat(interaction.client)
    display_name = interaction.user.display_name
    content = add_metadata_to_message(display_name, message)
    bio = await _bio_for(profiles, interaction.user.id)
    guild_id = interaction.guild.id if interaction.guild else 0

    try:
        await interaction.response.defer(thinking=True)
        reply = await chat_service.chat(
            guild_id, interaction.user.id, display_name, bio, content
        )
    except ChatCooldownError:
        await _respond(interaction, t("chat.cooldown"), ephemeral=True)
        return
    except ChatTimeoutError:
        await _respond(interaction, t("chat.timeout"))
        return
    except ChatUnavailableError:
        await _respond(interaction, t("chat.unavailable"))
        return
    except Exception:
        logger.exception("Chat command failed")
        await _respond(interaction, t("chat.unavailable"))
        return

    await _respond(interaction, reply or t("chat.unavailable"))
