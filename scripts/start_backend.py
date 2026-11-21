"""Local Windows launcher; keeps driver settings out of persistent configuration."""

import argparse
import os
from pathlib import Path
import subprocess
import sys


DRIVER_CHECK = """
import sys
try:
    import psycopg
    assert psycopg.pq.__impl__ == 'python'
    print('Psycopg Python implementation loaded; libpq version:', psycopg.pq.version())
except Exception:
    print('Driver preflight failed: verify the official PostgreSQL client installation.', file=sys.stderr)
    sys.exit(1)
"""

DATABASE_CHECK = """
import json
import sys
try:
    from app.db import get_conn
    with get_conn() as connection:
        with connection.transaction():
            connection.execute('SET TRANSACTION READ ONLY')
            result = connection.execute('SELECT 1').fetchone()
        print(json.dumps({'database_check': 'passed' if list(result.values()) == [1] else 'failed',
                          'tls_in_use': connection.info.ssl_in_use}))
except Exception as error:
    message = str(error).lower()
    state = getattr(error, 'sqlstate', None) or ''
    if state.startswith('28') or 'authentication failed' in message or 'tenant or user not found' in message:
        category = 'authentication failure'
    elif 'ssl' in message or 'tls' in message or 'certificate' in message:
        category = 'SSL/TLS failure'
    elif any(term in message for term in ('10013', 'permission denied', 'forbidden by its access permissions')):
        category = 'network access denied by the local execution environment'
    elif any(term in message for term in ('translate host', 'getaddrinfo', 'connection refused', 'timeout', 'timed out')):
        category = 'DNS/network failure'
    else:
        category = 'local configuration or driver failure'
    print(json.dumps({'database_check': 'failed', 'category': category}), file=sys.stderr)
    sys.exit(1)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres-bin', type=Path, default=Path('C:/Program Files/PostgreSQL/19/bin'))
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--no-reload', action='store_true')
    parser.add_argument('--check-db', action='store_true', help='Run only a sanitized, read-only SELECT 1 check.')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535.')

    backend = Path(__file__).resolve().parents[1] / 'backend'
    python = backend / '.venv' / 'Scripts' / 'python.exe'
    client = args.postgres_bin.resolve()
    if not python.is_file():
        parser.error('Missing backend/.venv. Install backend/requirements.txt there first.')
    if not (client / 'libpq.dll').is_file():
        parser.error('Official client libpq.dll not found; supply --postgres-bin.')
    if not (backend / '.env').is_file():
        parser.error('Missing backend/.env. Configure it locally first.')

    # A child-only copy: neither this process nor the system environment is changed.
    child_env = os.environ.copy()
    child_env['PATH'] = str(client) + os.pathsep + child_env.get('PATH', '')
    child_env['PSYCOPG_IMPL'] = 'python'
    result = subprocess.run([str(python), '-B', '-c', DRIVER_CHECK], cwd=backend, env=child_env)
    if result.returncode:
        return result.returncode

    if args.check_db:
        child_env['PGCONNECT_TIMEOUT'] = '10'
        child_env['PGOPTIONS'] = '-c default_transaction_read_only=on -c statement_timeout=5000'
        child_env['PGSSLMODE'] = 'require'
        command = [str(python), '-B', '-c', DATABASE_CHECK]
    else:
        command = [str(python), '-B', '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(args.port)]
        if not args.no_reload:
            command += ['--reload', '--reload-dir', 'app']
    try:
        return subprocess.run(command, cwd=backend, env=child_env).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
