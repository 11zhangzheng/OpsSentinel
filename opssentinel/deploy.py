"""Install the native controller and an opt-in disposable Compose lab on Linux."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
try:
    import pwd
except ImportError:  # File-generation and migration tests also run on Windows.
    pwd = None
import secrets
import shutil
import subprocess
import sys

import yaml

from .host_agent import load_config
from .models import ServiceCreate
from .process_lock import ProcessLock
from .store import Store

ROOT = Path('/opt/opssentinel')
CONFIG = Path('/etc/opssentinel')
DATA = Path('/var/lib/opssentinel-controller')
LAB = Path('/srv/opssentinel-lab')
UNITS = Path('/etc/systemd/system')


def write_new(path: Path, content: str, mode=0o600):
    """Preserve credentials and operator configuration on repeat installation."""
    if path.exists():
        if path.is_symlink():
            raise ValueError(f'Refusing symlink: {path}')
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'w') as handle:
        handle.write(content)


def env_value(path: Path, key: str):
    for line in path.read_text().splitlines():
        if line.startswith(key + '='):
            value = line.split('=', 1)[1]
            if len(value) < 24 or any(c.isspace() for c in value):
                raise ValueError('Token must contain at least 24 non-whitespace characters')
            return value
    raise ValueError(f'Missing {key} in credential file')


def controller_unit(python: Path) -> str:
    if not python.is_absolute() or any(c in str(python) for c in '\n\r"%'):
        raise ValueError('Invalid interpreter path')
    return f'''[Unit]
Description=OpsSentinel persistent controller
After=network-online.target opssentinel-agent.service
Wants=network-online.target

[Service]
Type=simple
User=opssentinel
Group=opssentinel
WorkingDirectory={ROOT}
EnvironmentFile={CONFIG}/controller.env
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=OPS_DEPLOYMENT_MODE=systemd
ExecStart="{python}" -m opssentinel --host 127.0.0.1 --port 8765 --data-dir {DATA} --config {CONFIG}/controller.yaml --no-demo
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={DATA}
MemoryMax=250M
TasksMax=64

[Install]
WantedBy=multi-user.target
'''


def activate_import(path: Path, service_id: str, agent_token: str, *, auto_restart: bool):
    """Call only on the new, stopped controller after source ownership was released."""
    lock = ProcessLock(path.parent/'controller.lock')
    lock.acquire()
    store = None
    try:
        store = Store(path)
        services = store.list_services()
        if len(services) != 1 or services[0]['id'] != service_id:
            raise ValueError('Import must contain exactly the selected service')
        service = services[0]
        if service['enabled'] or service['auto_actions'] or service['connector'] != 'agent' or service['agent_service'] != 'cloud_test_api':
            raise ValueError('Import must be a paused, unarmed cloud_test_api handoff')
        store.patch_service(service_id, {'target': 'http://127.0.0.1:9876', 'agent_token': agent_token,
                                         'enabled': True, 'auto_actions': ['restart_service'] if auto_restart else []})
        store.reset_check_counts(service_id, invalidate=True)
        store.event('controller_migrated', '单服务历史已迁入云端；采集地址更新，重新累计新观测', service_id)
    finally:
        if store:
            store.close()
        lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Checked-out source containing examples/cloud-lab')
    parser.add_argument('--with-lab', action='store_true', help='Create/update only the dedicated opssentinel-lab test API')
    parser.add_argument('--auto-restart', action='store_true', help='Allow restart of the disposable test API')
    parser.add_argument('--import-db', type=Path, help='Private handoff snapshot exported from a paused controller')
    parser.add_argument('--service-id', default='cloud-test-api')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('Run as root on Linux; the controller itself runs as the opssentinel account')
    if not args.with_lab:
        parser.error('This first-install profile requires --with-lab; it never discovers or adopts arbitrary services')
    source = args.source.resolve()
    python = ROOT/'.venv/bin/python'
    if not python.is_file() or not (source/'examples/cloud-lab/compose.yaml').is_file():
        parser.error('Run scripts/install-linux.sh from a complete checkout first')
    subprocess.run(['docker', 'compose', 'version'], check=True)
    subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'], check=True)
    for directory in (ROOT, CONFIG, DATA, LAB):
        if directory.is_symlink():
            parser.error('Managed directories cannot be symlinks')
    if args.import_db and ((DATA/'state.sqlite3').exists() or not args.import_db.is_file()):
        parser.error('Import requires a new controller data directory and an existing handoff file')
    existing_unit = UNITS/'opssentinel-controller.service'
    if existing_unit.exists() and 'Description=OpsSentinel persistent controller' not in existing_unit.read_text():
        parser.error('A different controller unit exists; refusing to overwrite it')
    # An installed service may be restarted only after its process has stopped.
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'opssentinel-controller']).returncode == 0:
        parser.error('Stop the existing controller before running this installer')
    for directory in (ROOT, CONFIG, DATA, LAB):
        directory.mkdir(parents=True, exist_ok=True)
    CONFIG.chmod(0o700)
    DATA.chmod(0o700)
    try:
        account = pwd.getpwnam('opssentinel')
    except KeyError:
        subprocess.run(['useradd', '--system', '--user-group', '--home-dir', str(DATA), '--shell', '/usr/sbin/nologin', 'opssentinel'], check=True)
        account = pwd.getpwnam('opssentinel')
    if account.pw_uid == 0:
        raise ValueError('Controller account must be unprivileged')
    for name in ('app.py', 'compose.yaml'):
        target = LAB/name
        if target.is_symlink():
            raise ValueError('Lab files cannot be symlinks')
        if name == 'compose.yaml' and target.exists() and yaml.safe_load(target.read_text()).get('name') != 'opssentinel-lab':
            raise ValueError('Refusing to overwrite a different Compose project')
        shutil.copyfile(source/'examples/cloud-lab'/name, target)
        target.chmod(0o644)
    write_new(CONFIG/'agent.env', 'OPS_AGENT_TOKEN=' + secrets.token_urlsafe(32) + '\n')
    agent_token = env_value(CONFIG/'agent.env', 'OPS_AGENT_TOKEN')
    write_new(CONFIG/'agent.yaml', yaml.safe_dump({'state_dir': '/var/lib/opssentinel-agent', 'services': {
        'cloud_test_api': {'compose_file': str(LAB/'compose.yaml'), 'compose_service': 'api',
                           'health_url': 'http://127.0.0.1:18080/health', 'business_url': 'http://127.0.0.1:18080/ready',
                           'allowed_actions': ['restart_service'] if args.auto_restart else []}}}, sort_keys=False))
    agent = load_config(CONFIG/'agent.yaml')['services'].get('cloud_test_api')
    if not agent or agent['compose_file'] != str(LAB/'compose.yaml') or agent['compose_service'] != 'api':
        raise ValueError('Existing agent configuration does not match the dedicated lab')
    if args.auto_restart and 'restart_service' not in agent['allowed_actions']:
        raise ValueError('Explicitly allow restart_service in existing Agent config first')
    write_new(CONFIG/'controller.env', 'OPS_API_TOKEN=' + secrets.token_urlsafe(32) + '\nOPS_LINUX_AGENT_TOKEN=' + agent_token + '\n')
    env_value(CONFIG/'controller.env', 'OPS_API_TOKEN')
    if env_value(CONFIG/'controller.env', 'OPS_LINUX_AGENT_TOKEN') != agent_token:
        raise ValueError('Agent token changed; update the private controller environment first')
    service = ServiceCreate(name='云端 · 故障演练 API', connector='agent', target='http://127.0.0.1:9876',
                            agent_service='cloud_test_api', agent_token=agent_token,
                            auto_actions=['restart_service'] if args.auto_restart else [], interval_seconds=5,
                            resource_rules=[{'metric': m, 'above': a, 'recover_below': b, 'for_checks': 3}
                                            for m,a,b in [('cpu_percent',90,80),('memory_percent',90,85),('disk_percent',85,80)]]).model_dump()
    service.pop('agent_token')
    service.update(id=args.service_id, agent_token_env='OPS_LINUX_AGENT_TOKEN')
    write_new(CONFIG/'controller.yaml', yaml.safe_dump({'services': [service]}, allow_unicode=True, sort_keys=False), 0o644)
    if args.import_db:
        with args.import_db.open('rb') as incoming, (DATA/'state.sqlite3').open('xb') as target:
            shutil.copyfileobj(incoming, target)
        (DATA/'state.sqlite3').chmod(0o600)
        activate_import(DATA/'state.sqlite3', args.service_id, agent_token, auto_restart=args.auto_restart)
    # The config contains only an environment-variable reference, not a credential.
    # systemd reads the root-only EnvironmentFile before changing user.
    os.chown(CONFIG, 0, account.pw_gid)
    CONFIG.chmod(0o750)
    os.chown(CONFIG/'controller.yaml', 0, account.pw_gid)
    (CONFIG/'controller.yaml').chmod(0o640)
    for path in [DATA, *DATA.iterdir()]:
        if path.is_symlink():
            raise ValueError('Controller data cannot contain symlinks')
        os.chown(path, account.pw_uid, account.pw_gid)
    existing_unit.write_text(controller_unit(python))
    existing_unit.chmod(0o644)
    agent_unit = UNITS/'opssentinel-agent.service'
    write_new(agent_unit, (source/'examples/cloud-lab/opssentinel-agent.service').read_text(), 0o644)
    subprocess.run(['systemd-analyze', 'verify', str(existing_unit), str(agent_unit)], check=True)
    subprocess.run(['docker', 'compose', '-f', str(LAB/'compose.yaml'), 'up', '-d', '--no-build'], check=True)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'opssentinel-agent', 'opssentinel-controller'], check=True)
    print(json.dumps({'dashboard': 'http://127.0.0.1:8765', 'service_id': args.service_id,
                      'credential_file': str(CONFIG/'controller.env'), 'controller_user': 'opssentinel',
                      'note': 'Connect via SSH forwarding. Closing the browser does not stop monitoring.'}))


if __name__ == '__main__':
    main()
