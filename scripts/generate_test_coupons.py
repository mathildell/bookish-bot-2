"""Render streak coupons with made-up values for visual checks.

Writes PNGs to ``assets/tmp`` without touching the database or any image host.

Usage (from the repository root)::

    .venv/bin/python scripts/generate_test_coupons.py
    .venv/bin/python scripts/generate_test_coupons.py --username mememe --level 10
    .venv/bin/python scripts/generate_test_coupons.py --xp 1234 --date 2026-10-19
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.services.coupons import render_coupon  # noqa: E402

DEFAULT_OUTPUT_DIR = ROOT / "assets" / "tmp"


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return cleaned or "coupon"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate test streak coupons with bogus values."
    )
    parser.add_argument("--username", default="Test Reader")
    parser.add_argument("--level", type=int, default=10)
    parser.add_argument("--xp", type=int, default=999)
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=date.today(),
        metavar="YYYY-MM-DD",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Where to write the PNG (default: a name under assets/tmp).",
    )
    args = parser.parse_args()

    if args.output is not None:
        destination = args.output
    else:
        destination = (
            DEFAULT_OUTPUT_DIR
            / f"coupon-{_slug(args.username)}-L{args.level}-{args.date}.png"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)

    rendered = render_coupon(
        display_name=args.username,
        level=args.level,
        on_date=args.date,
        xp=args.xp,
    )
    try:
        shutil.move(str(rendered), destination)
    finally:
        rendered.unlink(missing_ok=True)

    print(destination)


if __name__ == "__main__":
    main()
