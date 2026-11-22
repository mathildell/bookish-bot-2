"""The bot client: a ``commands.Bot`` that carries the shared services.

Commands reach services through ``bot_of(interaction)`` so the attributes below
are declared (and type-checkable) in one place instead of being attached ad hoc.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from .services.coupons import DEFAULT_FALLBACK_IMAGE

if TYPE_CHECKING:
    from .services.birthdays import BirthdayService
    from .services.biweekly_theme import BiweeklyThemeService
    from .services.booster_react import BoosterReactService
    from .services.booster_role import BoosterRoleService
    from .services.chat import ChatService
    from .services.coupons import CouponService
    from .services.intro import IntroService
    from .services.level import WeeklyLeaderboardService
    from .services.level_rewards import LevelRewardService
    from .services.member_profiles import MemberProfileService
    from .services.release_sources import ReleaseScraper
    from .services.releases import ReleaseService
    from .services.reminders import ReminderService
    from .services.reviews import ReviewService
    from .services.settings import BotSettingsService
    from .services.streaks import StreakService
    from .services.tickets import TicketService
    from .services.view_messages import ViewMessageService
    from .services.xp_boosts import XpBoostService


class BookishBot(commands.Bot):
    booster_reacts: BoosterReactService
    booster_roles: BoosterRoleService
    level_rewards: LevelRewardService
    xp_boosts: XpBoostService
    reviews: ReviewService
    intros: IntroService
    releases: ReleaseService
    streaks: StreakService
    reminders: ReminderService
    birthdays: BirthdayService
    tickets: TicketService
    weekly: WeeklyLeaderboardService
    biweekly_theme: BiweeklyThemeService
    bot_settings: BotSettingsService
    view_messages: ViewMessageService
    coupons: CouponService
    release_scraper: ReleaseScraper
    member_profiles: MemberProfileService
    chat: ChatService

    # Runtime settings; loaded from the database in on_ready, so they start
    # disabled/unset until the first reload.
    booster_role_id: int = 0
    bot_updates_channel_id: int = 0
    admin_test_channel_id: int = 0
    reference_forum_channel_id: int = 0
    reference_channel_id: int = 0
    reference_message_id: int = 0
    transparent_embed_image: str | None = None
    default_embed_color: discord.Colour | None = None
    coupon_fallback_image: str = DEFAULT_FALLBACK_IMAGE


def bot_of(interaction: discord.Interaction) -> BookishBot:
    """The bot behind ``interaction``, typed so its services are known."""
    return interaction.client  # type: ignore[return-value]
