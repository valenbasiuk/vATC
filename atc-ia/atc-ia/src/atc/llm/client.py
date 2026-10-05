"""LLM client: OpenAI-compatible chat endpoint (OpenRouter, Anthropic compat, Google AI Studio compat,
Ollama...) plus an offline stub so the loop works with no key at all.

Config comes from env vars:
    ATC_LLM_BASE_URL   e.g. https://openrouter.ai/api/v1   (empty -> stub mode)
    ATC_LLM_API_KEY
    ATC_LLM_MODEL      e.g. a free OpenRouter model while developing, Claude Haiku 4.5 later
"""

from __future__ import annotations

import json
import os
import re
import urllib.request


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
    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float = 15.0) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s

    def complete(self, messages: list[dict]) -> str:
        body = json.dumps(
            {"model": self.model, "messages": messages, "max_tokens": 120, "temperature": 0.3}
        ).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            data = json.load(resp)
        return clean_reply(data["choices"][0]["message"]["content"])
        # TODO(Phase 5): streaming (stream=true, SSE) so TTS can start on the first sentence.


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
    )
