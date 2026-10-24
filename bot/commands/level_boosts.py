"""XP boost admin commands (``/xp boosts ...``)."""

from __future__ import annotations

import discord
from discord import app_commands

from ..texts import t
from .checks import require_admin
from .listing import new_embed, set_transparent_image


def _boosts(interaction: discord.Interaction):
    return interaction.client.xp_boosts


def _is_channel(obj: discord.abc.GuildChannel | discord.Thread) -> bool:
    return isinstance(
        obj,
        (
            discord.TextChannel,
            discord.VoiceChannel,
            discord.StageChannel,
            discord.ForumChannel,
            discord.CategoryChannel,
            discord.Thread,
        ),
    )


def register_boosts_group(xp_group: app_commands.Group) -> None:
    boosts_group = app_commands.Group(
        name="boosts",
        description=t("level.boosts.group_description"),
        parent=xp_group,
    )

    @boosts_group.command(name="set", description=t("level.boosts.set.description"))
    @app_commands.describe(
        role=t("level.boosts.set.param_role"),
        channel=t("level.boosts.set.param_channel"),
        percent=t("level.boosts.set.param_percent"),
    )
    async def set_boost(
        interaction: discord.Interaction,
        percent: int,
        role: discord.Role | None = None,
        channel: discord.TextChannel
        | discord.VoiceChannel
        | discord.StageChannel
        | discord.ForumChannel
        | discord.CategoryChannel
        | discord.Thread
        | None = None,
    ) -> None:
        if not await require_admin(interaction):
            return
        if role is None and channel is None:
            await interaction.response.send_message(
                t("level.boosts.missing_target"), ephemeral=True
            )
            return
        if role is not None and channel is not None:
            await interaction.response.send_message(
                t("level.boosts.both_targets"), ephemeral=True
            )
            return
        if percent < 1 or percent > 500:
            await interaction.response.send_message(
                t("level.boosts.invalid_percent"), ephemeral=True
            )
            return
        service = _boosts(interaction)
        if role is not None:
            await service.set_role(role.id, percent)
            await interaction.response.send_message(
                t("level.boosts.role.set.success", role=role.mention, percent=percent)
            )
            return
        if channel is not None:
            await service.set_channel(channel.id, percent)
            await interaction.response.send_message(
                t(
                    "level.boosts.channel.set.success",
                    channel=channel.mention,
                    percent=percent,
                )
            )

    @boosts_group.command(name="remove", description=t("level.boosts.remove.description"))
    @app_commands.describe(
        role=t("level.boosts.remove.param_role"),
        channel=t("level.boosts.remove.param_channel"),
    )
    async def remove_boost(
        interaction: discord.Interaction,
        role: discord.Role | None = None,
        channel: discord.TextChannel
        | discord.VoiceChannel
        | discord.StageChannel
        | discord.ForumChannel
        | discord.CategoryChannel
        | discord.Thread
        | None = None,
    ) -> None:
        if not await require_admin(interaction):
            return
        if role is None and channel is None:
            await interaction.response.send_message(
                t("level.boosts.missing_target"), ephemeral=True
            )
            return
        if role is not None and channel is not None:
            await interaction.response.send_message(
                t("level.boosts.both_targets"), ephemeral=True
            )
            return
        service = _boosts(interaction)
        if role is not None:
            if await service.remove_role(role.id):
                await interaction.response.send_message(
                    t("level.boosts.role.remove.success", role=role.mention),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    t("level.boosts.role.remove.fail", role=role.mention),
                    ephemeral=True,
                )
            return
        if channel is not None:
            if await service.remove_channel(channel.id):
                await interaction.response.send_message(
                    t("level.boosts.channel.remove.success", channel=channel.mention),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    t("level.boosts.channel.remove.fail", channel=channel.mention),
                    ephemeral=True,
                )

    @boosts_group.command(name="list", description=t("level.boosts.list.description"))
    async def list_boosts(interaction: discord.Interaction) -> None:
        if not await require_admin(interaction):
            return
        service = _boosts(interaction)
        role_boosts = service.role_boosts
        channel_boosts = service.channel_boosts
        if not role_boosts and not channel_boosts:
            await interaction.response.send_message(
                t("level.boosts.list.empty"), ephemeral=True
            )
            return
        embed = new_embed(interaction.client)
        embed.title = t("level.boosts.list.title")
        guild = interaction.guild
        if role_boosts:
            lines = []
            for role_id, percent in sorted(role_boosts.items(), key=lambda item: item[0]):
                role = guild.get_role(role_id) if guild else None
                name = role.mention if role else f"<@&{role_id}>"
                lines.append(t("level.boosts.list.role_line", target=name, percent=percent))
            embed.add_field(
                name=t("level.boosts.list.role_field"),
                value="\n".join(lines),
                inline=False,
            )
        if channel_boosts:
            lines = []
            for channel_id, percent in sorted(channel_boosts.items(), key=lambda item: item[0]):
                channel = guild.get_channel(channel_id) if guild else None
                name = channel.mention if channel else f"<#{channel_id}>"
                lines.append(t("level.boosts.list.channel_line", target=name, percent=percent))
            embed.add_field(
                name=t("level.boosts.list.channel_field"),
                value="\n".join(lines),
                inline=False,
            )
        set_transparent_image(embed, interaction.client)
        await interaction.response.send_message(embed=embed)

    return boosts_group
