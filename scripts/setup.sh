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

echo "Pulling the main coding model (~9 GB)..."
ollama pull qwen2.5-coder:14b

echo "Pulling the small utility model (~2.6 GB)..."
ollama pull qwen3:4b

echo
echo "Done. Next: ./scripts/serve.sh to start the tuned server."
echo "Optional bigger models (see docs/models.md before pulling):"
echo "  ollama pull devstral:24b       # ~14 GB, strongest tool use"
echo "  ollama pull qwen3-coder:30b    # ~18 GB MoE, tight fit on 24 GB"
