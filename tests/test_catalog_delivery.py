import unittest

from frost.domain import Fault
from frost.operations import Operations
from frost.service import Service
from tests.support import Harness, bulletin


class CatalogDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.h.source()
        self.h.subscribe()
        self.service = self.h.service

    def test_repeated_collection_updates_evidence_not_notification(self):
        self.h.collect([bulletin()])
        self.h.clock.advance(5)
        self.h.collect([bulletin()])
        self.assertEqual(self.service.catalog.get("notice-a")["version"], 1)
        self.assertEqual(self.service.delivery.status("district")["pending"], 1)
        self.assertEqual(self.service.stats()["captures"], 2)
        self.assertEqual(self.service.stats()["versions"], 1)

    def test_revision_conflict_rolls_back_entire_round(self):
        self.h.collect([bulletin()])
        self.h.clock.advance(5)
        result = self.h.collect([bulletin("new"), bulletin(body="改了内容但未升版本")])
        self.assertEqual(result["error"]["code"], "revision_conflict")
        self.assertEqual(self.service.stats()["versions"], 1)
        self.assertEqual(self.service.stats()["observations"], 1)
        self.assertEqual(self.service.stats()["captures"], 2)
        self.assertEqual(len(self.service.catalog.list()), 1)

    def test_older_revision_reappearing_after_removal_is_rejected(self):
        self.h.collect([bulletin(revision=2)])
        self.h.clock.advance(5)
        self.h.collect([])
        self.h.clock.advance(5)
        result = self.h.collect([bulletin(revision=1)])
        self.assertEqual(result["error"]["code"], "revision_regression")
        self.assertEqual(self.service.catalog.get("notice-a")["state"], "unavailable")

    def test_source_priority_conflict_and_recovery(self):
        self.h.collect([bulletin()])
        self.h.source("mirror", priority=20)
        self.h.collect([bulletin(body="镜像确认的最新内容")], "mirror")
        current = self.service.catalog.get("notice-a")
        self.assertEqual(current["winner_source"], "mirror")
        self.assertEqual(current["document"]["body"], "镜像确认的最新内容")
        self.h.source("peer", priority=20)
        self.h.collect([bulletin(body="同优先级不同事实")], "peer")
        self.assertEqual(self.service.catalog.get("notice-a")["state"], "conflict")
        self.service.sources.set_enabled("peer", False, self.h.clock())
        self.assertEqual(self.service.catalog.get("notice-a")["state"], "active")
        self.assertEqual(self.service.catalog.get("notice-a")["winner_source"], "mirror")

    def test_equal_content_from_two_sources_does_not_conflict(self):
        self.h.collect([bulletin()])
        self.h.source("mirror")
        self.h.collect([bulletin("mirror-id", canonical_id="notice-a", revision=7)], "mirror")
        self.assertEqual(self.service.catalog.get("notice-a")["state"], "active")
        self.assertEqual(self.service.catalog.get("notice-a")["version"], 1)
        self.assertEqual(len(self.service.catalog.evidence("notice-a")), 2)

    def test_region_change_withdraws_old_audience(self):
        self.h.subscribe("south-team", regions=["south"])
        self.h.collect([bulletin()])
        self.h.clock.advance(5)
        self.h.collect([bulletin(revision=2, regions=["south"], issued="2026-11-19T08:00:00+08:00")])
        messages = self.service.delivery.claim("south-team", self.h.clock())
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"]["version"], 2)
        first = self.service.delivery.claim("district", self.h.clock())[0]
        self.assertEqual(first["payload"]["kind"], "upsert")
        self.service.delivery.ack("district", first["id"], first["token"], self.h.clock())
        second = self.service.delivery.claim("district", self.h.clock())[0]
        self.assertEqual(second["payload"]["kind"], "withdraw")
        self.assertEqual(second["payload"]["state"], "active")

    def test_removed_bulletin_withdraws_only_after_complete_round(self):
        self.h.collect([bulletin()])
        self.h.clock.advance(5)
        self.h.collect([])
        self.assertEqual(self.service.catalog.get("notice-a")["state"], "unavailable")
        changes = self.service.catalog.changes()
        self.assertEqual([row["kind"] for row in changes], ["upsert", "withdraw"])
        self.assertEqual(self.service.stats()["versions"], 1)
        self.assertEqual(self.service.delivery.status("district")["pending"], 2)

    def test_staleness_and_conditional_response_restore_visibility(self):
        self.h.collect([bulletin()])
        self.h.clock.advance(300)
        self.assertEqual(self.service.catalog.refresh(self.h.clock()), 1)
        self.assertEqual(self.service.catalog.get("notice-a")["state"], "unavailable")
        self.h.fetcher.put("https://example.test/official", status=304, body=b"")
        self.assertEqual(self.service.collect("official")["state"], "unchanged")
        self.assertEqual(self.service.catalog.get("notice-a")["state"], "active")
        self.assertEqual(self.service.catalog.get("notice-a")["version"], 3)

    def test_cancellation_is_authoritative_over_lower_priority(self):
        self.h.collect([bulletin()])
        self.h.source("cancel", priority=30)
        self.h.collect([bulletin(state="cancelled")], "cancel")
        self.assertEqual(self.service.catalog.get("notice-a")["state"], "cancelled")
        self.assertEqual(self.service.catalog.changes()[-1]["kind"], "withdraw")
        self.assertEqual(Operations(self.service).explain("notice-a")["candidates"][0]["selected"], True)

    def test_late_subscriber_receives_current_only(self):
        self.h.collect([bulletin()])
        self.h.clock.advance(5)
        self.h.collect([bulletin(revision=2, body="第二次修订")])
        self.h.subscribe("late")
        messages = self.service.delivery.claim("late", self.h.clock())
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"]["version"], 2)

    def test_expired_delivery_reclaims_and_old_ack_is_rejected(self):
        self.h.collect([bulletin()])
        first = self.service.delivery.claim("district", self.h.clock(), lease=1)[0]
        self.assertEqual(self.service.delivery.claim("district", self.h.clock()), [])
        self.h.clock.advance(1)
        second = self.service.delivery.claim("district", self.h.clock())[0]
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["token"], second["token"])
        self.assertEqual(second["attempt"], 2)
        with self.assertRaises(Fault):
            self.service.delivery.ack("district", first["id"], first["token"], self.h.clock())
        result = self.service.delivery.ack("district", second["id"], second["token"], self.h.clock())
        self.assertFalse(result["repeated"])
        repeated = self.service.delivery.ack("district", second["id"], second["token"], self.h.clock())
        self.assertTrue(repeated["repeated"])

    def test_nack_and_restart_preserve_retry(self):
        self.h.collect([bulletin()])
        first = self.service.delivery.claim("district", self.h.clock())[0]
        self.service.delivery.nack("district", first["id"], first["token"], self.h.clock(), delay=10)
        restarted = Service(self.h.path, self.h.fetcher, self.h.clock)
        self.assertEqual(restarted.delivery.claim("district", self.h.clock()), [])
        self.h.clock.advance(10)
        message = restarted.delivery.claim("district", self.h.clock())[0]
        self.assertEqual(message["id"], first["id"])
        self.assertEqual(message["attempt"], 2)
        self.assertTrue(restarted.verify()["ok"])

    def test_threshold_and_facility_filter(self):
        self.h.subscribe("red-only", min_level="red")
        self.h.subscribe("power-team", facilities=["power"])
        self.h.collect([bulletin()])
        self.assertEqual(self.service.delivery.status("red-only")["pending"], 0)
        self.assertEqual(self.service.delivery.status("power-team")["pending"], 0)
        self.h.clock.advance(5)
        self.h.collect([bulletin(revision=2, level="red", facilities=["power"])])
        self.assertEqual(self.service.delivery.status("red-only")["pending"], 0)
        self.assertEqual(self.service.delivery.status("power-team")["pending"], 1)

    def test_transaction_rolls_back_on_downstream_failure(self):
        original = self.service.delivery.changed
        def fail(*args):
            raise RuntimeError("模拟进程事务中断")
        self.service.delivery.changed = fail
        with self.assertRaises(RuntimeError):
            self.h.collect([bulletin()])
        self.service.delivery.changed = original
        self.assertEqual(self.service.stats()["observations"], 0)
        self.assertEqual(self.service.stats()["bulletins"], 0)
        self.assertEqual(self.service.stats()["deliveries"], 0)
        self.assertEqual(self.service.stats()["captures"], 1)
        self.h.clock.advance(60)
        self.assertEqual(self.h.collect([bulletin()])["state"], "complete")
