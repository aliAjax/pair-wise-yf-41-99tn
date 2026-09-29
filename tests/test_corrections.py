import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine, is_material_change, merge_correction
from src.service import DomainService


def _published_event(service, admin):
    service.create(
        admin, "station", {"code": "STA-1", "lat": 35.0, "lon": 110.0}
    )
    service.create(
        admin, "station", {"code": "STA-2", "lat": 35.1, "lon": 110.1}
    )
    event = service.create(
        admin,
        "event",
        {
            "title": "Event-A",
            "origin_time": "2026-01-01T00:00:00Z",
            "location": "Region-A",
            "magnitude": 4.2,
            "reports": [
                {"station": "STA-1", "time_offset": 2, "distance_km": 1.0},
                {"station": "STA-2", "time_offset": -1, "distance_km": 1.5},
            ],
        },
    )
    event_id = event["id"]
    service.transition(admin, event_id, "associate", {})
    service.transition(
        admin, event_id, "review", {"reviewer": "R-1", "magnitude": 4.2}
    )
    service.transition(
        admin, event_id, "publish", {"communication_id": "C-1"}
    )
    return event_id


class CorrectionRulesTest(unittest.TestCase):
    def test_material_change_detection(self):
        current = {"location": "A", "origin_time": "t0", "magnitude": 4.0}
        self.assertTrue(is_material_change(current, {"magnitude": 4.1}))
        self.assertTrue(is_material_change(current, {"location": "B"}))
        self.assertTrue(is_material_change(current, {"origin_time": "t1"}))
        self.assertFalse(is_material_change(current, {"magnitude": 4.0}))

    def test_merge_groups_by_station_and_appends_waveform(self):
        current = {"reports": [{"station": "S1", "time_offset": 1}]}
        merged, report, added = merge_correction(
            current,
            {
                "station": "S1",
                "message_id": "m1",
                "waveforms": [{"channel": "BHZ"}],
            },
        )
        self.assertTrue(added)
        self.assertEqual(len(merged["reports"]), 1)
        self.assertEqual(merged["reports"][0]["waveforms"], [{"channel": "BHZ"}])
        # 同通道重复补传不重复追加
        merged, report, added = merge_correction(
            merged,
            {"station": "S1", "waveforms": [{"channel": "BHZ", "extra": True}]},
        )
        self.assertFalse(added)
        self.assertEqual(len(merged["reports"][0]["waveforms"]), 1)


class CorrectionWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.station1 = Actor("STA-1", "station")
        self.station2 = Actor("STA-2", "station")
        self.event_id = _published_event(self.service, self.admin)

    def tearDown(self):
        self.tmp.cleanup()

    def test_waveform_supplement_keeps_publish_and_review(self):
        entity = self.service.get(self.event_id)
        version_before = entity["version"]
        updated = self.service.submit_correction(
            self.station1,
            self.event_id,
            {
                "station": "STA-1",
                "message_id": "MSG-1",
                "waveforms": [{"channel": "BHZ", "samples": 1024}],
            },
        )
        self.assertEqual(updated["status"], "published")
        self.assertEqual(updated["version"], version_before + 1)
        # 审校结论（震级/审核员）保留
        self.assertEqual(updated["data"]["magnitude"], 4.2)
        self.assertEqual(updated["data"].get("reviewer"), "R-1")
        report = next(
            r for r in updated["data"]["reports"] if r["station"] == "STA-1"
        )
        self.assertEqual(report["waveforms"][0]["channel"], "BHZ")
        revision = self.service.revisions(self.event_id)[-1]
        self.assertFalse(revision["material"])
        self.assertEqual(revision["revision_status"], "applied")

    def test_epicenter_change_withdraws_published_and_pends_review(self):
        updated = self.service.submit_correction(
            self.station2,
            self.event_id,
            {
                "station": "STA-2",
                "message_id": "MSG-2",
                "location": "Region-B",
                "reason": "relocated",
            },
        )
        self.assertEqual(updated["status"], "revision_pending")
        self.assertEqual(updated["data"]["location"], "Region-B")
        pending = self.service.revisions(status="pending_review")
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["material"])
        self.assertEqual(pending[0]["station"], "STA-2")

        # 复核通过后重新发布，待复核修订关闭
        self.service.transition(
            self.admin, self.event_id, "review",
            {"reviewer": "R-1", "magnitude": 4.3},
        )
        self.service.transition(
            self.admin, self.event_id, "publish",
            {"communication_id": "C-2"},
        )
        self.assertEqual(self.service.get(self.event_id)["status"], "published")
        self.assertEqual(self.service.revisions(status="pending_review"), [])
        self.assertEqual(
            self.service.revisions(self.event_id)[-3]["revision_status"],
            "reviewed",
        )

    def test_origin_time_change_before_publish_also_pends(self):
        event = self.service.create(
            self.admin, "event",
            {
                "title": "Event-B",
                "origin_time": "2026-02-01T00:00:00Z",
                "location": "Region-C",
                "reports": [
                    {"station": "STA-1", "time_offset": 0, "distance_km": 1.0},
                    {"station": "STA-2", "time_offset": 0, "distance_km": 1.2},
                ],
            },
        )
        self.service.transition(self.admin, event["id"], "associate", {})
        updated = self.service.submit_correction(
            self.station1, event["id"],
            {"station": "STA-1", "message_id": "MSG-9",
             "origin_time": "2026-02-01T00:00:05Z"},
        )
        self.assertEqual(updated["status"], "revision_pending")

    def test_duplicate_message_id_is_idempotent(self):
        payload = {
            "station": "STA-1",
            "message_id": "DUP-1",
            "waveforms": [{"channel": "BHZ"}],
        }
        first = self.service.submit_correction(
            self.station1, self.event_id, payload
        )
        second = self.service.submit_correction(
            self.station1, self.event_id, dict(payload)
        )
        self.assertEqual(first["version"], second["version"])
        station_revisions = [
            r for r in self.service.revisions(self.event_id)
            if r.get("message_id") == "DUP-1"
        ]
        self.assertEqual(len(station_revisions), 1)

    def test_viewer_cannot_submit_correction(self):
        with self.assertRaises(PermissionDenied):
            self.service.submit_correction(
                Actor("x", "viewer"),
                self.event_id,
                {"station": "STA-1", "magnitude": 5.0},
            )

    def test_candidate_event_cannot_be_corrected(self):
        event = self.service.create(
            self.admin, "event",
            {
                "title": "Event-C",
                "origin_time": "t0",
                "location": "X",
                "reports": [
                    {"station": "STA-1", "time_offset": 0, "distance_km": 1.0},
                    {"station": "STA-2", "time_offset": 0, "distance_km": 1.0},
                ],
            },
        )
        with self.assertRaises(InvalidTransition):
            self.service.submit_correction(
                self.station1, event["id"],
                {"station": "STA-1", "message_id": "m", "magnitude": 4.9},
            )


class ConcurrentCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.service = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.event_id = _published_event(self.service, self.admin)

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_stations_concurrent_changes_both_land(self):
        barrier = threading.Barrier(2)
        errors = []

        def worker(station, message_id, magnitude):
            try:
                barrier.wait()
                # 每个线程使用独立 service/repository，模拟两个台站进程
                service = DomainService(
                    SQLiteRepository(self.db_path), RuleEngine()
                )
                service.submit_correction(
                    Actor(station, "station"),
                    self.event_id,
                    {
                        "station": station,
                        "message_id": message_id,
                        "magnitude": magnitude,
                    },
                )
            except Exception as exc:  # pragma: no cover - 仅用于收集失败
                errors.append(repr(exc))

        threads = [
            threading.Thread(
                target=worker, args=("STA-1", "C-A", 4.5)
            ),
            threading.Thread(
                target=worker, args=("STA-2", "C-B", 4.6)
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        event = self.service.get(self.event_id)
        self.assertEqual(event["status"], "revision_pending")
        self.assertEqual(event["data"]["magnitude"], 4.6)
        reports = {r["station"] for r in event["data"]["reports"]}
        self.assertEqual(reports, {"STA-1", "STA-2"})
        station_revisions = [
            r
            for r in self.service.revisions(self.event_id)
            if r["station"] in ("STA-1", "STA-2")
        ]
        self.assertEqual(len(station_revisions), 2)
        versions = sorted(r["version"] for r in station_revisions)
        self.assertEqual(len(set(versions)), 2)

    def test_stale_expected_version_is_not_retried(self):
        with self.assertRaises(ConflictError):
            self.service.submit_correction(
                Actor("STA-1", "station"),
                self.event_id,
                {"station": "STA-1", "message_id": "C-X", "magnitude": 4.8},
                expected_version=1,
            )


class LegacyMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "legacy.db"
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE entities (
                id TEXT PRIMARY KEY, kind TEXT, status TEXT, version INTEGER,
                data TEXT, created_by TEXT, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT, actor_id TEXT,
                actor_role TEXT, action TEXT, from_status TEXT, to_status TEXT,
                detail TEXT, created_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO entities VALUES "
            "('LEGACY-1','event','published',3,"
            "'{\"title\":\"old\",\"location\":\"X\",\"magnitude\":3.1}',"
            "'admin','2025-01-01','2025-01-02')"
        )
        connection.execute(
            "INSERT INTO audit_log(entity_id,actor_id,actor_role,action,"
            "from_status,to_status,detail,created_at) VALUES "
            "('LEGACY-1','admin','admin','publish','reviewed','published',"
            "'{}','2025-01-02')"
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_event_is_queryable_revisable_and_chain_backfilled(self):
        service = DomainService(
            SQLiteRepository(self.db_path), RuleEngine()
        )
        event = service.get("LEGACY-1")
        self.assertEqual(event["status"], "published")

        revisions = service.revisions("LEGACY-1")
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["version"], 1)

        updated = service.submit_correction(
            Actor("S-OLD", "station"),
            "LEGACY-1",
            {"station": "S-OLD", "message_id": None, "magnitude": 3.4},
        )
        self.assertEqual(updated["version"], 4)
        self.assertEqual(updated["status"], "revision_pending")
        self.assertEqual(updated["data"]["magnitude"], 3.4)
        self.assertEqual(len(service.revisions("LEGACY-1")), 2)

    def test_audit_log_is_immutable(self):
        DomainService(SQLiteRepository(self.db_path), RuleEngine())
        connection = sqlite3.connect(self.db_path)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE audit_log SET action = 'x' WHERE id = 1"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM audit_log WHERE id = 1")
        connection.close()


if __name__ == "__main__":
    unittest.main()
