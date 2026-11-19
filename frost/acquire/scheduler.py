from __future__ import annotations

from frost.domain import Fault, bounded_int, canonical_url, compact, require_fields, text
from frost.ledger import uid


class Scheduler:
    def __init__(self, ledger, catalog):
        self.ledger = ledger
        self.catalog = catalog

    def register(self, value: dict, now: int):
        keys = {"id", "url", "format", "authority", "priority", "interval", "stale_after", "max_pages", "max_bytes"}
        require_fields(value, keys, {"id", "url", "format", "authority"})
        identifier = text(value["id"], "来源编号", 100)
        url = canonical_url(value["url"])
        authority = text(value["authority"], "发布机构", 200)
        format = value["format"]
        if format not in {"json", "html", "atom"}:
            raise Fault("invalid_format", "仅支持JSON、HTML和Atom来源", 422)
        priority = bounded_int(value.get("priority", 10), "权威优先级", 0, 100)
        interval = bounded_int(value.get("interval", 60), "采集周期", 5, 86400)
        stale_after = bounded_int(value.get("stale_after", 3600), "可信时长", interval, 604800)
        pages = bounded_int(value.get("max_pages", 30), "分页上限", 1, 100)
        size = bounded_int(value.get("max_bytes", 1048576), "单页字节预算", 128, 4194304)
        with self.ledger.transaction() as connection:
            if connection.execute("SELECT 1 FROM sources WHERE id=? OR url=?", (identifier, url)).fetchone():
                raise Fault("source_exists", "来源编号或地址已存在")
            connection.execute(
                "INSERT INTO sources(id,url,format,authority,priority,interval,stale_after,max_pages,max_bytes,next_due) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (identifier, url, format, authority, priority, interval, stale_after, pages, size, now),
            )
            self.ledger.audit(connection, now, "source_created", identifier, value)
        return self.get(identifier)

    def get(self, identifier):
        row = self.ledger.one("SELECT * FROM sources WHERE id=?", (identifier,))
        if not row:
            raise Fault("not_found", "来源不存在", 404)
        row.pop("lease_token", None)
        return row

    def set_enabled(self, identifier: str, enabled: bool, now: int):
        if type(enabled) is not bool:
            raise Fault("invalid_enabled", "启用值必须是布尔值", 422)
        with self.ledger.transaction() as connection:
            row = connection.execute("SELECT * FROM sources WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise Fault("not_found", "来源不存在", 404)
            if bool(row["enabled"]) == enabled:
                return self.get(identifier)
            connection.execute(
                "UPDATE sources SET enabled=?,generation=generation+1,lease_token=NULL,lease_until=NULL,next_due=? WHERE id=?",
                (int(enabled), now, identifier),
            )
            connection.execute(
                "UPDATE runs SET state='abandoned',finished=?,error='来源启用状态改变' WHERE source_id=? AND state='running'",
                (now, identifier),
            )
            for affected in connection.execute(
                "SELECT DISTINCT canonical_id FROM observations WHERE source_id=?", (identifier,),
            ).fetchall():
                self.catalog.recompute(connection, affected[0], now)
            self.ledger.audit(connection, now, "source_enabled", identifier, {"enabled": enabled})
        return self.get(identifier)

    def claim(self, now: int, identifier: str | None = None, lease: int = 60):
        bounded_int(lease, "采集租约", 1, 300)
        with self.ledger.transaction() as connection:
            query = "SELECT * FROM sources WHERE enabled=1 AND next_due<=? AND (lease_until IS NULL OR lease_until<=?)"
            parameters = [now, now]
            if identifier:
                query += " AND id=?"
                parameters.append(identifier)
            row = connection.execute(query + " ORDER BY next_due,id LIMIT 1", parameters).fetchone()
            if not row:
                return None
            token, run_id = uid(), uid()
            connection.execute(
                "UPDATE runs SET state='abandoned',finished=?,error='租约到期' WHERE source_id=? AND state='running'",
                (now, row["id"]),
            )
            connection.execute("UPDATE sources SET lease_token=?,lease_until=? WHERE id=?", (token, now + lease, row["id"]))
            connection.execute(
                "INSERT INTO runs(id,source_id,token,state,started) VALUES(?,?,?,'running',?)",
                (run_id, row["id"], token, now),
            )
            result = dict(row)
            result.update(lease_token=token, lease_until=now + lease, run_id=run_id)
            return result

    def check_lease(self, connection, source, now: int):
        row = connection.execute("SELECT * FROM sources WHERE id=?", (source["id"],)).fetchone()
        if (not row or not row["enabled"] or row["lease_token"] != source["lease_token"]
                or row["lease_until"] <= now or row["generation"] != source["generation"]):
            raise Fault("stale_lease", "采集租约失效，旧轮次不能提交")
        return row

    def heartbeat(self, source, now: int, lease: int = 60):
        bounded_int(lease, "采集租约", 1, 300)
        with self.ledger.transaction() as connection:
            self.check_lease(connection, source, now)
            connection.execute("UPDATE sources SET lease_until=? WHERE id=?", (now + lease, source["id"]))
        source["lease_until"] = now + lease

    def fail(self, source, fault: Fault, now: int):
        with self.ledger.transaction() as connection:
            self.check_lease(connection, source, now)
            failures = source["failures"] + 1
            delay = getattr(fault, "retry_after", None)
            if delay is None:
                delay = min(3600, source["interval"] * 2 ** min(failures - 1, 6))
            connection.execute(
                "UPDATE sources SET failures=?,last_error=?,next_due=?,lease_token=NULL,lease_until=NULL WHERE id=?",
                (failures, fault.code, now + delay, source["id"]),
            )
            connection.execute(
                "UPDATE runs SET state='failed',finished=?,error=? WHERE id=?",
                (now, compact(fault.as_dict()), source["run_id"]),
            )
            self.ledger.audit(connection, now, "collection_failed", source["id"], fault.as_dict())
