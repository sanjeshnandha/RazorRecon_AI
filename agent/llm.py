"""
One adapter over the OpenAI-compatible chat-completions endpoints that Gemini
and Grok both expose.

Deliberately written against urllib rather than a vendor SDK. The whole project
has six dependencies and no vendor lock-in; the agent should not be the thing
that breaks either. The wire format is small enough that an SDK buys nothing.

Configuration is entirely environment-driven:

    FINCTL_LLM_PROVIDER   gemini | grok            (default: whichever key is set)
    FINCTL_LLM_MODEL      overrides the default model for that provider
    FINCTL_LLM_BASE_URL   overrides the endpoint entirely
    GEMINI_API_KEY / GOOGLE_API_KEY
    XAI_API_KEY / GROK_API_KEY

If no key is present the panel degrades to a clear message rather than an
exception -- a missing key must never take the reconciliation demo down with it.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

PROVIDERS = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "keys": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "label": "Google Gemini",
    },
    "grok": {
        "base_url": "https://api.x.ai/v1",
        "model": "grok-4-fast",
        "keys": ("XAI_API_KEY", "GROK_API_KEY"),
        "label": "xAI Grok",
    },
}

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMUnavailable(RuntimeError):
    """No key configured, or the provider could not be reached. Callers turn
    this into an explanatory response rather than a 500 -- the engine's numbers
    are already computed and must stay readable without the agent."""


@dataclass
class LLMConfig:
    provider: str
    label: str
    model: str
    base_url: str
    api_key: str = field(repr=False, default="")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


def resolve_config() -> LLMConfig:
    """Pick a provider from the environment. An explicit FINCTL_LLM_PROVIDER
    wins; otherwise the first provider with a key present, in declaration order."""
    wanted = (os.environ.get("FINCTL_LLM_PROVIDER") or "").strip().lower()
    order = [wanted] if wanted in PROVIDERS else list(PROVIDERS)

    chosen, key = None, ""
    for name in order:
        spec = PROVIDERS[name]
        found = next((os.environ[k] for k in spec["keys"] if os.environ.get(k)), "")
        if found or (wanted and name == wanted):
            chosen, key = name, found
            break
    if chosen is None:
        chosen = order[0]

    spec = PROVIDERS[chosen]
    return LLMConfig(
        provider=chosen,
        label=spec["label"],
        model=os.environ.get("FINCTL_LLM_MODEL") or spec["model"],
        base_url=(os.environ.get("FINCTL_LLM_BASE_URL") or spec["base_url"]).rstrip("/"),
        api_key=key.strip(),
    )


class LLMClient:
    """Minimal chat-completions client with tool-calling.

    `complete` takes the OpenAI message/tool shapes and returns the raw assistant
    message dict, so the caller owns the tool loop. Keeping the loop out of here
    means the client stays trivially stubbable in tests.
    """

    def __init__(self, config: LLMConfig | None = None, timeout: float = 60.0,
                 max_retries: int = 3):
        self.config = config or resolve_config()
        self.timeout = timeout
        self.max_retries = max_retries
        self.calls = 0

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 temperature: float = 0.0, max_tokens: int = 1500) -> dict:
        if not self.config.configured:
            raise LLMUnavailable(
                f"No API key found for {self.config.label}. Set "
                f"{' or '.join(PROVIDERS[self.config.provider]['keys'])} and restart the API.")

        payload = {
            "model": self.config.model,
            "messages": messages,
            # temperature 0: the same question over the same run should give the
            # same explanation. An investigator that reworded itself every time
            # would be useless as an audit artefact.
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.config.base_url}/chat/completions", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.config.api_key}"})

        last = None
        for attempt in range(self.max_retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    self.calls += 1
                    data = json.loads(r.read().decode())
                choices = data.get("choices") or []
                if not choices:
                    raise LLMUnavailable(f"{self.config.label} returned no choices")
                return choices[0].get("message") or {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:400]
                last = f"HTTP {e.code} from {self.config.label}: {detail}"
                if e.code not in RETRY_STATUS or attempt == self.max_retries - 1:
                    raise LLMUnavailable(last) from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = f"could not reach {self.config.label}: {e}"
                if attempt == self.max_retries - 1:
                    raise LLMUnavailable(last) from e
            time.sleep(0.6 * (2 ** attempt))
        raise LLMUnavailable(last or "unknown transport failure")


def status() -> dict:
    """What the UI needs to decide whether to offer the panel at all."""
    c = resolve_config()
    return {
        "provider": c.provider, "label": c.label, "model": c.model,
        "configured": c.configured,
        "expected_env": list(PROVIDERS[c.provider]["keys"]),
        "available_providers": [
            {"provider": n, "label": s["label"], "default_model": s["model"],
             "configured": any(os.environ.get(k) for k in s["keys"])}
            for n, s in PROVIDERS.items()],
    }
