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
**tester** writes and runs tests at the end.

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

| Role | Model | RAM (Q4) | Why |
|---|---|---|---|
| Main (coder/planner/reviewer) | `qwen2.5-coder:14b` | ~9 GB | Best coding quality-per-GB; leaves room for context and your IDE. |
| Utility (summaries, commit msgs) | `qwen3:4b` | ~2.6 GB | Fast, co-loads alongside the main model. |
| Power option | `qwen3-coder:30b` (MoE, 3B active) | ~18 GB | Faster and smarter, but a tight fit — see `docs/models.md` before using. |
| Agentic/tool-use option | `devstral:24b` | ~14 GB | Strong tool-calling; good if the coder agent struggles with tools. |

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
- **Everything is slow / the Mac is swapping:** see the memory section of
  [docs/models.md](docs/models.md); the usual causes are context set too
  high or a second big model loaded.

## Safety note

The coder and tester agents can write files and run shell commands **inside
the workspace directory you pass with `--workspace`**. Point them at a scratch
directory or a git repo with a clean tree, and review diffs before trusting
output — local 14B models are good, but they are not infallible.
