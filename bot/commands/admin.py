"""Admin slash commands."""

from __future__ import annotations

import discord
from discord import app_commands

from ..services.booster_react import build_identifiers, parse_emoji

admin_group = app_commands.Group(
    name="admin",
    description="Administrative commands",
    default_permissions=discord.Permissions(manage_guild=True),
)


def _display_name_for(user: discord.User, interaction: discord.Interaction) -> str:
    member = (
        interaction.guild.get_member(user.id) if interaction.guild else None
    )
    if member is not None:
        return member.display_name or member.name
    return user.global_name or user.name


def _success_reply(
    content: str, *, color: int = 0x57F287
) -> discord.Embed:
    return discord.Embed(description=content, color=color)


@admin_group.command(
    name="booster_react_add",
    description="Register a booster's custom reaction emoji",
)
@app_commands.describe(
    user="The booster to register",
    emoji="Emoji to react with (unicode or <:name:id>)",
    nicknames="Optional comma-separated nicknames to also match",
)
async def booster_react_add(
    interaction: discord.Interaction,
    user: discord.User,
    emoji: str,
    nicknames: str | None = None,
) -> None:
    await interaction.response.defer()

    try:
        parse_emoji(emoji)
    except ValueError:
        await interaction.followup.send(
            f"`{emoji}` doesn't look like a valid emoji.", ephemeral=True
        )
        return

    identifiers = build_identifiers(
        discord_user_id=user.id,
        username=user.name,
        display_name=_display_name_for(user, interaction),
        nicknames=nicknames,
    )

    service = interaction.client.booster_reacts
    record = await service.upsert(
        discord_user_id=user.id,
        identifiers=identifiers,
        emoji=emoji,
    )

    extra = f" ({', '.join(identifiers[3:])})" if len(identifiers) > 3 else ""
    await interaction.followup.send(
        embed=_success_reply(
            f"{emoji} **{user.mention}** is registered (id `{record.id}`){extra}"
        )
    )


@admin_group.command(
    name="booster_react_remove",
    description="Remove a booster's custom reaction emoji",
)
@app_commands.describe(user="The booster to unregister")
async def booster_react_remove(
    interaction: discord.Interaction, user: discord.User
) -> None:
    await interaction.response.defer()

    service = interaction.client.booster_reacts
    removed = await service.delete(user.id)
    if removed:
        await interaction.followup.send(
            embed=_success_reply(
                f"Removed {user.mention}'s booster reaction."
            )
        )
    else:
        await interaction.followup.send(
            f"{user.mention} has no booster reaction registered.",
            ephemeral=True,
        )


@admin_group.command(
    name="booster_react_view",
    description="Show a booster's custom reaction emoji and matching identifiers",
)
@app_commands.describe(user="The booster to inspect")
async def booster_react_view(
    interaction: discord.Interaction, user: discord.User
) -> None:
    await interaction.response.defer()

    service = interaction.client.booster_reacts
    record = await service.find(user.id)
    if record is None:
        await interaction.followup.send(
            f"{user.mention} has no booster reaction registered.",
            ephemeral=True,
        )
        return

    aliases = (record.identifiers or [])[1:]
    suffix = f" ({', '.join(aliases)})" if aliases else ""
    await interaction.followup.send(
        embed=_success_reply(
            f"{record.emoji} **{user.mention}** triggers: `{record.identifiers[0]}`{suffix} "
            f"(id `{record.id}`)"
        )
    )


def register_commands(bot: discord.ext.commands.Bot, guild_id: int) -> None:
    """Bind the admin group to the configured guild and register it."""
    bot.tree.add_command(admin_group, guild=discord.Object(id=guild_id))