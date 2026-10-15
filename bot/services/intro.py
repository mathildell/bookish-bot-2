"""Introduction form: admin-managed questions and member introductions.

Ported from the old Ruby "Introduction" feature:
- admins add ordered, toggleable questions;
- a sticky message in the intro channel opens a modal form via a button;
- each member posts at most one introduction, which they can edit afterwards;
- introductions are embeds titled with the member's display name and their
  avatar as a thumbnail, with the question as field label and their answer as
  field value.
"""

from __future__ import annotations

from datetime import timezone

import discord
from sqlalchemy import delete, func, select

from ..db import session_scope
from ..models import IntroPost, IntroQuestion, IntroSetting
from ..texts import t

# Discord modals hold at most five text inputs.
MODAL_QUESTION_LIMIT = 5
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024


def blank_answer() -> str:
    """Placeholder shown for a question the member left unanswered."""
    return t("intro.blank_answer")


def select_modal_questions(questions: list) -> list:
    """Return the active questions that fit in one modal, in order."""
    return [question for question in questions if question.active][
        :MODAL_QUESTION_LIMIT
    ]


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _utc(value):
    """Attach UTC to a naive datetime so Discord renders it correctly."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def build_intro_embed(
    user: discord.abc.User,
    questions: list,
    answers: dict,
    last_edited_at,
    client: discord.Client,
) -> discord.Embed:
    """Build the embed posted for a member's introduction."""
    embed = discord.Embed(
        colour=getattr(client, "default_embed_color", None),
        timestamp=_utc(last_edited_at),
    )

    embed.title = discord.utils.escape_markdown(
        getattr(user, "display_name", None)
        or getattr(user, "name", str(user.id))
    )
    avatar = getattr(user, "display_avatar", None)
    avatar_url = getattr(avatar, "url", None)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    for question in questions:
        answer = (answers.get(str(question.id)) or "").strip() or blank_answer()
        embed.add_field(
            name=_truncate(question.content, FIELD_NAME_LIMIT),
            value=_truncate(answer, FIELD_VALUE_LIMIT),
            inline=False,
        )

    image = getattr(client, "transparent_embed_image", None)
    if image:
        embed.set_image(url=image)

    return embed


