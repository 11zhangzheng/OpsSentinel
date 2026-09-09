#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$PROJECT_ROOT"

if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON_EXE="$PROJECT_ROOT/.venv/bin/python"
elif [ -x "$PROJECT_ROOT/.venv/Scripts/python.exe" ]; then
    PYTHON_EXE="$PROJECT_ROOT/.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_EXE=$(command -v python3)
elif command -v python >/dev/null 2>&1; then
    PYTHON_EXE=$(command -v python)
else
    printf '%s\n' 'Python 3.11+ was not found. Install Python and create a project .venv.' >&2
    exit 1
fi

if ! "$PYTHON_EXE" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    printf '%s\n' 'Python 3.11 or newer is required.' >&2
    exit 1
fi

if ! "$PYTHON_EXE" -c 'import fastapi, uvicorn, httpx, pydantic, yaml, psutil' 2>/dev/null; then
    printf '%s\n' 'Dependencies are missing. From the project root, run:' >&2
    if [ ! -x "$PROJECT_ROOT/.venv/bin/python" ] && [ ! -x "$PROJECT_ROOT/.venv/Scripts/python.exe" ]; then
        printf '"%s" -m venv .venv\n' "$PYTHON_EXE" >&2
    fi
    if [ -x "$PROJECT_ROOT/.venv/Scripts/python.exe" ]; then
        printf '%s\n' ".venv/Scripts/python.exe -m pip install -e '.[dev]'" >&2
    else
        printf '%s\n' ".venv/bin/python -m pip install -e '.[dev]'" >&2
    fi
    exit 1
fi

DEMO_PORT=${OPS_DEMO_PORT:-8765}
DEMO_DATA_DIR=${OPS_DEMO_DATA_DIR:-"$PROJECT_ROOT/.opssentinel"}
printf 'OpsSentinel isolated demo: http://127.0.0.1:%s (Ctrl+C to stop)\n' "$DEMO_PORT"
exec "$PYTHON_EXE" -m opssentinel --host 127.0.0.1 --port "$DEMO_PORT" --data-dir "$DEMO_DATA_DIR" --demo
