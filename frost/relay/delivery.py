from __future__ import annotations

import json

from frost.domain import FACILITIES, LEVELS, Fault, bounded_int, compact, require_fields, string_set, text
from frost.ledger import uid


class Delivery:
    def __init__(self, ledger):
        self.ledger = ledger

    def matches(self, subscription, bulletin) -> bool:
        if not subscription["enabled"] or bulletin["state"] != "active":
            return False
        document = json.loads(bulletin["document"])
        regions = set(json.loads(subscription["regions"]))
        facilities = set(json.loads(subscription["facilities"]))
        return (
            bool(regions.intersection(document["regions"]))
            and bool(facilities.intersection(document["facilities"]))
            and LEVELS[document["level"]] >= LEVELS[subscription["min_level"]]
        )

    def register(self, value: dict, now: int):
        require_fields(value, {"id", "regions", "facilities", "min_level"}, {"id", "regions", "facilities"})
        identifier = text(value["id"], "订阅编号", 100)
        regions = string_set(value["regions"], "区域")
        facilities = string_set(value["facilities"], "设施", FACILITIES)
        level = value.get("min_level", "info")
        if level not in LEVELS:
            raise Fault("invalid_level", "订阅级别无效", 422)
        with self.ledger.transaction() as connection:
            if connection.execute("SELECT 1 FROM subscriptions WHERE id=?", (identifier,)).fetchone():
                raise Fault("subscription_exists", "订阅编号已存在")
            connection.execute(
                "INSERT INTO subscriptions(id,regions,facilities,min_level,created) VALUES(?,?,?,?,?)",
                (identifier, compact(regions), compact(facilities), level, now),
            )
            subscription = connection.execute("SELECT * FROM subscriptions WHERE id=?", (identifier,)).fetchone()
            for bulletin in connection.execute("SELECT * FROM bulletins WHERE state='active'").fetchall():
                self.reconcile(connection, subscription, bulletin, now)
            self.ledger.audit(connection, now, "subscription_created", identifier, value)
        return self.subscription(identifier)

    def subscription(self, identifier):
        row = self.ledger.one("SELECT * FROM subscriptions WHERE id=?", (identifier,))
        if not row:
            raise Fault("not_found", "订阅不存在", 404)
        row["regions"] = json.loads(row["regions"])
        row["facilities"] = json.loads(row["facilities"])
        return row

    def reconcile(self, connection, subscription, bulletin, now: int):
        previous = connection.execute(
            "SELECT * FROM memberships WHERE subscription_id=? AND bulletin_id=?",
            (subscription["id"], bulletin["id"]),
        ).fetchone()
        match = self.matches(subscription, bulletin)
        active_before = bool(previous and previous["active"])
        if not match and not active_before:
            return
        if previous and previous["version"] == bulletin["version"] and match == active_before:
            return
        kind = "upsert" if match else "withdraw"
        payload = {
            "bulletin_id": bulletin["id"], "version": bulletin["version"],
            "kind": kind, "state": bulletin["state"],
            "document": json.loads(bulletin["document"]) if match else None,
        }
        connection.execute(
            "INSERT INTO deliveries(id,subscription_id,bulletin_id,bulletin_version,kind,payload,available) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (uid(), subscription["id"], bulletin["id"], bulletin["version"], kind, compact(payload), now),
        )
        connection.execute(
            "INSERT INTO memberships VALUES(?,?,?,?) ON CONFLICT(subscription_id,bulletin_id) "
            "DO UPDATE SET active=excluded.active,version=excluded.version",
            (subscription["id"], bulletin["id"], int(match), bulletin["version"]),
        )

    def changed(self, connection, bulletin, now):
        for subscription in connection.execute("SELECT * FROM subscriptions").fetchall():
            self.reconcile(connection, subscription, bulletin, now)

    def claim(self, identifier: str, now: int, limit: int = 20, lease: int = 30):
        bounded_int(limit, "交付批量", 1, 100)
        bounded_int(lease, "交付租约", 1, 300)
        with self.ledger.transaction() as connection:
            subscription = connection.execute("SELECT * FROM subscriptions WHERE id=?", (identifier,)).fetchone()
            if not subscription:
                raise Fault("not_found", "订阅不存在", 404)
            if not subscription["enabled"]:
                return []
            rows = connection.execute(
                "SELECT d.* FROM deliveries d WHERE d.subscription_id=? AND d.state!='acked' "
                "AND d.available<=? AND (d.lease_until IS NULL OR d.lease_until<=?) "
                "AND NOT EXISTS (SELECT 1 FROM deliveries older WHERE older.subscription_id=d.subscription_id "
                "AND older.bulletin_id=d.bulletin_id AND older.bulletin_version<d.bulletin_version "
                "AND older.state!='acked') ORDER BY d.available,d.bulletin_version,d.id LIMIT ?",
                (identifier, now, now, limit),
            ).fetchall()
            result = []
            for row in rows:
                token = uid()
                connection.execute(
                    "UPDATE deliveries SET state='leased',token=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                    (token, now + lease, row["id"]),
                )
                result.append({"id": row["id"], "token": token, "lease_until": now + lease,
                               "attempt": row["attempts"] + 1, "payload": json.loads(row["payload"])})
            return result

    def ack(self, identifier: str, delivery_id: str, token: str, now: int):
        return self._settle(identifier, delivery_id, token, now, True, 0)

    def nack(self, identifier: str, delivery_id: str, token: str, now: int, delay: int = 10):
        bounded_int(delay, "重试延时", 1, 86400)
        return self._settle(identifier, delivery_id, token, now, False, delay)

    def _settle(self, identifier, delivery_id, token, now, success, delay):
        with self.ledger.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE id=? AND subscription_id=?", (delivery_id, identifier),
            ).fetchone()
            if not row:
                raise Fault("not_found", "交付消息不存在", 404)
            if row["state"] == "acked" and row["token"] == token and success:
                return {"state": "acked", "repeated": True}
            if row["state"] != "leased" or row["token"] != token or row["lease_until"] <= now:
                raise Fault("stale_delivery", "交付租约已失效")
            if success:
                connection.execute("UPDATE deliveries SET state='acked',acked=?,lease_until=NULL WHERE id=?", (now, delivery_id))
            else:
                connection.execute(
                    "UPDATE deliveries SET state='pending',available=?,token=NULL,lease_until=NULL WHERE id=?",
                    (now + delay, delivery_id),
                )
            state = "acked" if success else "pending"
            self.ledger.audit(connection, now, "delivery_" + state, identifier, {"delivery_id": delivery_id})
            return {"state": state, "repeated": False}

    def status(self, identifier):
        self.subscription(identifier)
        rows = self.ledger.rows(
            "SELECT state,count(*) AS count FROM deliveries WHERE subscription_id=? GROUP BY state", (identifier,),
        )
        return {state: next((row["count"] for row in rows if row["state"] == state), 0)
                for state in ("pending", "leased", "acked")}
