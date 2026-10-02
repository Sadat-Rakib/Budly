#!/usr/bin/env bash
# Budly setup helper (macOS / Linux). Checks the tools, installs Budly, prepares .env.
set -u

echo "=== Budly setup ==="

if command -v python3 >/dev/null 2>&1; then
    echo "[ok] $(python3 --version)"
else
    echo "[MISSING] Python 3.12+ is required. Install from https://www.python.org/downloads/"
    exit 1
fi

if command -v uv >/dev/null 2>&1; then
    echo "[ok] $(uv --version)"
else
    echo "[MISSING] uv is required. Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

echo "[..] Installing dependencies..."
uv sync || { echo "[FAIL] uv sync failed. See the output above."; exit 1; }
echo "[ok] Dependencies installed."

if [ -f .env ]; then
    echo "[ok] .env already exists - keeping it."
else
    cp .env.example .env
    echo "[ok] Created .env from .env.example."
    echo "     Next: open .env and add your Canvas URL and access token."
fi

echo
echo "=== Done ==="
echo "Start Budly with:   uv run budly start"
echo "Then open:          http://127.0.0.1:8000"
echo "Check Canvas with:  uv run budly test-canvas"
