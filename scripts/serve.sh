#!/usr/bin/env bash
# Start Ollama with settings tuned for a 24 GB Apple Silicon Mac running
# a multi-agent workload. Keep this terminal open while agents run.
set -euo pipefail

# Two agents may talk to the model at once (planner+coder overlap is rare,
# but coder+reviewer can pipeline). Each slot costs KV-cache memory, so
# don't raise this beyond 3 with a 14B model.
export OLLAMA_NUM_PARALLEL=2

# Allow the big coder model and the small utility model to stay loaded
# side by side (about 9 GB + 2.6 GB).
export OLLAMA_MAX_LOADED_MODELS=2

# Flash attention + 8-bit KV cache roughly halves context memory with no
# meaningful quality loss — this is what makes 16k+ contexts comfortable.
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_KV_CACHE_TYPE=q8_0

# Keep models in memory between agent turns instead of reloading each time.
export OLLAMA_KEEP_ALIVE=30m

# Default context window for models that don't specify one. 16k is a good
# balance on 24 GB; see docs/models.md before raising it.
export OLLAMA_CONTEXT_LENGTH=16384

echo "Starting Ollama (parallel=2, kv-cache=q8_0, ctx=16384)..."
exec ollama serve
