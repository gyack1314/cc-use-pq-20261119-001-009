from __future__ import annotations

import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import uvicorn

from frost.acquire.http import HttpFetcher
from frost.api import create_app
from frost.domain import compact, digest
from frost.evidence import Evidence


class Portal(BaseHTTPRequestHandler):
    mode = "normal"
    requests_seen = []

    def log_message(self, *args):
        return None

    def do_GET(self):
        type(self).requests_seen.append((self.path, self.headers.get("If-None-Match")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/feed")
            self.end_headers()
            return
        if self.path == "/large":
            self.send_response(200)
            self.send_header("Content-Length", "5000")
            self.end_headers()
            return
        if self.path not in {"/feed", "/page2"}:
            self.send_error(404)
            return
        if self.mode == "unchanged" and self.path == "/feed":
            self.send_response(304)
            self.end_headers()
            return
        identifier = "heat-north" if self.path == "/feed" else "road-east"
        item = {
            "id": identifier, "canonical_id": identifier, "revision": 1,
            "title": "合成冬季设施运行通告", "body": "本地合成演练内容，不代表实际灾情。",
            "issued": "2026-11-18T00:00:00Z", "expires": "2026-12-20T00:00:00Z",
            "regions": ["north"], "facilities": ["heat" if self.path == "/feed" else "road"],
            "level": "orange", "state": "active",
        }
        snapshot = "shifted" if self.mode == "shifted" and self.path == "/page2" else "winter-1"
        body = compact({"snapshot": snapshot, "total": 2, "items": [item],
                        "next": "/page2" if self.path == "/feed" else None}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("ETag", '"winter-1"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run():
    clock_value = [1795046400]
    portal = ThreadingHTTPServer(("127.0.0.1", 0), Portal)
    thread = threading.Thread(target=portal.serve_forever, daemon=True)
    thread.start()
    Portal.mode = "normal"
    Portal.requests_seen = []
    origin = "http://127.0.0.1:" + str(portal.server_port)
    server = None
    server_thread = None
    try:
        with tempfile.TemporaryDirectory() as directory:
            key = "synthetic-local-key-12345"
            app = create_app(Path(directory) / "frost.sqlite3", HttpFetcher(allow_loopback=True), lambda: clock_value[0], key)
            config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
            server = uvicorn.Server(config)
            listener = config.bind_socket()
            endpoint = "http://127.0.0.1:" + str(listener.getsockname()[1])
            server_thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
            server_thread.start()
            with httpx.Client(base_url=endpoint, headers={"X-Frost-Key": key}, timeout=10) as client:
                response = client.get("/health")
                response.raise_for_status()
                assert response.json()["status"] == "ok"
                source = {"id": "winter", "url": origin + "/feed", "format": "json", "authority": "合成保障部门", "interval": 5}
                response = client.post("/sources", json=source)
                assert response.status_code == 201, response.text
                response = client.post("/subscriptions", json={"id": "north", "regions": ["north"], "facilities": ["heat"]})
                assert response.status_code == 201, response.text
                response = client.post("/sources/winter/collect")
                assert response.json()["state"] == "complete", response.text
                run_id = response.json()["run_id"]
                assert response.json()["records"] == 2
                bulletins = client.get("/bulletins").json()["items"]
                assert len(bulletins) == 2
                messages = client.post("/subscriptions/north/claim?lease=1").json()["items"]
                assert len(messages) == 1
                first = messages[0]
                clock_value[0] += 1
                reclaimed = client.post("/subscriptions/north/claim").json()["items"][0]
                assert first["id"] == reclaimed["id"] and first["token"] != reclaimed["token"]
                path = "/subscriptions/north/deliveries/" + first["id"] + "/ack"
                assert client.post(path, json={"token": first["token"]}).status_code == 409
                assert client.post(path, json={"token": reclaimed["token"]}).status_code == 200
                bundle = client.get("/runs/" + run_id + "/bundle").content
                assert Evidence(app.state.service.ledger).verify_bundle(bundle)["captures"] == 2
                Portal.mode = "shifted"
                clock_value[0] += 5
                response = client.post("/sources/winter/collect")
                assert response.json()["error"]["code"] == "snapshot_changed", response.text
                assert len(client.get("/bulletins").json()["items"]) == 2
                assert len(client.get("/changes").json()["items"]) == 2
                Portal.mode = "unchanged"
                clock_value[0] += 5
                response = client.post("/sources/winter/collect")
                assert response.json()["state"] == "unchanged", response.text
                assert Portal.requests_seen[-1][1] == '"winter-1"'
                assert client.get("/operations/verify").json()["ok"]
                assert client.get("/operations/evidence-integrity").json()["ok"]
                result = {"bulletins": len(bulletins), "delivery_recovered": True, "partial_round_rejected": True,
                          "conditional_fetch": True, "bundle_sha256": digest(bundle), "network": "loopback"}
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                return result
    finally:
        if server:
            server.should_exit = True
        if server_thread:
            server_thread.join(5)
        portal.shutdown()
        portal.server_close()
        thread.join(5)


if __name__ == "__main__":
    run()
