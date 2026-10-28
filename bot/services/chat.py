"""Local LLM chat via OpenAI-compatible API (Ollama/llama.cpp)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import aiohttp

from ..texts import t

logger = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"([.!?])\s+")


class ChatService:
    def __init__(self, config: dict | None) -> None:
        cfg = config or {}
        self.base_url = (cfg.get("base_url") or "http://127.0.0.1:11434/v1").rstrip("/")
        self.model = cfg.get("model") or "qwen2.5:3b"
        self.timeout_seconds = int(cfg.get("timeout_seconds") or 60)
        self.cooldown_seconds = int(cfg.get("cooldown_seconds") or 10)
        self.max_tokens = int(cfg.get("max_tokens") or 200)
        self.temperature = float(cfg.get("temperature") or 0.8)
        self.num_ctx = int(cfg.get("num_ctx") or 4096)

        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Semaphore(1)
        self._cooldowns: dict[tuple[int, int], float] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _check_cooldown(self, guild_id: int, user_id: int) -> bool:
        key = (guild_id, user_id)
        now = time.monotonic()
        last = self._cooldowns.get(key)
        if last is not None and now - last < self.cooldown_seconds:
            return False
        self._cooldowns[key] = now
        return True

    def build_system_prompt(self, display_name: str | None, bio: str | None) -> str:
        persona = t("chat.persona")
        parts = [persona]
        if display_name:
            parts.append(f"The person talking to you is called {display_name}.")
        if bio and bio.strip():
            parts.append(f"What you remember about them: {bio.strip()}")
        return "\n\n".join(parts)

    @staticmethod
    def extract_reply(payload: dict[str, Any]) -> str:
        try:
            choices = payload.get("choices") or []
            if not choices:
                return ""
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
            if not content:
                return ""
            return str(content).strip()
        except Exception:
            return ""

    @staticmethod
    def truncate_reply(text: str, limit: int = 400) -> str:
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        # try to cut at sentence boundary
        truncated = text[: limit + 50]
        # find last sentence end before limit
        last = -1
        for match in _SENTENCE_END.finditer(truncated):
            end = match.end()
            if end <= limit:
                last = end
        if last > 0:
            return truncated[:last].strip()
        return text[:limit].rstrip() + "..."

    async def chat(self, guild_id: int, user_id: int, display_name: str | None, bio: str | None, user_message: str) -> str | None:
        if not self._check_cooldown(guild_id, user_id):
            raise ChatCooldownError()
        user_message = (user_message or "").strip()
        if not user_message:
            return None
        system = self.build_system_prompt(display_name, bio)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
            "options": {"num_ctx": self.num_ctx},
        }
        session = await self._get_session()
        url = f"{self.base_url}/chat/completions"
        async with self._lock:
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        logger.warning("Chat API error %s: %s", resp.status, text[:200])
                        raise ChatUnavailableError()
                    data = await resp.json()
            except ChatUnavailableError:
                raise
            except asyncio.TimeoutError:
                raise ChatTimeoutError()
            except Exception:
                logger.exception("Chat request failed")
                raise ChatUnavailableError()
        reply = self.extract_reply(data)
        if not reply:
            raise ChatUnavailableError()
        return self.truncate_reply(reply, 400)


class ChatError(Exception):
    pass


class ChatCooldownError(ChatError):
    pass


class ChatTimeoutError(ChatError):
    pass


class ChatUnavailableError(ChatError):
    pass
