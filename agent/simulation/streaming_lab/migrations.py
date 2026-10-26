"""Checksum-verified, serialized migrations for the isolated stream schema only."""
from hashlib import sha256
import os
from pathlib import Path


def migrate(db):
    directory = Path(os.environ.get('LAB_MIGRATIONS', '/lab-migrations'))
    paths = sorted(directory.glob('*.sql'))
    if not paths:
        raise RuntimeError('Streaming lab migrations are missing')
    with db.transaction():
        db.execute("SELECT pg_advisory_xact_lock(480002)")
        db.execute('CREATE TABLE IF NOT EXISTS stream.schema_migrations (version text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT clock_timestamp())')
        for path in paths:
            source = path.read_bytes()
            digest = sha256(source).hexdigest()
            existing = db.execute('SELECT sha256 FROM stream.schema_migrations WHERE version=%s', (path.name,)).fetchone()
            if existing:
                if existing['sha256'] != digest:
                    raise RuntimeError('Applied streaming migration checksum changed: '+path.name)
                continue
            db.execute(source.decode('utf-8'))
            db.execute('INSERT INTO stream.schema_migrations(version,sha256) VALUES(%s,%s)', (path.name,digest))
