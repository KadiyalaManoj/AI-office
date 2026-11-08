"""Run the browser office on localhost with any model provider.

    python -m agents.server --provider ollama

The page in office/index.html expects to run on claude.ai and talks to `window.claude`. This server serves that
same, unmodified file and adds one script (office/local-shim.js) that provides `window.claude` from this machine:
model calls go to /api/sample (and from there to your provider), saved tasks go to the browser's localStorage,
and GitHub pull requests and email run through /api/ship and /api/email (see agents/integrations.py).
Slack, Calendar and "Build on my computer" need claude.ai and stay unavailable.
"""

from __future__ import annotations

import argparse
import json
import socketserver
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .integrations import Integrations, IntegrationError, load_env_file
from .office import parse_json
from .pipeline import correction_prompt
from .providers import PROVIDERS, ProviderError, build_provider, default_models

OFFICE_DIR = Path(__file__).resolve().parent.parent / "office"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
MAX_BODY = 4_000_000
TOKENS = {"quick": 3000, "default": 6000, "complex": 8000}
ONLINE_LABEL = "Agents online · Claude"


def render_page(label: str, office_dir: Path = OFFICE_DIR) -> str:
    """The unmodified page with the local shim loaded first and the connection label corrected."""
    html = (office_dir / "index.html").read_text(encoding="utf-8")
    html = html.replace("<script", '<script src="/local-shim.js"></script>\n<script', 1)
    return html.replace(ONLINE_LABEL, f"Agents online · {label}")


class OfficeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, provider, model: str, planner_model: str, label: str,
                 office_dir: Path = OFFICE_DIR, integrations: Integrations | None = None):
        super().__init__(address, Handler)
        self.provider, self.model, self.planner_model = provider, model, planner_model
        self.label, self.office_dir = label, office_dir
        self.integrations = integrations or Integrations()

    def ship(self, body: dict) -> dict:
        texts = body.get("texts")
        if not (isinstance(texts, list) and texts and all(isinstance(t, str) for t in texts)):
            raise ValueError("Send the agents' replies as a list of texts.")
        return self.integrations.open_pull_request(str(body.get("repo") or ""), str(body.get("title") or "AI Office"),
                                                   texts[:50])

    def email(self, body: dict) -> dict:
        to = self.integrations.send_email(str(body.get("to") or ""), str(body.get("subject") or "AI Office"),
                                          str(body.get("body") or ""))
        return {"to": to}

    def server_bind(self):
        # The stock HTTPServer looks up this machine's full hostname here, which can stall for seconds on macOS.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]

    def sample(self, body: dict) -> dict:
        tier = body.get("tier")
        tier = tier if isinstance(tier, str) and tier in TOKENS else "default"
        model = self.planner_model if tier == "quick" else self.model
        prompt = body.get("messages") or body.get("prompt")
        valid_turns = isinstance(prompt, list) and all(
            isinstance(m, dict) and isinstance(m.get("role"), str) and isinstance(m.get("content"), str)
            for m in prompt)
        if not (isinstance(prompt, str) and prompt) and not (valid_turns and prompt):
            raise ValueError("Send a prompt string or a list of {role, content} messages.")
        max_tokens = TOKENS[tier]
        text, cut = self.provider.complete(model, prompt, max_tokens)
        if not body.get("json"):
            return {"text": text, "truncated": cut}
        data = parse_json(text)
        if not data and isinstance(prompt, str):
            text, cut = self.provider.complete(model, correction_prompt(prompt, "it was not a JSON object."),
                                               max_tokens)
            data = parse_json(text)
        if not data:
            raise ValueError("invalid_json")
        return {"json": data, "text": text, "truncated": cut}


