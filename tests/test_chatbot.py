from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bot.commands import chatbot as chatbot_cmds
from bot.commands.chatbot import chatbot_group
from bot.services.chat import ChatService
from bot.texts import t


class TestChatbot(unittest.TestCase):
    def test_chatbot_group_exists(self) -> None:
        self.assertEqual(chatbot_group.name, "chatbot")

    def test_settings_command_is_registered(self) -> None:
        names = {command.name for command in chatbot_group.commands}
        self.assertIn("settings", names)


class SettingsCommandTests(unittest.IsolatedAsyncioTestCase):
    def _interaction(self, *, enabled: bool, model: str | None):
        chat = ChatService({"model": "default-model"})
        chat.list_models = AsyncMock(return_value=["default-model", "other"])
        settings = SimpleNamespace(
            chat_enabled=enabled,
            chat_model=model,
            chat_persona="Be nice",
            chat_prompt_name=None,
            chat_prompt_info=None,
            default_embed_color=None,
            transparent_embed_image=None,
        )
        client = SimpleNamespace(
            chat=chat,
            bot_settings=settings,
            member_profiles=SimpleNamespace(all=AsyncMock(return_value=[1, 2])),
        )
        interaction = MagicMock()
        interaction.client = client
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction, chat

    async def _run(self, interaction) -> discord.Embed:
        with patch.object(
            chatbot_cmds, "require_admin", AsyncMock(return_value=True)
        ), patch.object(
            chatbot_cmds, "bot_of", lambda i: i.client
        ):
            await chatbot_cmds.chatbot_settings.callback(interaction)
        return interaction.followup.send.call_args.kwargs["embed"]

    @staticmethod
    def _fields(embed: discord.Embed) -> dict[str, str]:
        return {field.name: field.value for field in embed.fields}

    async def test_shows_the_active_model_and_state(self) -> None:
        interaction, chat = self._interaction(enabled=True, model="other")
        embed = await self._run(interaction)
        fields = self._fields(embed)
        self.assertEqual(fields[t("chatbot.settings.status_field")], t("chatbot.settings.status_on"))
        model = fields[t("chatbot.settings.model_field")]
        self.assertIn("`other`", model)
        self.assertIn(t("chatbot.settings.model_set"), model)
        self.assertIn(t("chatbot.settings.reach_ok"), model)
        prompts = fields[t("chatbot.settings.prompts_field")]
        self.assertIn(f"Persona: {t('chatbot.settings.custom')}", prompts)
        self.assertIn(f"Names instructions: {t('chatbot.settings.builtin')}", prompts)
        self.assertEqual(fields[t("chatbot.settings.bios_field")], "2")

    async def test_unset_model_falls_back_to_the_default(self) -> None:
        interaction, _ = self._interaction(enabled=True, model=None)
        fields = self._fields(await self._run(interaction))
        model = fields[t("chatbot.settings.model_field")]
        self.assertIn("`default-model`", model)
        self.assertIn(t("chatbot.settings.model_default"), model)

    async def test_missing_model_is_flagged(self) -> None:
        interaction, _ = self._interaction(enabled=True, model="ghost")
        fields = self._fields(await self._run(interaction))
        self.assertIn(
            t("chatbot.settings.reach_missing"),
            fields[t("chatbot.settings.model_field")],
        )

    async def test_off_never_contacts_the_model_server(self) -> None:
        interaction, chat = self._interaction(enabled=False, model=None)
        fields = self._fields(await self._run(interaction))
        chat.list_models.assert_not_called()
        self.assertEqual(fields[t("chatbot.settings.status_field")], t("chatbot.settings.status_off"))
        self.assertIn(
            t("chatbot.settings.reach_skipped"),
            fields[t("chatbot.settings.model_field")],
        )

    async def test_unreachable_server(self) -> None:
        interaction, chat = self._interaction(enabled=True, model=None)
        chat.list_models = AsyncMock(side_effect=RuntimeError("down"))
        fields = self._fields(await self._run(interaction))
        self.assertIn(
            t("chatbot.settings.reach_down"),
            fields[t("chatbot.settings.model_field")],
        )


if __name__ == "__main__":
    unittest.main()
