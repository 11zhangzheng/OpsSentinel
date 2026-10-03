import json
from pathlib import Path, PurePosixPath
import sqlite3

from fastapi.testclient import TestClient
import pytest

from opssentinel.app import create_app
from opssentinel.deploy import activate_import, controller_unit
from opssentinel.lab import check_lab_document
from opssentinel.reports import incident_report
from opssentinel.store import Store, now
from opssentinel.transfer import export_service


def config(name='cloud', *, enabled=False):
    return {'name': name, 'connector': 'agent', 'target': 'http://127.0.0.1:19876',
            'agent_service': 'cloud_test_api', 'agent_token': 'private-host-token',
            'enabled': enabled, 'auto_actions': [], 'interval_seconds': 5,
            'failure_threshold': 2, 'recovery_threshold': 2}


def observation(healthy=False):
    return {'healthy': healthy, 'reachable': True, 'summary': 'HTTP 200' if healthy else 'HTTP 503',
            'checks': [{'name': 'business', 'ok': healthy, 'detail': 'ready' if healthy else '<script>bad</script> | [link](https://evil.test)'}],
            'metrics': {}, 'logs': ['private-raw-log'], 'facts': {'arbitrary_secret': 'omit-this'}, 'observed_at': now()}


def populate(path):
    store = Store(path)
    for sid in ['selected', 'other']:
        store.add_service(config(sid), sid)
        service = store.record_observation(sid, observation())
        incident = store.create_incident(service, observation())
        aid = store.begin_action(incident, 'restart_service')
        store.finish_action(aid, {'ok': True, 'summary': 'restart returned', 'details': {}})
        store.update_incident(incident['id'], status='resolved', resolution_kind='mitigated', resolved_at=now(),
                              evidence={'initial': observation(), 'before_actions': [observation()], 'recovery': observation(True)})
    return store


def test_transfer_preserves_selected_history_and_removes_other_service(tmp_path):
    source, target = tmp_path/'source.sqlite3', tmp_path/'target.sqlite3'
    store = populate(source)
    try:
        summary = export_service(source, 'selected', target)
        assert summary['counts'] == {'services': 1, 'incidents': 1, 'actions': 1, 'observations': 1, 'telemetry': 1}
        with StoreContext(target) as copied:
            assert copied.list_services()[0]['enabled'] is False
            assert copied.list_incidents()[0]['service_id'] == 'selected'
            assert copied.incident_actions(copied.list_incidents()[0]['id'])[0]['status'] == 'completed'
        assert len(store.list_services()) == 2
        assert len(store.list_incidents()) == 2
        with sqlite3.connect(target) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not db.execute('SELECT 1 FROM events WHERE service_id != ?', ('selected',)).fetchone()
    finally:
        store.close()


class StoreContext:
    def __init__(self, path): self.store = Store(path)
    def __enter__(self): return self.store
    def __exit__(self, *args): self.store.close()


@pytest.mark.parametrize('changes', [{'enabled': True}, {'auto_actions': ['restart_service']}])
def test_transfer_refuses_active_authority(tmp_path, changes):
    with StoreContext(tmp_path/'source.sqlite3') as store:
        store.add_service({**config(), **changes}, 'selected')
        with pytest.raises(ValueError, match='Pause'):
            export_service(tmp_path/'source.sqlite3', 'selected', tmp_path/'out.sqlite3')
        assert not (tmp_path/'out.sqlite3').exists()


def test_transfer_refuses_running_action_and_existing_destination(tmp_path):
    with StoreContext(tmp_path/'source.sqlite3') as store:
        service = store.add_service(config(), 'selected')
        incident = store.create_incident(service, observation())
        store.begin_action(incident, 'restart_service')
        with pytest.raises(ValueError, match='In-flight'):
            export_service(tmp_path/'source.sqlite3', 'selected', tmp_path/'out.sqlite3')
        (tmp_path/'out.sqlite3').write_text('existing data')
        with pytest.raises(FileExistsError):
            export_service(tmp_path/'source.sqlite3', 'selected', tmp_path/'out.sqlite3')
        assert (tmp_path/'out.sqlite3').read_text() == 'existing data'


def test_import_changes_only_connection_and_authority_not_incident_evidence(tmp_path):
    with StoreContext(tmp_path/'state.sqlite3') as store:
        service = store.add_service(config(), 'selected')
        store.record_observation('selected', observation())
        incident = store.create_incident(service, observation())
        store.update_incident(incident['id'], status='escalated')
        before = store.get_incident(incident['id'])
    activate_import(tmp_path/'state.sqlite3', 'selected', 'new-private-token', auto_restart=True)
    with StoreContext(tmp_path/'state.sqlite3') as store:
        service = store.get_service('selected')
        assert service['target'] == 'http://127.0.0.1:9876'
        assert service['agent_token'] == 'new-private-token'
        assert service['enabled'] and service['auto_actions'] == ['restart_service']
        assert service['latest'] is None and service['consecutive_failures'] == 0
        assert store.get_incident(incident['id']) == before
    with pytest.raises(ValueError, match='paused'):
        activate_import(tmp_path/'state.sqlite3', 'selected', 'again', auto_restart=True)


def test_report_uses_persisted_action_and_excludes_raw_context(tmp_path):
    store = populate(tmp_path/'state.sqlite3')
    try:
        incident = store.list_incidents()[0]
        report = incident_report(incident, store.incident_actions(incident['id']), store.events(incident['id']))
        assert 'restart returned' in report and 'completed' in report
        assert 'private-raw-log' not in report and 'omit-this' not in report
        assert '<script>' not in report and '&lt;script&gt;' in report
        assert '[link](https://evil.test)' not in report
        assert '本系统动作成功返回' in report
        incident.update(status='escalated', resolved_at=None)
        assert '尚未确认业务恢复' in incident_report(incident, [], [])
        incident.update(status='resolved', resolution_kind='externally_recovered')
        assert '不能归因于本系统动作' in incident_report(incident, [], [])
    finally:
        store.close()


def test_report_endpoint_requires_controller_auth(tmp_path):
    token = 'private-controller-token-long-enough'
    with TestClient(create_app(data_dir=tmp_path, api_token=token, schedule=False)) as client:
        store = client.app.state.store
        incident = store.create_incident(store.add_service(config(), 'selected'), observation())
        store.update_incident(incident['id'], status='escalated')
        endpoint = '/api/incidents/' + incident['id'] + '/report'
        assert client.get(endpoint).status_code == 401
        response = client.get(endpoint, headers={'Authorization': 'Bearer ' + token})
        assert response.status_code == 200 and '尚未确认业务恢复' in response.json()['markdown']
        assert 'private-host-token' not in response.text
        assert client.get('/api/incidents/absent/report', headers={'Authorization': 'Bearer ' + token}).status_code == 404


def test_systemd_controller_is_unprivileged_and_persistent():
    unit = controller_unit(PurePosixPath('/opt/opssentinel/.venv/bin/python'))
    assert 'User=opssentinel' in unit and 'Restart=on-failure' in unit
    assert '--no-demo' in unit and '--host 127.0.0.1' in unit
    assert 'ProtectSystem=strict' in unit
    assert 'OPS_AGENT_TOKEN=' not in unit and 'docker.sock' not in unit


@pytest.mark.parametrize('doc', [{}, {'name':'production','services':{'api':{}}},
                                  {'name':'opssentinel-lab','services':{'api':{},'db':{}}}])
def test_drill_rejects_nonlab_targets(doc):
    with pytest.raises(ValueError): check_lab_document(doc)
