"""Bot settings commands (``/settings ...``).

Admins view and edit the values that used to live in ``config.yaml`` and
``/releases``/``/reviews``: cleanup retention windows, the review repost
cooldown, the daily release post hour, the updates channel, the transparent
embed background and the default embed colour. The values are typed per key, so
the same ``/settings edit`` command parses days, hours, channels, URLs and
colours.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import discord
from discord import app_commands

from ..services.coupons import DEFAULT_FALLBACK_IMAGE
from ..texts import t
from .bot_say import parse_message_link
from .checks import require_admin
from .listing import chunked, new_embed, parse_hex_colour, send_paginated

settings_group = app_commands.Group(
    name="settings",
    description=t("settings.group_description"),
    default_permissions=discord.Permissions(manage_guild=True),
)

PURGE_BOOK_RELEASES = "purge_book_releases"
PURGE_STREAK_LOGS = "purge_streak_logs"
REVIEW_REPOST_COOLDOWN = "review_repost_cooldown_days"
RELEASES_POST_HOUR = "releases_post_hour"
WEEKLY_LEADERBOARD_HOUR = "weekly_leaderboard_hour"
BOT_UPDATES_CHANNEL = "bot_updates_channel_id"
TRANSPARENT_IMAGE = "transparent_embed_image"
DEFAULT_COLOR = "default_embed_color"
BUDDY_READ_CHANNEL = "buddy_read_channel_id"
GEN_CHAT_CHANNEL = "gen_chat_channel_id"
BUDDY_READ_MESSAGE_LINK = "buddy_read_message_link"
ADMIN_TEST_CHANNEL = "admin_test_channel_id"
COUPON_FALLBACK_IMAGE = "coupon_fallback_image"

# Interpretation of each key's value.
_KINDS = {
    PURGE_BOOK_RELEASES: "days",
    PURGE_STREAK_LOGS: "days",
    REVIEW_REPOST_COOLDOWN: "days",
    RELEASES_POST_HOUR: "hour",
    WEEKLY_LEADERBOARD_HOUR: "hour",
    BOT_UPDATES_CHANNEL: "channel",
    TRANSPARENT_IMAGE: "url",
    DEFAULT_COLOR: "color",
    BUDDY_READ_CHANNEL: "channel",
    GEN_CHAT_CHANNEL: "channel",
    BUDDY_READ_MESSAGE_LINK: "message_link",
    ADMIN_TEST_CHANNEL: "channel",
    COUPON_FALLBACK_IMAGE: "url",
}

SETTING_KEYS = tuple(_KINDS)

MIN_DAYS = 1
MAX_DAYS = 3650
MIN_HOUR = 1
MAX_HOUR = 23
# Literal accepted by the optional settings to unset them.
CLEAR = "none"

_CHANNEL_RE = re.compile(r"<#(\d+)>")


def setting_label(key: str) -> str:
    """Human-readable name for a setting key."""
    return t(f"settings.keys.{key}")


def parse_days(raw: str) -> int:
    """Parse a whole number of days within the allowed range."""
    text = raw.strip()
    if not text.isdigit():
        raise ValueError(raw)
    days = int(text)
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise ValueError(raw)
    return days


def parse_hour(raw: str) -> int:
    """Parse a whole UTC hour within the allowed range."""
    text = raw.strip()
    if not text.isdigit():
        raise ValueError(raw)
    hour = int(text)
    if not MIN_HOUR <= hour <= MAX_HOUR:
        raise ValueError(raw)
    return hour


def parse_channel_id(raw: str) -> int | None:
    """Parse a channel mention/id, or ``None`` for the clear literal."""
    text = raw.strip()
    if text.lower() == CLEAR:
        return None
    match = _CHANNEL_RE.fullmatch(text)
    if match:
        return int(match.group(1))
    if text.isdigit():
        return int(text)
    raise ValueError(raw)


def parse_url(raw: str) -> str | None:
    """Parse an http(s) URL, or ``None`` for the clear literal."""
    text = raw.strip()
    if text.lower() == CLEAR:
        return None
    if not text.startswith(("http://", "https://")):
        raise ValueError(raw)
    return text


def parse_color(raw: str) -> discord.Colour | None:
    """Parse a hex colour, or ``None`` for the clear literal."""
    text = raw.strip()
    if text.lower() == CLEAR:
        return None
    return parse_hex_colour(text)


def parse_message_link_setting(raw: str) -> str | None:
    """Parse a Discord message jump link, or ``None`` for the clear literal."""
    text = raw.strip()
    if text.lower() == CLEAR:
        return None
    if parse_message_link(text) is None:
        raise ValueError(raw)
    return text


def _current_days(interaction: discord.Interaction, key: str) -> int:
    if key == PURGE_BOOK_RELEASES:
        return interaction.client.releases.purge_days
    if key == PURGE_STREAK_LOGS:
        return interaction.client.streaks.log_retention_days
    return interaction.client.reviews.repost_cooldown_days


def _current_value(
    interaction: discord.Interaction, key: str
) -> tuple[str, str]:
    """Return the current value of a key and its unit (empty when unit-less)."""
    kind = _KINDS[key]
    settings = interaction.client.bot_settings
    if kind == "days":
        days = _current_days(interaction, key)
        return str(days), t("settings.list.unit_days", s="" if days == 1 else "s")
    if kind == "hour":
        if key == WEEKLY_LEADERBOARD_HOUR:
            hour = interaction.client.weekly.post_hour
        else:
            hour = interaction.client.releases.post_hour
        return f"{hour:02d}:00", t("settings.list.unit_hour")
    if kind == "channel":
        if key == BUDDY_READ_CHANNEL:
            channel_id = settings.reference_forum_channel_id
        elif key == GEN_CHAT_CHANNEL:
            channel_id = settings.gen_chat_channel_id
        elif key == ADMIN_TEST_CHANNEL:
            channel_id = settings.admin_test_channel_id
        else:
            channel_id = settings.updates_channel_id
        if channel_id:
            return f"<#{channel_id}>", ""
        return t("settings.list.value_none"), ""
    if kind == "url":
        if key == COUPON_FALLBACK_IMAGE:
            fallback = getattr(settings, "coupon_fallback_image", None)
            return fallback or DEFAULT_FALLBACK_IMAGE, ""
        return settings.transparent_embed_image or t("settings.list.value_none"), ""
    if kind == "message_link":
        link = settings.reference_message_link
        return link or t("settings.list.value_none"), ""
    return settings.default_embed_color or t("settings.list.value_none"), ""


def current_display(interaction: discord.Interaction, key: str) -> str:
    """Render the current value of a setting key for inline use."""
    value, unit = _current_value(interaction, key)
    return f"{value} {unit}" if unit else value


def current_value_line(interaction: discord.Interaction, key: str) -> str:
    """Render one setting's value as a ``Value: ...`` line."""
    value, unit = _current_value(interaction, key)
    if _KINDS[key] == "channel":
        return t("settings.list.value_plain", value=value)
    if unit:
        return t("settings.list.value_unit", value=value, unit=unit)
    return t("settings.list.value", value=value)


