"""Streak level coupons: render a card from a template and host the image.

A member who passes a streak level earns a coupon showing their display name,
the finished level, the date and the XP earned. The card is drawn onto a random
template from ``assets/coupons`` with Pillow, then uploaded to imgbb (falling
back to tmpfiles) and, whatever happens, the local file is deleted. When both
hosts fail the configured fallback image is used instead.
"""

from __future__ import annotations

import logging
import os
import random
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw

from .image_hosting import ImageHost, ImageHostError
from .image_text import fit_single_line

logger = logging.getLogger("bh2")

DEFAULT_FALLBACK_IMAGE = (
    "https://i.ibb.co/5NxxgrF/ticket.png"
)

_ASSETS = Path(__file__).resolve().parents[2] / "assets"
TEMPLATE_DIR = _ASSETS / "coupons"
FONT_REGULAR = _ASSETS / "fonts" / "FlowerBee.ttf"

IMGBB_EXPIRATION_SECONDS = 7 * 24 * 3600

# Placement of each field on the 600x360 templates, tuned to the blank lines.
# (Horizontal center, then placement from top)
NAME_CENTER = (300, 118)
NAME_SIZE = 65
NAME_MAX_WIDTH = 350
LEVEL_CENTER = (436, 167)
LEVEL_SIZE = 35
LEVEL_MAX_WIDTH = 65
DATE_CENTER = (333, 200)
DATE_SIZE = 30
DATE_MAX_WIDTH = 170
XP_CENTER = (311, 247)
XP_SIZE = 40
XP_MAX_WIDTH = 170


class CouponError(Exception):
    """Raised when a coupon template or font cannot be loaded."""


CouponUploadError = ImageHostError


@dataclass(frozen=True)
class Coupon:
    level: int
    xp: int
    on_date: date
    url: str
    color: int | None = None


def coupon_date(on_date: date) -> str:
    """The coupon's date in the ``MMMM DD`` format it is rendered with."""
    return on_date.strftime("%B %d")


def _templates() -> list[Path]:
    templates = sorted(TEMPLATE_DIR.glob("*.png"))
    if not templates:
        raise CouponError(f"no coupon templates in {TEMPLATE_DIR}")
    return templates


def _extract_color_from_template(template: Path) -> int | None:
    """Extract the hex color from template filename like bg-9EA93F.png."""
    match = re.search(r"bg-([0-9a-fA-F]{6})", template.name)
    if match:
        try:
            return int(match.group(1), 16)
        except ValueError:
            return None
    return None


def render_coupon(
    *, display_name: str, level: int, on_date: date, xp: int
) -> tuple[Path, int | None]:
    """Draw the coupon onto a random template, returning the temporary file and color."""
    template = random.choice(_templates())
    color = _extract_color_from_template(template)
    with Image.open(template) as opened:
        image = opened.convert("RGBA")
    draw = ImageDraw.Draw(image)

    fields = (
        (display_name, FONT_REGULAR, NAME_SIZE, NAME_MAX_WIDTH, NAME_CENTER),
        (str(level), FONT_REGULAR, LEVEL_SIZE, LEVEL_MAX_WIDTH, LEVEL_CENTER),
        (coupon_date(on_date), FONT_REGULAR, DATE_SIZE, DATE_MAX_WIDTH, DATE_CENTER),
        (str(xp), FONT_REGULAR, XP_SIZE, XP_MAX_WIDTH, XP_CENTER),
    )
    for text, font_path, size, max_width, (x, y) in fields:
        font, fitted = fit_single_line(draw, text, font_path, size, max_width)
        draw.text(
            (x, y),
            fitted,
            font=font,
            fill=(255, 255, 255),
            anchor="ms",
        )

    handle, path = tempfile.mkstemp(prefix="coupon-", suffix=".png")
    os.close(handle)
    image.save(path, "PNG")
    return Path(path), color


class CouponService:
    """Renders coupons and hosts them, with imgbb -> tmpfiles -> fallback."""

    def __init__(self, imgbb_api_key: str | None = None, timeout: float = 30.0) -> None:
        self._host = ImageHost(imgbb_api_key, timeout)

    async def generate(
        self,
        *,
        display_name: str,
        level: int,
        on_date: date,
        xp: int,
        fallback_image: str,
    ) -> Coupon:
        """Render, upload and delete a coupon, using the fallback on any error."""
        path: Path | None = None
        color: int | None = None
        try:
            path, color = render_coupon(
                display_name=display_name, level=level, on_date=on_date, xp=xp
            )
            url = await self._host.upload(
                path, expiration_seconds=IMGBB_EXPIRATION_SECONDS
            )
        except Exception:
            logger.exception(
                "Could not create the level %s coupon; using the fallback image",
                level,
            )
            url = fallback_image or DEFAULT_FALLBACK_IMAGE
            color = None
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
        return Coupon(level=level, xp=xp, on_date=on_date, url=url, color=color)
