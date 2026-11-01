from __future__ import annotations

import unittest
from types import SimpleNamespace

from bot.commands.chat import is_utility_message, mentions_user
from bot.services.chat import ChatService


def _message(**overrides: object) -> SimpleNamespace:
    data: dict[str, object] = {
        "content": "a plain reply",
        "embeds": [],
        "attachments": [],
        "components": [],
        "mentions": [],
        "role_mentions": [],
        "mention_everyone": False,
        "pinned": False,
        "interaction_metadata": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


class TestChat(unittest.TestCase):
    def test_build_system_prompt_no_bio(self) -> None:
        svc = ChatService({})
        prompt = svc.build_system_prompt("Dez", None)
        self.assertIn("bookish Discord server", prompt)
        self.assertIn("Dez is an exact display name", prompt)
        self.assertNotIn("What you remember about them", prompt)

    def test_build_system_prompt_with_bio(self) -> None:
        svc = ChatService({})
        prompt = svc.build_system_prompt("Dez", "Hates poetry picks")
        self.assertIn("Dez is an exact display name", prompt)
        self.assertIn("What you remember about them: Hates poetry picks", prompt)

    def test_truncate_reply_at_sentence(self) -> None:
        svc = ChatService({})
        text = "Hello there. This is a long sentence that goes on."
        result = svc.truncate_reply(text, 20)
        self.assertTrue(len(result) <= 20 or result.endswith("..."))
        self.assertIn("Hello there.", result)

    def test_truncate_reply_hard(self) -> None:
        svc = ChatService({})
        text = "This is a very long sentence without ending punctuation"
        result = svc.truncate_reply(text, 10)
        self.assertEqual(result, "This is a...")

    def test_extract_reply(self) -> None:
        svc = ChatService({})
        payload = {"choices": [{"message": {"content": "  Hi there  "}}]}
        self.assertEqual(svc.extract_reply(payload), "Hi there")

    def test_clean_reply_strips_envelope(self) -> None:
        svc = ChatService({})
        reply = "{username: mtrics, message: Ah, a fellow fan! Hehe.}"
        self.assertEqual(svc.clean_reply(reply), "Ah, a fellow fan! Hehe.")

    def test_clean_reply_strips_json_envelope(self) -> None:
        svc = ChatService({})
        reply = '{"username": "mtrics", "message": "Sure thing."}'
        self.assertEqual(svc.clean_reply(reply), "Sure thing.")

    def test_clean_reply_strips_code_fence(self) -> None:
        svc = ChatService({})
        self.assertEqual(svc.clean_reply("```json\nHi there\n```"), "Hi there")

    def test_clean_reply_keeps_plain_text(self) -> None:
        svc = ChatService({})
        self.assertEqual(svc.clean_reply("  Plain reply.  "), "Plain reply.")


class TestUtilityMessage(unittest.TestCase):
    def test_plain_text_is_conversational(self) -> None:
        self.assertFalse(is_utility_message(_message()))  # type: ignore[arg-type]

    def test_embed_is_utility(self) -> None:
        message = _message(embeds=[object()])
        self.assertTrue(is_utility_message(message))  # type: ignore[arg-type]

    def test_attachment_is_utility(self) -> None:
        message = _message(attachments=[object()])
        self.assertTrue(is_utility_message(message))  # type: ignore[arg-type]

    def test_button_is_utility(self) -> None:
        message = _message(components=[object()])
        self.assertTrue(is_utility_message(message))  # type: ignore[arg-type]

    def test_member_ping_is_utility(self) -> None:
        message = _message(mentions=[object()])
        self.assertTrue(is_utility_message(message))  # type: ignore[arg-type]

    def test_role_ping_is_utility(self) -> None:
        message = _message(role_mentions=[object()])
        self.assertTrue(is_utility_message(message))  # type: ignore[arg-type]

    def test_everyone_ping_is_utility(self) -> None:
        message = _message(mention_everyone=True)
        self.assertTrue(is_utility_message(message))  # type: ignore[arg-type]

    def test_pinned_is_utility(self) -> None:
        message = _message(pinned=True)
        self.assertTrue(is_utility_message(message))  # type: ignore[arg-type]

    def test_plain_command_output_is_conversational(self) -> None:
        message = _message(interaction_metadata=object())
        self.assertFalse(is_utility_message(message))  # type: ignore[arg-type]

    def test_rich_command_output_is_utility(self) -> None:
        message = _message(interaction_metadata=object(), embeds=[object()])
        self.assertTrue(is_utility_message(message))  # type: ignore[arg-type]


class TestMentionsUser(unittest.TestCase):
    def test_plain_mention(self) -> None:
        self.assertTrue(mentions_user("<@42> are you proud?", 42))

    def test_nickname_mention(self) -> None:
        self.assertTrue(mentions_user("look <@!42>", 42))

    def test_other_user(self) -> None:
        self.assertFalse(mentions_user("<@7> hi", 42))

    def test_no_mention(self) -> None:
        self.assertFalse(mentions_user("yay!!", 42))


if __name__ == "__main__":
    unittest.main()
