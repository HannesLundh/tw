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
import shutil
import signal
import subprocess
import sys
import urllib.request
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

# Commands that install software or modify the machine outside the workspace.
# A blocked agent once ran sudo/apt/brew/npm -g in a loop, appended PATH
# exports to four different shell profiles, and deleted /opt/homebrew/bin/func
# trying to conjure a missing SDK. Agents must report BLOCKED instead.
# JSON schemas for the structured-output roles. When the server is Ollama,
# these are enforced by constrained decoding on the native /api/chat endpoint
# (format parameter): the model cannot emit tokens that violate the schema,
# so "reviewer reply was not valid JSON" becomes impossible. Other servers
# fall back to prompt-and-parse.
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "title", "description"],
            },
        }
    },
    "required": ["tasks"],
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "severity": {"type": "string", "enum": ["required", "note"]},
                    "problem": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["severity", "problem"],
            },
        },
    },
    "required": ["verdict", "findings"],
}

SCHEMAS = {"plan": PLAN_SCHEMA, "review": REVIEW_SCHEMA}

# Toolchain binaries worth verifying deterministically. Preflight checks the
# ones named in the request before any LLM call, and a BLOCKED claim about
# one of these is fact-checked against PATH instead of taken on faith.
TOOLCHAIN_BINARIES = [
    "dotnet", "func", "node", "npm", "npx", "deno", "bun", "go", "cargo",
    "rustc", "java", "javac", "mvn", "gradle", "kotlin", "swift", "ruby",
    "php", "docker", "kubectl", "terraform", "make", "cmake", "gcc", "clang",
]

FORBIDDEN_COMMANDS = [
    (r"\bsudo\b", "sudo"),
    (r"\bapt(-get)?\b", "apt package management"),
    (r"\bbrew\s+(install|reinstall|uninstall|remove|link|unlink|tap|upgrade)\b",
     "brew package management"),
    (r"\bnpm\b.*\s(-g|--global)\b", "global npm installs"),
    (r">\s*['\"]?(~|\$HOME|/(?!tmp/|dev/null))", "redirecting output outside the workspace"),
    (r"\b(rm|mv|cp|chmod|chown|ln|touch|mkdir)\b[^|;&>]*\s+['\"]?(~|\$HOME|/(?!tmp/|dev/null))",
     "modifying paths outside the workspace"),
    (r"\b(curl|wget)\b[^|]*\|[^|]*\b(sh|bash|zsh)\b", "piping a download into a shell"),
    (r"dotnet-install\.(sh|ps1)|rustup\.rs|get-pip\.py|nodesource\.com",
     "running an SDK installer"),
    (r"\bunzip\b.*-d\s+['\"]?/", "extracting into a system path"),
]

KNOWN_TOOLS = {"list_files", "read_file", "write_file", "run_command"}


def parse_text_tool_calls(text: str, known_tools=None) -> list[tuple[str, dict]]:
    """Extract tool calls a model printed as JSON text instead of using the
    native tool-calling API. Local models do this constantly, even when their
    chat template supports function calling — without this fallback the call
    would be mistaken for a final answer and silently dropped.

    Recognizes {"name": ..., "arguments": {...}} objects (also "parameters",
    or nested under "function"), anywhere in the reply, fenced or not.
    """
    if known_tools is None:
        known_tools = KNOWN_TOOLS
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
        if name in known_tools and isinstance(args, dict):
            calls.append((name, args))
    return calls


