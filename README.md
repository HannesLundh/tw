# Local Multi-Agent Coding Team (Apple Silicon, 24 GB)

A practical setup for running a **multi-agent coding workflow entirely on a
MacBook M5 Pro with 24 GB unified memory**, using local models served by
[Ollama](https://ollama.com) (or LM Studio / llama.cpp — anything with an
OpenAI-compatible API).

## The core idea

With 24 GB you cannot load one big model per agent. The pattern that works is:

```
                ┌────────────────────────────┐
                │  ONE local model server    │
                │  (Ollama, port 11434)      │
                │  qwen2.5-coder:14b         │
                └──────────▲─────────────────┘
                           │  concurrent chat sessions
        ┌───────────┬──────┴──────┬────────────┐
        │ Planner   │ Coder       │ Reviewer   │  Tester
        │ (role     │ (role       │ (role      │  (role
        │  prompt)  │  prompt +   │  prompt)   │   prompt +
        │           │  file tools)│            │   run tools)
        └───────────┴─────────────┴────────────┘
```

**Agents are roles, not models.** Each agent is a separate conversation with
its own system prompt and tools, all hitting the same served model. That gives
you real multi-agent behavior (plan → implement → review → test, with the
reviewer genuinely pushing back on the coder) at the memory cost of a single
model. Optionally a second *small* model (~2–3 GB) is co-loaded for cheap
utility roles.

## Quickstart

```bash
# 1. Install and pull models (~12 GB download)
./scripts/setup.sh

# 2. Start the server with tuned settings (keep this terminal open)
./scripts/serve.sh

# 3. Install the orchestrator dependency and run the team
pip install -r orchestrator/requirements.txt
python orchestrator/run_team.py \
  "Build a CLI todo app in Python with add/list/done commands and tests" \
  --workspace ~/code/todo-app
```

The orchestrator runs the full loop: the **planner** breaks the request into
tasks, the **coder** implements each task with file/shell tools, the
**reviewer** critiques the diff and can send it back for fixes, and the
**tester** writes and runs tests at the end. For runnable apps and services
the tester must also pass a **local smoke test** — start the server, hit it,
kill it — because code that compiles but crashes on startup is not done.

## What's in this repo

| Path | Purpose |
|---|---|
| `agents/*.md` | System prompts for each role (planner, coder, reviewer, tester). Tool-agnostic — also usable in Aider, CrewAI, OpenHands, etc. |
| `team.json` | Maps roles → models + prompts. Swap models here without touching code. |
| `orchestrator/run_team.py` | The multi-agent pipeline (~300 lines, no framework, OpenAI-compatible API). |
| `scripts/setup.sh` | Installs Ollama and pulls the recommended models. |
| `scripts/serve.sh` | Starts Ollama with memory/concurrency settings tuned for 24 GB. |
| `docs/models.md` | Which models fit in 24 GB, memory math, and macOS tuning (KV cache, wired-memory limit). |

## Recommended models for 24 GB (short version)

| Config | Model | RAM (Q4) | Why |
|---|---|---|---|
| `team.json` (default) | `devstral-small-2:24b` | ~15 GB | Dec 2025 agentic coding model, trained for exactly this multi-step tool workload; freshest API knowledge. |
| `team-qwen3coder.json` | `qwen3-coder:30b` (MoE, 3B active) | ~19 GB | 2–3× faster generation, 256K context; tight fit — see `docs/models.md` first. |
| `team-light.json` | `qwen2.5-coder:14b` | ~9 GB | Lightweight fallback; late-2024 knowledge shows on modern frameworks. |
| Utility role | `qwen3:4b` | ~2.6 GB | Cheap roles (summaries, triage); co-load only with the 14B. |

Pick a config with `--config`, e.g.
`python orchestrator/run_team.py "..." --workspace ~/w --config team-qwen3coder.json`.

Full details, memory math, and tuning: **[docs/models.md](docs/models.md)**.

## Scaling patterns

- **Parallel agents:** `serve.sh` sets `OLLAMA_NUM_PARALLEL=2` so two agents
  can talk to the model simultaneously. Each parallel slot costs KV-cache
  memory, so don't raise it beyond 2–3 at 14B.
