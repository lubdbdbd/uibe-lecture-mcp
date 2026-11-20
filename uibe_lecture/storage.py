from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

TERMINAL = {"confirmed", "already_enrolled", "full", "closed", "ineligible", "conflict", "cancelled", "unconfirmed", "failed", "dry_run", "interaction_required"}


def data_dir() -> Path:
    p = Path(os.environ.get("UIBE_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
    p.mkdir(parents=True, exist_ok=True)
    return p


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / "state.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS objects(kind TEXT, id TEXT, body TEXT, PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS events(n INTEGER PRIMARY KEY, at REAL, job TEXT, event TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS leases(id TEXT PRIMARY KEY, owner TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS gate(id INTEGER PRIMARY KEY CHECK(id=1), next_at REAL);
            INSERT OR IGNORE INTO gate VALUES(1,0);
            CREATE TABLE IF NOT EXISTS request_state(id INTEGER PRIMARY KEY CHECK(id=1), last_at REAL, cooldown_until REAL);
            INSERT OR IGNORE INTO request_state VALUES(1,0,0);
            ''')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def put(self, kind, ident, body):
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO objects VALUES(?,?,?)", (kind,ident,json.dumps(body,ensure_ascii=False)))

    def get(self, kind, ident):
        with self.connect() as c:
            row = c.execute("SELECT body FROM objects WHERE kind=? AND id=?", (kind,ident)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind):
        with self.connect() as c:
            rows = c.execute("SELECT body FROM objects WHERE kind=? ORDER BY rowid", (kind,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def event(self, job, event, **body):
        # Callers pass summaries only. No form fields, cookies, or full HTML.
        with self.connect() as c:
            c.execute("INSERT INTO events(at,job,event,body) VALUES(?,?,?,?)", (time.time(),job,event,json.dumps(body,ensure_ascii=False)))

    def events(self, job=None, limit=50):
        with self.connect() as c:
            sql = "SELECT * FROM events" + (" WHERE job=?" if job else "") + " ORDER BY n DESC LIMIT ?"
            rows = c.execute(sql, (job,min(200,limit)) if job else (min(200,limit),)).fetchall()
        return [dict(r, body=json.loads(r["body"])) for r in rows]

    def lease(self, ident, owner, ttl=30):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            r=c.execute("SELECT owner,expires FROM leases WHERE id=?",(ident,)).fetchone()
            if r and r["owner"] != owner and r["expires"] > time.time():
                return False
            c.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?)",(ident,owner,time.time()+ttl))
        return True

    def release(self, ident, owner):
        with self.connect() as c:
            c.execute("DELETE FROM leases WHERE id=? AND owner=?",(ident,owner))

    @contextmanager
    def locked(self, ident, ttl=120):
        owner = uuid.uuid4().hex
        if not self.lease(ident,owner,ttl):
            raise RuntimeError("另一操作正在处理此任务，请稍后重试")
        try:
            yield
        finally:
            self.release(ident,owner)

    def reserve_slot(self, interval):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            slot = max(time.time(), c.execute("SELECT next_at FROM gate WHERE id=1").fetchone()[0])
            c.execute("UPDATE gate SET next_at=? WHERE id=1",(slot+interval,))
        return max(0,slot-time.time())

    def defer_requests(self, seconds):
        with self.connect() as c:
            until=time.time()+seconds
            c.execute("UPDATE gate SET next_at=MAX(next_at,?) WHERE id=1",(until,))
            c.execute("UPDATE request_state SET cooldown_until=MAX(cooldown_until,?) WHERE id=1",(until,))

    def claim_request_slot(self, interval, critical=False):
        """Claim only an immediately available slot, without reserving future polls."""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT gate.next_at,request_state.last_at,request_state.cooldown_until FROM gate JOIN request_state ON gate.id=request_state.id WHERE gate.id=1").fetchone()
            current=time.time()
            available=max(row["cooldown_until"], row["last_at"]+0.05 if critical else row["next_at"])
            if available>current:
                return available-current
            c.execute("UPDATE gate SET next_at=MAX(next_at,?) WHERE id=1",(current+interval,))
            c.execute("UPDATE request_state SET last_at=? WHERE id=1",(current,))
            return 0

