import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, directory: Path):
        self.path = directory / "app.sqlite"
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                    request_hash TEXT NOT NULL, request_json TEXT NOT NULL,
                    status TEXT NOT NULL, result_json TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL,
                    type TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    incident_id TEXT PRIMARY KEY, id TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
                    body TEXT NOT NULL, approval_note TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def create(self, key: str, request: dict):
        raw = json.dumps(request, sort_keys=True)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM incidents WHERE request_key=?", (key,)).fetchone()
            if row:
                if row["request_hash"] != digest:
                    raise Conflict("Idempotency-Key already belongs to another request")
                return dict(row), False
            iid = str(uuid4())
            c.execute(
                "INSERT INTO incidents VALUES (?, ?, ?, ?, 'running', NULL, NULL)",
                (iid, key, digest, raw),
            )
        return self.get(iid), True

    def get(self, iid):
        with self.connect() as c:
            row = c.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
        if row is None:
            raise KeyError(iid)
        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        saved = result.pop("result_json")
        result["result"] = json.loads(saved) if saved else None
        result.pop("request_hash")
        return result

    def finish(self, iid, status, result=None, error=None):
        with self.connect() as c:
            c.execute(
                "UPDATE incidents SET status=?, result_json=?, error=? WHERE id=?",
                (status, json.dumps(result) if result is not None else None, error, iid),
            )

    def event(self, iid, kind, payload):
        with self.connect() as c:
            c.execute(
                "INSERT INTO events(incident_id,type,payload) VALUES (?,?,?)",
                (iid, kind, json.dumps(payload)),
            )

    def events(self, iid, after=0):
        with self.connect() as c:
            return [
                {"seq": r["seq"], "type": r["type"], "data": json.loads(r["payload"])}
                for r in c.execute(
                    "SELECT * FROM events WHERE incident_id=? AND seq>? ORDER BY seq", (iid, after)
                )
            ]

    def ticket(self, iid, report, note):
        # Atomic unique incident_id makes replay after a checkpoint failure safe.
        with self.connect() as c:
            c.execute(
                "INSERT OR IGNORE INTO tickets VALUES (?,?,?,?,?)",
                (iid, "MOCK-" + iid[:8], report["summary"], json.dumps(report), note),
            )
            return dict(c.execute("SELECT * FROM tickets WHERE incident_id=?", (iid,)).fetchone())
