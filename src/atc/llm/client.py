"""LLM client: OpenAI-compatible chat endpoint (OpenRouter, Anthropic compat, Google AI Studio compat,
NVIDIA NIM...) plus an offline stub so the loop works with no key at all.
A local model (Ollama) was tried and dropped: it made MSFS stutter and was too slow to answer.

Config comes from env vars:
    ATC_LLM_BASE_URL   e.g. https://openrouter.ai/api/v1   (empty -> stub mode)
    ATC_LLM_API_KEY
    ATC_LLM_TIMEOUT_S  per-attempt timeout, default 10
    ATC_LLM_MODEL      e.g. a free OpenRouter model while developing, Claude Haiku 4.5 later;
                       comma-separated list = fallback order (first that answers wins)
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request


class DailyQuotaExceeded(RuntimeError):
    """HTTP 429 for a per-day limit (OpenRouter free tier), as opposed to a short burst limit."""


class StubLLM:
    """Canned replies. Good enough to test the loop, the prompt builder and the voice."""

    def complete(self, messages: list[dict]) -> str:
        pilot = messages[-1]["content"].split("PILOT TRANSMISSION:", 1)[-1].strip().lower()
        if "taxi" in pilot:
            return "Taxi to the active runway via the main taxiway, hold short. (stub)"
        if "takeoff" in pilot or "ready" in pilot or "departure" in pilot:
            m = re.search(r"Runway in use \(computed[^)]*\): (\S+)", messages[-1]["content"])
            return f"Runway {m.group(1)}, cleared for takeoff. (stub)" if m else "Cleared for takeoff. (stub)"
        if "inbound" in pilot or "landing" in pilot:
            return "Enter the pattern, report downwind. (stub)"
        return "Say again. (stub)"


class OpenAICompatLLM:
    """`model` may be a comma-separated list: tried in order, next one on any failure (fallback)."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float = 10.0, retries: int = 1) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.models = [m.strip() for m in model.split(",") if m.strip()]
        self.timeout_s = timeout_s
        self.retries = retries  # extra tries on the SAME model for 429/5xx before falling back

    @property
    def model(self) -> str:
        return ", ".join(self.models)

    def _call(self, model: str, messages: list[dict]) -> str:
        payload = {"model": model, "messages": messages, "max_tokens": 120, "temperature": 0.3}
        # No thinking tokens (latency). Each provider spells it differently and rejects the others' field (HTTP 400).
        if "openrouter" in self.url:
            payload["reasoning"] = {"enabled": False}
        elif "googleapis" in self.url:
            payload["reasoning_effort"] = "none"
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    data = json.load(resp)
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if exc.code == 429 and "per-day" in detail:  # daily quota, not a burst: retrying can't help
                    raise DailyQuotaExceeded(detail) from None
                if exc.code in (429, 502, 503) and attempt < self.retries:
                    time.sleep(1.0)  # free tiers get rate-limited in bursts
                    continue
                raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
        text = clean_reply((data["choices"][0]["message"].get("content") or ""))
        if not text:  # e.g. a reasoning model spent all max_tokens thinking
            raise RuntimeError("empty reply")
        if looks_like_thinking(text):
            raise RuntimeError("reply looks like chain-of-thought, not a radio call")
        return text
        # TODO(Phase 5): streaming (stream=true, SSE) so TTS can start on the first sentence.

    def complete(self, messages: list[dict]) -> str:
        errors = []
        free_quota_gone = False
        for model in self.models:
            if free_quota_gone and model.endswith(":free"):
                continue  # OpenRouter's daily limit is shared by every :free model
            try:
                return self._call(model, messages)
            except DailyQuotaExceeded:
                free_quota_gone = model.endswith(":free")
                errors.append(f"{model}: daily quota used up")
                print(f"[LLM {model}: daily free quota used up (OpenRouter: 50/day, 1000/day after a one-time "
                      "10 credit top-up); resets 00:00 UTC]")
            except Exception as exc:  # HTTP error, timeout, bad JSON, empty reply -> next model
                errors.append(f"{model}: {exc}")
                print(f"[LLM {model} failed, trying next: {str(exc)[:120]}]")
        raise RuntimeError("all models failed: " + " | ".join(errors))


_THINKING_MARKERS = ("thinking process", "analyze the user", "analyze user input", "let me think", "step-by-step", "<think", "user safety", "response safety")


def looks_like_thinking(text: str) -> bool:
    """A controller reply is 1-2 short sentences. Long or meta text means a reasoning model leaked."""
    low = text.lower()
    return len(text.split()) > 70 or any(m in low for m in _THINKING_MARKERS)


def clean_reply(text: str) -> str:
    """Strip things a TTS voice should never read out loud."""
    text = re.sub(r"[*_`#>]", "", text)
    return " ".join(text.split())


def make_llm():
    base = os.environ.get("ATC_LLM_BASE_URL", "").strip()
    if not base:
        return StubLLM()
    return OpenAICompatLLM(
        base_url=base,
        api_key=os.environ.get("ATC_LLM_API_KEY", ""),
        model=os.environ.get("ATC_LLM_MODEL", ""),
        timeout_s=float(os.environ.get("ATC_LLM_TIMEOUT_S", "10")),
    )
