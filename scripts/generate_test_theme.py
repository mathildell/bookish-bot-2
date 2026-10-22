"""Render bi-weekly theme cards with made-up content for visual checks.

Writes PNGs to ``assets/tmp`` without touching the database or any image host.

Usage (from the repository root)::

    .venv/bin/python scripts/generate_test_theme.py
    .venv/bin/python scripts/generate_test_theme.py --prompt "A book with a map"
    .venv/bin/python scripts/generate_test_theme.py --start 2026-10-20 --end 2026-11-02
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.services.theme_images import render_theme  # noqa: E402

DEFAULT_OUTPUT_DIR = ROOT / "assets" / "tmp"
DEFAULT_PROMPT = "Read a book that features a library or a bookshop."
DEFAULT_DESCRIPTION = "Tell us which place you would visit and why."


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return cleaned or "theme"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate test bi-weekly theme cards with bogus values."
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--description", default=DEFAULT_DESCRIPTION)
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=date.today() + timedelta(days=1),
        metavar="YYYY-MM-DD",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
    )
    parser.add_argument(
        "--no-description",
        action="store_true",
        help="Render the prompt without a description.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Where to write the PNG (default: a name under assets/tmp).",
    )
    args = parser.parse_args()

    end = args.end or (args.start + timedelta(days=13))
    description = None if args.no_description else args.description
    if args.output is not None:
        destination = args.output
    else:
        destination = (
            DEFAULT_OUTPUT_DIR
            / f"theme-{_slug(args.prompt)[:60]}-{args.start}.png"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)

    rendered = render_theme(
        prompt=args.prompt,
        description=description,
        start=args.start,
        end=end,
    )
    try:
        shutil.move(str(rendered), destination)
    finally:
        rendered.unlink(missing_ok=True)

    print(destination)


if __name__ == "__main__":
    main()
