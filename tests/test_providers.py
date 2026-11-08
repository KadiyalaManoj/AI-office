import io
import json
import urllib.error

import pytest

from agents.providers import (AnthropicProvider, OllamaProvider, OpenAICompatProvider, ProviderError,
                              RetryingProvider, build_provider, clean_text, default_models)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_returning(payload, seen=None):
    def opener(req, timeout):
        if seen is not None:
            seen.append((req.full_url, json.loads(req.data), dict(req.header_items())))
        return FakeResponse(json.dumps(payload).encode())
    return opener


def test_ollama_sends_context_window_and_reads_reply():
    seen = []
    p = OllamaProvider("localhost:11434", num_ctx=16384,
                       opener=opener_returning({"message": {"content": " hello "}, "done_reason": "stop"}, seen))
    assert p.complete("qwen", "hi", 500) == ("hello", False)
    url, body, _ = seen[0]
    assert url == "http://localhost:11434/api/chat"
    assert body["options"] == {"num_ctx": 16384, "num_predict": 500}
    assert body["stream"] is False and body["messages"] == [{"role": "user", "content": "hi"}]


def test_ollama_flags_length_cutoff_and_strips_thinking():
    p = OllamaProvider(opener=opener_returning({"message": {"content": "<think>hmm\nok</think>answer"},
                                                "done_reason": "length"}))
    assert p.complete("m", "x") == ("answer", True)
    assert clean_text("<think>a</think>\n\nb") == "b"


def test_ollama_errors_say_how_to_fix_them():
    def refuse(req, timeout):
        raise urllib.error.URLError("refused")

    with pytest.raises(ProviderError, match="ollama serve"):
        OllamaProvider(opener=refuse).complete("m", "x")

    def missing(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b"model not found"))

    with pytest.raises(ProviderError, match="ollama pull qwen"):
        OllamaProvider(opener=missing).complete("qwen", "x")


def test_openai_compatible_request_and_reply():
    seen = []
    payload = {"choices": [{"message": {"content": "done"}, "finish_reason": "length"}]}
    p = OpenAICompatProvider("http://x/v1/", "sk-1", opener=opener_returning(payload, seen))
    assert p.complete("gpt", [{"role": "user", "content": "q"}], 99) == ("done", True)
    url, body, headers = seen[0]
    assert url == "http://x/v1/chat/completions" and body["max_tokens"] == 99
    assert headers["Authorization"] == "Bearer sk-1"


def test_server_errors_are_transient_but_bad_requests_are_not():
    def err(code):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, code, "e", {}, io.BytesIO(b""))
        return opener

    with pytest.raises(ProviderError) as busy:
        OpenAICompatProvider(opener=err(429)).complete("m", "x")
    with pytest.raises(ProviderError) as bad:
        OpenAICompatProvider(opener=err(400)).complete("m", "x")
    assert busy.value.transient and not bad.value.transient


class Flaky:
    name = "flaky"

    def __init__(self, errors):
        self.errors, self.calls = list(errors), 0

    def complete(self, model, prompt, max_tokens=3000):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok", False


def test_retrying_provider_retries_transient_errors_with_growing_pauses():
    pauses = []
    flaky = Flaky([ProviderError("busy", True), ProviderError("busy", True)])
    assert RetryingProvider(flaky, retries=3, delay=1, sleep=pauses.append).complete("m", "x") == ("ok", False)
    assert flaky.calls == 3 and pauses == [1, 2]


def test_retrying_provider_gives_up_and_never_retries_permanent_errors():
    flaky = Flaky([ProviderError("busy", True)] * 5)
    with pytest.raises(ProviderError):
        RetryingProvider(flaky, retries=2, sleep=lambda s: None).complete("m", "x")
    assert flaky.calls == 3

    perm = Flaky([ProviderError("bad key", False)])
    with pytest.raises(ProviderError):
        RetryingProvider(perm, sleep=lambda s: None).complete("m", "x")
    assert perm.calls == 1


def test_anthropic_provider_maps_errors_and_replies():
    class RateLimited(Exception):
        status_code = 429

    class Messages:
        def create(self, **kw):
            raise RateLimited("slow down")

    class Client:
        messages = Messages()

    with pytest.raises(ProviderError) as e:
        AnthropicProvider(Client()).complete("m", "x")
    assert e.value.transient


def test_provider_choice_and_defaults(monkeypatch):
    monkeypatch.delenv("AI_OFFICE_MODEL", raising=False)
    monkeypatch.delenv("AI_OFFICE_PLANNER_MODEL", raising=False)
    assert default_models("ollama")[0].startswith("qwen")
    monkeypatch.setenv("AI_OFFICE_MODEL", "mine")
    assert default_models("openai")[0] == "mine"
    assert build_provider("ollama").name == "ollama"
    with pytest.raises(ValueError):
        build_provider("nope")


def test_a_slow_model_is_reported_as_a_timeout_not_as_not_running():
    def slow(req, timeout):
        raise urllib.error.URLError(TimeoutError("timed out"))

    with pytest.raises(ProviderError) as e:
        OllamaProvider(opener=slow, timeout=5).complete("m", "x")
    assert "did not answer within 5s" in str(e.value) and "isn't running" not in str(e.value)
    assert not e.value.transient          # retrying a model that is too slow only wastes more time

    def dropped(req, timeout):
        raise ConnectionResetError("reset")

    with pytest.raises(ProviderError) as e2:
        OpenAICompatProvider(opener=dropped).complete("m", "x")
    assert e2.value.transient


def test_missing_anthropic_key_says_how_to_fix_it():
    class Messages:
        def create(self, **kw):
            raise RuntimeError("Could not resolve authentication method. Expected one of api_key")

    class Client:
        messages = Messages()

    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY.*--provider ollama"):
        AnthropicProvider(Client()).complete("m", "x")


def test_only_http_addresses_are_accepted():
    for bad in ("file:///etc/passwd", "ftp://host", "gopher://x"):
        with pytest.raises(ValueError, match="http"):
            OllamaProvider(bad)
        with pytest.raises(ValueError, match="http"):
            OpenAICompatProvider(bad)
    assert OllamaProvider("127.0.0.1:11434").base_url == "http://127.0.0.1:11434"
    assert OpenAICompatProvider("https://x/v1/").base_url == "https://x/v1"
