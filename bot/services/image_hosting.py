"""Upload generated images to imgbb, falling back to tmpfiles.

The uploader verifies that the returned URL actually serves a valid image, so
Discord does not receive a link to an error page or a file that is not ready
yet. Both generated coupons and bi-weekly theme cards use this module.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
from pathlib import Path

import aiohttp
from PIL import Image

logger = logging.getLogger("bh2")

IMGBB_URL = "https://api.imgbb.com/1/upload"
TMPFILES_URL = "https://tmpfiles.org/api/v1/upload"
TMPFILES_DIRECT_RE = re.compile(r'https://tmpfiles\.org/dl/[^"\'<>\s]+')
IMAGE_VERIFY_ATTEMPTS = 3
IMAGE_VERIFY_DELAY = 1.0


class ImageHostError(Exception):
    """Raised when every image host fails."""


class ImageHost:
    """Uploads an image file to imgbb when configured, then to tmpfiles."""

    def __init__(self, imgbb_api_key: str | None = None, timeout: float = 30.0) -> None:
        self.imgbb_api_key = (imgbb_api_key or "").strip() or None
        self.timeout = timeout

    async def upload(self, path: Path, *, expiration_seconds: int) -> str:
        """Upload ``path`` and return a direct URL for the image."""
        errors: list[Exception] = []
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            uploaders = [self._upload_tmpfiles]
            if self.imgbb_api_key:
                uploaders.insert(0, self._upload_imgbb)
            for uploader in uploaders:
                try:
                    return await uploader(http, path, expiration_seconds)
                except Exception as exc:
                    errors.append(exc)
                    logger.warning("%s failed: %s", uploader.__name__, exc)
        raise ImageHostError("; ".join(str(error) for error in errors))

    async def _verify_image(self, http: aiohttp.ClientSession, url: str) -> None:
        """Reject a host URL that does not actually serve the uploaded image.

        The uploads can succeed while the host's CDN is down, serving an error
        page, or briefly not exposing the new file yet. Discord then fetches
        the URL too early and silently drops the image, so retry before
        handing the link over.
        """
        error: Exception | None = None
        for attempt in range(1, IMAGE_VERIFY_ATTEMPTS + 1):
            if attempt > 1:
                await asyncio.sleep(IMAGE_VERIFY_DELAY)
            try:
                await self._fetch_image(http, url)
                return
            except Exception as exc:
                error = exc
                logger.debug(
                    "Image not ready (attempt %d/%d): %s",
                    attempt,
                    IMAGE_VERIFY_ATTEMPTS,
                    exc,
                )
        raise ImageHostError(f"could not load {url}: {error}") from error

    async def _fetch_image(
        self, http: aiohttp.ClientSession, url: str
    ) -> None:
        async with http.get(url) as response:
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").lower()
            if content_type and not (
                content_type.startswith("image/")
                or content_type.startswith("application/octet-stream")
            ):
                raise ImageHostError(
                    f"host returned {content_type!r} instead of an image"
                )
            data = await response.read()
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
        except Exception as exc:
            raise ImageHostError(
                f"host returned an invalid image: {exc}"
            ) from exc

    async def _upload_imgbb(
        self, http: aiohttp.ClientSession, path: Path, expiration_seconds: int
    ) -> str:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        params = {
            "key": self.imgbb_api_key,
            "expiration": str(expiration_seconds),
        }
        async with http.post(IMGBB_URL, params=params, data={"image": encoded}) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
        if not payload.get("success"):
            raise ImageHostError(f"imgbb: {payload.get('error')}")
        url = payload["data"]["url"]
        await self._verify_image(http, url)
        return url

    async def _upload_tmpfiles(
        self, http: aiohttp.ClientSession, path: Path, expiration_seconds: int
    ) -> str:
        form = aiohttp.FormData()
        with path.open("rb") as handle:
            form.add_field(
                "file", handle, filename=path.name, content_type="image/png"
            )
            async with http.post(TMPFILES_URL, data=form) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        url = (payload.get("data") or {}).get("url")
        if not url:
            raise ImageHostError(f"tmpfiles: {payload}")
        async with http.get(url) as response:
            response.raise_for_status()
            page = await response.text()
        direct = _tmpfiles_direct_url(page)
        if direct is None:
            raise ImageHostError("tmpfiles: direct download link not found")
        await self._verify_image(http, direct)
        return direct


def _tmpfiles_direct_url(page: str) -> str | None:
    """The expiring direct-download link embedded in a tmpfiles page."""
    match = TMPFILES_DIRECT_RE.search(page)
    return match.group(0) if match else None
