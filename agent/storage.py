from __future__ import annotations
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class Storage:
    def __init__(self, directory: str, recover: bool = True):
        self.path = Path(directory) / 'localpilot.sqlite3'
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute('PRAGMA busy_timeout=15000')
        self.path.chmod(0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, at REAL, tool TEXT, details TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, request_id TEXT UNIQUE, fingerprint TEXT, snapshot TEXT)')
        # A restart cannot reconstruct a live subprocess handle; never retry old work.
        for job_id, body in self.db.execute('SELECT id,snapshot FROM jobs').fetchall():
            snapshot = json.loads(body)
            if recover and not snapshot.get('detached') and snapshot['status'] in ('running', 'starting'):
                snapshot.update(status='interrupted', finished_at=time.time())
                self.db.execute('UPDATE jobs SET snapshot=? WHERE id=?', (json.dumps(snapshot), job_id))
        self.db.commit()

    def event(self, tool: str, details: dict) -> str:
        receipt = uuid.uuid4().hex
        with self.lock:
            self.db.execute('INSERT INTO events VALUES (?,?,?,?)', (receipt, time.time(), tool, json.dumps(details, ensure_ascii=False)))
            self.db.commit()
        return receipt

    def job(self, snapshot: dict, request_id: str | None = None, fingerprint: str = ''):
        with self.lock:
            self.db.execute('INSERT INTO jobs VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot',
                            (snapshot['job_id'], request_id, fingerprint, json.dumps(snapshot, ensure_ascii=False)))
            self.db.commit()

    def get_job(self, job_id: str) -> dict | None:
        with self.lock:
            result = self.db.execute('SELECT snapshot FROM jobs WHERE id=?', (job_id,)).fetchone()
        return json.loads(result[0]) if result else None

    def find_request(self, request_id: str) -> tuple | None:
        with self.lock:
            result = self.db.execute('SELECT fingerprint,snapshot FROM jobs WHERE request_id=?', (request_id,)).fetchone()
        return (result[0], json.loads(result[1])) if result else None
