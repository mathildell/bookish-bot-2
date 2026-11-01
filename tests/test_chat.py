from __future__ import annotations

import unittest

from bot.services.chat import ChatService


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


if __name__ == "__main__":
    unittest.main()
