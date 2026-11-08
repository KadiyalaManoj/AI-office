"""The localhost server: serves the unmodified page plus the local shim, and answers model calls."""

import http.client
import json
import threading

import pytest

from agents.integrations import IntegrationError
from agents.providers import ProviderError
from agents.server import OFFICE_DIR, OfficeServer, render_page


class FakeProvider:
    name = "fake"

    def __init__(self, replies=None):
        self.replies, self.calls = list(replies or ["hello"]), []

    def complete(self, model, prompt, max_tokens=3000):
        self.calls.append((model, prompt, max_tokens))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply, False


@pytest.fixture
def serve():
    servers = []

    def start(provider, integrations=None):
        server = OfficeServer(("127.0.0.1", 0), provider, "big-model", "small-model", "fake · big-model",
                              integrations=integrations)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def call(server, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    hdrs = dict(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body)
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, payload, hdrs)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def test_page_is_served_with_the_shim_before_the_office_script(serve):
    status, data = call(serve(FakeProvider()), "GET", "/")
    html = data.decode()
    assert status == 200
    assert html.index("/local-shim.js") < html.index("three.min.js")
    assert "Agents online · fake · big-model" in html and "Agents online · Claude" not in html


def test_the_original_page_file_is_left_untouched():
    original = (OFFICE_DIR / "index.html").read_text(encoding="utf-8")
    render_page("x")
    assert "/local-shim.js" not in original and "Agents online · Claude" in original


def test_shim_is_served(serve):
    status, data = call(serve(FakeProvider()), "GET", "/local-shim.js")
    assert status == 200 and b"window.claude" in data


def test_text_sample_uses_the_main_model_and_quick_uses_the_planner(serve):
    provider = FakeProvider(["answer"])
    server = serve(provider)
    status, data = call(server, "POST", "/api/sample", {"prompt": "hi", "tier": "default"})
    assert status == 200 and json.loads(data) == {"text": "answer", "truncated": False}
    call(server, "POST", "/api/sample", {"prompt": "plan", "tier": "quick"})
    assert [c[0] for c in provider.calls] == ["big-model", "small-model"]


def test_chat_turns_are_passed_through(serve):
    provider = FakeProvider()
    turns = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    call(serve(provider), "POST", "/api/sample", {"messages": turns})
    assert provider.calls[0][1] == turns


def test_json_mode_parses_and_retries_once(serve):
    provider = FakeProvider(["not json", '```json\n{"kind": "feature", "team": ["nova"]}\n```'])
    status, data = call(serve(provider), "POST", "/api/sample", {"prompt": "plan", "json": True})
    assert status == 200 and json.loads(data)["json"] == {"kind": "feature", "team": ["nova"]}
    assert len(provider.calls) == 2 and "rejected" in provider.calls[1][1]


def test_json_mode_reports_invalid_json(serve):
    status, data = call(serve(FakeProvider(["nope"])), "POST", "/api/sample", {"prompt": "x", "json": True})
    assert status == 502 and json.loads(data)["error"]["code"] == "invalid_json"


def test_provider_errors_reach_the_page_with_a_usable_message(serve):
    server = serve(FakeProvider([ProviderError("Ollama isn't running. Start it with: ollama serve")]))
    status, data = call(server, "POST", "/api/sample", {"prompt": "x"})
    err = json.loads(data)["error"]
    assert status == 502 and err["code"] == "provider_error" and "ollama serve" in err["message"]

    status, data = call(serve(FakeProvider([ProviderError("slow down", True)])), "POST", "/api/sample",
                        {"prompt": "x"})
    assert json.loads(data)["error"]["code"] == "rate_limited"


def test_bad_requests_are_rejected(serve):
    server = serve(FakeProvider())
    assert call(server, "POST", "/api/sample", {"nothing": 1})[0] == 400
    assert call(server, "POST", "/api/sample", [1, 2])[0] == 400
    assert call(server, "POST", "/api/other", {"prompt": "x"})[0] == 404
    assert call(server, "GET", "/missing")[0] == 404
    status, _ = call(server, "POST", "/api/sample", None, {"Content-Type": "text/plain", "Content-Length": "2"})
    assert status in (400, 415)


def test_requests_for_other_hostnames_or_origins_are_refused(serve):
    provider = FakeProvider()
    server = serve(provider)
    assert call(server, "GET", "/", headers={"Host": "evil.example:8000"})[0] == 403
    assert call(server, "POST", "/api/sample", {"prompt": "x"}, {"Host": "evil.example"})[0] == 403
    assert call(server, "POST", "/api/sample", {"prompt": "x"}, {"Origin": "https://evil.example"})[0] == 403
    port = server.server_address[1]
    assert call(server, "POST", "/api/sample", {"prompt": "x"}, {"Origin": "http://localhost:9999"})[0] == 403
    assert call(server, "POST", "/api/sample", {"prompt": "x"}, {"Origin": f"http://127.0.0.1:{port}"})[0] == 200
    assert len(provider.calls) == 1


