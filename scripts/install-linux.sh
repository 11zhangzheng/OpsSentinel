#!/bin/sh
# Run from a reviewed checkout. Requires Docker with Compose, systemd, Python3/pip.
set -eu
for install_arg in "$@"; do
    if [ "$install_arg" = '--help' ] || [ "$install_arg" = '-h' ]; then
        echo 'Usage: sudo bash scripts/install-linux.sh --with-lab [--auto-restart] [--import-db FILE --service-id ID]'
        echo 'Requires systemd, Docker Compose, and Python3/pip. See docs/quickstart-linux.md.'
        exit 0
    fi
done
if [ "$(id -u)" != 0 ]; then
    echo 'Run with sudo. Only the host Agent needs root; the controller uses a dedicated account.' >&2
    exit 1
fi
docker compose version >/dev/null
docker info >/dev/null
command -v systemctl >/dev/null
repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
runtime_root=/opt/opssentinel
if [ ! -x "$runtime_root/.venv/bin/python" ]; then
    python3 -m pip --version >/dev/null
    python3 -m pip install --target /opt/opssentinel-bootstrap 'uv==0.12.10'
    export UV_PYTHON_INSTALL_DIR=/opt/opssentinel-python
    /opt/opssentinel-bootstrap/bin/uv python install 3.12
    /opt/opssentinel-bootstrap/bin/uv venv --python 3.12 "$runtime_root/.venv"
fi
# Refuse hot upgrades: operators must first stop the managed controller/agent.
if systemctl is-active --quiet opssentinel-controller || systemctl is-active --quiet opssentinel-agent; then
    echo 'Stop opssentinel-controller and opssentinel-agent before upgrading; persisted work will be retained.' >&2
    exit 1
fi
"$runtime_root/.venv/bin/python" -m ensurepip --upgrade
"$runtime_root/.venv/bin/python" -m pip install "$repo_dir"
cd /
exec "$runtime_root/.venv/bin/python" -m opssentinel.deploy --source "$repo_dir" "$@"
