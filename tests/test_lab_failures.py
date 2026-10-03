import json
from types import SimpleNamespace

import httpx
import pytest
import yaml

from opssentinel import deploy, lab


@pytest.mark.parametrize('attempted,cleanup_offline', [(True, False), (False, False), (True, True)])
def test_drill_failure_does_not_replay_an_attempted_or_unknown_action(tmp_path, monkeypatch, attempted, cleanup_offline):
    compose = tmp_path/'compose.yaml'
    compose.write_text(yaml.safe_dump({'name': 'opssentinel-lab', 'services': {'api': {
        'command': ['python', '-u', '/app/app.py'], 'ports': ['127.0.0.1:18080:8080'],
        'volumes': ['/srv/opssentinel-lab/app.py:/app/app.py:ro'],
        'image': 'public.ecr.aws/docker/library/python@sha256:' + 'a'*64}}}))
    monkeypatch.setattr(lab, 'COMPOSE', compose)
    monkeypatch.setattr(lab, 'os', SimpleNamespace(name='posix', geteuid=lambda: 0))
    monkeypatch.setattr(deploy, 'env_value', lambda *args: 'private-token-not-in-output')
    monkeypatch.setattr('sys.argv', ['lab', 'automatic', '--allow-faults', '--output', str(tmp_path/'results')])
    service = {'id': 'lab', 'connector': 'agent', 'agent_service': 'cloud_test_api',
               'target': 'http://127.0.0.1:9876', 'health': 'healthy', 'freshness': 'fresh',
               'maintenance_active': False, 'enabled': True, 'interval_seconds': 5, 'auto_actions': [],
               'latest': {'facts': {'allowed_actions': ['restart_service']}}}
    requests, commands = [], []
    class Client:
        reads = 0
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def request(self, method, path, **kwargs):
            requests.append((method, path, kwargs))
            if method == 'GET':
                self.reads += 1
                if self.reads == 2 or (self.reads == 3 and cleanup_offline):
                    raise httpx.ReadTimeout('Controller became unavailable')
                incidents = [] if self.reads == 1 else [{'id':'new', 'service_id':'lab', 'attempts':int(attempted)}]
                body = {'services': [service], 'incidents': incidents}
            else:
                body = service
            return httpx.Response(200, json=body, request=httpx.Request(method, 'http://localhost'+path))
    monkeypatch.setattr(lab.httpx, 'Client', lambda **kwargs: Client())
    def command(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0, stderr='', stdout='a'*64 if 'ps' in args else '')
    monkeypatch.setattr(lab.subprocess, 'run', command)
    with pytest.raises(httpx.ReadTimeout):
        lab.main()
    restart_calls = [c for c in commands if 'restart' in c]
    assert len(restart_calls) == (0 if attempted or cleanup_offline else 1)
    assert any(r[2].get('json', {}).get('enabled') is False for r in requests)
    result = json.loads(next((tmp_path/'results').glob('run-*/result.json')).read_text())
    assert result['passed'] is False
    assert 'private-token-not-in-output' not in json.dumps(result)
    if cleanup_offline:
        assert 'cleanup_error' in result
