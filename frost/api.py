from __future__ import annotations

import hmac
import os

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, Response

from frost.domain import Fault, require_fields, text
from frost.service import Service


def create_app(database=None, fetcher=None, clock=None, admin_key=None):
    key = admin_key if admin_key is not None else os.environ.get("FROST_ADMIN_KEY")
    if not key or len(key) < 16:
        raise ValueError("FROST_ADMIN_KEY至少16字符")
    service = Service(database or os.environ.get("FROST_DATABASE", "state/frost.sqlite3"), fetcher, clock)
    app = FastAPI(title="霜讯通告采集", version="1.0.0")
    app.state.service = service

    def authorized(x_frost_key: str | None = Header(default=None)):
        if x_frost_key is None or not hmac.compare_digest(x_frost_key.encode(), key.encode()):
            raise Fault("unauthorized", "管理密钥不匹配", 401)

    protected = [Depends(authorized)]

    @app.exception_handler(Fault)
    async def fault_handler(request: Request, fault: Fault):
        return JSONResponse(status_code=fault.status, content={"error": fault.as_dict()})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/sources", dependencies=protected, status_code=201)
    def add_source(value: dict):
        return service.sources.register(value, service.clock())

    @app.get("/sources", dependencies=protected)
    def sources(after: str = "", limit: int = Query(default=100, ge=1, le=500)):
        rows = service.ledger.rows("SELECT id FROM sources WHERE id>? ORDER BY id LIMIT ?", (after, limit))
        return {"items": [service.sources.get(row["id"]) for row in rows]}

    @app.get("/sources/{identifier}", dependencies=protected)
    def source(identifier: str):
        return service.sources.get(identifier)

    @app.put("/sources/{identifier}/enabled", dependencies=protected)
    def enable(identifier: str, value: dict):
        require_fields(value, {"enabled"}, {"enabled"})
        return service.sources.set_enabled(identifier, value["enabled"], service.clock())

    @app.post("/sources/{identifier}/collect", dependencies=protected)
    def collect(identifier: str):
        return service.collect(identifier)

    @app.post("/scheduler/run", dependencies=protected)
    def schedule(limit: int = Query(default=10, ge=1, le=100)):
        return service.run_due(limit)

    @app.get("/sources/{identifier}/runs", dependencies=protected)
    def runs(identifier: str, after: str = "", limit: int = Query(default=100, ge=1, le=500)):
        return {"items": service.runs(identifier, after, limit)}

    @app.get("/runs/{identifier}", dependencies=protected)
    def run(identifier: str):
        return service.run(identifier)

    @app.post("/catalog/refresh", dependencies=protected)
    def refresh():
        return {"changed": service.catalog.refresh(service.clock())}

    @app.get("/bulletins", dependencies=protected)
    def bulletins(after: str = "", limit: int = Query(default=100, ge=1, le=500), state: str | None = None):
        return {"items": service.catalog.list(after, limit, state)}

    @app.get("/bulletins/{identifier}", dependencies=protected)
    def bulletin(identifier: str):
        return service.catalog.get(identifier)

    @app.get("/bulletins/{identifier}/evidence", dependencies=protected)
    def evidence(identifier: str):
        return {"items": service.catalog.evidence(identifier)}

    @app.get("/captures/{identifier}", dependencies=protected)
    def capture(identifier: str):
        record = service.ledger.capture(identifier)
        return Response(content=record["body"], media_type="application/octet-stream",
                        headers={"X-Content-SHA256": record["body_sha"], "X-Content-Type-Options": "nosniff"})

    @app.get("/changes", dependencies=protected)
    def changes(after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
        return {"items": service.catalog.changes(after, limit)}

    @app.post("/subscriptions", dependencies=protected, status_code=201)
    def subscribe(value: dict):
        return service.delivery.register(value, service.clock())

    @app.get("/subscriptions/{identifier}", dependencies=protected)
    def subscription(identifier: str):
        return service.delivery.subscription(identifier)

    @app.get("/subscriptions/{identifier}/status", dependencies=protected)
    def delivery_status(identifier: str):
        return service.delivery.status(identifier)

    @app.post("/subscriptions/{identifier}/claim", dependencies=protected)
    def claim(identifier: str, limit: int = Query(default=20, ge=1, le=100), lease: int = Query(default=30, ge=1, le=300)):
        return {"items": service.delivery.claim(identifier, service.clock(), limit, lease)}

    @app.post("/subscriptions/{identifier}/deliveries/{delivery_id}/ack", dependencies=protected)
    def ack(identifier: str, delivery_id: str, value: dict):
        require_fields(value, {"token"}, {"token"})
        return service.delivery.ack(identifier, delivery_id, text(value["token"], "租约令牌"), service.clock())

    @app.post("/subscriptions/{identifier}/deliveries/{delivery_id}/nack", dependencies=protected)
    def nack(identifier: str, delivery_id: str, value: dict):
        require_fields(value, {"token", "delay"}, {"token"})
        return service.delivery.nack(identifier, delivery_id, text(value["token"], "租约令牌"),
                                     service.clock(), value.get("delay", 10))

    @app.get("/audit/{identifier}", dependencies=protected)
    def audit(identifier: str, after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
        return {"items": service.ledger.history(identifier, after, limit)}

    @app.get("/operations/stats", dependencies=protected)
    def stats():
        return service.stats()

    @app.get("/runs/{identifier}/bundle", dependencies=protected)
    def bundle(identifier: str):
        from frost.evidence import Evidence
        return Response(content=Evidence(service.ledger).bundle(identifier), media_type="application/zip")

    @app.get("/captures/{identifier}/metadata", dependencies=protected)
    def capture_metadata(identifier: str):
        from frost.evidence import Evidence
        return Evidence(service.ledger).describe(identifier)

    @app.get("/operations/verify", dependencies=protected)
    def verify():
        return service.verify()

    @app.get("/sources/{identifier}/health", dependencies=protected)
    def source_health(identifier: str):
        from frost.operations import Operations
        return Operations(service).source_health(identifier)

    @app.get("/bulletins/{identifier}/explanation", dependencies=protected)
    def explanation(identifier: str):
        from frost.operations import Operations
        return Operations(service).explain(identifier)

    @app.get("/sources/{identifier}/versions/{external_id}", dependencies=protected)
    def versions(identifier: str, external_id: str, after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
        from frost.operations import Operations
        return {"items": Operations(service).versions(identifier, external_id, after, limit)}

    @app.get("/runs/{identifier}/coverage", dependencies=protected)
    def coverage(identifier: str):
        from frost.operations import Operations
        return Operations(service).coverage(identifier)

    @app.get("/operations/evidence-integrity", dependencies=protected)
    def evidence_integrity(after: str = "", limit: int = Query(default=100, ge=1, le=500)):
        from frost.operations import Operations
        return Operations(service).verify_evidence(after, limit)

    return app
