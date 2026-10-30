"""Normalize stored Goodreads review feeds to profile URLs.

Goodreads retired the ``list_rss`` feeds; the review poller now reads each
member's profile updates. Run this once against a database that already has
``review_feeds`` rows:

    .venv/bin/python scripts/migrate_review_profiles.py

It rewrites legacy ``list_rss``/reviews URLs to
``https://www.goodreads.com/user/show/<id>`` and resets every ``feed_since`` to
now, so deploying the new poller does not post each member's existing recent
updates as a backlog. Running it again resets the tracking window again.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402
from bot.db import close_db, create_tables, init_db, session_scope  # noqa: E402
from bot.models import ReviewFeed  # noqa: E402
from bot.services.level import utcnow  # noqa: E402
from bot.services.reviews import normalize_profile_url  # noqa: E402


async def run(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    init_db(config.mysql_url)
    await create_tables()

    now = utcnow()
    normalized = 0
    skipped = 0
    async with session_scope() as session:
        feeds = (await session.scalars(select(ReviewFeed))).all()
        for feed in feeds:
            profile_url = normalize_profile_url(feed.feed_url)
            if profile_url is None:
                skipped += 1
                print(
                    f"Skipping member {feed.discord_user_id}: "
                    f"unrecognised URL {feed.feed_url}"
                )
                continue
            if feed.feed_url != profile_url:
                feed.feed_url = profile_url
                normalized += 1
            feed.feed_since = now
        await session.commit()

    print(
        f"Normalized {normalized} feed(s), reset {len(feeds) - skipped} "
        f"tracking window(s), skipped {skipped}."
    )
    await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(ROOT / "config.yaml"),
        help="Path to config.yaml",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
