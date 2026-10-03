"""Export one paused service and its history as a private SQLite handoff snapshot."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3


def export_service(source: Path, sid: str, destination: Path) -> dict:
    source, destination = Path(source).resolve(), Path(destination).absolute()
    if not source.is_file() or source == destination:
        raise ValueError('An existing source and a distinct new destination are required')
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(destination)) as snapshot:
                original.backup(snapshot)
        with closing(sqlite3.connect(destination)) as snapshot, snapshot:
            row = snapshot.execute('SELECT config FROM services WHERE id=?', (sid,)).fetchone()
            if row is None:
                raise ValueError('Unknown service')
            config = json.loads(row[0])
            if config.get('connector') != 'agent' or config.get('enabled', True) or config.get('auto_actions'):
                raise ValueError('Pause the source agent service and clear auto_actions before transfer')
            running = snapshot.execute("SELECT 1 FROM actions a JOIN incidents i ON i.id=a.incident_id WHERE i.service_id=? AND a.status='running'", (sid,)).fetchone()
            busy = snapshot.execute("SELECT 1 FROM incidents WHERE service_id=? AND status IN ('investigating','remediating')", (sid,)).fetchone()
            if running or busy:
                raise ValueError('In-flight work must finish before transfer')
            snapshot.execute('PRAGMA secure_delete=ON')
            snapshot.execute('DELETE FROM actions WHERE incident_id NOT IN (SELECT id FROM incidents WHERE service_id=?)', (sid,))
            for table in ('events', 'observations', 'telemetry', 'resource_states', 'resource_alerts', 'incidents'):
                snapshot.execute(f'DELETE FROM {table} WHERE service_id IS NULL OR service_id != ?', (sid,))
            snapshot.execute('DELETE FROM services WHERE id != ?', (sid,))
            snapshot.commit()
            snapshot.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            snapshot.execute('PRAGMA journal_mode=DELETE')
            snapshot.execute('VACUUM')
            assert snapshot.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            summary = {table: snapshot.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                       for table in ('services', 'incidents', 'actions', 'observations', 'telemetry')}
        return {'service_id': sid, 'counts': summary, 'source_remains_paused': True}
    except Exception:
        destination.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(destination) + suffix).unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--service-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export_service(args.source, args.service_id, args.output)))


if __name__ == '__main__':
    main()
