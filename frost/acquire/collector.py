from __future__ import annotations

from frost.acquire.http import FetchFailure, retry_delay
from frost.bulletins.extract import parse_page, semantic
from frost.domain import Fault, compact, digest, same_origin
from frost.ledger import uid


class Collector:
    def __init__(self, ledger, scheduler, catalog, fetcher, clock):
        self.ledger = ledger
        self.scheduler = scheduler
        self.catalog = catalog
        self.fetcher = fetcher
        self.clock = clock

    def step(self, identifier: str | None = None):
        source = self.scheduler.claim(self.clock(), identifier)
        if source is None:
            return {"state": "idle"}
        try:
            return self.collect(source)
        except Fault as fault:
            if fault.code == "stale_lease":
                return {"state": "abandoned", "run_id": source["run_id"], "error": fault.as_dict()}
            try:
                self.scheduler.fail(source, fault, self.clock())
            except Fault as lease_fault:
                if lease_fault.code != "stale_lease":
                    raise
            return {"state": "failed", "run_id": source["run_id"], "error": fault.as_dict()}

    def capture(self, source, response):
        identifier = uid()
        allowed_headers = {key: value for key, value in response.headers.items()
                           if key in {"content-type", "etag", "last-modified", "retry-after", "location"}}
        with self.ledger.transaction() as connection:
            self.scheduler.check_lease(connection, source, self.clock())
            connection.execute(
                "INSERT INTO captures VALUES(?,?,?,?,?,?,?,?)",
                (identifier, source["run_id"], response.url, response.status, compact(allowed_headers),
                 response.body, digest(response.body), self.clock()),
            )
        return identifier

    def collect(self, source):
        visited = set()
        records = {}
        snapshot = None
        total = None
        url = source["url"]
        root_headers = {}
        while url:
            self.scheduler.heartbeat(source, self.clock())
            if url in visited:
                raise Fault("pagination_cycle", "来源分页出现循环")
            if len(visited) >= source["max_pages"]:
                raise Fault("page_limit", "来源轮次超过分页预算")
            headers = {}
            if not visited and source["last_success"] is not None:
                if source["etag"]:
                    headers["If-None-Match"] = source["etag"]
                if source["modified"]:
                    headers["If-Modified-Since"] = source["modified"]
            response = self.fetcher.get(url, headers, source["max_bytes"])
            same_origin(source["url"], response.url)
            if len(response.body) > source["max_bytes"]:
                raise Fault("body_limit", "来源响应超过字节预算")
            capture_id = self.capture(source, response)
            if response.status == 304:
                if visited or source["last_success"] is None:
                    raise Fault("unexpected_not_modified", "缺少完整基线或分页返回304")
                return self.commit(source, [], None, 1, {}, unchanged=True)
            if response.status in {429, 503}:
                raise FetchFailure("source_throttled", "来源要求稍后重试", retry_delay(response.headers.get("retry-after"), self.clock()))
            if response.status != 200:
                raise FetchFailure("http_status", f"来源返回HTTP {response.status}")
            page = parse_page(response.body, source["format"], response.headers.get("content-type", ""))
            if snapshot is None:
                snapshot, total = page.snapshot, page.total
                root_headers = response.headers
            elif page.snapshot != snapshot or page.total != total:
                raise Fault("snapshot_changed", "分页过程中来源快照或总数改变")
            visited.add(url)
            for document in page.items:
                identifier = document["id"]
                if identifier in records:
                    if semantic(records[identifier][0]) != semantic(document):
                        raise Fault("duplicate_conflict", "同轮分页重复编号内容不一致")
                else:
                    records[identifier] = (document, capture_id)
            if len(records) > total:
                raise Fault("coverage_excess", "抓取编号数量超过来源声明总数")
            url = same_origin(response.url, page.next_url) if page.next_url else None
        if len(records) != total:
            raise Fault("incomplete_coverage", "来源末页未覆盖声明的全部通告")
        return self.commit(source, list(records.values()), snapshot, len(visited), root_headers)

    def commit(self, source, records, snapshot, pages, headers, unchanged=False):
        now = self.clock()
        with self.ledger.transaction() as connection:
            self.scheduler.check_lease(connection, source, now)
            connection.execute(
                "UPDATE sources SET last_success=?,last_error='',failures=0,next_due=?,lease_token=NULL,lease_until=NULL "
                "WHERE id=?", (now, now + source["interval"], source["id"]),
            )
            if not unchanged:
                connection.execute("UPDATE sources SET etag=?,modified=? WHERE id=?",
                                   (headers.get("etag"), headers.get("last-modified"), source["id"]))
                affected = self.catalog.ingest(connection, source, source["run_id"], records, now)
            else:
                affected = 0
                for row in connection.execute("SELECT DISTINCT canonical_id FROM observations WHERE source_id=?", (source["id"],)).fetchall():
                    affected += int(self.catalog.recompute(connection, row[0], now))
            state = "unchanged" if unchanged else "complete"
            connection.execute("UPDATE runs SET state=?,finished=?,snapshot=?,pages=?,records=? WHERE id=?",
                               (state, now, snapshot, pages, len(records), source["run_id"]))
            self.ledger.audit(connection, now, "collection_" + state, source["id"],
                              {"run_id": source["run_id"], "pages": pages, "records": len(records)})
        return {"state": state, "run_id": source["run_id"], "pages": pages, "records": len(records), "affected": affected}