class Workspace:
    """File and shell access confined to one directory."""

    def __init__(self, root: Path, command_timeout: int):
        self.root = root.resolve()
        self.command_timeout = command_timeout
        self.written_files: set[str] = set()
        # Bumped on every write that actually changes a file; the repetition
        # guard keys on it so that re-running a command after a real change
        # is always allowed, while write-nothing-rerun loops stay blocked.
        self.write_generation = 0

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

        # A byte-identical rewrite is a no-op; say so instead of letting the
        # model believe it "fixed" something.
        if target.is_file() and target.read_text(errors="replace") == content:
            return (
                f"no change: {path} already contains exactly this content. "
                "Rewriting it cannot change any outcome — if something still "
                "fails, the cause is elsewhere."
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
        self.write_generation += 1
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
        for pattern, reason in FORBIDDEN_COMMANDS:
            if re.search(pattern, command):
                return (
                    f"BLOCKED COMMAND ({reason}). Agents may not install software "
                    "or modify anything outside the workspace. If this task needs "
                    "a tool that is not installed, stop working and make the FIRST "
                    "line of your final summary: BLOCKED: <what is missing>."
                )
        if re.search(r"\bpip3?\s+install\b|-m\s+pip\s+install\b", command) and ".venv" not in command:
            return (
                "BLOCKED COMMAND (installing into the system Python). Create a "
                "project venv first (python3 -m venv .venv) and use "
                ".venv/bin/pip install — or report BLOCKED if that can't work."
            )

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
            # Reap anything the command left running (a forgotten background
            # server would squat on its port for every later command).
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                header += (
                    "\nnote: background process(es) left running by this command "
                    "were terminated. Servers do not survive between commands — "
                    "start, probe, and kill a server within ONE command."
                )
            except (ProcessLookupError, PermissionError):
                pass
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

    def __init__(self, name: str, spec: dict, client: OpenAI, workspace: Workspace,
                 native_url: str | None = None):
        self.name = name
        self.model = spec["model"]
        self.temperature = spec.get("temperature", 0.2)
        self.use_tools = spec.get("tools", False)
        self.max_tool_rounds = spec.get("max_tool_rounds", 25)
        self.schema = SCHEMAS.get(spec.get("schema", ""))
        self.extra_params = spec.get("params", {})
        self.native_url = native_url
        self.warned_native_fallback = False
        self.system_prompt = (REPO_ROOT / spec["prompt"]).read_text()
        self.max_context_chars = spec.get("max_context_chars", 60_000)
        self.client = client
        self.workspace = workspace
        self.warned_text_fallback = False

    def _shrink(self, messages: list) -> None:
        """Trim old tool output once the conversation outgrows the model's
        context. Without this, a long tool session makes every subsequent
        request slower (minutes of prompt processing on a 14B) and eventually
        pushes the task itself out of the window."""
        def total() -> int:
            return sum(len(str(m.get("content") or ""))
                       for m in messages if isinstance(m, dict))
        if total() <= self.max_context_chars:
            return
        # Never touch the system prompt, the task, or the last few exchanges.
        for m in messages[2:-4]:
            if not isinstance(m, dict):
                continue
            content = str(m.get("content") or "")
            if m.get("role") in ("tool", "user") and len(content) > 600:
                m["content"] = content[:500] + "\n[... older output trimmed ...]"
                if total() <= self.max_context_chars:
                    break

    def _execute(self, name: str, args: dict, seen: dict) -> str:
        """Dispatch a tool call, short-circuiting pathological repetition.
        The signature includes the workspace's write generation, so any call
        becomes legal again after a file actually changed — rerunning a build
        after an edit is mandatory, not a loop. Only same-call-with-nothing-
        changed repetition gets blocked (4th occurrence)."""
        signature = f"{self.workspace.write_generation}:{name}:" + json.dumps(args, sort_keys=True)
        seen[signature] = seen.get(signature, 0) + 1
        if seen[signature] > 3:
            print(f"    [{self.name}] blocked repeated call: {name}")
            return (
                f"REPEATED CALL BLOCKED: you already made this exact {name} call "
                "3 times. Repeating it will not change the outcome. Either take a "
                "genuinely different action or stop and write your final summary, "
                "honestly stating what still fails."
            )
        result = self.workspace.dispatch(name, args)
        if name == "run_command":
            # Surface failures on the console so the human can see what
            # actually broke instead of trusting the agent's diagnosis.
            code = re.match(r"exit code: (\d+)", result)
            if code and code.group(1) != "0":
                print(f"    [{self.name}] ! exit {code.group(1)}: {error_snippet(result)}")
            elif result.startswith("ERROR: command killed"):
                print(f"    [{self.name}] ! command timed out")
        return result

    def _structured_native(self, messages: list) -> str | None:
        """Schema-constrained generation via Ollama's native /api/chat, where
        the 'format' parameter makes schema-violating output impossible.
        Returns None on any failure so the caller falls back to prompt-and-
        parse (e.g. when the server is LM Studio or an older Ollama)."""
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": self.schema,
            "options": {"temperature": self.temperature, **self.extra_params},
        }).encode()
        request = urllib.request.Request(
            f"{self.native_url}/api/chat", data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                body = json.loads(response.read())
            content = body["message"]["content"]
            json.loads(content)  # must be valid JSON, or fall back
            return content
        except Exception as exc:
            if not self.warned_native_fallback:
                print(f"  [{self.name}] structured output unavailable "
                      f"({type(exc).__name__}); falling back to prompt-and-parse")
                self.warned_native_fallback = True
            return None

    def run(self, user_message: str) -> str:
        """One fresh conversation: system prompt + task, tool loop if enabled."""
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_message},
        ]
        if self.schema and not self.use_tools and self.native_url:
            reply = self._structured_native(messages)
            if reply is not None:
                return reply
        seen: dict = {}
        blocked_streak = 0
        total_blocked = 0
        stop_reason = (
            "Tool budget exhausted. Summarize what you completed and what "
            "remains. If your role's report format ends with a RESULT: line, "
            "you MUST include it, based on what you actually observed."
        )
        for _ in range(self.max_tool_rounds if self.use_tools else 1):
            self._shrink(messages)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                tools=TOOL_SCHEMAS if self.use_tools else None,
                **self.extra_params,
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
            # for repetition is stuck; so is one that keeps rotating through
            # blocked/forbidden calls (which resets the streak but goes
            # nowhere). Cut both off instead of paying for more generations.
            is_blocked = lambda r: r.startswith(("REPEATED CALL BLOCKED", "BLOCKED COMMAND"))
            total_blocked += sum(1 for r in batch_results if is_blocked(r))
            if batch_results and all(is_blocked(r) for r in batch_results):
                blocked_streak += 1
            else:
                blocked_streak = 0
            if blocked_streak >= 2 or total_blocked >= 8:
                print(f"    [{self.name}] agent keeps hitting blocked calls; forcing final answer")
                stop_reason = (
                    "STOP. Your calls keep being blocked; no further tool calls "
                    "will be executed. Write your final answer now, honestly "
                    "stating what works and what still fails. If you were blocked "
                    "by a missing tool, make the FIRST line: BLOCKED: <what is missing>. "
                    "If your role's report format ends with a RESULT: line, include it."
                )
                break
        # Loop ended without a final answer: ask for one, without tools.
        messages.append({"role": "user", "content": stop_reason})
        response = self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=self.temperature,
            **self.extra_params,
        )
        return response.choices[0].message.content or ""


