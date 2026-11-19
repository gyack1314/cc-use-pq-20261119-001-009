from __future__ import annotations

import json

from frost.bulletins.extract import semantic
from frost.domain import Fault, compact, digest


class Catalog:
    def __init__(self, ledger, delivery):
        self.ledger = ledger
        self.delivery = delivery

    def ingest(self, connection, source, run_id: str, records: list[tuple[dict, str]], now: int):
        affected = set()
        for document, capture_id in records:
            external_id = document["id"]
            old = connection.execute(
                "SELECT * FROM observations WHERE source_id=? AND external_id=?", (source["id"], external_id),
            ).fetchone()
            fingerprint = semantic(document)
            latest = connection.execute(
                "SELECT revision,document FROM versions WHERE source_id=? AND external_id=? ORDER BY revision DESC LIMIT 1",
                (source["id"], external_id),
            ).fetchone()
            if latest and document["revision"] < latest["revision"]:
                raise Fault("revision_regression", "来源回退至已处理的旧修订")
            if latest and json.loads(latest["document"])["canonical_id"] != document["canonical_id"]:
                raise Fault("identity_conflict", "来源通告编号变更了归并身份")
            historical = connection.execute(
                "SELECT * FROM versions WHERE source_id=? AND external_id=? AND revision=?",
                (source["id"], external_id, document["revision"]),
            ).fetchone()
            if historical and historical["semantic_sha"] != fingerprint:
                raise Fault("revision_conflict", "同一通告修订号出现不同内容")
            if old and old["canonical_id"] != document["canonical_id"]:
                raise Fault("identity_conflict", "来源通告编号变更了归并身份")
            if old and document["revision"] < old["revision"]:
                raise Fault("revision_regression", "来源回退至已处理的旧修订")
            connection.execute(
                "INSERT INTO versions VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (source["id"], external_id, document["revision"], fingerprint, compact(document), capture_id, run_id),
            )
            connection.execute(
                "INSERT INTO observations VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source_id,external_id) DO UPDATE SET revision=excluded.revision,"
                "document=excluded.document,semantic_sha=excluded.semantic_sha,"
                "capture_id=excluded.capture_id,seen_run=excluded.seen_run",
                (source["id"], external_id, document["canonical_id"], document["revision"] ,
                 compact(document), fingerprint, capture_id, run_id),
            )
            affected.add(document["canonical_id"])
        removed = connection.execute(
            "SELECT canonical_id FROM observations WHERE source_id=? AND seen_run!=?", (source["id"], run_id),
        ).fetchall()
        affected.update(row["canonical_id"] for row in removed)
        connection.execute("DELETE FROM observations WHERE source_id=? AND seen_run!=?", (source["id"], run_id))
        for canonical_id in sorted(affected):
            self.recompute(connection, canonical_id, now)
        return len(affected)

    def recompute(self, connection, canonical_id: str, now: int):
        candidates = connection.execute(
            "SELECT o.*,s.priority,s.last_success,s.stale_after,s.enabled FROM observations o "
            "JOIN sources s ON s.id=o.source_id WHERE o.canonical_id=? AND s.enabled=1 "
            "AND s.last_success IS NOT NULL AND s.last_success+s.stale_after>?",
            (canonical_id, now),
        ).fetchall()
        previous = connection.execute("SELECT * FROM bulletins WHERE id=?", (canonical_id,)).fetchone()
        eligible = []
        for row in candidates:
            document = json.loads(row["document"])
            if document["expires"] > now:
                eligible.append((row, document))
        winner = None
        state = "unavailable"
        if eligible:
            best_priority = max(row["priority"] for row, _ in eligible)
            top = [(row, document) for row, document in eligible if row["priority"] == best_priority]
            newest = max(document["issued"] for _, document in top)
            top = [(row, document) for row, document in top if document["issued"] == newest]
            contents = {self.public_sha(document) for _, document in top}
            if len(contents) > 1:
                state = "conflict"
            else:
                winner = min(top, key=lambda pair: (pair[0]["source_id"], pair[0]["external_id"]))
                state = winner[1]["state"]
        document = self.public_document(winner[1]) if winner else None
        fingerprint = digest({"state": state, "document": document})
        if previous and previous["semantic_sha"] == fingerprint:
            connection.execute(
                "UPDATE bulletins SET winner_source=?,winner_external=? WHERE id=?",
                (winner[0]["source_id"] if winner else None, winner[0]["external_id"] if winner else None, canonical_id),
            )
            return False
        if not previous and not eligible:
            return False
        version = previous["version"] + 1 if previous else 1
        encoded = compact(document) if document is not None else None
        connection.execute(
            "INSERT INTO bulletins VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "version=excluded.version,state=excluded.state,winner_source=excluded.winner_source,"
            "winner_external=excluded.winner_external,document=excluded.document,"
            "semantic_sha=excluded.semantic_sha,updated=excluded.updated",
            (canonical_id, version, state, winner[0]["source_id"] if winner else None,
             winner[0]["external_id"] if winner else None, encoded, fingerprint, now),
        )
        kind = "upsert" if state == "active" else "withdraw"
        connection.execute(
            "INSERT INTO changes(bulletin_id,version,kind,document,created) VALUES(?,?,?,?,?)",
            (canonical_id, version, kind, encoded, now),
        )
        bulletin = connection.execute("SELECT * FROM bulletins WHERE id=?", (canonical_id,)).fetchone()
        self.delivery.changed(connection, bulletin, now)
        self.ledger.audit(connection, now, "bulletin_changed", canonical_id, {"version": version, "state": state})
        return True

    def public_document(self, document: dict):
        return {key: value for key, value in document.items() if key not in {"id", "revision"}}

    def public_sha(self, document: dict):
        return digest(self.public_document(document))

    def refresh(self, now: int):
        with self.ledger.transaction() as connection:
            identifiers = {row[0] for row in connection.execute("SELECT DISTINCT canonical_id FROM observations")}
            identifiers.update(row[0] for row in connection.execute("SELECT id FROM bulletins"))
            return sum(self.recompute(connection, identifier, now) for identifier in sorted(identifiers))

    def get(self, identifier):
        row = self.ledger.one("SELECT * FROM bulletins WHERE id=?", (identifier,))
        if not row:
            raise Fault("not_found", "通告不存在", 404)
        row["document"] = json.loads(row["document"]) if row["document"] else None
        return row

    def list(self, after: str = "", limit: int = 100, state: str | None = None):
        if state and state not in {"active", "cancelled", "conflict", "unavailable"}:
            raise Fault("invalid_state", "通告筛选状态无效", 422)
        query = "SELECT * FROM bulletins WHERE id>?"
        parameters = [after]
        if state:
            query += " AND state=?"
            parameters.append(state)
        query += " ORDER BY id LIMIT ?"
        parameters.append(limit)
        rows = self.ledger.rows(query, parameters)
        for row in rows:
            row["document"] = json.loads(row["document"]) if row["document"] else None
        return rows

    def changes(self, after: int = 0, limit: int = 100):
        rows = self.ledger.rows("SELECT * FROM changes WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit))
        for row in rows:
            row["document"] = json.loads(row["document"]) if row["document"] else None
        return rows

    def evidence(self, identifier: str):
        self.get(identifier)
        return self.ledger.rows(
            "SELECT o.source_id,o.external_id,o.revision,o.capture_id,o.seen_run,c.url,c.body_sha,c.fetched,"
            "s.authority,s.priority,s.last_success,s.last_error FROM observations o "
            "JOIN captures c ON c.id=o.capture_id JOIN sources s ON s.id=o.source_id "
            "WHERE o.canonical_id=? ORDER BY s.priority DESC,o.source_id,o.external_id", (identifier,),
        )
