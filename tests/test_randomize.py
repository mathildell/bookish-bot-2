"""Unit tests for the ``/randomize`` command helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bot.commands import randomize


class TestRandomizeParse(unittest.TestCase):
    def test_split_comma_and_trim(self) -> None:
        self.assertEqual(
            randomize.parse_items("person, something, heya"),
            ["person", "something", "heya"],
        )

    def test_extra_whitespace(self) -> None:
        self.assertEqual(
            randomize.parse_items(" tea ,  coffee ,  water  "),
            ["tea", "coffee", "water"],
        )

    def test_trailing_commas_and_blanks(self) -> None:
        self.assertEqual(
            randomize.parse_items("a,b,,  ,c,"),
            ["a", "b", "c"],
        )

    def test_single_item_or_empty(self) -> None:
        self.assertEqual(randomize.parse_items("only"), ["only"])
        self.assertEqual(randomize.parse_items(""), [])
        self.assertEqual(randomize.parse_items("   "), [])
        self.assertEqual(randomize.parse_items(",,,"), [])

    def test_duplicates_kept_as_weights(self) -> None:
        self.assertEqual(
            randomize.parse_items("cat, dog, cat"),
            ["cat", "dog", "cat"],
        )


class TestRandomizeChoice(unittest.TestCase):
    def test_choice_from_parsed_list(self) -> None:
        items = randomize.parse_items("a, b, c")
        with patch("bot.commands.randomize.random.choice") as choice:
            choice.return_value = "b"
            winner = randomize.random.choice(items)
        self.assertEqual(winner, "b")
        choice.assert_called_once()


if __name__ == "__main__":
    unittest.main()