def error_snippet(result: str) -> str:
    """Pick the most informative line of a failed command's output: the first
    line that mentions an error, else the last non-empty line — build tools
    tend to bury the verdict at the end, after pages of info chatter."""
    lines = [
        line.strip() for line in result.splitlines()[1:]
        if line.strip() and not line.startswith(("stdout:", "stderr:"))
    ]
    for line in lines:
        if re.search(r"\berror\b|not found|No such file|Unhandled exception", line, re.IGNORECASE):
            return line[:150]
    return lines[-1][:150] if lines else "(no output)"


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


def preflight_tools(request: str):
    """Check toolchain binaries named in the request against PATH, before
    burning any model time. Returns ({tool: path}, [missing])."""
    mentioned = [
        t for t in TOOLCHAIN_BINARIES
        if re.search(rf"(?<![\w./-]){re.escape(t)}(?![\w-])", request)
    ]
    found, missing = {}, []
    for tool in mentioned:
        path = shutil.which(tool)
        (found.update({tool: path}) if path else missing.append(tool))
    return found, missing


def installed_tools_in(text: str) -> dict:
    """Toolchain binaries mentioned in a BLOCKED claim that actually exist.
    Matches inside hyphenated compounds too — a claim about a phantom
    'dotnet-isolated runtime' is really a claim about dotnet."""
    return {
        t: shutil.which(t) for t in TOOLCHAIN_BINARIES
        if re.search(rf"(?<![\w.]){re.escape(t)}(?!\w)", text) and shutil.which(t)
    }


def apply_final_verify(passed: bool, test_report: str, workspace: "Workspace",
                       verify_cmd: str | None):
    """Independently run the request's stated verification after a claimed
    PASS. An agent's word is not evidence: a tester once reported PASS on a
    build it had broken with its own final file write."""
    if not (passed and verify_cmd):
        return passed, test_report
    banner(f"FINAL VERIFY: {verify_cmd}")
    result = workspace.run_command(verify_cmd)
    ok = result.startswith("exit code: 0")
    print("verified OK" if ok else result[:2500])
    if not ok:
        print("Final verification FAILED — overriding the tester's PASS.")
        test_report += (
            f"\n\nINDEPENDENT VERIFICATION FAILED: `{verify_cmd}` exited "
            f"nonzero AFTER you reported PASS — your last file writes likely "
            f"broke it:\n{result[:4000]}"
        )
    return ok, test_report


