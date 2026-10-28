from __future__ import annotations

import unittest

from bot.commands.bio import bio_group


class TestBio(unittest.TestCase):
    def test_bio_group_exists(self) -> None:
        self.assertEqual(bio_group.name, "bio")


if __name__ == "__main__":
    unittest.main()
