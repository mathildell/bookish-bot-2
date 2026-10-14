"""Level reward admin commands (``/xp rewards ...``)."""

from __future__ import annotations

import discord
from discord import app_commands

from ..db import session_scope
from ..services import level as level_service
from ..services.level_rewards import (
    LevelRewardService,
    resolve_member,
    sync_member_roles,
)
from ..texts import t
from .checks import require_admin
from .listing import new_embed, set_transparent_image


def _rewards(interaction: discord.Interaction) -> LevelRewardService:
    return interaction.client.level_rewards


def register_rewards_group(xp_group: app_commands.Group) -> None:
    rewards_group = app_commands.Group(
        name="rewards",
        description=t("level.rewards.group_description"),
        parent=xp_group,
    )

    @rewards_group.command(
        name="set", description=t("level.rewards.set.description")
    )
    @app_commands.describe(
        level=t("level.rewards.set.param_level"),
        role=t("level.rewards.set.param_role"),
    )
    async def set_reward(
        interaction: discord.Interaction, level: int, role: discord.Role
    ) -> None:
        if not await require_admin(interaction):
            return
        if level < 0:
            level = 0
        await _rewards(interaction).set(level, role.id)
        await interaction.response.send_message(
            t("level.rewards.set.success", level=level, role=role.mention)
        )

    @rewards_group.command(
        name="remove", description=t("level.rewards.remove.description")
    )
    @app_commands.describe(level=t("level.rewards.remove.param_level"))
    async def remove_reward(
        interaction: discord.Interaction, level: int
    ) -> None:
        if not await require_admin(interaction):
            return
        if await _rewards(interaction).remove(level):
            await interaction.response.send_message(
                t("level.rewards.remove.success", level=level), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                t("level.rewards.remove.fail", level=level), ephemeral=True
            )

    @rewards_group.command(
        name="list", description=t("level.rewards.list.description")
    )
    async def list_rewards(interaction: discord.Interaction) -> None:
        if not await require_admin(interaction):
            return
        rewards = _rewards(interaction).rewards
        if not rewards:
            await interaction.response.send_message(t("level.rewards.list.empty"))
            return
        embed = new_embed(interaction.client)
        embed.title = t("level.rewards.list.title")
        guild = interaction.guild
        for lvl in sorted(rewards):
            role_id = rewards[lvl]
            role = guild.get_role(role_id) if guild else None
            value = role.mention if role else str(role_id)
            embed.add_field(
                name=f"{t('level.rewards.level_field')} {lvl}",
                value=value,
                inline=True,
            )
        set_transparent_image(embed, interaction.client)
        await interaction.response.send_message(embed=embed)

    @rewards_group.command(
        name="resync", description=t("level.rewards.resync.description")
    )
    async def resync(interaction: discord.Interaction) -> None:
        if not await require_admin(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                t("level.rewards.resync.done", count=0), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        service = _rewards(interaction)
        async with session_scope() as session:
            rows = await level_service.all_user_xp(session, guild_id=guild.id)

        count = 0
        for user_xp in rows:
            member = await resolve_member(guild, user_xp.user_id)
            if member is None:
                continue
            if await sync_member_roles(guild, member, user_xp.level, service):
                count += 1
        await interaction.followup.send(
            t("level.rewards.resync.done", count=count), ephemeral=True
        )

    return rewards_group
