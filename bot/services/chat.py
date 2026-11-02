"""Local LLM chat via OpenAI-compatible API (Ollama/llama.cpp)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from typing import Any

import aiohttp

from ..texts import t

logger = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"([.!?])\s+")
# The model sometimes mirrors the prompt's {username: ..., message: ...} envelope.
_ENVELOPE_RE = re.compile(
    r"^\{\s*\"?username\"?\s*:.*?\"?message\"?\s*:\s*(?P<body>.*?)\s*\}\s*$",
    re.DOTALL | re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r"^```[\w-]*\s*\n?(?P<body>.*?)\n?```\s*$", re.DOTALL)
MODEL_CACHE_SECONDS = 30
MODEL_FETCH_TIMEOUT = 2.0
REPLY_MAX_LENGTH = 400
DEFAULT_HISTORY_DEPTH = 3


class ChatError(Exception):
    pass


class ChatCooldownError(ChatError):
    pass


class ChatTimeoutError(ChatError):
    pass


class ChatUnavailableError(ChatError):
    pass


def curl_command(url: str, payload: dict[str, Any]) -> str:
    """A copy-pasteable curl command reproducing a chat completion request."""
    body = shlex.quote(json.dumps(payload, ensure_ascii=False))
    return (
        f"curl -sS {shlex.quote(url)} "
        "-H 'Content-Type: application/json' "
        f"-d {body}"
    )


def _as_id(value: Any) -> int:
    """Coerce an id from message metadata or config to an int (0 when unusable)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clean_override(value: str | None) -> str | None:
    """Normalise a stored prompt override, treating blanks as unset."""
    text = str(value).strip() if value else ""
    return text or None


