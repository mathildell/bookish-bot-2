"""Streak level coupons: render a card from a template and host the image.

A member who passes a streak level earns a coupon showing their display name,
the finished level, the date and the XP earned. The card is drawn onto a random
template from ``assets/coupons`` with Pillow, then uploaded to imgbb (falling
back to tmpfiles) and, whatever happens, the local file is deleted. When both
hosts fail the configured fallback image is used instead.
"""

from __future__ import annotations

import base64
import logging
import os
import random
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("bh2")

DEFAULT_FALLBACK_IMAGE = (
    "https://i.ibb.co/5NxxgrF/ticket.png"
)

_ASSETS = Path(__file__).resolve().parents[2] / "assets"
TEMPLATE_DIR = _ASSETS / "coupons"
FONT_REGULAR = _ASSETS / "fonts" / "FlowerBee.ttf"

IMGBB_URL = "https://api.imgbb.com/1/upload"
TMPFILES_URL = "https://tmpfiles.org/api/v1/upload"
TMPFILES_DIRECT_RE = re.compile(r'https://tmpfiles\.org/dl/[^"\'<>\s]+')
IMGBB_EXPIRATION_SECONDS = 7 * 24 * 3600

# Placement of each field on the 600x360 templates, tuned to the blank lines.
NAME_CENTER = (300, 112)
NAME_SIZE = 30
NAME_MAX_WIDTH = 320
LEVEL_CENTER = (436, 173)
LEVEL_SIZE = 22
LEVEL_MAX_WIDTH = 55
DATE_CENTER = (333, 206)
DATE_SIZE = 20
DATE_MAX_WIDTH = 155
XP_CENTER = (311, 252)
XP_SIZE = 22
XP_MAX_WIDTH = 155


class CouponError(Exception):
    """Raised when a coupon template or font cannot be loaded."""


@dataclass(frozen=True)
class Coupon:
    level: int
    xp: int
    on_date: date
    url: str


def coupon_date(on_date: date) -> str:
    """The coupon's date in the ``MMMM DD`` format it is rendered with."""
    return on_date.strftime("%B %d")


def _tmpfiles_direct_url(page: str) -> str | None:
    """The expiring direct-download link embedded in a tmpfiles page."""
    match = TMPFILES_DIRECT_RE.search(page)
    return match.group(0) if match else None


def _templates() -> list[Path]:
    templates = sorted(TEMPLATE_DIR.glob("*.png"))
    if not templates:
        raise CouponError(f"no coupon templates in {TEMPLATE_DIR}")
    return templates


def _fit(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: Path,
    start_size: int,
    max_width: int,
) -> tuple[ImageFont.FreeTypeFont, str]:
    """Largest font (down to 10pt) and text that fit ``max_width``."""
    size = start_size
    while size > 10:
        font = ImageFont.truetype(str(font_path), size)
        if draw.textlength(text, font=font) <= max_width:
            return font, text
        size -= 1
    font = ImageFont.truetype(str(font_path), 10)
    truncated = text
    while truncated and draw.textlength(f"{truncated}…", font=font) > max_width:
        truncated = truncated[:-1]
    return font, f"{truncated}…"


def _text_colours(image: Image.Image) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Pick a legible text colour and its outline from the template's brightness."""
    centre = image.crop((130, 60, 470, 300)).convert("L")
    brightness = centre.resize((1, 1)).getpixel((0, 0))
    if brightness > 150:
        return (48, 40, 32), (245, 240, 230)
    return (255, 255, 255), (44, 64, 46)


def render_coupon(
    *, display_name: str, level: int, on_date: date, xp: int
) -> Path:
    """Draw the coupon onto a random template, returning the temporary file."""
    template = random.choice(_templates())
    with Image.open(template) as opened:
        image = opened.convert("RGB")
    draw = ImageDraw.Draw(image)
    fill, stroke = _text_colours(image)

    fields = (
        (display_name, FONT_REGULAR, NAME_SIZE, NAME_MAX_WIDTH, NAME_CENTER),
        (str(level), FONT_REGULAR, LEVEL_SIZE, LEVEL_MAX_WIDTH, LEVEL_CENTER),
        (coupon_date(on_date), FONT_REGULAR, DATE_SIZE, DATE_MAX_WIDTH, DATE_CENTER),
        (str(xp), FONT_REGULAR, XP_SIZE, XP_MAX_WIDTH, XP_CENTER),
    )
    for text, font_path, size, max_width, (x, y) in fields:
        font, fitted = _fit(draw, text, font_path, size, max_width)
        draw.text(
            (x, y),
            fitted,
            font=font,
            fill=fill,
            anchor="ms",
            stroke_width=2,
            stroke_fill=stroke,
        )

    handle, path = tempfile.mkstemp(prefix="coupon-", suffix=".png")
    os.close(handle)
    image.save(path, "PNG")
    return Path(path)


class CouponUploadError(Exception):
    """Raised when every image host fails."""


class CouponService:
    """Renders coupons and hosts them, with imgbb -> tmpfiles -> fallback."""

    def __init__(self, imgbb_api_key: str | None = None, timeout: float = 30.0) -> None:
        self.imgbb_api_key = (imgbb_api_key or "").strip() or None
        self.timeout = timeout

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
        try:
            path = render_coupon(
                display_name=display_name, level=level, on_date=on_date, xp=xp
            )
            url = await self._upload(path)
        except Exception:
            logger.exception("Could not create the level %s coupon", level)
            url = fallback_image or DEFAULT_FALLBACK_IMAGE
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
        return Coupon(level=level, xp=xp, on_date=on_date, url=url)

    async def _upload(self, path: Path) -> str:
        errors: list[Exception] = []
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            uploaders = [self._upload_tmpfiles]
            if self.imgbb_api_key:
                uploaders.insert(0, self._upload_imgbb)
            for uploader in uploaders:
                try:
                    return await uploader(http, path)
                except Exception as exc:
                    errors.append(exc)
                    logger.warning("%s failed: %s", uploader.__name__, exc)
        raise CouponUploadError("; ".join(str(error) for error in errors))

    async def _upload_imgbb(
        self, http: aiohttp.ClientSession, path: Path
    ) -> str:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        params = {"key": self.imgbb_api_key, "expiration": str(IMGBB_EXPIRATION_SECONDS)}
        async with http.post(IMGBB_URL, params=params, data={"image": encoded}) as response:
            payload = await response.json(content_type=None)
        if not payload.get("success"):
            raise CouponUploadError(f"imgbb: {payload.get('error')}")
        return payload["data"]["url"]

    async def _upload_tmpfiles(
        self, http: aiohttp.ClientSession, path: Path
    ) -> str:
        form = aiohttp.FormData()
        with path.open("rb") as handle:
            form.add_field(
                "file", handle, filename=path.name, content_type="image/png"
            )
            async with http.post(TMPFILES_URL, data=form) as response:
                payload = await response.json(content_type=None)
        url = (payload.get("data") or {}).get("url")
        if not url:
            raise CouponUploadError(f"tmpfiles: {payload}")
        async with http.get(url) as response:
            page = await response.text()
        direct = _tmpfiles_direct_url(page)
        if direct is None:
            raise CouponUploadError("tmpfiles: direct download link not found")
        return direct
