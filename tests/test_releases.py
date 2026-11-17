"""Unit tests for the release announcement posting window."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime

from bot.services import releases as release_service


class PostHourTests(unittest.TestCase):
    def test_before_the_hour_is_not_due(self) -> None:
        self.assertFalse(
            release_service.is_post_due(
                datetime(2026, 11, 17, 9, 59), post_hour=13
            )
        )

    def test_on_the_hour_is_due(self) -> None:
        self.assertTrue(
            release_service.is_post_due(
                datetime(2026, 11, 17, 13, 0), post_hour=13
            )
        )

    def test_after_the_hour_is_due(self) -> None:
        self.assertTrue(
            release_service.is_post_due(
                datetime(2026, 11, 17, 18, 30), post_hour=13
            )
        )


class PostDueTests(unittest.TestCase):
    def test_posts_nothing_before_the_hour(self) -> None:
        service = release_service.ReleaseService(guild_id=1)
        service._post_hour = 13
        posted = asyncio.run(
            service.post_due(
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                now=datetime(2026, 11, 17, 8, 0),
            )
        )
        self.assertEqual(posted, 0)


if __name__ == "__main__":
    unittest.main()
