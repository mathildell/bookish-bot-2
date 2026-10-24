"""Bot-wide per-guild settings edited via ``/settings``.

These are the values that used to live in ``config.yaml`` (the updates channel,
the transparent embed background and the default embed colour). They are stored
per guild so ``/settings edit`` can change them at runtime.
"""

from __future__ import annotations

from sqlalchemy import select

from ..db import session_scope
from ..models import BotSetting


class BotSettingsService:
    """Persistence for the bot-wide settings shown by ``/settings list``."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self._updates_channel_id: int | None = None
        self._transparent_embed_image: str | None = None
        self._default_embed_color: str | None = None
        self._reference_forum_channel_id: int | None = None
        self._gen_chat_channel_id: int | None = None
        self._reference_message_link: str | None = None
        self._admin_test_channel_id: int | None = None
        self._coupon_fallback_image: str | None = None

    @property
    def updates_channel_id(self) -> int | None:
        return self._updates_channel_id

    @property
    def transparent_embed_image(self) -> str | None:
        return self._transparent_embed_image

    @property
    def default_embed_color(self) -> str | None:
        return self._default_embed_color

    @property
    def reference_forum_channel_id(self) -> int | None:
        return self._reference_forum_channel_id

    @property
    def gen_chat_channel_id(self) -> int | None:
        return self._gen_chat_channel_id

    @property
    def reference_message_link(self) -> str | None:
        return self._reference_message_link

    @property
    def admin_test_channel_id(self) -> int | None:
        return self._admin_test_channel_id

    @property
    def coupon_fallback_image(self) -> str | None:
        return self._coupon_fallback_image

    async def reload(self) -> None:
        async with session_scope() as session:
            row = await self._find(session)
        self._updates_channel_id = row.updates_channel_id if row else None
        self._transparent_embed_image = (
            row.transparent_embed_image if row else None
        )
        self._default_embed_color = row.default_embed_color if row else None
        self._reference_forum_channel_id = (
            row.reference_forum_channel_id if row else None
        )
        self._gen_chat_channel_id = (
            row.gen_chat_channel_id if row else None
        )
        self._reference_message_link = (
            row.reference_message_link if row else None
        )
        self._admin_test_channel_id = (
            row.admin_test_channel_id if row else None
        )
        self._coupon_fallback_image = (
            row.coupon_fallback_image if row else None
        )

    async def set_updates_channel(self, channel_id: int | None) -> None:
        await self._update(updates_channel_id=channel_id)
        self._updates_channel_id = channel_id

    async def set_transparent_embed_image(self, image: str | None) -> None:
        await self._update(transparent_embed_image=image)
        self._transparent_embed_image = image

    async def set_default_embed_color(self, color: str | None) -> None:
        await self._update(default_embed_color=color)
        self._default_embed_color = color

    async def set_reference_forum_channel(self, channel_id: int | None) -> None:
        await self._update(reference_forum_channel_id=channel_id)
        self._reference_forum_channel_id = channel_id

    async def set_gen_chat_channel(self, channel_id: int | None) -> None:
        await self._update(gen_chat_channel_id=channel_id)
        self._gen_chat_channel_id = channel_id

    async def set_reference_message_link(self, link: str | None) -> None:
        await self._update(reference_message_link=link)
        self._reference_message_link = link

    async def set_admin_test_channel(self, channel_id: int | None) -> None:
        await self._update(admin_test_channel_id=channel_id)
        self._admin_test_channel_id = channel_id

    async def set_coupon_fallback_image(self, image: str | None) -> None:
        await self._update(coupon_fallback_image=image)
        self._coupon_fallback_image = image

    async def _find(self, session) -> BotSetting | None:
        return (
            await session.execute(
                select(BotSetting).where(BotSetting.guild_id == self.guild_id)
            )
        ).scalar_one_or_none()

    async def _update(self, **values) -> None:
        async with session_scope() as session:
            row = await self._find(session)
            if row is None:
                row = BotSetting(guild_id=self.guild_id, **values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            await session.commit()
