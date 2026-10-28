from __future__ import annotations

import unittest

from bot.services.chat import ChatService


class TestChat(unittest.TestCase):
    def test_build_system_prompt_no_bio(self) -> None:
        svc = ChatService({})
        prompt = svc.build_system_prompt("Dez", None)
        self.assertIn("You are the resident bot", prompt)
        self.assertIn("called Dez", prompt)
        self.assertNotIn("What you remember about them", prompt)

    def test_build_system_prompt_with_bio(self) -> None:
        svc = ChatService({})
        prompt = svc.build_system_prompt("Dez", "Hates poetry picks")
        self.assertIn("called Dez", prompt)
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


if __name__ == "__main__":
    unittest.main()