def test_config_endpoint(serve):
    status, data = call(serve(FakeProvider()), "GET", "/api/config")
    assert status == 200 and json.loads(data) == {"provider": "fake", "model": "big-model",
                                                  "planner_model": "small-model"}


def test_malformed_requests_get_an_error_reply_not_a_dropped_connection(serve):
    server = serve(FakeProvider())
    assert call(server, "POST", "/api/sample", {"prompt": "x", "tier": ["quick"]})[0] == 200
    assert call(server, "POST", "/api/sample", {"messages": [{"role": "user"}]})[0] == 400
    assert call(server, "POST", "/api/sample", {"messages": ["hello"]})[0] == 400
    assert call(server, "POST", "/api/sample", {"prompt": 42})[0] == 400

    class Exploding(FakeProvider):
        def complete(self, model, prompt, max_tokens=3000):
            raise RuntimeError("boom")

    status, data = call(serve(Exploding()), "POST", "/api/sample", {"prompt": "x"})
    assert status == 500 and "boom" in json.loads(data)["error"]["message"]


class FakeIntegrations:
    def __init__(self):
        self.shipped, self.mailed = [], []

    def status(self):
        return {"github": {"ready": True, "repos": ["me/repo"], "reason": ""},
                "email": {"ready": True, "to": ["me@x.com"], "reason": ""}}

    def open_pull_request(self, repo, title, texts):
        self.shipped.append((repo, title, texts))
        if repo != "me/repo":
            raise IntegrationError("not an allowed repository")
        return {"prUrl": "https://github.com/me/repo/pull/1", "branch": "office/x", "files": ["a.py"]}

    def send_email(self, to, subject, body):
        self.mailed.append((to, subject, body))
        return to or "me@x.com"


def test_ship_endpoint_opens_a_pull_request(serve):
    integ = FakeIntegrations()
    server = serve(FakeProvider(), integ)
    status, data = call(server, "POST", "/api/ship", {"repo": "me/repo", "title": "T", "texts": ["File: a.py"]})
    assert status == 200 and json.loads(data)["prUrl"].endswith("/pull/1")
    assert integ.shipped == [("me/repo", "T", ["File: a.py"])]


def test_ship_endpoint_rejects_bad_input_and_reports_refusals(serve):
    server = serve(FakeProvider(), FakeIntegrations())
    assert call(server, "POST", "/api/ship", {"repo": "me/repo", "texts": []})[0] == 400
    assert call(server, "POST", "/api/ship", {"repo": "me/repo", "texts": [1]})[0] == 400
    status, data = call(server, "POST", "/api/ship", {"repo": "x/y", "texts": ["t"]})
    err = json.loads(data)["error"]
    assert status == 400 and err["code"] == "integration_error" and "allowed" in err["message"]


def test_email_endpoint_sends_mail(serve):
    integ = FakeIntegrations()
    status, data = call(serve(FakeProvider(), integ), "POST", "/api/email",
                        {"to": "me@x.com", "subject": "S", "body": "B"})
    assert status == 200 and json.loads(data) == {"to": "me@x.com"}
    assert integ.mailed == [("me@x.com", "S", "B")]


def test_integration_endpoints_refuse_other_origins(serve):
    integ = FakeIntegrations()
    server = serve(FakeProvider(), integ)
    evil = {"Origin": "http://localhost:3000"}          # another app on the same machine
    assert call(server, "POST", "/api/email", {"subject": "s", "body": "b"}, evil)[0] == 403
    assert call(server, "POST", "/api/ship", {"repo": "me/repo", "texts": ["t"]}, evil)[0] == 403
    assert integ.mailed == [] and integ.shipped == []


def test_integrations_status_endpoint(serve):
    status, data = call(serve(FakeProvider(), FakeIntegrations()), "GET", "/api/integrations")
    assert status == 200 and json.loads(data)["github"]["ready"] is True


def test_status_never_contains_the_smtp_password(serve):
    from agents.integrations import Integrations
    cfg = Integrations(smtp_user="me@gmail.com", smtp_password="super-secret-app-pass")
    status, data = call(serve(FakeProvider(), cfg), "GET", "/api/integrations")
    assert status == 200 and b"super-secret" not in data
