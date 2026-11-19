from __future__ import annotations

import base64
import io
import json
import zipfile

from frost.domain import Fault, compact, digest


class Evidence:
    def __init__(self, ledger):
        self.ledger = ledger

    def bundle(self, run_id: str, max_bytes: int = 16777216):
        with self.ledger.reader() as connection:
            run = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise Fault("not_found", "采集轮次不存在", 404)
            size = connection.execute("SELECT COALESCE(SUM(length(body)),0) FROM captures WHERE run_id=?", (run_id,)).fetchone()[0]
            if size > max_bytes:
                raise Fault("export_limit", "本轮证据超过单次导出预算，请逐份读取", 413)
            captures = connection.execute("SELECT * FROM captures WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
            source = connection.execute("SELECT id,url,format,authority,priority FROM sources WHERE id=?", (run["source_id"],)).fetchone()
            manifest = {"schema_version": 1, "source": dict(source), "run": dict(run), "captures": []}
            manifest["run"].pop("token", None)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
                for capture in captures:
                    if digest(capture["body"]) != capture["body_sha"]:
                        raise Fault("evidence_corruption", "原始响应校验不符", 500)
                    name = "bodies/" + capture["id"] + ".bin"
                    record = {key: capture[key] for key in ("id", "url", "status", "body_sha", "fetched")}
                    record.update(path=name, bytes=len(capture["body"]), headers=json.loads(capture["headers"]))
                    manifest["captures"].append(record)
                    self._write(archive, name, capture["body"])
                self._write(archive, "manifest.json", compact(manifest).encode())
            return output.getvalue()

    def _write(self, archive, name, body):
        info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
        info.external_attr = 0o100644 << 16
        archive.writestr(info, body)

    def verify_bundle(self, body: bytes, max_bytes: int = 16777216):
        if len(body) > max_bytes + 1048576:
            raise Fault("export_limit", "证据包超过读取预算", 413)
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                if len(names) != len(set(names)) or len(names) > 102:
                    raise Fault("invalid_bundle", "证据包成员重复或过多", 422)
                if sum(info.file_size for info in infos) > max_bytes + 1048576:
                    raise Fault("export_limit", "证据包展开超过预算", 413)
                manifest = json.loads(archive.read("manifest.json"))
                if manifest.get("schema_version") != 1:
                    raise Fault("invalid_bundle", "证据包格式版本不受支持", 422)
                records = manifest.get("captures")
                if not isinstance(records, list) or len(records) > 100:
                    raise Fault("invalid_bundle", "证据包清单无效", 422)
                expected = {"manifest.json"}
                identifiers = set()
                for record in records:
                    identifier = record["id"]
                    if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
                        raise Fault("invalid_bundle", "证据编号无效", 422)
                    path = "bodies/" + identifier + ".bin"
                    if identifier in identifiers or record["path"] != path:
                        raise Fault("invalid_bundle", "证据清单路径或编号重复", 422)
                    identifiers.add(identifier)
                    expected.add(path)
                    raw = archive.read(path)
                    if len(raw) != record["bytes"] or digest(raw) != record["body_sha"]:
                        raise Fault("evidence_corruption", "证据包字节校验不符", 422)
                if expected != set(names):
                    raise Fault("invalid_bundle", "证据包含未声明成员", 422)
                return {"ok": True, "run_id": manifest["run"]["id"], "captures": len(records)}
        except (zipfile.BadZipFile, KeyError, ValueError, TypeError, RuntimeError) as exc:
            raise Fault("invalid_bundle", "证据包损坏或缺少清单", 422) from exc

    def describe(self, capture_id: str):
        row = self.ledger.capture(capture_id)
        raw = row.pop("body")
        row["bytes"] = len(raw)
        row["verified"] = digest(raw) == row["body_sha"]
        return row
