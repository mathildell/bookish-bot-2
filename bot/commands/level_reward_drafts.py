"""Draft level reward commands (``/xp draft ...``).

Temporary: lets admins build and show off a new reward list before it replaces
the live one. Remove this module (and its registration in ``level.py``) once
the new rewards are published.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..client import bot_of
from ..db import session_scope
from ..services import level as level_service
from ..services import level_reward_drafts as drafts
from ..services.embeds import new_embed, set_transparent_image
from ..services.level_rewards import LevelRewardService, sync_member_roles
from ..services.members import resolve_members
from ..texts import t
from .checks import require_admin
from .listing import chunked
from .views import ExpiringView, ensure_author

logger = logging.getLogger("bh2.level_reward_drafts")

# Discord's limit of embeds per message.
EMBEDS_PER_MESSAGE = 10


def _draft_embed(
    client: discord.Client, level: int, role_id: int, role: discord.Role | None
) -> discord.Embed:
    embed = new_embed(client)
    embed.title = t("level.draft.list.entry_title", level=level)
    if role is None:
        embed.description = t("level.draft.list.missing_role", role_id=role_id)
    else:
        icon = role.display_icon
        if isinstance(icon, discord.Asset):
            embed.set_thumbnail(url=icon.url)
            embed.description = role.mention
        elif icon:
            embed.description = f"{icon} {role.mention}"
        else:
            embed.description = role.mention
        if role.colour.value:
            embed.colour = role.colour
    set_transparent_image(embed, client)
    return embed


async def _resync(
    guild: discord.Guild, service: LevelRewardService, stale: set[int]
) -> int:
    """Strip the dropped reward roles, then sync every member's reward role."""
    async with session_scope() as session:
        rows = await level_service.all_user_xp(session, guild_id=guild.id)
    members = await resolve_members(guild, [row.user_id for row in rows])
    stale_roles = [role for rid in stale if (role := guild.get_role(rid))]
    count = 0
    for user_xp, member in zip(rows, members):
        if member is None:
            continue
        held = [role for role in stale_roles if role in member.roles]
        if held:
            try:
                await member.remove_roles(
                    *held, atomic=False, reason=t("level.rewards.reason")
                )
            except discord.HTTPException:
                logger.warning(
                    "Could not remove old reward roles from %s",
                    member.id,
                    exc_info=True,
                )
                continue
        if await sync_member_roles(guild, member, user_xp.level, service):
            count += 1
    return count


class PublishConfirmView(ExpiringView):
    """Ephemeral confirmation before the draft replaces the live rewards."""

    def __init__(self, draft: dict[int, int], author_id: int) -> None:
        super().__init__(timeout=120)
        self.draft = draft
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_author(interaction, self.author_id)

    @discord.ui.button(
        label=t("level.draft.publish.confirm_button"),
        style=discord.ButtonStyle.danger,
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content=t("level.draft.publish.working"), view=None
        )
        service = bot_of(interaction).level_rewards
        stale = drafts.stale_role_ids(service.rewards, self.draft)
        await drafts.publish(service.guild_id, self.draft)
        await service.reload()
        count = await _resync(interaction.guild, service, stale)
        await interaction.edit_original_response(
            content=t(
                "level.draft.publish.done",
                rewards=len(self.draft),
                count=count,
            )
        )

    @discord.ui.button(
        label=t("level.draft.publish.cancel_button"),
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content=t("level.draft.publish.cancelled"), view=None
        )


def register_draft_group(xp_group: app_commands.Group) -> None:
    draft_group = app_commands.Group(
        name="draft",
        description=t("level.draft.group_description"),
        parent=xp_group,
    )

    @draft_group.command(name="set", description=t("level.draft.set.description"))
    @app_commands.describe(
        level=t("level.rewards.set.param_level"),
        role=t("level.rewards.set.param_role"),
    )
    async def set_draft(
        interaction: discord.Interaction, level: int, role: discord.Role
    ) -> None:
        if not await require_admin(interaction):
            return
        level = max(level, 0)
        await drafts.set_draft(interaction.guild_id, level, role.id)
        await interaction.response.send_message(
            t("level.draft.set.success", level=level, role=role.mention),
            ephemeral=True,
        )

    @draft_group.command(
        name="remove", description=t("level.draft.remove.description")
    )
    @app_commands.describe(level=t("level.rewards.remove.param_level"))
    async def remove_draft(interaction: discord.Interaction, level: int) -> None:
        if not await require_admin(interaction):
            return
        key = (
            "level.draft.remove.success"
            if await drafts.remove_draft(interaction.guild_id, level)
            else "level.draft.remove.fail"
        )
        await interaction.response.send_message(t(key, level=level), ephemeral=True)

    @draft_group.command(name="list", description=t("level.draft.list.description"))
    async def list_draft(interaction: discord.Interaction) -> None:
        if not await require_admin(interaction):
            return
        draft = await drafts.all_drafts(interaction.guild_id)
        if not draft:
            await interaction.response.send_message(
                t("level.draft.empty"), ephemeral=True
            )
            return
        guild = interaction.guild
        embeds = [
            _draft_embed(
                interaction.client, level, draft[level], guild.get_role(draft[level])
            )
            for level in sorted(draft)
        ]
        messages = list(chunked(embeds, EMBEDS_PER_MESSAGE))
        await interaction.response.send_message(
            t("level.draft.list.header"), embeds=messages[0]
        )
        for chunk in messages[1:]:
            await interaction.followup.send(embeds=chunk)

    @draft_group.command(
        name="publish", description=t("level.draft.publish.description")
    )
    async def publish_draft(interaction: discord.Interaction) -> None:
        if not await require_admin(interaction):
            return
        draft = await drafts.all_drafts(interaction.guild_id)
        if not draft:
            await interaction.response.send_message(
                t("level.draft.empty"), ephemeral=True
            )
            return
        live = bot_of(interaction).level_rewards.rewards
        view = PublishConfirmView(draft, interaction.user.id)
        await interaction.response.send_message(
            t("level.draft.publish.confirm", live=len(live), draft=len(draft)),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    return draft_group