def configure_chat(client) -> None:
    """Push the stored chatbot settings into the running chat service."""
    service = getattr(client, "chat", None)
    settings = getattr(client, "bot_settings", None)
    if service is None or settings is None:
        return
    service.configure(
        getattr(settings, "chat_model", None),
        getattr(settings, "chat_persona", None),
        getattr(settings, "chat_prompt_name", None),
        getattr(settings, "chat_prompt_info", None),
    )


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
        # How many replied-to messages to walk up the reply ladder for context.
        self.history_depth = max(int(cfg.get("history") or DEFAULT_HISTORY_DEPTH), 0)
        # Filled in from the logged-in user once the bot is ready.
        self.bot_user_id = 0

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
        self.model = (
            str(model).strip() if model and str(model).strip() else self._default_model
        )
        self._persona_override = _clean_override(persona)
        self._prompt_name_override = _clean_override(prompt_name)
        self._prompt_info_override = _clean_override(prompt_info)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _ollama_root(self) -> str:
        """The Ollama native API root, i.e. ``base_url`` without its ``/v1``."""
        return (
            self.base_url[: -len("/v1")]
            if self.base_url.endswith("/v1")
            else self.base_url
        )

    @staticmethod
    def _format_model_display(name: str, details: dict | None = None) -> str:
        """The model name with its size or architecture when the API reports it."""
        details = details or {}
        meta = details.get("details")
        meta = meta if isinstance(meta, dict) else {}

        size = details.get("size") or details.get("size_bytes") or meta.get("size")
        if isinstance(size, int) and size > 0:
            gb = size / (1024**3)
            if gb >= 1:
                return f"{name} ({gb:.1f} GB)"
            return f"{name} ({size / 1024**2:.0f} MB)"

        for hint in (meta.get("parameter_size"), meta.get("family")):
            if hint:
                return f"{name} ({hint})"
        return name

    async def _fetch_models(
        self, session: aiohttp.ClientSession, url: str, items_key: str
    ) -> list[tuple[str, dict]]:
        """The models one endpoint lists, as ``(name, raw entry)`` pairs."""
        timeout = aiohttp.ClientTimeout(total=MODEL_FETCH_TIMEOUT)
        try:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status >= 400:
                    return []
                data = await resp.json()
        except Exception:
            logger.debug("Could not list chat models from %s", url, exc_info=True)
            return []

        found: list[tuple[str, dict]] = []
        for item in data.get(items_key) or []:
            name = item.get("id") or item.get("name") or item.get("model")
            if name:
                found.append((str(name), item))
        return found

    async def list_models(self) -> list[str]:
        now = time.monotonic()
        if self._models and now - self._models_at < MODEL_CACHE_SECONDS:
            return list(self._models)

        session = await self._get_session()
        # Try the OpenAI-compatible endpoint first, then Ollama's native tags.
        found = await self._fetch_models(session, f"{self.base_url}/models", "data")
        if not found:
            found = await self._fetch_models(
                session, f"{self._ollama_root()}/api/tags", "models"
            )
        if not found:
            logger.warning("Could not list chat models")

        details: dict[str, dict] = {}
        for name, item in found:
            details.setdefault(name, item)
        self._models = sorted(details)
        self._model_details = details
        self._models_at = now
        return list(self._models)

    async def list_models_detailed(self) -> list[tuple[str, str]]:
        models = await self.list_models()
        return [
            (self._format_model_display(name, self._model_details.get(name)), name)
            for name in models
        ]

    def _check_cooldown(self, guild_id: int, user_id: int) -> bool:
        """Whether the member may chat now, starting a new cooldown when so."""
        key = (guild_id, user_id)
        now = time.monotonic()
        last = self._cooldowns.get(key)
        if last is not None and now - last < self.cooldown_seconds:
            return False
        self._cooldowns[key] = now
        return True

    def build_system_prompt(self, display_name: str | None, bio: str | None) -> str:
        """The system message: persona plus the optional name and bio hints."""
        parts = [self._persona_override or t("chat.persona")]
        bio = (bio or "").strip()
        if display_name:
            template = self._prompt_name_override or t("chat.prompt_name")
            parts.append(template.replace("%{name}", display_name))
        if bio:
            template = self._prompt_info_override or t("chat.prompt_info")
            parts.append(template.replace("%{info}", bio))
        return "\n\n".join(parts)

    @staticmethod
    def clean_reply(text: str) -> str:
        """Unwrap a reply the model dressed up as JSON, an envelope or a fence."""
        reply = (text or "").strip()
        fenced = _CODE_FENCE_RE.match(reply)
        if fenced is not None:
            reply = fenced["body"].strip()
        enveloped = _ENVELOPE_RE.match(reply)
        if enveloped is not None:
            reply = enveloped["body"].strip()
        if len(reply) > 1 and reply[0] in "\"'" and reply[-1] == reply[0]:
            reply = reply[1:-1].strip()
        return reply

    @staticmethod
    def extract_reply(payload: dict[str, Any]) -> str:
        try:
            content = payload["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            return ""
        return str(content).strip()

    @staticmethod
    def truncate_reply(text: str, limit: int = REPLY_MAX_LENGTH) -> str:
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        truncated = text[: limit + 50]
        last_end = max(
            (
                match.end()
                for match in _SENTENCE_END.finditer(truncated)
                if match.end() <= limit
            ),
            default=0,
        )
        if last_end > 0:
            return truncated[:last_end].strip()
        return text[:limit].rstrip() + "..."

    def _history_entry(self, entry: dict | None) -> dict | None:
        """A replied-to message as a chat message, or None when there is none."""
        if not entry:
            return None
        author_id = _as_id(entry.get("author_id"))
        role = "assistant" if author_id == self.bot_user_id else "user"
        return {"role": role, "content": entry.get("message", "")}

    async def _request_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST the payload (one request at a time) and return the JSON body."""
        url = f"{self.base_url}/chat/completions"
        logger.info(
            "LLM request (model=%s, messages=%d):\n%s",
            self.model,
            len(payload["messages"]),
            curl_command(url, payload),
        )
        session = await self._get_session()
        async with self._lock:
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        logger.warning("Chat API error %s: %s", resp.status, text[:200])
                        raise ChatUnavailableError()
                    return await resp.json()
            except ChatError:
                raise
            except asyncio.TimeoutError:
                raise ChatTimeoutError()
            except Exception:
                logger.exception("Chat request failed")
                raise ChatUnavailableError()

    async def chat(
        self,
        guild_id: int,
        user_id: int,
        display_name: str | None,
        bio: str | None,
        user_message: str,
        history: list[dict] | None = None,
    ) -> str | None:
        if not self._check_cooldown(guild_id, user_id):
            raise ChatCooldownError()
        user_message = (user_message or "").strip()
        if not user_message:
            return None

        # History arrives oldest first; an empty list adds no context.
        messages = [
            {"role": "system", "content": self.build_system_prompt(display_name, bio)},
            *(self._history_entry(entry) for entry in history or [] if entry),
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
        data = await self._request_completion(payload)
        reply = self.clean_reply(self.extract_reply(data))
        if not reply:
            raise ChatUnavailableError()
        return self.truncate_reply(reply, REPLY_MAX_LENGTH)
