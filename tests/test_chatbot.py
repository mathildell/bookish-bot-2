from __future__ import annotations

import unittest

from bot.commands.chatbot import chatbot_group


class TestChatbot(unittest.TestCase):
    def test_chatbot_group_exists(self) -> None:
        self.assertEqual(chatbot_group.name, "chatbot")


if __name__ == "__main__":
    unittest.main()
