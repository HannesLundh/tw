#!/usr/bin/env bash
# One-time setup: install Ollama and pull the recommended models for 24 GB.
set -euo pipefail

if ! command -v ollama >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing Ollama via Homebrew..."
    brew install ollama
  else
    echo "Homebrew not found. Install Ollama from https://ollama.com/download and re-run."
    exit 1
  fi
fi

# Start a temporary server if one isn't already running, so we can pull.
if ! curl -s http://localhost:11434/api/version >/dev/null 2>&1; then
  echo "Starting a temporary Ollama server for the downloads..."
  ollama serve >/dev/null 2>&1 &
  TEMP_SERVER_PID=$!
  trap 'kill $TEMP_SERVER_PID 2>/dev/null || true' EXIT
  sleep 3
fi

echo "Pulling the main coding model (~15 GB, needs Ollama >= 0.13.3)..."
ollama pull devstral-small-2:24b

echo "Pulling the small utility model (~2.6 GB)..."
ollama pull qwen3:4b

echo
echo "Done. Next: ./scripts/serve.sh to start the tuned server."
echo "Recommended for the 15 GB model: raise the GPU wired-memory limit first:"
echo "  sudo sysctl iogpu.wired_limit_mb=20480   # resets on reboot"
echo "Alternative models (see docs/models.md, pick with --config):"
echo "  ollama pull qwen3-coder:30b      # ~19 GB MoE, fastest; team-qwen3coder.json"
echo "  ollama pull qwen2.5-coder:14b    # ~9 GB lightweight; team-light.json"
