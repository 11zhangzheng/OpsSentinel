"""Repeatable fault drills restricted to the disposable opssentinel-lab project."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import httpx
import yaml

COMPOSE = Path('/srv/opssentinel-lab/compose.yaml')


def check_lab_document(document):
    services = document.get('services', {})
    if document.get('name') != 'opssentinel-lab' or set(services) != {'api'}:
        raise ValueError('Faults are allowed only on the dedicated, single-service opssentinel-lab project')
    api = services['api']
    if api.get('command') != ['python', '-u', '/app/app.py'] or api.get('ports') != ['127.0.0.1:18080:8080']:
        raise ValueError('Lab identity does not match the bundled fixture')
    if api.get('volumes') != ['/srv/opssentinel-lab/app.py:/app/app.py:ro'] or not str(api.get('image', '')).startswith('public.ecr.aws/docker/library/python@sha256:'):
        raise ValueError('Lab must use the bundled bind mount and immutable Python image')
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', choices=['approval', 'automatic', 'unhealthy', 'maintenance'])
    parser.add_argument('--allow-faults', action='store_true', help='Explicitly allow interrupting the disposable API')
    parser.add_argument('--output', type=Path, default=Path('/var/lib/opssentinel-lab-results'))
    args = parser.parse_args()
    if not args.allow_faults or os.name != 'posix' or os.geteuid() != 0:
        parser.error('Use --allow-faults as root on the Linux lab host')
    check_lab_document(yaml.safe_load(COMPOSE.read_text()))
    from .deploy import env_value
    token = env_value(Path('/etc/opssentinel/controller.env'), 'OPS_API_TOKEN')
    headers = {'Authorization': 'Bearer ' + token, 'X-OpsSentinel-Request': 'dashboard'}
    run_dir = args.output/('run-' + uuid.uuid4().hex)
    run_dir.mkdir(parents=True, mode=0o700)
    result = {'scenario': args.scenario, 'passed': False, 'started_at': time.time()}
    compose = ['docker', 'compose', '-f', str(COMPOSE)]

    def command(*tail):
        completed = subprocess.run(compose + list(tail), capture_output=True, text=True, timeout=30)
        if completed.returncode:
            raise RuntimeError('Lab Docker command failed: ' + completed.stderr[-1000:])
        return completed.stdout.strip()

    with httpx.Client(base_url='http://127.0.0.1:8765/api', headers=headers, trust_env=False, timeout=35) as client:
        def call(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        state = call('GET', '/state')
        candidates = [s for s in state['services'] if s['connector'] == 'agent'
                      and s['agent_service'] == 'cloud_test_api' and s['target'] == 'http://127.0.0.1:9876']
        if len(candidates) != 1:
            raise ValueError('Exactly one native lab monitoring entry is required')
        service = candidates[0]
        sid = service['id']
        if service['health'] != 'healthy' or service['freshness'] != 'fresh' or service['maintenance_active']:
            raise ValueError('Start from a fresh healthy service outside maintenance')
        if any(i['service_id'] == sid and i['status'] != 'resolved' for i in state['incidents']):
            raise ValueError('Resolve the existing lab incident before starting another drill')
        if 'restart_service' not in service.get('latest', {}).get('facts', {}).get('allowed_actions', []):
            raise ValueError('Agent must explicitly allow restart_service for this lab')
        before = command('ps', '-a', '-q', 'api')
        if len(before) != 64 or any(c not in '0123456789abcdef' for c in before):
            raise ValueError('Exactly one existing lab container is required')
        old_ids = {i['id'] for i in state['incidents']}
        old_actions = service['auto_actions']
        maintenance = False
        injected = False
        try:
            call('PATCH', '/services/' + sid, json={'auto_actions': [] if args.scenario == 'approval' else ['restart_service']})
            if args.scenario == 'maintenance':
                call('POST', '/services/' + sid + '/maintenance', json={'minutes': 5, 'reason': '可重复故障演练：验证维护抑制'})
                maintenance = True
            injected = True
            if args.scenario == 'unhealthy':
                command('kill', '-s', 'SIGUSR1', 'api')
            else:
                command('stop', '--timeout', '3', 'api')
            print(args.scenario + ': fault injected into the disposable API', flush=True)
            samples = set()
            approved = False
            deadline = time.monotonic() + max(180, service['interval_seconds'] * 12)
            while time.monotonic() < deadline:
                state = call('GET', '/state')
                current = next(s for s in state['services'] if s['id'] == sid)
                incidents = [i for i in state['incidents'] if i['service_id'] == sid and i['id'] not in old_ids]
                if len(incidents) > 1:
                    raise AssertionError('Duplicate incidents created')
                if maintenance:
                    if incidents or command('ps', '--status', 'running', '-q', 'api'):
                        raise AssertionError('Maintenance failed to suppress recovery')
                    if current['health'] == 'unhealthy' and current.get('latest', {}).get('observed_at'):
                        samples.add(current['last_check_at'])
                    if len(samples) >= 3:
                        call('DELETE', '/services/' + sid + '/maintenance')
                        maintenance = False
                        result['maintenance_failed_samples_without_actions'] = len(samples)
                        print('maintenance: three failed probes without an incident/action; maintenance ended', flush=True)
                elif incidents:
                    incident = incidents[0]
                    if incident['status'] == 'awaiting_approval':
                        if args.scenario != 'approval' or approved or incident['attempts'] != 0:
                            raise AssertionError('Unexpected approval or prior action')
                        if command('ps', '--status', 'running', '-q', 'api'):
                            raise AssertionError('Container restarted before approval')
                        call('POST', '/incidents/' + incident['id'] + '/approve')
                        approved = True
                    if incident['status'] == 'resolved':
                        if incident['resolution_kind'] != 'mitigated' or incident['attempts'] != 1 or current['consecutive_successes'] < 2:
                            raise AssertionError('Recovery lacks exactly one confirmed action and fresh probes')
                        if command('ps', '-a', '-q', 'api') != before:
                            raise AssertionError('Container identity changed')
                        if len([e for e in incident['events'] if e['kind'] == 'action_started']) != 1:
                            raise AssertionError('Unexpected action count')
                        if bool([e for e in incident['events'] if e['kind'] == 'approved']) != (args.scenario == 'approval'):
                            raise AssertionError('Approval evidence does not match the scenario')
                        report = call('GET', '/incidents/' + incident['id'] + '/report')
                        (run_dir/'incident.md').write_text(report['markdown'], encoding='utf-8')
                        result.update(passed=True, incident_id=incident['id'], attempts=incident['attempts'],
                                      same_container=True, elapsed_seconds=round(time.time()-result['started_at'], 1))
                        break
                    if incident['status'] == 'escalated' and incident['attempts']:
                        raise RuntimeError('Action failed or result is uncertain; no blind retries')
                time.sleep(2)
            if not result['passed']:
                raise TimeoutError('No verified recovery before the drill deadline')
        except Exception as exc:
            result['error'] = str(exc)
            raise
        finally:
            try:
                if injected and not result['passed']:
                    # Freeze scheduling before any fallback. If an action has begun,
                    # keep the service paused for investigation rather than replay it.
                    call('PATCH', '/services/' + sid, json={'enabled': False, 'auto_actions': []})
                    failed = call('GET', '/state')
                    attempted = any(i['service_id'] == sid and i['id'] not in old_ids and i['attempts']
                                    for i in failed['incidents'])
                    if attempted:
                        result['cleanup'] = 'Paused for inspection; an action was attempted, so no fallback write was sent'
                    else:
                        command('restart', '--no-deps', '--timeout', '3', 'api')
                        call('PATCH', '/services/' + sid, json={'enabled': service['enabled'], 'auto_actions': old_actions})
                        result['cleanup'] = 'No recovery action had started; disposable API restarted as fallback'
                else:
                    call('PATCH', '/services/' + sid, json={'auto_actions': old_actions})
                if maintenance:
                    call('DELETE', '/services/' + sid + '/maintenance')
            except Exception as exc:
                result['cleanup_error'] = str(exc)
            finally:
                (run_dir/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({**result, 'output': str(run_dir)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
