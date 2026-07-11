#!/usr/bin/env python3
"""Multi-agent coding team on a single local LLM server.

Pipeline: planner -> (coder -> reviewer)* per task -> tester.
All agents are role prompts multiplexed over one OpenAI-compatible endpoint
(Ollama, LM Studio, llama-server). No framework, so you can read all of it.

Usage:
    python orchestrator/run_team.py "Build a CLI todo app" --workspace ~/code/todo
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parent.parent

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the workspace, recursively, as relative paths.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace root."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file in the workspace. Parent directories are created automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace root."},
                    "content": {"type": "string", "description": "Full new content of the file."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the workspace root and return stdout, stderr and the exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."}
                },
                "required": ["command"],
            },
        },
    },
]

IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache",
                ".agent-backups"}

# An entire file body that is just a bracketed token, e.g.
# "<updated-content-of-todo_manager.py>". Local models emit these instead of
# real content and would silently destroy working files.
PLACEHOLDER_RE = re.compile(r"^<[^<>\n]{0,120}>$")

KNOWN_TOOLS = {"list_files", "read_file", "write_file", "run_command"}


def parse_text_tool_calls(text: str) -> list[tuple[str, dict]]:
    """Extract tool calls a model printed as JSON text instead of using the
    native tool-calling API. Local models do this constantly, even when their
    chat template supports function calling — without this fallback the call
    would be mistaken for a final answer and silently dropped.

    Recognizes {"name": ..., "arguments": {...}} objects (also "parameters",
    or nested under "function"), anywhere in the reply, fenced or not.
    """
    calls = []
    decoder = json.JSONDecoder()
    idx = 0
    while (brace := text.find("{", idx)) != -1:
        try:
            obj, consumed = decoder.raw_decode(text[brace:])
        except json.JSONDecodeError:
            idx = brace + 1
            continue
        idx = brace + consumed
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get("function"), dict):
            obj = obj["function"]
        name = obj.get("name") or obj.get("tool")
        args = obj.get("arguments", obj.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                continue
        if name in KNOWN_TOOLS and isinstance(args, dict):
            calls.append((name, args))
    return calls


class Workspace:
    """File and shell access confined to one directory."""

    def __init__(self, root: Path, command_timeout: int):
        self.root = root.resolve()
        self.command_timeout = command_timeout
        self.written_files: set[str] = set()

    def _resolve(self, rel_path: str) -> Path:
        path = (self.root / rel_path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"path escapes the workspace: {rel_path}")
        return path

    def list_files(self) -> str:
        names = []
        for path in sorted(self.root.rglob("*")):
            if any(part in IGNORED_DIRS for part in path.parts):
                continue
            if path.is_file():
                names.append(str(path.relative_to(self.root)))
        return "\n".join(names) if names else "(workspace is empty)"

    def read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            return f"ERROR: no such file: {path}"
        text = target.read_text(errors="replace")
        if len(text) > 40_000:
            return text[:40_000] + f"\n... [truncated, file is {len(text)} chars]"
        return text

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)

        # Refuse placeholder bodies outright: they would replace a working
        # file with garbage, and the resulting "syntax error on line 1" sends
        # every later agent down a rabbit hole.
        stripped = content.strip()
        if PLACEHOLDER_RE.match(stripped) or (
            len(stripped) < 200 and "placeholder" in stripped.lower()
        ):
            return (
                f"ERROR: refused to write {path}: the content you sent is a "
                f"placeholder ({stripped[:80]!r}), not real file content. "
                "write_file replaces the ENTIRE file — resend the call with the "
                "complete, literal content of the file."
            )

        # Keep the pre-run original of any file we overwrite, so a bad write
        # never destroys the only copy.
        if target.is_file():
            backup = self.root / ".agent-backups" / path
            if not backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_text(target.read_text(errors="replace"))

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.written_files.add(path)
        result = f"wrote {len(content)} chars to {path}"

        # Instant feedback beats discovering a broken file three tool calls
        # later via a confusing pytest collection error.
        if target.suffix == ".py":
            try:
                compile(content, path, "exec")
            except SyntaxError as exc:
                result += (
                    f"\nWARNING: the file was written but is NOT valid Python — "
                    f"line {exc.lineno}: {exc.msg}. Fix it before running anything."
                )
        return result

    def run_command(self, command: str) -> str:
        # Run in a fresh process group with stdin closed. On timeout, kill the
        # WHOLE group: subprocess.run's own timeout only kills the shell, and
        # surviving children (pytest, a launched app) keep the output pipes
        # open, hanging the orchestrator forever.
        proc = subprocess.Popen(
            command, shell=True, cwd=self.root, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=self.command_timeout)
            header = f"exit code: {proc.returncode}"
        except subprocess.TimeoutExpired:
            try:
                # start_new_session makes the group id == proc.pid; killing by
                # that id works even if the shell itself already exited and
                # only orphaned children remain in the group.
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # Something survived the kill and is holding the pipes open;
                # abandon the output rather than hang the orchestrator.
                for stream in (proc.stdout, proc.stderr):
                    if stream:
                        stream.close()
                stdout, stderr = "", ""
            header = (
                f"ERROR: command killed after {self.command_timeout}s timeout. "
                "It may hang or wait for input (there is none: stdin is closed). "
                "Do NOT rerun it unchanged — fix the command or the code instead."
            )
        out = header + "\n"
        if stdout:
            out += f"stdout:\n{stdout[-8000:]}\n"
        if stderr:
            out += f"stderr:\n{stderr[-8000:]}\n"
        return out

    def dispatch(self, name: str, args: dict) -> str:
        try:
            if name == "list_files":
                return self.list_files()
            if name == "read_file":
                return self.read_file(args["path"])
            if name == "write_file":
                return self.write_file(args["path"], args["content"])
            if name == "run_command":
                return self.run_command(args["command"])
            return f"ERROR: unknown tool {name}"
        except Exception as exc:  # surface tool failures to the model, not the user
            return f"ERROR: {exc}"


class Agent:
    """One role: a system prompt, a model, and optionally the workspace tools."""

    def __init__(self, name: str, spec: dict, client: OpenAI, workspace: Workspace):
        self.name = name
        self.model = spec["model"]
        self.temperature = spec.get("temperature", 0.2)
        self.use_tools = spec.get("tools", False)
        self.max_tool_rounds = spec.get("max_tool_rounds", 25)
        self.system_prompt = (REPO_ROOT / spec["prompt"]).read_text()
        self.client = client
        self.workspace = workspace
        self.warned_text_fallback = False

    def _execute(self, name: str, args: dict, seen: dict) -> str:
        """Dispatch a tool call, short-circuiting pathological repetition.
        Rerunning tests after an edit is normal; making the exact same call a
        4th time means the agent is looping and needs a shove, not a result."""
        signature = name + json.dumps(args, sort_keys=True)
        seen[signature] = seen.get(signature, 0) + 1
        if seen[signature] > 3:
            print(f"    [{self.name}] blocked repeated call: {name}")
            return (
                f"REPEATED CALL BLOCKED: you already made this exact {name} call "
                "3 times. Repeating it will not change the outcome. Either take a "
                "genuinely different action or stop and write your final summary, "
                "honestly stating what still fails."
            )
        return self.workspace.dispatch(name, args)

    def run(self, user_message: str) -> str:
        """One fresh conversation: system prompt + task, tool loop if enabled."""
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_message},
        ]
        seen: dict = {}
        blocked_streak = 0
        stop_reason = "Tool budget exhausted. Summarize what you completed and what remains."
        for _ in range(self.max_tool_rounds if self.use_tools else 1):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                tools=TOOL_SCHEMAS if self.use_tools else None,
            )
            msg = response.choices[0].message
            batch_results = []

            if msg.tool_calls:
                # Native tool-calling path.
                messages.append(msg)
                for call in msg.tool_calls:
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    print(f"    [{self.name}] {call.function.name}({summarize_args(args)})")
                    result = self._execute(call.function.name, args, seen)
                    batch_results.append(result)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result,
                    })
            else:
                # Fallback path: the model may have written tool calls as JSON
                # text in its reply instead of using the tool-calling API.
                text_calls = parse_text_tool_calls(msg.content or "") if self.use_tools else []
                if not text_calls:
                    return msg.content or ""
                if not self.warned_text_fallback:
                    print(f"    [{self.name}] note: model emits tool calls as text; parsing them from the reply")
                    self.warned_text_fallback = True
                messages.append({"role": "assistant", "content": msg.content})
                results = []
                for name, args in text_calls:
                    print(f"    [{self.name}] {name}({summarize_args(args)})")
                    result = self._execute(name, args, seen)
                    batch_results.append(result)
                    results.append(f"Result of {name}:\n{result}")
                messages.append({
                    "role": "user",
                    "content": "\n\n".join(results)
                    + "\n\nContinue with the task. Call more tools if you need them; "
                    "when you are finished, reply with your plain-text summary and no JSON.",
                })

            # An agent whose every call in two consecutive rounds was blocked
            # for repetition is stuck; cut it off instead of paying for more
            # generations that will repeat the same call.
            if batch_results and all(r.startswith("REPEATED CALL BLOCKED") for r in batch_results):
                blocked_streak += 1
            else:
                blocked_streak = 0
            if blocked_streak >= 2:
                print(f"    [{self.name}] agent kept repeating blocked calls; forcing final answer")
                stop_reason = (
                    "STOP. You repeated the same blocked calls again; no further tool "
                    "calls will be executed. Write your final answer now, honestly "
                    "stating what works and what still fails."
                )
                break
        # Loop ended without a final answer: ask for one, without tools.
        messages.append({"role": "user", "content": stop_reason})
        response = self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=self.temperature,
        )
        return response.choices[0].message.content or ""


def summarize_args(args: dict) -> str:
    parts = []
    for key, value in args.items():
        text = str(value).replace("\n", " ")
        parts.append(f"{key}={text[:60]}{'…' if len(text) > 60 else ''}")
    return ", ".join(parts)


def extract_json(text: str):
    """Parse JSON from a model reply, tolerating prose and code fences."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = min((i for i in (text.find("["), text.find("{")) if i != -1), default=-1)
    if start == -1:
        raise ValueError(f"no JSON found in reply:\n{text}")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text[start:])
    return obj


def banner(text: str):
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


def main():
    parser = argparse.ArgumentParser(description="Run the local multi-agent coding team.")
    parser.add_argument("request", help="What you want built or changed.")
    parser.add_argument("--workspace", required=True, help="Directory the team may read/write.")
    parser.add_argument("--config", default=str(REPO_ROOT / "team.json"))
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text())
    workspace_dir = Path(args.workspace).expanduser()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(workspace_dir, config["pipeline"].get("command_timeout_seconds", 120))

    client = OpenAI(
        base_url=config["server"]["base_url"],
        api_key=config["server"].get("api_key", "local"),
    )
    agents = {
        name: Agent(name, spec, client, workspace)
        for name, spec in config["agents"].items()
    }
    max_review_rounds = config["pipeline"].get("max_review_rounds", 2)

    # ---- Plan -------------------------------------------------------------
    banner("PLANNER")
    existing = workspace.list_files()
    plan_reply = agents["planner"].run(
        f"User request:\n{args.request}\n\nExisting files in the workspace:\n{existing}"
    )
    tasks = extract_json(plan_reply)
    for task in tasks:
        print(f"  {task['id']}. {task['title']}")

    # ---- Implement + review, task by task ---------------------------------
    completed_summaries = []
    for task in tasks:
        banner(f"TASK {task['id']}: {task['title']}")
        context = ""
        if completed_summaries:
            context = "Already completed by you or teammates:\n" + "\n".join(
                f"- {s}" for s in completed_summaries
            ) + "\n\n"
        task_text = (
            f"{context}Overall goal: {args.request}\n\n"
            f"Your task now:\n{task['title']}\n{task['description']}\n"
            f"Files expected: {', '.join(task.get('files', [])) or 'your judgment'}"
        )

        feedback = None
        for round_num in range(max_review_rounds + 1):
            prompt = task_text
            if feedback:
                prompt += (
                    "\n\nThe reviewer rejected the previous attempt. "
                    "Address every 'required' finding:\n"
                    + json.dumps(feedback, indent=2)
                )
            print(f"  -- coder (round {round_num + 1}) --")
            coder_summary = agents["coder"].run(prompt)
            print(f"  coder: {coder_summary.strip()[:500]}")

            changed = sorted(workspace.written_files)
            file_dump = "\n\n".join(
                f"### {p}\n{workspace.read_file(p)}" for p in changed
            ) or "(no files were written)"
            print("  -- reviewer --")
            review_reply = agents["reviewer"].run(
                f"Task:\n{task['title']}\n{task['description']}\n\n"
                f"Coder's summary:\n{coder_summary}\n\n"
                f"Files changed so far in this run:\n{file_dump}"
            )
            try:
                review = extract_json(review_reply)
            except ValueError:
                print("  reviewer reply was not valid JSON; accepting the task as-is")
                review = {"verdict": "approve", "findings": []}

            required = [f for f in review.get("findings", []) if f.get("severity") == "required"]
            if review.get("verdict") == "approve" or not required:
                print("  reviewer: APPROVED")
                break
            print(f"  reviewer: REVISE ({len(required)} required findings)")
            feedback = required
        else:
            print("  review rounds exhausted; moving on with latest version")

        completed_summaries.append(f"Task {task['id']} ({task['title']}): {coder_summary.strip()[:300]}")

    # ---- Test, then feed failures back to the coder ------------------------
    banner("TESTER")
    plan_text = json.dumps(tasks, indent=2)
    tester_prompt = (
        f"Overall goal: {args.request}\n\nThe plan that was implemented:\n{plan_text}\n\n"
        f"Files in the workspace:\n{workspace.list_files()}"
    )
    test_report = agents["tester"].run(tester_prompt)
    print(test_report)
    passed = "RESULT: PASS" in test_report

    max_fix_rounds = config["pipeline"].get("max_fix_rounds", 2)
    for fix_round in range(1, max_fix_rounds + 1):
        if passed:
            break
        banner(f"FIX ROUND {fix_round}: coder addresses the test failures")
        fix_summary = agents["coder"].run(
            f"Overall goal: {args.request}\n\n"
            f"The tester ran the suite and it FAILED. Tester's report:\n{test_report}\n\n"
            "Make the tests pass:\n"
            "- If the code is wrong, fix the code.\n"
            "- If a failing test is stale — it expects functions or names the "
            "current code never exposed — rewrite that test against the current "
            "public API instead of changing working code to satisfy it.\n"
            "- Rerun the failing test command yourself and confirm it passes "
            "before you finish."
        )
        print(f"  coder: {fix_summary.strip()[:500]}")
        banner(f"RETEST after fix round {fix_round}")
        test_report = agents["tester"].run(
            tester_prompt
            + "\n\nA previous test run failed and the coder has since pushed fixes. "
            "Re-verify from scratch: rerun the suite before writing your report."
        )
        print(test_report)
        passed = "RESULT: PASS" in test_report

    banner("DONE")
    print(f"Workspace: {workspace.root}")
    print(f"Files written this run: {len(workspace.written_files)}")
    print("Tests: PASS" if passed else "Tests: FAIL or inconclusive — read the report above.")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
