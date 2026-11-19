import io
import json
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from frost.api import create_app
from frost.bulletins.extract import normalize, parse_page
from frost.domain import Fault, canonical_url, compact, digest, same_origin, timestamp
from frost.evidence import Evidence
from frost.worker import Worker
from tests.support import Harness, bulletin, page


class FormatsTests(unittest.TestCase):
    def test_json_charset_and_duplicate_keys(self):
        value = page([bulletin()])
        parsed = parse_page(compact(value).encode("gb18030"), "json", "application/json; charset=gb18030")
        self.assertEqual(parsed.items[0]["title"], value["items"][0]["title"])
        with self.assertRaises(Fault) as error:
            parse_page(b'{"snapshot":"a","snapshot":"b"}', "json", "application/json")
        self.assertEqual(error.exception.code, "duplicate_key")
        with self.assertRaises(Fault):
            parse_page(b'\xff', "json", "application/json")
        with self.assertRaises(Fault):
            parse_page(b'{}', "json", "text/html")

    def test_html_extracts_only_article_fields(self):
        body = '''<html><meta name="snapshot" content="s1"><meta name="total" content="1">
        <nav>不要采集导航文字</nav><article data-id="x" data-canonical_id="c" data-revision="1"
        data-issued="2026-11-18T00:00:00Z" data-expires="2026-12-20T00:00:00Z"
        data-state="active" data-level="red" data-regions="north,south" data-facilities="heat">
        <h2 data-field="title">线路<b>检修</b></h2><p data-field="body">合成通告内容</p></article>
        <a rel="next" href="/p2">下一页</a></html>'''.encode()
        parsed = parse_page(body, "html", "text/html; charset=utf-8")
        self.assertEqual(parsed.snapshot, "s1")
        self.assertEqual(parsed.total, 1)
        self.assertEqual(parsed.items[0]["title"], "线路检修")
        self.assertEqual(parsed.items[0]["regions"], ["north", "south"])
        self.assertEqual(parsed.next_url, "/p2")
        self.assertNotIn("导航", compact(parsed.items))

    def test_html_repeated_metadata_and_unclosed_article_fail(self):
        with self.assertRaises(Fault) as repeated:
            parse_page(b'<meta name="snapshot" content="a"><meta name="snapshot" content="b">', "html", "text/html")
        self.assertEqual(repeated.exception.code, "ambiguous_html")
        with self.assertRaises(Fault) as unclosed:
            parse_page(b'<article data-id="x">', "html", "text/html")
        self.assertEqual(unclosed.exception.code, "malformed_html")

    def test_atom_mapping_and_entity_rejection(self):
        body = '''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:f="urn:frost:bulletins:1">
        <f:snapshot>s1</f:snapshot><f:total>1</f:total><entry><id>x</id><f:canonical-id>c</f:canonical-id>
        <f:revision>1</f:revision><title>合成电网通告</title><content type="text">合成检修区域</content>
        <updated>2026-11-18T00:00:00Z</updated><f:expires>2026-12-20T00:00:00Z</f:expires>
        <f:state>active</f:state><f:level>yellow</f:level><category scheme="urn:frost:region" term="north"/>
        <category scheme="urn:frost:facility" term="power"/></entry></feed>'''.encode()
        parsed = parse_page(body, "atom", "application/atom+xml")
        self.assertEqual(parsed.items[0]["canonical_id"], "c")
        self.assertEqual(parsed.items[0]["facilities"], ["power"])
        with self.assertRaises(Fault) as unsafe:
            parse_page(b'<!DOCTYPE x [<!ENTITY e "x">]><feed/>', "atom", "application/atom+xml")
        self.assertEqual(unsafe.exception.code, "unsafe_xml")

    def test_field_validation_preserves_business_boundaries(self):
        for override in ({"revision": True}, {"regions": []}, {"facilities": ["unknown"]},
                         {"level": "purple"}, {"state": "draft"}, {"expires": "2020-01-01T00:00:00Z"}):
            with self.subTest(override=override):
                with self.assertRaises(Fault):
                    normalize(bulletin(**override))
        with self.assertRaises(Fault):
            timestamp("2026-11-18T10:00:00")
        self.assertEqual(timestamp("2026-11-18T08:00:00+08:00"), timestamp("2026-11-18T00:00:00Z"))

    def test_url_normalization_and_authorized_origin(self):
        self.assertEqual(canonical_url("https://EXAMPLE.test:443"), "https://example.test/")
        self.assertEqual(same_origin("https://example.test/a/b", "../p2"), "https://example.test/p2")
        for url in ("file:///tmp/a", "https://user:secret@example.test/", "https://example.test/#x"):
            with self.assertRaises(Fault):
                canonical_url(url)
        with self.assertRaises(Fault):
            same_origin("https://example.test/", "http://example.test/")


