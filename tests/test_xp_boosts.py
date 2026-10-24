from __future__ import annotations

import unittest

from bot.services.xp_boosts import apply_boost, boost_percent


class XpBoostTests(unittest.TestCase):
    def test_apply_boost_rounds_down(self) -> None:
        self.assertEqual(apply_boost(20, 5), 21)
        self.assertEqual(apply_boost(20, 10), 22)
        self.assertEqual(apply_boost(0, 50), 0)
        self.assertEqual(apply_boost(15, 0), 15)

    def test_boost_percent_additive(self) -> None:
        role_boosts = {1: 5, 2: 10}
        channel_boosts = {10: 10}
        percent = boost_percent(
            role_boosts,
            channel_boosts,
            role_ids=[1, 2],
            channel_ids=[10],
        )
        self.assertEqual(percent, 25)

    def test_boost_percent_ignores_duplicates_and_missing(self) -> None:
        role_boosts = {1: 5}
        channel_boosts = {}
        percent = boost_percent(
            role_boosts,
            channel_boosts,
            role_ids=[1, 1, 99],
            channel_ids=[5],
        )
        self.assertEqual(percent, 5)

    def test_boost_percent_parent_inheritance(self) -> None:
        channel_boosts = {99: 10}
        percent = boost_percent(
            {},
            channel_boosts,
            role_ids=[],
            channel_ids=[123, 99],
        )
        self.assertEqual(percent, 10)


if __name__ == "__main__":
    unittest.main()
