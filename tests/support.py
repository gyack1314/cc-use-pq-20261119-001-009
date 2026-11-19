import copy
import json
import tempfile
from pathlib import Path

from frost.acquire.http import Response
from frost.domain import compact
from frost.service import Service


NOW = 1795046400


def bulletin(identifier="notice-a", **overrides):
    value = {
        "id": identifier, "canonical_id": identifier, "revision": 1,
        "title": "合成供暖线路检修通告", "body": "合成演练数据：北区供暖线路临时检修。",
        "issued": "2026-11-18T08:00:00+08:00", "expires": "2026-12-20T08:00:00+08:00",
        "state": "active", "level": "orange", "regions": ["north"], "facilities": ["heat"],
    }
    value.update(overrides)
    return value


def page(items, snapshot="s1", total=None, next=None):
    return {"snapshot": snapshot, "total": len(items) if total is None else total, "items": items, "next": next}


class Clock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        return self.value

    def advance(self, seconds=60):
        self.value += seconds


class Fetcher:
    def __init__(self):
        self.routes = {}
        self.requests = []

    def put(self, url, value=None, status=200, headers=None, body=None):
        self.routes[url] = Response(url, status, {"content-type": "application/json; charset=utf-8", **(headers or {})},
                                    body if body is not None else compact(value).encode())

    def get(self, url, headers, max_bytes):
        self.requests.append((url, dict(headers), max_bytes))
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(url, headers, max_bytes)
        return value


class Harness:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "frost.sqlite3"
        self.clock = Clock()
        self.fetcher = Fetcher()
        self.service = Service(self.path, self.fetcher, self.clock)

    def close(self):
        self.directory.cleanup()

    def source(self, identifier="official", **overrides):
        value = {"id": identifier, "url": "https://example.test/" + identifier,
                 "format": "json", "authority": "合成市保障部门", "interval": 5, "stale_after": 300}
        value.update(overrides)
        return self.service.sources.register(value, self.clock())

    def subscribe(self, identifier="district", **overrides):
        value = {"id": identifier, "regions": ["north"], "facilities": ["heat"]}
        value.update(overrides)
        return self.service.delivery.register(value, self.clock())

    def collect(self, items, identifier="official", **page_options):
        source = self.service.sources.get(identifier)
        self.fetcher.put(source["url"], page(items, **page_options))
        return self.service.collect(identifier)