def build_setting_blocks(interaction: discord.Interaction) -> list[str]:
    blocks = []
    for key in sorted(SETTING_KEYS, key=lambda k: setting_label(k).lower()):
        blocks.append(
            "\n".join(
                (
                    t("settings.list.heading", label=setting_label(key)),
                    current_value_line(interaction, key),
                    t(f"settings.list.info.{key}"),
                )
            )
        )
    return blocks


async def apply_value(
    interaction: discord.Interaction, key: str, raw: str
) -> None:
    """Parse and persist a setting key, updating the runtime value too."""
    kind = _KINDS[key]
    if kind == "days":
        days = parse_days(raw)
        if key == PURGE_BOOK_RELEASES:
            await interaction.client.releases.set_purge_days(days)
        elif key == PURGE_STREAK_LOGS:
            await interaction.client.streaks.set_log_retention_days(days)
        else:
            await interaction.client.reviews.set_repost_cooldown_days(days)
    elif kind == "hour":
        hour = parse_hour(raw)
        if key == WEEKLY_LEADERBOARD_HOUR:
            await interaction.client.weekly.set_post_hour(hour)
        else:
            await interaction.client.releases.set_post_hour(hour)
    elif kind == "channel":
        channel_id = parse_channel_id(raw)
        if (
            channel_id is not None
            and interaction.guild is not None
            and interaction.guild.get_channel(channel_id) is None
        ):
            raise ValueError(raw)
        if key == BUDDY_READ_CHANNEL:
            await interaction.client.bot_settings.set_reference_forum_channel(
                channel_id
            )
            interaction.client.reference_forum_channel_id = channel_id or 0
        elif key == GEN_CHAT_CHANNEL:
            await interaction.client.bot_settings.set_gen_chat_channel(
                channel_id
            )
            interaction.client.set_gen_chat_channel = channel_id or 0
        elif key == ADMIN_TEST_CHANNEL:
            await interaction.client.bot_settings.set_admin_test_channel(
                channel_id
            )
            interaction.client.admin_test_channel_id = channel_id or 0
        else:
            await interaction.client.bot_settings.set_updates_channel(channel_id)
            interaction.client.bot_updates_channel_id = channel_id or 0
    elif kind == "url":
        image = parse_url(raw)
        if key == COUPON_FALLBACK_IMAGE:
            await interaction.client.bot_settings.set_coupon_fallback_image(
                image
            )
            interaction.client.coupon_fallback_image = (
                image or DEFAULT_FALLBACK_IMAGE
            )
        else:
            await interaction.client.bot_settings.set_transparent_embed_image(
                image
            )
            interaction.client.transparent_embed_image = image
    elif kind == "message_link":
        link = parse_message_link_setting(raw)
        await interaction.client.bot_settings.set_reference_message_link(link)
        parsed = parse_message_link(link) if link else None
        interaction.client.reference_channel_id = parsed[0] if parsed else 0
        interaction.client.reference_message_id = parsed[1] if parsed else 0
    else:
        color = parse_color(raw)
        stored = f"#{color.value:06x}" if color is not None else None
        await interaction.client.bot_settings.set_default_embed_color(stored)
        interaction.client.default_embed_color = color


