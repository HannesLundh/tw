"""Shared fixtures for the offline test suite.

Everything here runs WITHOUT a live model: the mock servers speak just enough
of the OpenAI-compatible and Ollama-native protocols to exercise the
deterministic scaffold. No test in the default suite requires Ollama.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# Repo root makes `chat.chat` / `chat.web` importable as a package; the
# orchestrator dir makes `run_team` importable by its bare name.
import sys
for p in (str(REPO_ROOT), str(REPO_ROOT / "orchestrator")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _make_handler(responder):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            content = responder(self.path, body)
            if self.path.endswith("/api/chat"):  # Ollama native
                payload = {"message": {"role": "assistant", "content": content}}
            else:  # OpenAI-compatible /v1/chat/completions
                payload = {
                    "id": "mock", "object": "chat.completion", "created": 0,
                    "model": body.get("model", "mock"),
                    "choices": [{"index": 0,
                                 "message": {"role": "assistant", "content": content},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    return Handler


class MockLLM:
    """A tiny HTTP server on an ephemeral port that answers chat requests with
    whatever `responder(path, body)` returns."""

    def __init__(self, responder):
        self.server = HTTPServer(("127.0.0.1", 0), _make_handler(responder))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def native_url(self):
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture
def mock_llm():
    """Yields a factory: `with mock_llm(responder) as srv: srv.base_url`."""
    servers = []

    def factory(responder):
        srv = MockLLM(responder)
        srv.__enter__()
        servers.append(srv)
        return srv

    yield factory
    for srv in servers:
        srv.__exit__(None, None, None)


class StaticSite:
    """Serves one fixed HTML body at / on an ephemeral port (for fetch_page)."""

    def __init__(self, html: str):
        body = html.encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"


@pytest.fixture
def static_site():
    sites = []

    def factory(html):
        site = StaticSite(html)
        site.__enter__()
        sites.append(site)
        return site

    yield factory
    for site in sites:
        site.__exit__(None, None, None)


@pytest.fixture
def workspace(tmp_path):
    """A fresh Workspace rooted at a pytest tmp dir."""
    from run_team import Workspace
    return Workspace(tmp_path, command_timeout=3)
