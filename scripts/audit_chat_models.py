"""Benchmark every installed Ollama model against the bot's live chat settings.

The system prompt is built by ``ChatService.build_system_prompt`` from the same
persona, name and info texts the bot uses (including the ``/chatbot`` overrides
stored in the database, unless ``--no-db`` is passed). Sampling values and
``num_ctx`` come from the ``ai`` block of ``config.yaml`` exactly as the bot
reads them, so the numbers match what members actually experience.

Requests go to Ollama's native ``/api/chat`` rather than the OpenAI-compatible
``/v1`` endpoint, because only the native one reports token counts and
durations. The messages and options are otherwise identical.

Each model is scored to help pick one:

* speed (60%) - the estimated latency of a typical reply, measured as the
  prefill time plus ``ASSUMED_REPLY_TOKENS / decode tok/s``;
* hygiene (40%) - whether replies volunteer facts from the member's bio,
  leak JSON or fences, run long, or break character with assistant hedging.

Usage (from the repository root):

    .venv/bin/python scripts/audit_chat_models.py --show-prompt
    .venv/bin/python scripts/audit_chat_models.py
    .venv/bin/python scripts/audit_chat_models.py gemma3:4b-it-qat qwen2.5:3b
    .venv/bin/python scripts/audit_chat_models.py --messages 1 --timeout 300 \\
        --max-tokens 90 --json audit.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402
from bot.services.chat import REPLY_MAX_LENGTH, ChatService  # noqa: E402

DEFAULT_NAME = "Squirrel"
DEFAULT_BIO = (
    "Squirrel: obsessed with Stonehenge and Terry Pratchett's Discworld."
)
DEFAULT_MESSAGES = (
    "ugh, mondays are the worst",
    "hey, what are you reading right now?",
    "i finally finished my book today!!",
)

# A short Discord reply, used to compare models on equal footing.
ASSUMED_REPLY_TOKENS = 70
GOOD_SECONDS = 6.0
SLOW_SECONDS = 20.0
SPEED_WEIGHT = 0.6
HYGIENE_WEIGHT = 0.4

PENALTIES = {
    "dump": 0.45,
    "format": 0.30,
    "long": 0.20,
    "meta": 0.15,
    "empty": 1.00,
}

STOPWORDS = frozenset(
    """about after again against also and any are because been before being
    biggest books both but can could does doing down during each enjoys even
    ever every fan favorite favourite for from had has have her here hers him
    his huge how into its just like likes loves more most much must never not
    now obsessed obsession off once only other our out over own reading really
    same she should some such than that the their them then there these they
    thing this those through too under until very was way were what when where
    which while who why will with would your""".split()
)

META_PATTERNS = (
    "as an ai",
    "language model",
    "as instructed",
    "my instructions",
    "stay in character",
    "as a bot",
    "i'm sorry, but",
    "i am sorry, but",
)

FORMAT_MARKERS = ("{", "}", "```", '"message"', '"username"')

_SENTENCE = re.compile(r"[.!?]+")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{3,}")


@dataclass
class Reply:
    text: str
    chars: int = 0
    sentences: int = 0
    decode_tps: float = 0.0
    prefill_seconds: float = 0.0
    wall: float = 0.0
    load_seconds: float = 0.0
    dump: list[str] = field(default_factory=list)
    format_leak: bool = False
    too_long: bool = False
    meta: bool = False
    error: str | None = None


@dataclass
class ModelReport:
    model: str
    size: str
    replies: list[Reply] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> list[Reply]:
        return [r for r in self.replies if r.error is None]

    def metric(self, name: str, default: float = 0.0) -> float:
        values = [getattr(r, name) for r in self.ok]
        if not values:
            return default
        return sum(values) / len(values)

    @property
    def decode_tps(self) -> float:
        values = [r.decode_tps for r in self.ok if r.decode_tps > 0]
        if not values:
            return 0.0
        values.sort()
        return values[len(values) // 2]

    @property
    def prefill_seconds(self) -> float:
        return self.metric("prefill_seconds")

    @property
    def latency(self) -> float:
        if not self.ok or self.decode_tps <= 0:
            return float("inf")
        return self.prefill_seconds + ASSUMED_REPLY_TOKENS / self.decode_tps

    @property
    def cold_load_seconds(self) -> float:
        return max((r.load_seconds for r in self.replies), default=0.0)

    @property
    def dump_rate(self) -> float:
        if not self.ok:
            return 0.0
        return sum(1 for r in self.ok if r.dump) / len(self.ok)

    @property
    def hygiene(self) -> float:
        if not self.ok:
            return 0.0
        scores = []
        for reply in self.ok:
            penalty = 0.0
            if reply.dump:
                penalty += PENALTIES["dump"]
            if reply.format_leak:
                penalty += PENALTIES["format"]
            if reply.too_long:
                penalty += PENALTIES["long"]
            if reply.meta:
                penalty += PENALTIES["meta"]
            if not reply.text.strip():
                penalty += PENALTIES["empty"]
            scores.append(max(0.0, 1.0 - min(1.0, penalty)))
        return sum(scores) / len(scores)

    @property
    def speed_score(self) -> float:
        if self.latency == float("inf"):
            return 0.0
        span = SLOW_SECONDS - GOOD_SECONDS
        return max(0.0, min(1.0, (SLOW_SECONDS - self.latency) / span))

    @property
    def score(self) -> float:
        if not self.ok:
            return 0.0
        return 100.0 * (
            SPEED_WEIGHT * self.speed_score + HYGIENE_WEIGHT * self.hygiene
        )


def ollama_root(base_url: str) -> str:
    """Strip the OpenAI-compatible suffix to reach Ollama's native API."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root