def find_blocked(text: str):
    """Return the BLOCKED: line if an agent declared the task blocked."""
    for line in text.strip().splitlines()[:5]:
        if line.strip().lstrip("*# ").startswith("BLOCKED:"):
            return line.strip().lstrip("*# ")
    return None


def abort_blocked(blocked_line: str, workspace: "Workspace"):
    banner("RUN BLOCKED")
    print(blocked_line)
    print(
        "\nThe team cannot finish this on the current machine — agents are not\n"
        "allowed to install software or modify anything outside the workspace.\n"
        "Install what's missing yourself, then rerun the same request."
    )
    print(f"Workspace: {workspace.root}")
    sys.exit(2)


def run_challenging_blocked(agent: "Agent", prompt: str, workspace: "Workspace") -> str:
    """Run an agent, but don't take the first BLOCKED for an answer.

    Models reach for BLOCKED as an easy exit — e.g. declaring a missing NuGet
    package a "missing tool" when 'dotnet add package' would fix it from
    inside the workspace. Challenge once; abort only if the agent reconfirms.
    """
    reply = agent.run(prompt)
    blocked = find_blocked(reply)
    if not blocked:
        return reply
    print(f"  [{agent.name}] declared blocked; challenging once: {blocked[:120]}")

    actually_installed = installed_tools_in(blocked)
    if actually_installed:
        evidence = "; ".join(f"'{t}' IS installed at {p}"
                             for t, p in actually_installed.items())
        challenge = (
            prompt
            + f"\n\nIn a previous attempt you stopped with:\n{blocked}\n\n"
            f"That diagnosis is FALSE: {evidence} (verified on PATH just now). "
            "Your command failed for a different reason. Rerun the failing "
            "command, read its actual stdout/stderr, and fix the real problem "
            "— a compile error, a broken project file, a wrong flag. Do not "
            "claim a tool is missing when it is installed."
        )
    else:
        challenge = (
            prompt
            + f"\n\nIn a previous attempt you stopped with:\n{blocked}\n\n"
            "Double-check before the whole run is aborted. BLOCKED is ONLY for "
            "system-level tools — compilers, SDKs, CLI binaries — that would "
            "require installing software on the machine. A missing LIBRARY or "
            "PACKAGE is never a blocker: add it yourself inside the workspace "
            "with the project's package manager, e.g. 'dotnet add package <Name>', "
            "'npm install <name>' (project-local, no -g), '.venv/bin/pip install "
            "<name>', or 'cargo add <name>'. If your blocker is really a package, "
            "add it now and finish the task. And a COMPILER ERROR (CS####, "
            "TS####, 'could not be found', 'undefined reference') is never an "
            "environment problem — it means YOUR code is wrong: read the error, "
            "fix the file and line it names. Reply BLOCKED again only if a "
            "genuine system tool is missing."
        )
    reply = agent.run(challenge)
    blocked = find_blocked(reply)
    if not blocked:
        return reply
    contradicted = installed_tools_in(blocked)
    if contradicted:
        # The claim is demonstrably false, so this is a confused agent, not a
        # missing prerequisite. Don't kill a run whose earlier work may be
        # fine — let the pipeline (review loop, next task, tester) carry on.
        print(
            "  NOTE: agent claims a missing tool but PATH disagrees ("
            + ", ".join(f"{t} -> {p}" for t, p in contradicted.items())
            + "); continuing the pipeline instead of aborting."
        )
        return reply
    abort_blocked(blocked, workspace)


