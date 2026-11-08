"""Model providers: one small interface so the office runs on Anthropic, OpenAI-compatible APIs or local Ollama.

Every provider answers `complete(model, prompt, max_tokens) -> (text, truncated)`, where `prompt` is a string
or a list of {"role", "content"} messages. Nothing else in the office knows which model is behind it.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

PROVIDERS = ("anthropic", "ollama", "openai")

# (main model, planner model) used when neither the flag nor the environment names one.
DEFAULT_MODELS = {
    "anthropic": ("claude-sonnet-5-5", "claude-haiku-5-5"),
    "ollama": ("qwen2.5-coder:7b", "qwen2.5-coder:7b"),
    "openai": ("gpt-4o", "gpt-4o-mini"),
}
OLLAMA_URL = "http://localhost:11434"
OPENAI_URL = "https://api.openai.com/v1"
THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class ProviderError(RuntimeError):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def as_messages(prompt) -> list[dict]:
    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    return [{"role": str(m["role"]), "content": str(m["content"])} for m in prompt]


def clean_text(text: str) -> str:
    """Drop the <think> blocks that local reasoning models put in front of their answer."""
    return THINK_RE.sub("", text or "").strip()


def default_models(provider: str) -> tuple[str, str]:
    main, planner = DEFAULT_MODELS[provider]
    return os.environ.get("AI_OFFICE_MODEL", main), os.environ.get("AI_OFFICE_PLANNER_MODEL", planner)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client):
        self.client = client

    def complete(self, model: str, prompt, max_tokens: int = 3000) -> tuple[str, bool]:
        try:
            msg = self.client.messages.create(model=model, max_tokens=max_tokens, messages=as_messages(prompt))
        except Exception as e:
            if "Could not resolve authentication" in str(e):
                raise ProviderError("No Anthropic API key. Set ANTHROPIC_API_KEY (from console.anthropic.com), "
                                    "or run without Claude using --provider ollama.") from e
            status = getattr(e, "status_code", None)
            transient = (status in (408, 409, 429) or (isinstance(status, int) and status >= 500)
                         or type(e).__name__ in ("APIConnectionError", "APITimeoutError", "RateLimitError"))
            raise ProviderError(str(e), transient=transient) from e
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        return text, getattr(msg, "stop_reason", None) == "max_tokens"


def _http_url(url: str) -> str:
    """Only http(s) addresses are allowed, so a mistyped or hostile value can never read local files."""
    url = url if "://" in url else "http://" + url
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"The API address must start with http:// or https://, got '{url}'.")
    return url.rstrip("/")


def _post_json(url: str, body: dict, headers: dict, timeout: int, opener: Callable, what: str) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",  # noqa: S310 (scheme checked)
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with opener(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300] if hasattr(e, "read") else ""
        raise ProviderError(f"{what} returned HTTP {e.code}. {detail}".strip(),
                            transient=e.code in (408, 409, 429) or e.code >= 500) from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise ProviderError(f"{what} did not answer within {timeout}s. A larger model may need a longer wait "
                                f"or a faster machine.") from e
        raise ProviderError(f"Could not reach {what} at {url}: {e.reason}", transient=False) from e
    except TimeoutError as e:
        raise ProviderError(f"{what} did not answer within {timeout}s.") from e
    except OSError as e:
        raise ProviderError(f"{what} dropped the connection: {e}", transient=True) from e
    except ValueError as e:
        raise ProviderError(f"{what} sent a reply that is not JSON.") from e


class OllamaProvider:
    """Talks to Ollama's native API, because only it lets us raise the context window (num_ctx)."""
    name = "ollama"

    def __init__(self, base_url: str = OLLAMA_URL, num_ctx: int | None = None, timeout: int = 900,
                 opener: Callable = urllib.request.urlopen):
        self.base_url = _http_url(base_url)
        self.num_ctx = num_ctx or int(os.environ.get("AI_OFFICE_NUM_CTX", "16384"))
        self.timeout, self.opener = timeout, opener

    def complete(self, model: str, prompt, max_tokens: int = 3000) -> tuple[str, bool]:
        body = {"model": model, "messages": as_messages(prompt), "stream": False,
                "options": {"num_ctx": self.num_ctx, "num_predict": max_tokens}}
        try:
            data = _post_json(f"{self.base_url}/api/chat", body, {}, self.timeout, self.opener, "Ollama")
        except ProviderError as e:
            if "HTTP 404" in str(e):
                raise ProviderError(f"Ollama has no model '{model}'. Run: ollama pull {model}") from e
            if "Could not reach" in str(e):
                raise ProviderError(f"Ollama isn't running at {self.base_url}. Start it with: ollama serve") from e
            raise
        text = clean_text((data.get("message") or {}).get("content", ""))
        return text, data.get("done_reason") == "length"


class OpenAICompatProvider:
    """OpenAI, LM Studio, vLLM and anything else that speaks /chat/completions."""
    name = "openai"

    def __init__(self, base_url: str = OPENAI_URL, api_key: str = "", timeout: int = 600,
                 opener: Callable = urllib.request.urlopen):
        self.base_url = _http_url(base_url)
        self.api_key, self.timeout, self.opener = api_key, timeout, opener

    def complete(self, model: str, prompt, max_tokens: int = 3000) -> tuple[str, bool]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": model, "messages": as_messages(prompt), "max_tokens": max_tokens}
        data = _post_json(f"{self.base_url}/chat/completions", body, headers, self.timeout, self.opener,
                          "the model API")
        choice = (data.get("choices") or [{}])[0]
        text = clean_text((choice.get("message") or {}).get("content") or "")
        return text, choice.get("finish_reason") == "length"


class RetryingProvider:
    """Retries rate limits, dropped connections and 5xx answers with a growing pause. Other errors pass through."""

    def __init__(self, inner, retries: int = 3, delay: float = 2.0, sleep: Callable[[float], None] = time.sleep):
        self.inner, self.retries, self.delay, self.sleep = inner, retries, delay, sleep
        self.name = inner.name

    def complete(self, model: str, prompt, max_tokens: int = 3000) -> tuple[str, bool]:
        for attempt in range(self.retries + 1):
            try:
                return self.inner.complete(model, prompt, max_tokens)
            except ProviderError as e:
                if not e.transient or attempt == self.retries:
                    raise
                self.sleep(self.delay * 2 ** attempt)
        raise AssertionError("unreachable")


def build_provider(name: str, base_url: str | None = None, retries: int = 3) -> RetryingProvider:
    inner: Any
    if name == "anthropic":
        import anthropic  # imported here so Ollama and OpenAI users never need an Anthropic key
        inner = AnthropicProvider(anthropic.Anthropic())
    elif name == "ollama":
        inner = OllamaProvider(base_url or os.environ.get("OLLAMA_HOST") or OLLAMA_URL)
    elif name == "openai":
        inner = OpenAICompatProvider(base_url or os.environ.get("OPENAI_BASE_URL") or OPENAI_URL,
                                     os.environ.get("OPENAI_API_KEY", ""))
    else:
        raise ValueError(f"Unknown provider '{name}'. Choose one of: {', '.join(PROVIDERS)}")
    return RetryingProvider(inner, retries=retries)