def http_json(url: str, payload: dict | None, timeout: float) -> dict:
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def list_installed_models(root: str, timeout: float) -> list[tuple[str, str]]:
    """Every locally installed model, as ``ollama list`` shows it."""
    payload = http_json(f"{root}/api/tags", None, timeout)
    found = []
    for entry in payload.get("models") or []:
        name = entry.get("name") or entry.get("model")
        if not name or "embed" in str(name).lower():
            continue
        size = entry.get("size") or 0
        if isinstance(size, int) and size > 0:
            found.append((str(name), f"{size / (1024 ** 3):.1f} GB"))
        else:
            found.append((str(name), "-"))
    return sorted(found)


def bio_keywords(bio: str, name: str | None) -> list[str]:
    """Distinctive words from the bio whose mention counts as recitation."""
    name_words = {word.lower() for word in _WORD.findall(name or "")}
    keywords = []
    for word in _WORD.findall(bio or ""):
        lowered = word.lower().strip("'").removesuffix("'s")
        if len(lowered) < 4 or lowered in STOPWORDS:
            continue
        if lowered in name_words or any(lowered in n for n in name_words):
            continue
        if lowered not in keywords:
            keywords.append(lowered)
    return keywords


def analyse(reply: str, keywords: list[str], max_chars: int) -> Reply:
    text = reply.strip()
    lowered = text.lower()
    sentences = [part for part in _SENTENCE.split(text) if part.strip()]
    return Reply(
        text=text,
        chars=len(text),
        sentences=len(sentences) or (1 if text else 0),
        dump=[word for word in keywords if word in lowered],
        format_leak=any(marker in text for marker in FORMAT_MARKERS),
        too_long=len(text) > min(max_chars, REPLY_MAX_LENGTH)
        or len(sentences) > 3,
        meta=any(pattern in lowered for pattern in META_PATTERNS),
    )


def db_prompts(guild_id: int, mysql_url: str) -> tuple[str | None, ...]:
    """The ``/chatbot`` persona/name/info overrides, or empty on any failure."""

    async def fetch() -> tuple[str | None, ...]:
        from bot.db import close_db, init_db
        from bot.services.settings import BotSettingsService

        init_db(mysql_url)
        try:
            service = BotSettingsService(guild_id)
            await service.reload()
        finally:
            await close_db()
        return (
            service.chat_persona,
            service.chat_prompt_name,
            service.chat_prompt_info,
        )

    try:
        return asyncio.run(fetch())
    except Exception as exc:  # noqa: BLE001 - the database is optional here
        print(f"! could not read /chatbot overrides ({exc}); using text defaults")
        return (None, None, None)


