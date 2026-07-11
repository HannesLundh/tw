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
import re
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

IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}


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
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.written_files.add(path)
        return f"wrote {len(content)} chars to {path}"

    def run_command(self, command: str) -> str:
        try:
            proc = subprocess.run(
                command, shell=True, cwd=self.root, capture_output=True,
                text=True, timeout=self.command_timeout,
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {self.command_timeout}s"
        out = f"exit code: {proc.returncode}\n"
        if proc.stdout:
            out += f"stdout:\n{proc.stdout[-8000:]}\n"
        if proc.stderr:
            out += f"stderr:\n{proc.stderr[-8000:]}\n"
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

    def run(self, user_message: str) -> str:
        """One fresh conversation: system prompt + task, tool loop if enabled."""
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_message},
        ]
        for _ in range(self.max_tool_rounds if self.use_tools else 1):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                tools=TOOL_SCHEMAS if self.use_tools else None,
            )
            msg = response.choices[0].message
            if not msg.tool_calls:
                return msg.content or ""
            messages.append(msg)
            for call in msg.tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                print(f"    [{self.name}] {call.function.name}({summarize_args(args)})")
                result = self.workspace.dispatch(call.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                })
        # Tool budget exhausted: ask for a final answer without tools.
        messages.append({
            "role": "user",
            "content": "Tool budget exhausted. Summarize what you completed and what remains.",
        })
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

    # ---- Test --------------------------------------------------------------
    banner("TESTER")
    plan_text = json.dumps(tasks, indent=2)
    test_report = agents["tester"].run(
        f"Overall goal: {args.request}\n\nThe plan that was implemented:\n{plan_text}\n\n"
        f"Files in the workspace:\n{workspace.list_files()}"
    )
    print(test_report)

    banner("DONE")
    print(f"Workspace: {workspace.root}")
    print(f"Files written this run: {len(workspace.written_files)}")
    passed = "RESULT: PASS" in test_report
    print("Tests: PASS" if passed else "Tests: FAIL or inconclusive — read the report above.")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
