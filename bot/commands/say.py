"""Fun admin tool: send a message as the bot (``/say``).

Admins may post arbitrary text (emojis included) into a channel or thread,
optionally as a reply to another message.
"""

from __future__ import annotations

import re

import discord
from discord import app_commands

from ..texts import t
from .checks import require_admin

MESSAGE_LIMIT = 2000

_MESSAGE_LINK = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/\d+/\d+/(\d+)"
)


def parse_message_id(value: str) -> int | None:
    """Return the message id from a bare id or a jump link, else ``None``."""
    value = value.strip()
    if value.isascii() and value.isdigit():
        return int(value)
    match = _MESSAGE_LINK.fullmatch(value)
    return int(match.group(1)) if match else None


def is_valid_message(text: str) -> bool:
    """Whether ``text`` is a non-empty message within Discord's length limit."""
    return bool(text.strip()) and len(text) <= MESSAGE_LIMIT


def render_message(text: str) -> str:
    """Turn literal ``\\n`` escapes into real line breaks.

    Slash-command string options are single-line, so this lets admins add line
    breaks to a message by typing ``\\n``.
    """
    return text.replace("\\n", "\n")


@app_commands.command(name="say", description=t("say.description"))
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    channel=t("say.param_channel"),
    message=t("say.param_message"),
    reply_to=t("say.param_reply_to"),
)
async def say_command(
    interaction: discord.Interaction,
    channel: discord.TextChannel | discord.Thread,
    message: str,
    reply_to: str | None = None,
) -> None:
    if not await require_admin(interaction):
        return

    message = render_message(message)
    if not is_valid_message(message):
        await interaction.response.send_message(
            t("say.invalid_message", limit=MESSAGE_LIMIT), ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    reference: discord.Message | None = None
    if reply_to:
        message_id = parse_message_id(reply_to)
        if message_id is not None:
            try:
                reference = await channel.fetch_message(message_id)
            except discord.HTTPException:
                reference = None
        if reference is None:
            await interaction.followup.send(
                t("say.invalid_reply", channel=channel.mention), ephemeral=True
            )
            return

    kwargs: dict = {"content": message}
    if reference is not None:
        kwargs["reference"] = reference
        kwargs["mention_author"] = True

    try:
        sent = await channel.send(**kwargs)
    except discord.HTTPException:
        await interaction.followup.send(
            t("say.send_failed", channel=channel.mention), ephemeral=True
        )
        return

    await interaction.followup.send(
        t("say.success", link=sent.jump_url), ephemeral=True
    )
