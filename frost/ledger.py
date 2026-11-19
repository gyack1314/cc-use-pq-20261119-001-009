from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from frost.domain import Fault, compact


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
 id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, format TEXT NOT NULL, authority TEXT NOT NULL,
 priority INTEGER NOT NULL, interval INTEGER NOT NULL, stale_after INTEGER NOT NULL,
 max_pages INTEGER NOT NULL, max_bytes INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 next_due INTEGER NOT NULL, lease_token TEXT, lease_until INTEGER, failures INTEGER NOT NULL DEFAULT 0,
 etag TEXT, modified TEXT, last_success INTEGER, last_error TEXT NOT NULL DEFAULT '',
 generation INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), token TEXT NOT NULL,
 state TEXT NOT NULL, started INTEGER NOT NULL, finished INTEGER, error TEXT,
 snapshot TEXT, pages INTEGER NOT NULL DEFAULT 0, records INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS captures(
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), url TEXT NOT NULL,
 status INTEGER NOT NULL, headers TEXT NOT NULL, body BLOB NOT NULL, body_sha TEXT NOT NULL,
 fetched INTEGER NOT NULL, UNIQUE(run_id,url)
);
CREATE TABLE IF NOT EXISTS versions(
 source_id TEXT NOT NULL REFERENCES sources(id), external_id TEXT NOT NULL,
 revision INTEGER NOT NULL, semantic_sha TEXT NOT NULL, document TEXT NOT NULL,
 capture_id TEXT NOT NULL REFERENCES captures(id), seen_run TEXT NOT NULL REFERENCES runs(id),
 PRIMARY KEY(source_id,external_id,revision)
);
CREATE TABLE IF NOT EXISTS observations(
 source_id TEXT NOT NULL REFERENCES sources(id), external_id TEXT NOT NULL,
 canonical_id TEXT NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL,
 semantic_sha TEXT NOT NULL, capture_id TEXT NOT NULL REFERENCES captures(id),
 seen_run TEXT NOT NULL REFERENCES runs(id), PRIMARY KEY(source_id,external_id)
);
CREATE INDEX IF NOT EXISTS observation_canonical ON observations(canonical_id);
CREATE TABLE IF NOT EXISTS bulletins(
 id TEXT PRIMARY KEY, version INTEGER NOT NULL, state TEXT NOT NULL, winner_source TEXT,
 winner_external TEXT, document TEXT, semantic_sha TEXT NOT NULL, updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS changes(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, bulletin_id TEXT NOT NULL,
 version INTEGER NOT NULL, kind TEXT NOT NULL, document TEXT, created INTEGER NOT NULL,
 UNIQUE(bulletin_id,version)
);
CREATE TABLE IF NOT EXISTS subscriptions(
 id TEXT PRIMARY KEY, regions TEXT NOT NULL, facilities TEXT NOT NULL, min_level TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships(
 subscription_id TEXT NOT NULL REFERENCES subscriptions(id), bulletin_id TEXT NOT NULL,
 active INTEGER NOT NULL, version INTEGER NOT NULL, PRIMARY KEY(subscription_id,bulletin_id)
);
CREATE TABLE IF NOT EXISTS deliveries(
 id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
 bulletin_id TEXT NOT NULL, bulletin_version INTEGER NOT NULL, kind TEXT NOT NULL,
 payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', available INTEGER NOT NULL,
 token TEXT, lease_until INTEGER, attempts INTEGER NOT NULL DEFAULT 0, acked INTEGER,
 UNIQUE(subscription_id,bulletin_id,bulletin_version,kind)
);
CREATE INDEX IF NOT EXISTS delivery_due ON deliveries(subscription_id,state,available);
CREATE TABLE IF NOT EXISTS audit(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, time INTEGER NOT NULL, category TEXT NOT NULL,
 entity TEXT NOT NULL, detail TEXT NOT NULL
);
"""


class Ledger:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("请使用临时文件以支持独立事务连接")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
        finally:
            connection.close()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def reader(self):
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.rollback()
            connection.close()

    def rows(self, query: str, parameters=()):
        with self.reader() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

    def one(self, query: str, parameters=()):
        with self.reader() as connection:
            row = connection.execute(query, parameters).fetchone()
            return dict(row) if row else None

    def audit(self, connection, now: int, category: str, entity: str, detail):
        connection.execute(
            "INSERT INTO audit(time,category,entity,detail) VALUES(?,?,?,?)",
            (now, category, entity, compact(detail)),
        )

    def history(self, entity: str, after: int = 0, limit: int = 100):
        rows = self.rows(
            "SELECT * FROM audit WHERE entity=? AND sequence>? ORDER BY sequence LIMIT ?",
            (entity, after, limit),
        )
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return rows

    def capture(self, capture_id: str):
        row = self.one("SELECT * FROM captures WHERE id=?", (capture_id,))
        if not row:
            raise Fault("not_found", "原始证据不存在", 404)
        row["headers"] = json.loads(row["headers"])
        return row


def uid() -> str:
    return uuid.uuid4().hex
