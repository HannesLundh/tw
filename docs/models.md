# Models and memory on a 24 GB Apple Silicon Mac

## The memory budget

Unified memory means the GPU and CPU share your 24 GB. Realistically:

| Consumer | Typical use |
|---|---|
| macOS + background apps | 4–6 GB |
| Your editor/browser while agents run | 2–4 GB |
| **Left for models + KV cache** | **~14–18 GB** |

By default macOS caps GPU-wired memory at roughly two-thirds of RAM
(~16 GB on a 24 GB machine). You can raise it when you need headroom for a
bigger model:

```bash
# ~20 GB wired limit; resets on reboot. Close heavy apps first.
sudo sysctl iogpu.wired_limit_mb=20480
```

If the machine starts swapping (Activity Monitor → Memory Pressure goes red),
you've overcommitted — drop to a smaller model or shorter context rather than
fighting swap, which makes token generation fall off a cliff.

## Model picks

All sizes are 4-bit quantizations (Q4_K_M or similar), which is the right
default: quality loss vs 8-bit is small, memory saving is large.

### Daily driver: `qwen2.5-coder:14b` (~9 GB)

The best coding quality-per-GB in this class. Fits with lots of room for
context, a second small model, and your IDE. Solid at tool/function calling,
which the coder and tester agents depend on. **Start here.**

### Utility: `qwen3:4b` (~2.6 GB)

Fast enough to feel instant. Use it for cheap roles — summaries, commit
messages, triage — and co-load it next to the 14B
(`OLLAMA_MAX_LOADED_MODELS=2`). To assign it a role, change that agent's
`model` in `team.json`.

### Tool-use specialist: `devstral:24b` (~14 GB)

Mistral's agentic coding model, trained specifically for multi-step tool use.
If the coder agent keeps fumbling tool calls with Qwen, try this. It fits,
but with less spare room — keep context at 16k and skip the co-loaded small
model, or raise the wired limit as shown above.

### Power option: `qwen3-coder:30b` (MoE, ~18 GB)

A 30B mixture-of-experts model with only ~3B parameters active per token, so
it generates *faster* than the dense 14B while being noticeably smarter. But
at ~18 GB plus KV cache it only works on 24 GB if you:

1. raise the wired limit (`sudo sysctl iogpu.wired_limit_mb=20480`),
2. keep context at 8k–16k with `OLLAMA_KV_CACHE_TYPE=q8_0`,
3. run it alone (`OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1`),
4. close memory-hungry apps.

Worth it for hard tasks; not worth it as your always-on default.

### What to avoid at 24 GB

- **Dense models ≥ 32B** (qwen2.5-coder:32b, llama-3.3-70b, etc.): a 32B Q4
  is ~20 GB before KV cache. Possible with aggressive tuning, but you'll be
  one browser tab away from swap the whole time.
- **8-bit quants of 14B+ models**: the memory doubles for a quality gain you
  will rarely notice in agent workflows.
- **Multiple large models loaded at once**: this is why the whole setup
  multiplexes agent roles over one served model.

## Context length vs memory

KV cache grows linearly with context and with `OLLAMA_NUM_PARALLEL` (each
parallel slot gets its own cache). Rough numbers for a 14B model with q8_0
KV cache: ~0.05 GB per 1k tokens per slot — so 16k × 2 slots ≈ 1.6 GB, and
64k × 2 slots ≈ 6.5 GB. That's the hidden cost people forget.

Guidelines:

- 16k (the `serve.sh` default) is enough for the orchestrator's task-sized
  prompts. The pipeline deliberately gives each agent a fresh, small context
  per task instead of one giant conversation.
- Go to 32k only when an agent genuinely needs to see many files at once,
  and consider dropping `OLLAMA_NUM_PARALLEL` to 1 when you do.
- `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0` (set in
  `serve.sh`) roughly halve KV memory; leave them on.

## MLX / LM Studio alternative

llama.cpp-based GGUF (what Ollama uses) is the most convenient, but Apple's
MLX runtime is typically 20–30 % faster on Apple Silicon. If you want that:

1. Install [LM Studio](https://lmstudio.ai), download the **MLX 4-bit**
   build of Qwen2.5-Coder-14B-Instruct.
2. Start its local server (default `http://localhost:1234/v1`).
3. Point this repo at it in `team.json`:

```json
"server": { "base_url": "http://localhost:1234/v1", "api_key": "lm-studio" }
```

and set each agent's `model` to the identifier LM Studio shows for the
loaded model. Everything else works unchanged, since the orchestrator only
speaks the OpenAI-compatible API.