def main():
    parser = argparse.ArgumentParser(description="Run the local multi-agent coding team.")
    parser.add_argument("request", help="What you want built or changed.")
    parser.add_argument("--workspace", required=True, help="Directory the team may read/write.")
    parser.add_argument("--config", default=str(REPO_ROOT / "team.json"))
    parser.add_argument(
        "--verify", default=None,
        help="Shell command that must exit 0 in the workspace for the run to "
        "pass; overrides the tester's verdict. Default: auto-detected from "
        "\"verify with '<cmd>'\" in the request.",
    )
    args = parser.parse_args()

    verify_cmd = args.verify
    if not verify_cmd:
        match = re.search(r"[Vv]erif\w*\s+(?:with|using)\s+['\"`]([^'\"`]+)['\"`]", args.request)
        if match:
            verify_cmd = match.group(1)
    if verify_cmd:
        print(f"Final verification command: {verify_cmd}")

    config = json.loads(Path(args.config).read_text())
    workspace_dir = Path(args.workspace).expanduser()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(workspace_dir, config["pipeline"].get("command_timeout_seconds", 120))

    existing_files = workspace.list_files()
    if not existing_files.startswith("("):
        count = len(existing_files.splitlines())
        print(
            f"Note: workspace already contains {count} file(s). Scaffolding tools "
            "(func init, npm create, ...) may refuse to overwrite existing files — "
            "for a green-field project, prefer an empty directory."
        )

    # Deterministic environment check before any model time is spent: if the
    # request names a toolchain binary, it must exist on THIS process's PATH
    # (which is what every agent command will inherit).
    found, missing = preflight_tools(args.request)
    if found:
        print("Preflight: " + ", ".join(f"{t} -> {p}" for t, p in found.items()))
    if missing:
        banner("PREFLIGHT FAILED")
        print("Tool(s) named in your request were not found on PATH:")
        for tool in missing:
            print(f"  - {tool}")
        print(
            "\nAgents inherit this terminal's environment, so they will hit the "
            "same wall.\nInstall the missing tool(s) or fix PATH, then rerun."
        )
        sys.exit(2)

    client = OpenAI(
        base_url=config["server"]["base_url"],
        api_key=config["server"].get("api_key", "local"),
        timeout=config["server"].get("request_timeout_seconds", 300),
        max_retries=1,
    )
    # Ollama enforces JSON schemas only on its native endpoint; derive it
    # from the OpenAI-compatible base_url (".../v1" -> server root).
    base_url = config["server"]["base_url"].rstrip("/")
    native_url = config["server"].get(
        "native_url",
        base_url[: -len("/v1")] if base_url.endswith("/v1") else None,
    )
    agents = {
        name: Agent(name, spec, client, workspace, native_url=native_url)
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
    if isinstance(tasks, dict):
        tasks = tasks.get("tasks", [])
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
            coder_summary = run_challenging_blocked(agents["coder"], prompt, workspace)
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
    test_report = run_challenging_blocked(agents["tester"], tester_prompt, workspace)
    print(test_report)
    passed = "RESULT: PASS" in test_report
    passed, test_report = apply_final_verify(passed, test_report, workspace, verify_cmd)

    max_fix_rounds = config["pipeline"].get("max_fix_rounds", 2)
    for fix_round in range(1, max_fix_rounds + 1):
        if passed:
            break
        banner(f"FIX ROUND {fix_round}: coder addresses the test failures")
        fix_summary = run_challenging_blocked(
            agents["coder"],
            f"Overall goal: {args.request}\n\n"
            f"The tester ran the suite and it FAILED. Tester's report:\n{test_report}\n\n"
            "Make the tests pass:\n"
            "- If the code is wrong, fix the code.\n"
            "- If a failing test is stale — it expects functions or names the "
            "current code never exposed — rewrite that test against the current "
            "public API instead of changing working code to satisfy it.\n"
            "- Rerun the failing test command yourself and confirm it passes "
            "before you finish.",
            workspace,
        )
        print(f"  coder: {fix_summary.strip()[:500]}")
        banner(f"RETEST after fix round {fix_round}")
        test_report = run_challenging_blocked(
            agents["tester"],
            tester_prompt
            + "\n\nA previous test run failed and the coder has since pushed fixes. "
            "Re-verify from scratch: rerun the suite before writing your report.",
            workspace,
        )
        print(test_report)
        passed = "RESULT: PASS" in test_report
        passed, test_report = apply_final_verify(passed, test_report, workspace, verify_cmd)

    banner("DONE")
    print(f"Workspace: {workspace.root}")
    print(f"Files written this run: {len(workspace.written_files)}")
    print("Tests: PASS" if passed else "Tests: FAIL or inconclusive — read the report above.")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
