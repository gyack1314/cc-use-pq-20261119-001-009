import unittest
from concurrent.futures import ThreadPoolExecutor

from frost.acquire.http import FetchFailure, retry_delay
from frost.domain import Fault
from frost.operations import Operations
from tests.support import Harness, bulletin, page


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.h.source()
        self.service = self.h.service

    def test_complete_round_creates_evidence_and_bulletin(self):
        result = self.h.collect([bulletin()])
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["records"], 1)
        self.assertEqual(self.service.catalog.get("notice-a")["state"], "active")
        evidence = self.service.catalog.evidence("notice-a")
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["seen_run"], result["run_id"])
        self.assertEqual(self.service.run(result["run_id"])["pages"], 1)
        self.assertTrue(self.service.verify()["ok"])

    def test_overlap_pages_count_distinct_identifiers(self):
        first = bulletin()
        second = bulletin("notice-b")
        self.h.fetcher.put("https://example.test/official", page([first], total=2, next="/p2"))
        self.h.fetcher.put("https://example.test/p2", page([first, second], total=2))
        result = self.service.collect("official")
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["records"], 2)
        self.assertEqual(len(self.service.catalog.list()), 2)

    def test_incomplete_round_preserves_previous_observations(self):
        self.h.collect([bulletin()])
        self.h.clock.advance(5)
        result = self.h.collect([], total=1, snapshot="s2")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"]["code"], "incomplete_coverage")
        self.assertEqual(self.service.catalog.get("notice-a")["version"], 1)
        self.assertEqual(self.service.stats()["observations"], 1)
        self.assertEqual(self.service.stats()["captures"], 2)

    def test_changed_snapshot_never_commits_first_page(self):
        self.h.fetcher.put("https://example.test/official", page([bulletin()], total=2, next="/p2"))
        self.h.fetcher.put("https://example.test/p2", page([bulletin("b")], snapshot="s2", total=2))
        result = self.service.collect("official")
        self.assertEqual(result["error"]["code"], "snapshot_changed")
        self.assertEqual(self.service.stats()["observations"], 0)
        self.assertEqual(self.service.stats()["captures"], 2)
        self.assertEqual(self.service.stats()["versions"], 0)

    def test_pagination_cycle_and_foreign_origin_fail(self):
        self.h.fetcher.put("https://example.test/official", page([], next="/official"))
        cycle = self.service.collect("official")
        self.assertEqual(cycle["error"]["code"], "pagination_cycle")
        self.h.clock.advance(5)
        self.h.fetcher.put("https://example.test/official", page([], next="https://foreign.test/x"))
        foreign = self.service.collect("official")
        self.assertEqual(foreign["error"]["code"], "foreign_link")
        self.assertEqual(len(self.h.fetcher.requests), 2)

    def test_conflicting_duplicate_fails_before_catalog(self):
        self.h.fetcher.put("https://example.test/official", page([bulletin(), bulletin(body="不同内容")], total=1))
        result = self.service.collect("official")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"]["code"], "duplicate_conflict")
        self.assertEqual(self.service.stats()["bulletins"], 0)

    def test_http_304_revalidates_without_duplicate_change(self):
        self.h.fetcher.put("https://example.test/official", page([bulletin()]), headers={"etag": '"a"'})
        self.service.collect("official")
        self.h.clock.advance(5)
        self.h.fetcher.put("https://example.test/official", status=304, body=b"")
        result = self.service.collect("official")
        self.assertEqual(result["state"], "unchanged")
        self.assertEqual(self.h.fetcher.requests[-1][1]["If-None-Match"], '"a"')
        self.assertEqual(len(self.service.catalog.changes()), 1)
        self.assertEqual(self.service.sources.get("official")["last_success"], self.h.clock())

    def test_first_304_is_not_success(self):
        self.h.fetcher.put("https://example.test/official", status=304, body=b"")
        result = self.service.collect("official")
        self.assertEqual(result["error"]["code"], "unexpected_not_modified")
        self.assertIsNone(self.service.sources.get("official")["last_success"])

    def test_retry_after_and_recovery(self):
        self.h.fetcher.put("https://example.test/official", status=429, body=b"", headers={"retry-after": "90"})
        result = self.service.collect("official")
        self.assertEqual(result["error"]["code"], "source_throttled")
        self.assertEqual(self.service.sources.get("official")["next_due"], self.h.clock() + 90)
        self.assertEqual(self.service.collect("official")["state"], "idle")
        self.h.clock.advance(90)
        recovered = self.h.collect([bulletin()])
        self.assertEqual(recovered["state"], "complete")
        self.assertEqual(self.service.sources.get("official")["failures"], 0)
        self.assertEqual(self.service.sources.get("official")["last_error"], "")

    def test_lease_is_exclusive_under_concurrency(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            claims = list(pool.map(lambda _: self.service.sources.claim(self.h.clock()), range(5)))
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        self.assertEqual(self.service.stats()["runs"], 1)
        self.assertEqual(self.service.collect("official")["state"], "idle")

    def test_expired_worker_cannot_write_after_new_claim(self):
        old = self.service.sources.claim(self.h.clock(), lease=1)
        self.h.clock.advance(1)
        new = self.service.sources.claim(self.h.clock())
        self.assertNotEqual(old["lease_token"], new["lease_token"])
        with self.assertRaises(Fault) as error:
            self.service.collector.commit(old, [], "s", 1, {})
        self.assertEqual(error.exception.code, "stale_lease")
        self.assertEqual(self.service.run(old["run_id"])["state"], "abandoned")
        self.assertEqual(self.service.run(new["run_id"])["state"], "running")

    def test_disabled_source_fences_running_worker(self):
        claim = self.service.sources.claim(self.h.clock())
        self.service.sources.set_enabled("official", False, self.h.clock())
        with self.assertRaises(Fault):
            self.service.sources.heartbeat(claim, self.h.clock())
        self.assertEqual(self.service.run(claim["run_id"])["state"], "abandoned")
        self.assertIsNone(self.service.sources.claim(self.h.clock()))

    def test_transport_error_does_not_destroy_catalog(self):
        self.h.collect([bulletin()])
        self.h.clock.advance(5)
        self.h.fetcher.routes["https://example.test/official"] = FetchFailure("transport_failure", "连接失败")
        result = self.service.collect("official")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.service.catalog.get("notice-a")["state"], "active")
        self.assertEqual(Operations(self.service).source_health("official")["state"], "degraded")

    def test_retry_delay_date_and_invalid_value(self):
        self.assertEqual(retry_delay("0", self.h.clock()), 1)
        self.assertEqual(retry_delay("9999999", self.h.clock()), 86400)
        self.assertIsNone(retry_delay("not-a-date", self.h.clock()))
        self.assertIsNone(retry_delay(None, self.h.clock()))
        self.assertEqual(retry_delay("Wed, 18 Nov 2026 00:00:01 GMT", self.h.clock()), 1)