class Handler(BaseHTTPRequestHandler):
    server: OfficeServer

    def log_message(self, fmt, *args):      # keep the terminal quiet; the office prints its own progress
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _local(self) -> bool:
        """Only answer requests addressed to localhost, and only from this server's own page.

        The Host check blocks DNS-rebinding. The Origin check (when a browser sends one) means another app or tab
        on a different localhost port cannot drive the email and GitHub endpoints.
        """
        host_header = self.headers.get("Host") or ""
        origin = self.headers.get("Origin")
        if host_header.rsplit(":", 1)[0] not in LOCAL_HOSTS:
            return False
        return origin is None or origin.split("://", 1)[-1] == host_header

    def do_GET(self):
        if not self._local():
            return self._json(403, {"error": {"code": "forbidden", "message": "Only localhost may use this."}})
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, render_page(self.server.label, self.server.office_dir).encode(),
                              "text/html; charset=utf-8")
        if path == "/local-shim.js":
            return self._send(200, (self.server.office_dir / "local-shim.js").read_bytes(),
                              "text/javascript; charset=utf-8")
        if path == "/api/config":
            return self._json(200, {"provider": self.server.provider.name, "model": self.server.model,
                                    "planner_model": self.server.planner_model})
        if path == "/api/integrations":
            return self._json(200, self.server.integrations.status())
        self._json(404, {"error": {"code": "not_found", "message": "Not found."}})

    def do_POST(self):
        if not self._local():
            return self._json(403, {"error": {"code": "forbidden", "message": "Only localhost may use this."}})
        routes = {"/api/sample": self.server.sample, "/api/ship": self.server.ship, "/api/email": self.server.email}
        action = routes.get(self.path.split("?", 1)[0])
        if action is None:
            return self._json(404, {"error": {"code": "not_found", "message": "Not found."}})
        # Requiring JSON forces browsers to send a CORS preflight for requests from other sites.
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(415, {"error": {"code": "bad_request", "message": "Send application/json."}})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                raise ValueError("Body size is not allowed.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Body must be a JSON object.")
            self._json(200, action(body))
        except ProviderError as e:
            self._json(502, {"error": {"code": "rate_limited" if e.transient else "provider_error",
                                       "message": str(e)}})
        except IntegrationError as e:
            self._json(400, {"error": {"code": "integration_error", "message": str(e)}})
        except ValueError as e:
            code = "invalid_json" if str(e) == "invalid_json" else "bad_request"
            self._json(502 if code == "invalid_json" else 400, {"error": {"code": code, "message": str(e)}})
        except Exception as e:      # never leave the page hanging on an unexpected failure
            self._json(500, {"error": {"code": "provider_error", "message": f"Unexpected server error: {e}"}})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the browser office on localhost with any model provider.")
    ap.add_argument("--provider", choices=PROVIDERS, default="ollama",
                    help="Model API: ollama (default, local), anthropic or openai")
    ap.add_argument("--base-url", help="API address for ollama or openai")
    ap.add_argument("--model", help="Model for the agents (default depends on --provider)")
    ap.add_argument("--planner-model", help="Model for quick planning steps (default depends on --provider)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--github-repo", action="append", default=[], metavar="OWNER/NAME",
                    help="A repository the page may open pull requests in (repeatable). This project's own "
                         "origin is always allowed.")
    args = ap.parse_args(argv)

    project = Path(__file__).resolve().parent.parent
    load_env_file(project / ".env")
    integrations = Integrations.from_env(project, args.github_repo)
    main_model, planner_model = default_models(args.provider)
    model, planner_model = args.model or main_model, args.planner_model or planner_model
    try:
        provider = build_provider(args.provider, args.base_url)
    except Exception as e:
        ap.error(f"Could not set up the {args.provider} provider: {e}")
    server = OfficeServer(("127.0.0.1", args.port), provider, model, planner_model, f"{args.provider} · {model}",
                          integrations=integrations)
    print(f"AI Office on http://localhost:{args.port}  ({args.provider}: {model}, planning: {planner_model})")
    state = integrations.status()
    for name in ("github", "email"):
        s = state[name]
        print(f"  {name}: {'ready' if s['ready'] else 'not set up. ' + s['reason']}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
