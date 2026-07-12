#!/usr/bin/env bash
# Start Ollama with settings tuned for a 24 GB Apple Silicon Mac running
# a multi-agent workload. Keep this terminal open while agents run.
set -euo pipefail

# The orchestrator talks to one agent at a time, and the 15 GB default
# model (devstral-small-2) wants every spare GB for KV cache. Raise these
# only if you switch to the 9 GB model AND want the qwen3:4b utility model
# co-loaded (then: NUM_PARALLEL=2, MAX_LOADED_MODELS=2).
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_MAX_LOADED_MODELS=1

# Flash attention + 8-bit KV cache roughly halves context memory with no
# meaningful quality loss — this is what makes 16k+ contexts comfortable.
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_KV_CACHE_TYPE=q8_0

# Keep models in memory between agent turns instead of reloading each time.
export OLLAMA_KEEP_ALIVE=30m

# Default context window for models that don't specify one. 16k is a good
# balance on 24 GB; see docs/models.md before raising it.
export OLLAMA_CONTEXT_LENGTH=16384

echo "Starting Ollama (parallel=1, kv-cache=q8_0, ctx=16384)..."
exec ollama serve
