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
MODEL_CACHE_SECONDS = 30
MODEL_FETCH_TIMEOUT = 2.0


class ChatService:
    def __init__(self, config: dict | None) -> None:
        cfg = config or {}
        self.base_url = (cfg.get("base_url") or "http://127.0.0.1:11434/v1").rstrip("/")
        self._default_model = cfg.get("model") or "qwen2.5:3b"
        self.model = self._default_model
        self.timeout_seconds = int(cfg.get("timeout_seconds") or 60)
        self.cooldown_seconds = int(cfg.get("cooldown_seconds") or 10)
        self.max_tokens = int(cfg.get("max_tokens") or 200)
        self.temperature = float(cfg.get("temperature") or 0.8)
        self.num_ctx = int(cfg.get("num_ctx") or 4096)

        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Semaphore(1)
        self._cooldowns: dict[tuple[int, int], float] = {}
        self._models: list[str] = []
        self._models_at: float = 0.0
        self._model_details: dict[str, dict] = {}
        self._persona_override: str | None = None
        self._prompt_name_override: str | None = None
        self._prompt_info_override: str | None = None

    def configure(
        self,
        model: str | None = None,
        persona: str | None = None,
        prompt_name: str | None = None,
        prompt_info: str | None = None,
    ) -> None:
        if model and str(model).strip():
            self.model = str(model).strip()
        else:
            self.model = self._default_model
        self._persona_override = persona.strip() if persona and str(persona).strip() else None
        self._prompt_name_override = (
            prompt_name.strip() if prompt_name and str(prompt_name).strip() else None
        )
        self._prompt_info_override = (
            prompt_info.strip() if prompt_info and str(prompt_info).strip() else None
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @staticmethod
    def _format_model_display(name: str, details: dict | None = None) -> str:
        details = details or {}
        parts = [name]
        size = details.get("size") or details.get("size_bytes") or details.get("details", {}).get("size")
        if isinstance(size, int) and size > 0:
            # bytes -> GB
            gb = size / (1024 ** 3)
            if gb >= 1:
                parts.append(f"({gb:.1f} GB)")
            else:
                mb = size / (1024 ** 2)
                parts.append(f"({mb:.0f} MB)")
        family = details.get("details", {}).get("family") if isinstance(details.get("details"), dict) else None
        parameter_size = details.get("details", {}).get("parameter_size") if isinstance(details.get("details"), dict) else None
        if parameter_size and not any("GB" in p or "MB" in p for p in parts):
            parts.append(f"({parameter_size})")
        elif family and len(parts) == 1:
            parts.append(f"({family})")
        return " ".join(parts)

    async def list_models(self) -> list[str]:
        now = time.monotonic()
        if now - self._models_at < MODEL_CACHE_SECONDS and self._models:
            return list(self._models)

        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=MODEL_FETCH_TIMEOUT)
        models: list[str] = []
        details: dict[str, dict] = {}
        # try OpenAI compatible first
        try:
            async with session.get(f"{self.base_url}/models", timeout=timeout) as resp:
                if resp.status < 400:
                    data = await resp.json()
                    for m in data.get("data") or []:
                        mid = m.get("id") or m.get("name")
                        if mid:
                            models.append(str(mid))
                            details[str(mid)] = m
        except Exception:
            pass
        # fallback to ollama api/tags
        if not models:
            try:
                root = self.base_url
                if root.endswith("/v1"):
                    root = root[: -len("/v1")]
                async with session.get(f"{root}/api/tags", timeout=timeout) as resp:
                    if resp.status < 400:
                        data = await resp.json()
                        for m in data.get("models") or []:
                            name = m.get("name") or m.get("model")
                            if name:
                                models.append(str(name))
                                details[str(name)] = m
            except Exception:
                logger.warning("Could not list chat models", exc_info=True)

        # deduplicate preserving order
        seen = set()
        ordered: list[str] = []
        for name in models:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        ordered = sorted(ordered)
        self._models = ordered
        self._model_details = details
        self._models_at = now
        return list(self._models)

    async def list_models_detailed(self) -> list[tuple[str, str]]:
        models = await self.list_models()
        result = []
        for name in models:
            result.append((self._format_model_display(name, self._model_details.get(name)), name))
        return result

    def _check_cooldown(self, guild_id: int, user_id: int) -> bool:
        key = (guild_id, user_id)
        now = time.monotonic()
        last = self._cooldowns.get(key)
        if last is not None and now - last < self.cooldown_seconds:
            return False
        self._cooldowns[key] = now
        return True

    def build_system_prompt(
        self,
        display_name: str | None,
        bio: str | None,
        persona: str | None = None,
        prompt_name: str | None = None,
        prompt_info: str | None = None,
    ) -> str:
        persona_text = (
            persona.strip()
            if persona and str(persona).strip()
            else (self._persona_override or t("chat.persona"))
        )
        parts = [persona_text]
        name_text = prompt_name if prompt_name and str(prompt_name).strip() else self._prompt_name_override
        if name_text and str(name_text).strip() and display_name:
            try:
                text = str(name_text)
                text = text.replace("%{name}", display_name)
                parts.append(text)
            except Exception:
                parts.append(t("chat.prompt_name", name=display_name))
        elif display_name:
            parts.append(t("chat.prompt_name", name=display_name))
        info_text = prompt_info if prompt_info and str(prompt_info).strip() else self._prompt_info_override
        if info_text and str(info_text).strip() and bio and str(bio).strip():
            try:
                text = str(info_text)
                text = text.replace("%{info}", str(bio).strip())
                parts.append(text)
            except Exception:
                parts.append(t("chat.prompt_info", info=str(bio).strip()))
        elif bio and str(bio).strip():
            parts.append(t("chat.prompt_info", info=str(bio).strip()))
        system_prompt = "\n\n".join(parts)
        return system_prompt

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
        
        logger.info("POST: %s: %s", url, payload)

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