@settings_group.command(name="list", description=t("settings.list.description"))
async def list_settings(interaction: discord.Interaction) -> None:
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    blocks = build_setting_blocks(interaction)
    if not blocks:
        embed = new_embed(interaction.client)
        embed.title = t("settings.list.title")
        embed.description = ""
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    pages = []
    for chunk in chunked(blocks, 8):
        embed = new_embed(interaction.client)
        embed.title = t("settings.list.title")
        embed.description = "\n".join(chunk)
        pages.append([embed])
    await send_paginated(interaction, pages=pages, total_results=len(blocks))


@settings_group.command(name="edit", description=t("settings.edit.description"))
@app_commands.choices(
    key=[
        app_commands.Choice(name=setting_label(key), value=key)
        for key in SETTING_KEYS
    ]
)
@app_commands.describe(
    key=t("settings.edit.param_key"),
    value=t("settings.edit.param_value"),
)
async def edit(
    interaction: discord.Interaction, key: str, value: str
) -> None:
    if not await require_admin(interaction):
        return
    try:
        await apply_value(interaction, key, value)
    except ValueError:
        await interaction.response.send_message(
            t(
                "settings.edit.invalid_value",
                value=value,
                expected=t(
                    f"settings.edit.expected.{_KINDS[key]}",
                    min=MIN_DAYS,
                    max=MAX_DAYS,
                    min_hour=MIN_HOUR,
                    max_hour=MAX_HOUR,
                ),
            ),
            ephemeral=True,
        )
        return
    kind = _KINDS[key]
    if kind == "hour":
        try:
            hour = parse_hour(value)
            dt = datetime.now(timezone.utc).replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            ts = int(dt.timestamp())
            await interaction.response.send_message(
                t(
                    "settings.edit.success",
                    label=setting_label(key),
                    value=f"{hour:02d}:00 UTC (<t:{ts}:t>)",
                )
            )
        except ValueError:
            await interaction.response.send_message(
                t(
                    "settings.edit.success",
                    label=setting_label(key),
                    value=current_display(interaction, key),
                )
            )
    else:
        await interaction.response.send_message(
            t(
                "settings.edit.success",
                label=setting_label(key),
                value=current_display(interaction, key),
            )
        )
