from __future__ import annotations

import json
import time

from frost.acquire.collector import Collector
from frost.acquire.http import HttpFetcher
from frost.acquire.scheduler import Scheduler
from frost.bulletins.catalog import Catalog
from frost.domain import Fault, bounded_int
from frost.ledger import Ledger
from frost.relay.delivery import Delivery


class Service:
    def __init__(self, database, fetcher=None, clock=None):
        self.clock = clock or (lambda: int(time.time()))
        self.ledger = Ledger(database)
        self.delivery = Delivery(self.ledger)
        self.catalog = Catalog(self.ledger, self.delivery)
        self.sources = Scheduler(self.ledger, self.catalog)
        self.collector = Collector(self.ledger, self.sources, self.catalog, fetcher or HttpFetcher(), self.clock)

    def collect(self, source_id=None):
        if source_id:
            self.sources.get(source_id)
        result = self.collector.step(source_id)
        result["refreshed"] = self.catalog.refresh(self.clock())
        return result

    def run_due(self, limit=10):
        bounded_int(limit, "调度数量", 1, 100)
        results = []
        for _ in range(limit):
            result = self.collector.step()
            if result["state"] == "idle":
                break
            results.append(result)
        refreshed = self.catalog.refresh(self.clock())
        return {"runs": results, "refreshed": refreshed}

    def runs(self, source_id, after: str = "", limit: int = 100):
        self.sources.get(source_id)
        bounded_int(limit, "轮次数量", 1, 500)
        rows = self.ledger.rows(
            "SELECT id,source_id,state,started,finished,error,snapshot,pages,records FROM runs "
            "WHERE source_id=? AND id>? ORDER BY id LIMIT ?", (source_id, after, limit),
        )
        return rows

    def run(self, run_id):
        row = self.ledger.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not row:
            raise Fault("not_found", "采集轮次不存在", 404)
        row.pop("token", None)
        row["captures"] = self.ledger.rows(
            "SELECT id,url,status,body_sha,fetched,length(body) AS bytes FROM captures WHERE run_id=? ORDER BY fetched,id",
            (run_id,),
        )
        return row

    def stats(self):
        with self.ledger.reader() as connection:
            result = {}
            for key in ("sources", "runs", "captures", "observations", "versions", "bulletins", "deliveries"):
                result[key] = connection.execute("SELECT count(*) FROM " + key).fetchone()[0]
            result["failed_sources"] = connection.execute("SELECT count(*) FROM sources WHERE failures>0").fetchone()[0]
            result["conflicts"] = connection.execute("SELECT count(*) FROM bulletins WHERE state='conflict'").fetchone()[0]
            result["pending_deliveries"] = connection.execute("SELECT count(*) FROM deliveries WHERE state!='acked'").fetchone()[0]
            result["time"] = self.clock()
            return result

    def verify(self):
        issues = []
        with self.ledger.reader() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                issues.append(integrity)
            for row in connection.execute("PRAGMA foreign_key_check"):
                issues.append("引用缺失：" + str(tuple(row)))
            for row in connection.execute(
                "SELECT b.id,b.version,MAX(c.version) AS change_version FROM bulletins b "
                "LEFT JOIN changes c ON c.bulletin_id=b.id GROUP BY b.id"
            ):
                if row["version"] != row["change_version"]:
                    issues.append("变更流版本不一致：" + row["id"])
            for row in connection.execute("SELECT id,payload,bulletin_version FROM deliveries"):
                payload = json.loads(row["payload"])
                if payload["version"] != row["bulletin_version"]:
                    issues.append("交付内容版本不一致：" + row["id"])
        return {"ok": not issues, "issues": issues}
