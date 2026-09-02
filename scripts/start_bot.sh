#!/usr/bin/env bash
# Start the bizcard intake bot.
# Loads project-local .env (if present), validates required tokens,
# and execs the Python module so launchd can supervise it.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

ENV_FILE="$PROJECT_ROOT/.env"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    echo "TELEGRAM_BOT_TOKEN is required. Set it in $ENV_FILE or your shell env." >&2
    exit 1
fi

# Make `codex` and `gws` discoverable when invoked via launchd (minimal PATH).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

PYTHON_BIN="${BIZCARD_PYTHON:-$(command -v python3)}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python interpreter not found at $PYTHON_BIN. Override with BIZCARD_PYTHON=." >&2
    exit 1
fi

exec "$PYTHON_BIN" -m bizcard_intake bot
