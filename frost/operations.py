from __future__ import annotations

import json

from frost.domain import Fault, bounded_int, digest


class Operations:
    def __init__(self, service):
        self.service = service
        self.ledger = service.ledger

    def source_health(self, identifier):
        source = self.service.sources.get(identifier)
        now = self.service.clock()
        successful = source["last_success"]
        if not source["enabled"]:
            state = "disabled"
        elif successful is None:
            state = "unobserved"
        elif successful + source["stale_after"] <= now:
            state = "stale"
        elif source["failures"]:
            state = "degraded"
        else:
            state = "fresh"
        latest = self.ledger.one(
            "SELECT id,state,started,finished,error,pages,records FROM runs WHERE source_id=? ORDER BY started DESC,rowid DESC LIMIT 1",
            (identifier,),
        )
        return {
            "source_id": identifier, "state": state, "now": now,
            "age_seconds": now - successful if successful is not None else None,
            "trusted_until": successful + source["stale_after"] if successful is not None else None,
            "next_due": source["next_due"], "last_error": source["last_error"],
            "lease_active": source["lease_until"] is not None and source["lease_until"] > now,
            "latest_run": latest,
        }

    def explain(self, identifier):
        bulletin = self.service.catalog.get(identifier)
        now = self.service.clock()
        candidates = []
        with self.ledger.reader() as connection:
            rows = connection.execute(
                "SELECT o.*,s.enabled,s.priority,s.last_success,s.stale_after,s.authority "
                "FROM observations o JOIN sources s ON s.id=o.source_id WHERE o.canonical_id=? "
                "ORDER BY s.priority DESC,o.source_id,o.external_id", (identifier,),
            ).fetchall()
            for row in rows:
                document = json.loads(row["document"])
                reason = None
                if not row["enabled"]:
                    reason = "source_disabled"
                elif row["last_success"] is None or row["last_success"] + row["stale_after"] <= now:
                    reason = "source_stale"
                elif document["expires"] <= now:
                    reason = "expired"
                candidates.append({
                    "source_id": row["source_id"], "external_id": row["external_id"],
                    "authority": row["authority"], "priority": row["priority"],
                    "revision": row["revision"], "issued": document["issued"],
                    "expires": document["expires"], "excluded_reason": reason,
                    "selected": row["source_id"] == bulletin["winner_source"] and row["external_id"] == bulletin["winner_external"],
                    "capture_id": row["capture_id"],
                })
        return {"bulletin": bulletin, "candidates": candidates, "now": now}

    def versions(self, source_id, external_id, after=0, limit=100):
        self.service.sources.get(source_id)
        bounded_int(after, "修订游标", 0, 1000000000)
        bounded_int(limit, "修订数量", 1, 500)
        rows = self.ledger.rows(
            "SELECT * FROM versions WHERE source_id=? AND external_id=? AND revision>? ORDER BY revision LIMIT ?",
            (source_id, external_id, after, limit),
        )
        for row in rows:
            row["document"] = json.loads(row["document"])
        return rows

    def coverage(self, run_id):
        run = self.service.run(run_id)
        if run["state"] not in {"complete", "unchanged"}:
            return {"run_id": run_id, "complete": False, "reason": run["state"], "captures": len(run["captures"])}
        rows = self.ledger.rows(
            "SELECT external_id,revision,capture_id,semantic_sha FROM versions WHERE seen_run=? ORDER BY external_id",
            (run_id,),
        )
        return {"run_id": run_id, "complete": True, "state": run["state"],
                "snapshot": run["snapshot"], "pages": run["pages"],
                "observed_count": run["records"], "new_versions": rows,
                "captures": run["captures"]}

    def verify_evidence(self, after: str = "", limit: int = 100):
        bounded_int(limit, "证据检查数量", 1, 500)
        rows = self.ledger.rows("SELECT id,body,body_sha FROM captures WHERE id>? ORDER BY id LIMIT ?", (after, limit))
        failures = [row["id"] for row in rows if digest(row["body"]) != row["body_sha"]]
        return {"ok": not failures, "checked": len(rows), "failures": failures,
                "next_after": rows[-1]["id"] if len(rows) == limit else None}