class IntroService:
    """CRUD for the intro channel, questions and member introductions."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self._channel_id: int | None = None
        self._sticky_message_id: int | None = None

    @property
    def channel_id(self) -> int | None:
        return self._channel_id

    @property
    def sticky_message_id(self) -> int | None:
        return self._sticky_message_id

    # ----------------------------------------------------------- settings

    async def reload(self) -> None:
        async with session_scope() as session:
            row = await self._find_setting(session)
        self._channel_id = row.channel_id if row is not None else None
        self._sticky_message_id = (
            row.sticky_message_id if row is not None else None
        )

    async def set_channel(
        self, channel_id: int
    ) -> tuple[int | None, int | None]:
        """Point the feature at ``channel_id``; return the previous setting."""
        async with session_scope() as session:
            row = await self._find_setting(session)
            previous = (
                (row.channel_id, row.sticky_message_id)
                if row is not None
                else (None, None)
            )
            if row is None:
                session.add(
                    IntroSetting(guild_id=self.guild_id, channel_id=channel_id)
                )
            else:
                row.channel_id = channel_id
                row.sticky_message_id = None
            await session.commit()
        self._channel_id = channel_id
        self._sticky_message_id = None
        return previous

    async def set_sticky(self, message_id: int) -> None:
        async with session_scope() as session:
            row = await self._find_setting(session)
            if row is None:
                return
            row.sticky_message_id = message_id
            await session.commit()
        self._sticky_message_id = message_id

    async def _find_setting(self, session) -> IntroSetting | None:
        return (
            await session.execute(
                select(IntroSetting).where(
                    IntroSetting.guild_id == self.guild_id
                )
            )
        ).scalar_one_or_none()

    # ---------------------------------------------------------- questions

    async def all_questions(self) -> list[IntroQuestion]:
        async with session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(IntroQuestion).order_by(
                            IntroQuestion.position, IntroQuestion.id
                        )
                    )
                )
                .scalars()
                .all()
            )

    async def active_questions(self) -> list[IntroQuestion]:
        async with session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(IntroQuestion)
                        .where(IntroQuestion.active.is_(True))
                        .order_by(IntroQuestion.position, IntroQuestion.id)
                    )
                )
                .scalars()
                .all()
            )

    async def get_question(self, question_id: int) -> IntroQuestion | None:
        async with session_scope() as session:
            return await self._find_question(session, question_id)

    async def add_question(
        self, content: str, placeholder: str | None, active: bool
    ) -> IntroQuestion:
        async with session_scope() as session:
            position = (
                await session.execute(select(func.max(IntroQuestion.position)))
            ).scalar()
            row = IntroQuestion(
                content=content,
                placeholder=placeholder,
                active=active,
                position=(position or 0) + 1,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def update_question(
        self,
        question_id: int,
        *,
        content: str | None = None,
        placeholder: str | None = None,
    ) -> IntroQuestion | None:
        async with session_scope() as session:
            row = await self._find_question(session, question_id)
            if row is None:
                return None
            if content is not None:
                row.content = content
            row.placeholder = placeholder
            await session.commit()
            await session.refresh(row)
        return row

    async def set_question_active(
        self, question_id: int, active: bool
    ) -> IntroQuestion | None:
        async with session_scope() as session:
            row = await self._find_question(session, question_id)
            if row is None:
                return None
            row.active = active
            await session.commit()
            await session.refresh(row)
        return row

    async def move_question(self, question_id: int, delta: int) -> bool:
        """Swap a question with its neighbour (``delta`` is -1 or +1)."""
        async with session_scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(IntroQuestion).order_by(
                            IntroQuestion.position, IntroQuestion.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            index = next(
                (i for i, row in enumerate(rows) if row.id == question_id),
                None,
            )
            if index is None:
                return False
            target = index + delta
            if target < 0 or target >= len(rows):
                return False
            rows[index].position, rows[target].position = (
                rows[target].position,
                rows[index].position,
            )
            await session.commit()
        return True

    async def remove_question(self, question_id: int) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                delete(IntroQuestion).where(IntroQuestion.id == question_id)
            )
            await session.commit()
        return result.rowcount > 0

    @staticmethod
    async def _find_question(session, question_id: int) -> IntroQuestion | None:
        return (
            await session.execute(
                select(IntroQuestion).where(IntroQuestion.id == question_id)
            )
        ).scalar_one_or_none()

    # -------------------------------------------------------------- posts

    async def get_post(self, user_id: int) -> IntroPost | None:
        async with session_scope() as session:
            return (
                await session.execute(
                    select(IntroPost).where(
                        IntroPost.guild_id == self.guild_id,
                        IntroPost.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()

    async def get_post_by_message(self, message_id: int) -> IntroPost | None:
        async with session_scope() as session:
            return (
                await session.execute(
                    select(IntroPost).where(
                        IntroPost.guild_id == self.guild_id,
                        IntroPost.message_id == message_id,
                    )
                )
            ).scalar_one_or_none()

    async def create_post(
        self,
        user_id: int,
        message_id: int,
        channel_id: int,
        answers: dict,
        now,
    ) -> IntroPost:
        async with session_scope() as session:
            row = IntroPost(
                guild_id=self.guild_id,
                user_id=user_id,
                message_id=message_id,
                channel_id=channel_id,
                answers=answers,
                first_posted_at=now,
                last_edited_at=now,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def update_post(
        self, post_id: int, answers: dict, now, message_id: int | None = None
    ) -> IntroPost | None:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(IntroPost).where(IntroPost.id == post_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.answers = answers
            row.last_edited_at = now
            if message_id is not None:
                row.message_id = message_id
            await session.commit()
            await session.refresh(row)
        return row

    async def delete_post(self, post_id: int) -> bool:
        async with session_scope() as session:
            result = await session.execute(
                delete(IntroPost).where(IntroPost.id == post_id)
            )
            await session.commit()
        return result.rowcount > 0
