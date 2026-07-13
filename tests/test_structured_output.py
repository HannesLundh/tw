"""Schema-constrained decoding via Ollama's native endpoint, and the clean
fallback to prompt-and-parse when it isn't available."""

import json

from run_team import Agent, extract_json


def _plan_native_responder(path, body):
    # Only answer with a valid plan if the schema actually arrived.
    if path.endswith("/api/chat") and body.get("format", {}).get("required") == ["tasks"]:
        return json.dumps({"tasks": [
            {"id": 1, "title": "Do it", "description": "under schema", "files": []}
        ]})
    return "NO SCHEMA RECEIVED"


def test_structured_native_path(mock_llm, workspace):
    srv = mock_llm(_plan_native_responder)
    spec = {"model": "m", "prompt": "agents/planner.md", "tools": False, "schema": "plan"}
    agent = Agent("planner", spec, client=None, workspace=workspace, native_url=srv.native_url)
    reply = agent.run("Build a thing")
    tasks = extract_json(reply)
    if isinstance(tasks, dict):
        tasks = tasks["tasks"]
    assert tasks[0]["title"] == "Do it"


def test_structured_native_falls_back_when_unreachable(workspace):
    spec = {"model": "m", "prompt": "agents/planner.md", "tools": False, "schema": "plan"}
    agent = Agent("planner", spec, client=None, workspace=workspace,
                  native_url="http://127.0.0.1:1")  # nothing listening
    assert agent._structured_native([{"role": "user", "content": "x"}]) is None
