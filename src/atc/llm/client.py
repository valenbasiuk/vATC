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
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass


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


# Free (or cheap) OpenAI-compatible providers. In ATC_LLM_MODEL write "<provider>:<model>"; the key comes from
# the env var named here (or the Windows user environment, so a `setx` key works without restarting VS Code).
PROVIDERS = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
}
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm")


def env_key(name: str) -> str:
    """Env var, else the Windows per-user value (HKCU\\Environment) that `setx` writes."""
    val = os.environ.get(name, "")
    if val or sys.platform != "win32":
        return val
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, name)[0])
    except OSError:
        return ""


@dataclass
class Endpoint:
    provider: str  # PROVIDERS key, or "custom" (ATC_LLM_BASE_URL)
    url: str
    key: str
    model: str

    @property
    def label(self) -> str:
        return self.model if self.provider == "custom" else f"{self.provider}:{self.model}"


def parse_endpoints(spec: str, base_url: str = "", api_key: str = "") -> list[Endpoint]:
    """'groq:llama-3.3-70b-versatile, gemini:gemini-2.5-flash-lite, nvidia/nemotron...:free' -> endpoints.
    Entries without a known provider prefix use ATC_LLM_BASE_URL / ATC_LLM_API_KEY (old style)."""
    out = []
    for item in (s.strip() for s in spec.split(",")):
        if not item:
            continue
        prefix, _, rest = item.partition(":")
        if prefix in PROVIDERS and rest:
            url, key_var = PROVIDERS[prefix]
            out.append(Endpoint(prefix, url, env_key(key_var), rest))
        elif base_url:
            provider = next((p for p, (u, _) in PROVIDERS.items() if u.split("/")[2] in base_url), "custom")
            key = api_key or (env_key(PROVIDERS[provider][1]) if provider != "custom" else "")
            out.append(Endpoint(provider, base_url.rstrip("/"), key, item))
    return out


class OpenAICompatLLM:
    """`model` may be a comma-separated list: tried in order, next one on any failure (fallback). Each entry may
    name its own provider ("groq:..."), so several free quotas stack. `deadline_s` caps one whole reply."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float = 10.0, retries: int = 1,
                 deadline_s: float = 12.0) -> None:
        self.endpoints = parse_endpoints(model, base_url, api_key)
        self.timeout_s = timeout_s
        self.retries = retries  # extra tries on the SAME model for 429/5xx before falling back
        self.deadline_s = deadline_s
        self.quota_gone: set[str] = set()  # providers (or "openrouter:free") out of daily quota this run

    @property
    def models(self) -> list[str]:
        return [e.label for e in self.endpoints]

    @property
    def model(self) -> str:
        return ", ".join(self.models)

    def _call(self, ep: Endpoint, messages: list[dict]) -> str:
        payload = {"model": ep.model, "messages": messages, "max_tokens": 120, "temperature": 0.3}
        # No thinking tokens (latency). Each provider spells it differently and rejects the others' field (HTTP 400).
        if "openrouter" in ep.url:
            payload["reasoning"] = {"enabled": False}
        elif "googleapis" in ep.url:
            payload["reasoning_effort"] = "none"
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            ep.url + "/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {ep.key}"},
        )
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    data = json.load(resp)
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                squashed = re.sub(r"[\s_-]", "", detail.lower())
                if exc.code == 429 and ("perday" in squashed or "rpd" in squashed or "tpd" in squashed):
                    raise DailyQuotaExceeded(detail) from None  # daily quota, not a burst: retrying can't help
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

    @staticmethod
    def _quota_group(ep: Endpoint) -> str:
        # OpenRouter's free daily limit is shared by every :free model; elsewhere a quota is per model.
        if ep.provider == "openrouter" and ep.model.endswith(":free"):
            return "openrouter:free"
        return ep.label

    def complete(self, messages: list[dict]) -> str:
        errors = []
        start = time.monotonic()
        for ep in self.endpoints:
            if self._quota_group(ep) in self.quota_gone:
                continue
            if not ep.key:
                errors.append(f"{ep.label}: no API key")
                continue
            left = self.deadline_s - (time.monotonic() - start)
            if left <= 0.5:
                errors.append("deadline reached")
                break
            future = _POOL.submit(self._call, ep, messages)
            try:
                return future.result(timeout=left)
            except FutureTimeout:
                errors.append(f"{ep.label}: no answer in {left:.0f} s")
                print(f"[LLM {ep.label} too slow, trying next]")
            except DailyQuotaExceeded:
                self.quota_gone.add(self._quota_group(ep))
                errors.append(f"{ep.label}: daily quota used up")
                print(f"[LLM {ep.label}: daily free quota used up for this run, skipping it]")
            except Exception as exc:  # HTTP error, timeout, bad JSON, empty reply -> next model
                errors.append(f"{ep.label}: {exc}")
                print(f"[LLM {ep.label} failed, trying next: {str(exc)[:120]}]")
        raise RuntimeError("all models failed: " + (" | ".join(errors) or "every model is out of daily quota"))


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
    """Stub when nothing is configured. ATC_LLM_MODEL with provider prefixes needs no ATC_LLM_BASE_URL."""
    base = os.environ.get("ATC_LLM_BASE_URL", "").strip()
    spec = os.environ.get("ATC_LLM_MODEL", "").strip()
    llm = OpenAICompatLLM(
        base_url=base,
        api_key=os.environ.get("ATC_LLM_API_KEY", ""),
        model=spec,
        timeout_s=float(os.environ.get("ATC_LLM_TIMEOUT_S", "10")),
        deadline_s=float(os.environ.get("ATC_LLM_DEADLINE_S", "12")),
    )
    if not llm.endpoints:
        return StubLLM()
    missing = [e.label for e in llm.endpoints if not e.key]
    if missing:
        print(f"[LLM: no API key for {', '.join(missing)}; those are skipped]")
    return llm