- **Two-model split:** run `qwen2.5-coder:14b` as coder + `qwen3:4b` as
  reviewer/summarizer concurrently (`OLLAMA_MAX_LOADED_MODELS=2`). Edit
  `team.json` to assign the small model to a role.
- **Using with other tools:** the prompts in `agents/` drop into
  [Aider](https://aider.chat) (`aider --model ollama/qwen2.5-coder:14b`),
  Continue, or any framework that accepts a system prompt. The orchestrator
  here is deliberately framework-free so you can read all of it.

## Troubleshooting

- **The coder prints JSON like `{"name": "write_file", ...}` instead of
  editing files.** Local models frequently emit tool calls as plain text
  instead of using the tool-calling API. The orchestrator detects this and
  executes those calls anyway (you'll see a one-line note per agent). If it
  happens constantly, try `devstral:24b`, which is trained for native tool
  use, and make sure Ollama is current (`brew upgrade ollama`).
- **"Files written this run: 0" with lots of reviewer rejections** was the
  symptom of the above before the fallback existed — update to the latest
  version of this repo.
- **A run seems frozen for minutes.** Check the last printed line. If it's a
  `run_command(...)`, the command itself is running — it gets hard-killed
  (whole process group) at the configured timeout, 120 s by default. If it's
  a coder/tester line with no tool call, the model is generating: on long
  contexts a 14B model can take ~1 min before the first token, which is
  normal. Minutes of true silence after that usually means swapping — see
  below.
- **An agent overwrote a file with placeholder text** like
  `<updated-content-of-file.py>`. Such writes are now rejected before they
  touch disk, Python files are syntax-checked on every write, and the
  pre-run original of any overwritten file is kept in
  `<workspace>/.agent-backups/` — restore with
  `cp .agent-backups/path/to/file path/to/file`.
- **Everything is slow / the Mac is swapping:** see the memory section of
  [docs/models.md](docs/models.md); the usual causes are context set too
  high or a second big model loaded.

## Safety note

The coder and tester agents can write files and run shell commands **inside
the workspace directory you pass with `--workspace`**. Point them at a scratch
directory or a git repo with a clean tree, and review diffs before trusting
output — local 14B models are good, but they are not infallible.

Guardrails enforced by the orchestrator:

- **No system modification.** Commands using sudo/apt/brew/npm -g, SDK
  install scripts, redirects into `$HOME` or system paths, or rm/mv/chmod
  outside the workspace are refused before they run. `pip install` is only
  allowed into a project-local `.venv`.
- **Missing tools end the run instead of derailing it.** If a task needs a
  compiler or CLI that isn't installed, the agent reports
  `BLOCKED: <tool> is not installed` and the run exits (code 2) with a
  message telling you what to install — no agent will try to "fix" your
  machine. Install the prerequisite (e.g. `brew install dotnet-sdk`
  yourself) and rerun. A BLOCKED claim is challenged once before aborting,
  because models love this exit: a missing *package* (NuGet, npm, pip,
  cargo) is not a blocker — agents add those themselves with project-local
  package managers. Claims are also fact-checked: toolchain binaries named
  in your request are verified on PATH before any model call (missing ones
  fail fast as `PREFLIGHT FAILED`), and a BLOCKED naming a tool that IS
  installed gets refuted with its actual path. Failed commands print their
  exit code and first error line to the console, so you can see what
  actually broke.
- **Placeholder writes are refused**, Python files are syntax-checked on
  write, and pre-run originals of overwritten files are kept in
  `<workspace>/.agent-backups/`.
- **PASS is verified, not trusted.** If the request says
  `verify with '<command>'` (or you pass `--verify '<command>'`), the
  orchestrator runs that command itself after the tester reports PASS; a
  nonzero exit overrides the verdict and sends the failure into the fix
  loop. Always phrase requests with a concrete verification command.
- **Runaway sessions are bounded**: shell commands are killed (whole process
  group) at a hard timeout, repeated identical tool calls are blocked, an
  agent that keeps hitting blocks is forced to wrap up, old tool output is
  trimmed once a conversation outgrows the context window, and LLM requests
  time out after 5 minutes instead of waiting forever.

The guardrails are pattern-based, not a sandbox — a determined shell command
can still slip through, so keep workspaces disposable.
