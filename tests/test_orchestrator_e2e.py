"""End-to-end pipeline runs against a mock LLM (no Ollama). The whole
orchestrator runs as a subprocess — the most faithful check — reaching the
in-process mock server over localhost. Covers the happy path, the test-fix
loop, the BLOCKED abort, and the independent --verify override."""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "orchestrator" / "run_team.py"

PLAN = json.dumps({"tasks": [
    {"id": 1, "title": "Create hello", "description": "write app.py printing hello",
     "files": ["app.py"]}
]})


def _text_call(name, args):
    return "```json\n%s\n```" % json.dumps({"name": name, "arguments": args})


def _saw_results(messages):
    return any(m.get("role") == "user" and "Result of" in str(m.get("content", ""))
               for m in messages)


def _last_user(messages):
    return next((str(m.get("content", "")) for m in reversed(messages)
                 if m.get("role") == "user"), "")


def _write_config(tmp_path, base_url, extra_pipeline=None):
    cfg = json.loads((REPO_ROOT / "team.json").read_text())
    cfg["server"]["base_url"] = base_url
    for spec in cfg["agents"].values():
        spec.pop("schema", None)  # exercise the OpenAI text path, not native
    if extra_pipeline:
        cfg["pipeline"].update(extra_pipeline)
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg))
    return path


def _run(config, workspace, request="Print hello", verify=None):
    cmd = [sys.executable, str(RUNNER), request, "--workspace", str(workspace),
           "--config", str(config), "--no-log"]
    if verify:
        cmd += ["--verify", verify]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def test_happy_path(mock_llm, tmp_path):
    def responder(path, body):
        system = body["messages"][0]["content"]
        msgs = body["messages"]
        if "PLANNER" in system:
            return PLAN
        if "CODER" in system:
            return ("Wrote app.py." if _saw_results(msgs)
                    else _text_call("write_file", {"path": "app.py", "content": "print('hello')\n"}))
        if "REVIEWER" in system:
            return json.dumps({"verdict": "approve", "findings": []})
        if "TESTER" in system:
            return ("Ran it.\nRESULT: PASS" if _saw_results(msgs)
                    else _text_call("run_command", {"command": "python3 app.py"}))
        return "?"

    srv = mock_llm(responder)
    cfg = _write_config(tmp_path, srv.base_url)
    result = _run(cfg, tmp_path / "ws")
    assert result.returncode == 0, result.stdout[-3000:]
    assert "Tests: PASS" in result.stdout


def test_test_fix_loop(mock_llm, tmp_path):
    state = {"tester_calls": 0}

    def responder(path, body):
        system = body["messages"][0]["content"]
        msgs = body["messages"]
        if "PLANNER" in system:
            return PLAN
        if "CODER" in system:
            return ("Wrote app.py." if _saw_results(msgs)
                    else _text_call("write_file", {"path": "app.py", "content": "print('hi')\n"}))
        if "REVIEWER" in system:
            return json.dumps({"verdict": "approve", "findings": []})
        if "TESTER" in system:
            if not _saw_results(msgs):
                return _text_call("run_command", {"command": "python3 app.py"})
            state["tester_calls"] += 1
            return ("Stale test fixed.\nRESULT: PASS" if state["tester_calls"] > 1
                    else "A stale test fails.\nRESULT: FAIL")
        return "?"

    srv = mock_llm(responder)
    cfg = _write_config(tmp_path, srv.base_url)
    result = _run(cfg, tmp_path / "ws")
    assert result.returncode == 0, result.stdout[-3000:]
    assert "FIX ROUND 1" in result.stdout and "Tests: PASS" in result.stdout


def test_blocked_aborts(mock_llm, tmp_path):
    def responder(path, body):
        system = body["messages"][0]["content"]
        if "PLANNER" in system:
            return PLAN
        if "CODER" in system:
            # a genuinely unknown tool: never contradicted by PATH -> abort
            return "BLOCKED: the frobnicator SDK is not installed; needed to build."
        return json.dumps({"verdict": "approve", "findings": []})

    srv = mock_llm(responder)
    cfg = _write_config(tmp_path, srv.base_url)
    result = _run(cfg, tmp_path / "ws")
    assert result.returncode == 2, result.stdout[-3000:]
    assert "RUN BLOCKED" in result.stdout


def test_verify_override_turns_pass_into_fail(mock_llm, tmp_path):
    def responder(path, body):
        system = body["messages"][0]["content"]
        msgs = body["messages"]
        if "PLANNER" in system:
            return PLAN
        if "CODER" in system:
            return ("Done." if _saw_results(msgs)
                    else _text_call("write_file", {"path": "app.py", "content": "print('hi')\n"}))
        if "REVIEWER" in system:
            return json.dumps({"verdict": "approve", "findings": []})
        if "TESTER" in system:
            return "RESULT: PASS"  # claims pass...
        return "?"

    srv = mock_llm(responder)
    cfg = _write_config(tmp_path, srv.base_url, extra_pipeline={"max_fix_rounds": 0})
    result = _run(cfg, tmp_path / "ws", verify="false")  # ...but verify fails
    assert result.returncode == 1, result.stdout[-3000:]
    assert "Final verification FAILED" in result.stdout
