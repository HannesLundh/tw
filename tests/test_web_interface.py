"""Tests for the FastAPI web interface. The chat endpoint's model call is
stubbed, so these run offline and exercise the web plumbing, not the LLM."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import chat.web as web

client = TestClient(web.app)


@pytest.fixture
def stub_answer(monkeypatch):
    """Replace the model call with a stub that echoes what it received, so we
    can assert on the request plumbing (system-prompt injection, JSON shape)."""
    captured = {}

    def fake_answer_turn(client_, config, messages):
        captured["messages"] = messages
        return "stubbed reply"

    monkeypatch.setattr(web, "answer_turn", fake_answer_turn)
    return captured


def test_get_index_returns_html():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert "Local Chat Agent" in html
    assert 'id="user-input"' in html and 'id="send-button"' in html
    assert "Thinking..." in html


def test_static_files_served():
    assert Path("chat/static/index.html").exists()
    assert client.get("/static/index.html").status_code == 200


def test_post_chat_returns_reply(stub_answer):
    response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 200
    assert response.json()["reply"] == "stubbed reply"


def test_post_chat_injects_system_prompt(stub_answer):
    client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hi"}]})
    roles = [m["role"] for m in stub_answer["messages"]]
    assert roles[0] == "system"  # server prepends the researcher prompt


def test_post_chat_does_not_double_inject_system(stub_answer):
    client.post("/api/chat", json={"messages": [
        {"role": "system", "content": "already here"},
        {"role": "user", "content": "Hi"},
    ]})
    assert sum(1 for m in stub_answer["messages"] if m["role"] == "system") == 1


def test_post_chat_empty_messages(stub_answer):
    response = client.post("/api/chat", json={"messages": []})
    assert response.status_code == 200
    assert "reply" in response.json()