def ask_model(
    root: str,
    model: str,
    system: str,
    message: str,
    options: dict,
    timeout: float,
) -> tuple[Reply, dict]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ],
        "stream": False,
        "think": False,
        "options": options,
    }
    started = time.perf_counter()
    try:
        data = http_json(f"{root}/api/chat", payload, timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        return Reply(text="", error=f"HTTP {exc.code}: {body}"), payload
    except Exception as exc:  # noqa: BLE001 - reported per model
        return Reply(text="", error=f"{type(exc).__name__}: {exc}"), payload
    wall = time.perf_counter() - started

    content = ((data.get("message") or {}).get("content") or "").strip()
    eval_count = int(data.get("eval_count") or 0)
    eval_ns = int(data.get("eval_duration") or 0)
    prompt_ns = int(data.get("prompt_eval_duration") or 0)

    reply = Reply(
        text=content,
        wall=wall,
        decode_tps=(eval_count / (eval_ns / 1e9)) if eval_ns else 0.0,
        prefill_seconds=(prompt_ns / 1e9) if prompt_ns else 0.0,
        load_seconds=int(data.get("load_duration") or 0) / 1e9,
    )
    return reply, payload


def audit_model(
    root: str,
    model: str,
    size: str,
    system: str,
    messages: list[str],
    options: dict,
    timeout: float,
    keywords: list[str],
    max_chars: int,
) -> ModelReport:
    report = ModelReport(model=model, size=size)
    for message in messages:
        raw, _ = ask_model(root, model, system, message, options, timeout)
        if raw.error is not None:
            report.error = raw.error
            return report
        checked = analyse(raw.text, keywords, max_chars)
        checked.decode_tps = raw.decode_tps
        checked.prefill_seconds = raw.prefill_seconds
        checked.wall = raw.wall
        checked.load_seconds = raw.load_seconds
        report.replies.append(checked)
    return report


def render(settings: dict, reports: list[ModelReport], show_replies: bool) -> None:
    print()
    print(
        f"model={settings['model']} base_url={settings['base_url']} "
        f"num_ctx={settings['num_ctx']} temperature={settings['temperature']} "
        f"max_tokens={settings['max_tokens']}"
    )
    print(
        f"system prompt: {settings['prompt_chars']} chars, "
        f"~{settings['prompt_tokens']} tokens | bio keywords: "
        f"{', '.join(settings['keywords']) or '-'}"
    )
    print(
        "latency = prefill + "
        f"{ASSUMED_REPLY_TOKENS} tokens at the measured decode rate"
    )
    print()

    header = (
        f"{'model':<46} {'size':>8} {'tok/s':>7} {'pref':>6} "
        f"{'lat':>6} {'dump':>5} {'fmt':>4} {'long':>5} {'score':>6}"
    )
    print(header)
    print("-" * len(header))
    for report in sorted(reports, key=lambda r: r.score, reverse=True):
        if not report.ok:
            reason = (report.error or "no reply")[:60]
            print(f"{report.model:<46} {report.size:>8}   failed: {reason}")
            continue
        floods = sum(1 for r in report.ok if r.format_leak)
        longs = sum(1 for r in report.ok if r.too_long)
        print(
            f"{report.model:<46} {report.size:>8} {report.decode_tps:>7.1f} "
            f"{report.prefill_seconds:>6.1f} {report.latency:>6.1f} "
            f"{report.dump_rate * 100:>4.0f}% {floods:>4} {longs:>5} "
            f"{report.score:>6.1f}"
        )

    if show_replies:
        print()
        for report in reports:
            if not report.ok:
                continue
            sample = report.ok[0]
            text = " ".join(sample.text.split())
            if len(text) > 220:
                text = text[:217] + "..."
            print(f"{report.model} ({sample.wall:.1f}s, {sample.chars} chars):")
            print(f"  {text or '(empty)'}")
            if sample.dump:
                print(f"  ! volunteered: {', '.join(sample.dump)}")

    scored = [r for r in reports if r.ok]
    if not scored:
        print("\nNo model produced a reply.")
        return
    best = max(scored, key=lambda r: r.score)
    fastest = min(scored, key=lambda r: r.latency)
    cleanest = min(scored, key=lambda r: (r.dump_rate, r.latency))
    print()
    print(
        f"Best overall: {best.model} "
        f"(score {best.score:.1f}, latency {best.latency:.1f}s, "
        f"dump rate {best.dump_rate * 100:.0f}%)"
    )
    print(f"Fastest: {fastest.model} ({fastest.latency:.1f}s per reply)")
    print(
        f"Most self-contained: {cleanest.model} "
        f"(dump rate {cleanest.dump_rate * 100:.0f}%)"
    )
    print(
        "Score = "
        f"{int(SPEED_WEIGHT * 100)}% speed ({GOOD_SECONDS:.0f}s good, "
        f"{SLOW_SECONDS:.0f}s poor) + {int(HYGIENE_WEIGHT * 100)}% hygiene "
        "(no bio recitation, no JSON, 1-3 sentences, no assistant hedging)."
    )
    print(
        "Prefill is warm after the first request per model; each member's bio "
        "differs in production, so expect a little more."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "models",
        nargs="*",
        help="models to test (default: everything ollama list shows)",
    )
    parser.add_argument("--name", default=DEFAULT_NAME, help="sample display name")
    parser.add_argument("--bio", default=DEFAULT_BIO, help="sample member bio")
    parser.add_argument(
        "--messages",
        type=int,
        default=len(DEFAULT_MESSAGES),
        help=f"how many test messages to send (max {len(DEFAULT_MESSAGES)})",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="override the reply cap (default: max_tokens from config.yaml)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="per-request timeout in seconds (default: ai.timeout_seconds)",
    )
    parser.add_argument(
        "--exclude",
        default=None,
        help="regex of model names to skip",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Ollama URL to audit (default: ai.base_url from config.yaml)",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="ignore the /chatbot overrides stored in the database",
    )
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="print the system prompt and request body, then exit",
    )
    parser.add_argument(
        "--no-replies",
        action="store_true",
        help="hide the sample reply per model",
    )
    parser.add_argument("--json", default=None, help="write raw results here")
    args = parser.parse_args()

    config = load_config()
    service = ChatService(config.get("ai"))
    if args.base_url:
        service.base_url = args.base_url.rstrip("/")

    persona = name_prompt = info_prompt = None
    if not args.no_db:
        guild_id = int(config.get("guild_id") or 0)
        if guild_id:
            persona, name_prompt, info_prompt = db_prompts(
                guild_id, config.mysql_url
            )
    service.configure(None, persona, name_prompt, info_prompt)

    system = service.build_system_prompt(args.name, args.bio)
    root = ollama_root(service.base_url)
    max_tokens = args.max_tokens or service.max_tokens
    timeout = args.timeout or float(service.timeout_seconds)
    keywords = bio_keywords(args.bio, args.name)
    messages = list(DEFAULT_MESSAGES[: max(1, args.messages)])
    options = {
        "num_ctx": service.num_ctx,
        "temperature": service.temperature,
        "num_predict": max_tokens,
    }

    if args.show_prompt:
        print(f"POST {root}/api/chat")
        print(
            json.dumps(
                {
                    "model": "<each installed model>",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": messages[0]},
                    ],
                    "stream": False,
                    "think": False,
                    "options": options,
                },
                indent=2,
            )
        )
        return 0

    try:
        installed = list_installed_models(root, timeout)
    except Exception as exc:  # noqa: BLE001 - nothing to audit without Ollama
        print(f"Could not reach Ollama at {root}: {exc}")
        return 1

    if args.models:
        wanted = set(args.models)
        selected = [entry for entry in installed if entry[0] in wanted]
        missing = wanted - {entry[0] for entry in selected}
        for name in sorted(missing):
            selected.append((name, "-"))
    else:
        selected = installed
    if args.exclude:
        pattern = re.compile(args.exclude)
        selected = [e for e in selected if not pattern.search(e[0])]
    if not selected:
        print("No models selected.")
        return 1

    print(
        f"Auditing {len(selected)} model(s) with {len(messages)} message(s), "
        f"cap {max_tokens} tokens, timeout {timeout:.0f}s"
    )

    reports = []
    for index, (model, size) in enumerate(selected, start=1):
        print(f"[{index}/{len(selected)}] {model} ...", flush=True)
        report = audit_model(
            root,
            model,
            size,
            system,
            messages,
            options,
            timeout,
            keywords,
            max_tokens,
        )
        if report.error:
            print(f"  failed: {report.error[:160]}")
        else:
            print(
                f"  {report.decode_tps:.1f} tok/s, "
                f"prefill {report.prefill_seconds:.1f}s, "
                f"latency {report.latency:.1f}s, "
                f"dump rate {report.dump_rate * 100:.0f}%"
            )
        reports.append(report)

    settings = {
        "model": service.model,
        "base_url": service.base_url,
        "num_ctx": service.num_ctx,
        "temperature": service.temperature,
        "max_tokens": max_tokens,
        "prompt_chars": len(system),
        "prompt_tokens": round(len(system) / 3.6),
        "keywords": keywords,
    }
    render(settings, reports, not args.no_replies)

    if args.json:
        payload = {
            "settings": settings,
            "system_prompt": system,
            "models": [
                {
                    "model": report.model,
                    "size": report.size,
                    "error": report.error,
                    "score": round(report.score, 1),
                    "decode_tps": round(report.decode_tps, 2),
                    "prefill_seconds": round(report.prefill_seconds, 2),
                    "latency": round(report.latency, 2)
                    if report.ok
                    else None,
                    "cold_load_seconds": round(report.cold_load_seconds, 2),
                    "dump_rate": report.dump_rate,
                    "replies": [
                        {
                            "text": reply.text,
                            "chars": reply.chars,
                            "sentences": reply.sentences,
                            "dump": reply.dump,
                            "format_leak": reply.format_leak,
                            "too_long": reply.too_long,
                            "meta": reply.meta,
                            "wall": round(reply.wall, 2),
                        }
                        for reply in report.replies
                    ],
                }
                for report in reports
            ],
        }
        Path(args.json).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