class EvidenceAPITests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.app = create_app(self.h.path, self.h.fetcher, self.h.clock, "local-synthetic-key-123")
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {"X-Frost-Key": "local-synthetic-key-123"}

    def post(self, path, value=None):
        return self.client.post(path, json=value, headers=self.headers)

    def get(self, path):
        return self.client.get(path, headers=self.headers)

    def prepare(self):
        source = {"id": "official", "url": "https://example.test/official", "format": "json", "authority": "合成部门"}
        self.assertEqual(self.post("/sources", source).status_code, 201)
        subscription = {"id": "district", "regions": ["north"], "facilities": ["heat"]}
        self.assertEqual(self.post("/subscriptions", subscription).status_code, 201)
        self.h.fetcher.put(source["url"], page([bulletin()]))
        result = self.post("/sources/official/collect")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["state"], "complete")
        return result.json()["run_id"]

    def test_authentication_validation_and_not_found(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/sources").status_code, 401)
        self.assertEqual(self.get("/sources/missing").status_code, 404)
        self.assertEqual(self.post("/sources", {"id": "x"}).status_code, 422)
        self.assertEqual(self.get("/changes?limit=0").status_code, 422)
        with self.assertRaises(ValueError):
            create_app(self.h.path, admin_key="short")

    def test_api_collect_claim_ack_and_raw_evidence(self):
        run_id = self.prepare()
        bulletin_result = self.get("/bulletins/notice-a").json()
        self.assertEqual(bulletin_result["state"], "active")
        evidence = self.get("/bulletins/notice-a/evidence").json()["items"][0]
        raw = self.get("/captures/" + evidence["capture_id"])
        self.assertEqual(digest(raw.content), raw.headers["x-content-sha256"])
        message = self.post("/subscriptions/district/claim").json()["items"][0]
        ack_path = "/subscriptions/district/deliveries/" + message["id"] + "/ack"
        ack = self.post(ack_path, {"token": message["token"]})
        self.assertEqual(ack.json()["state"], "acked")
        self.assertEqual(self.get("/subscriptions/district/status").json()["acked"], 1)
        self.assertTrue(self.get("/operations/verify").json()["ok"])
        self.assertEqual(self.get("/runs/" + run_id).json()["records"], 1)

    def test_export_is_deterministic_and_tamper_detected(self):
        run_id = self.prepare()
        evidence = Evidence(self.h.service.ledger)
        first = evidence.bundle(run_id)
        second = evidence.bundle(run_id)
        self.assertEqual(first, second)
        self.assertEqual(evidence.verify_bundle(first)["captures"], 1)
        changed = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(first)) as original, zipfile.ZipFile(changed, "w") as target:
            for name in original.namelist():
                raw = original.read(name)
                target.writestr(name, b"tampered" if name.startswith("bodies/") else raw)
        with self.assertRaises(Fault) as error:
            evidence.verify_bundle(changed.getvalue())
        self.assertEqual(error.exception.code, "evidence_corruption")
        self.assertEqual(self.get("/runs/" + run_id + "/bundle").content, first)

    def test_operations_trace_business_state(self):
        run_id = self.prepare()
        health = self.get("/sources/official/health").json()
        self.assertEqual(health["state"], "fresh")
        self.assertFalse(health["lease_active"])
        coverage = self.get("/runs/" + run_id + "/coverage").json()
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["observed_count"], 1)
        explanation = self.get("/bulletins/notice-a/explanation").json()
        self.assertTrue(explanation["candidates"][0]["selected"])
        self.assertTrue(self.get("/operations/evidence-integrity").json()["ok"])
        versions = self.get("/sources/official/versions/notice-a").json()["items"]
        self.assertEqual(versions[0]["revision"], 1)
        self.assertEqual(versions[0]["seen_run"], run_id)
        audit = self.get("/audit/official").json()["items"]
        self.assertEqual(audit[0]["category"], "source_created")

    def test_worker_run_once_and_stop(self):
        self.h.source()
        self.h.fetcher.put("https://example.test/official", page([bulletin()]))
        output = []
        worker = Worker(self.h.service, output=output.append)
        totals = worker.run(once=True)
        self.assertEqual(totals["complete"], 1)
        self.assertEqual(len(output), 1)
        self.assertEqual(json.loads(output[0])["state"], "complete")
        worker.stop()
        self.assertEqual(worker.run(once=True)["cycles"], 0)

    def test_parallel_message_claim_has_one_owner(self):
        self.prepare()
        service = self.app.state.service
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: service.delivery.claim("district", self.h.clock()), range(4)))
        self.assertEqual(sum(len(result) for result in results), 1)
        self.assertEqual(service.delivery.status("district")["leased"], 1)
