import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


admin = Actor("admin", "admin")
station = Actor("sta-2", "station")


class CorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.repo = SQLiteRepository(self.db_path)
        self.service = DomainService(self.repo, RuleEngine())
        self.event_id = self._seed_published_event()

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_published_event(self):
        self.service.create(
            admin, "station", {"code": "STA-1", "lat": 35.0, "lon": 110.0}
        )
        self.service.create(
            admin, "station", {"code": "STA-2", "lat": 35.1, "lon": 110.1}
        )
        event = self.service.create(
            admin,
            "event",
            {
                "title": "Event-A",
                "origin_time": "2026-01-01T00:00:00Z",
                "location": "Region-A",
                "reports": [
                    {"station": "STA-1", "time_offset": 2, "distance_km": 1.0},
                    {"station": "STA-2", "time_offset": -1, "distance_km": 1.5},
                ],
            },
        )
        event_id = event["id"]
        self.service.transition(admin, event_id, "associate", {})
        self.service.transition(
            admin, event_id, "review", {"reviewer": "R-1", "magnitude": 4.2}
        )
        self.service.transition(
            admin, event_id, "publish", {"communication_id": "C-1"}
        )
        return event_id

    def test_waveform_supplement_keeps_review_and_publish(self):
        updated = self.service.apply_correction(
            station,
            self.event_id,
            {"station": "STA-2", "waveform": "wf://sta2/2026010100"},
        )
        self.assertEqual(updated["status"], "published")
        self.assertEqual(updated["data"].get("magnitude"), 4.2)
        self.assertEqual(updated["data"].get("reviewer"), "R-1")
        self.assertEqual(updated["data"].get("communication_id"), "C-1")
        reports = {r["station"]: r for r in updated["data"]["reports"]}
        self.assertEqual(reports["STA-2"]["waveform"], "wf://sta2/2026010100")
        self.assertEqual(updated["version"], 5)

    def test_significant_correction_withdraws_and_marks_pending_review(self):
        updated = self.service.apply_correction(
            station,
            self.event_id,
            {"station": "STA-2", "magnitude": 4.8, "reason": "new amplitude"},
        )
        self.assertEqual(updated["status"], "pending_review")
        self.assertEqual(updated["data"]["magnitude"], 4.8)
        self.assertEqual(
            updated["data"]["withdrawn_releases"],
            [{"communication_id": "C-1", "reason": "new amplitude"}],
        )
        # 待复核修订可以继续初审并重新发布
        reviewed = self.service.transition(
            Actor("rev-1", "reviewer"),
            self.event_id,
            "review",
            {"reviewer": "R-2", "magnitude": 4.8},
        )
        self.assertEqual(reviewed["status"], "reviewed")
        republished = self.service.transition(
            Actor("rev-1", "reviewer"),
            self.event_id,
            "publish",
            {"communication_id": "C-2"},
        )
        self.assertEqual(republished["status"], "published")

    def test_concurrent_corrections_later_one_recomputed_on_new_version(self):
        current = self.service.get(self.event_id)
        first = self.service.apply_correction(
            Actor("sta-1", "station"),
            self.event_id,
            {"station": "STA-1", "waveform": "wf-sta-1"},
            expected_version=current["version"],
        )
        # 后到请求不带 expected_version：先落地的修订占用版本，后到按新版本重算
        second = self.service.apply_correction(
            Actor("sta-2", "station"),
            self.event_id,
            {"station": "STA-2", "waveform": "wf-sta-2"},
        )
        self.assertEqual(second["version"], first["version"] + 1)
        reports = {r["station"]: r for r in second["data"]["reports"]}
        self.assertEqual(reports["STA-1"]["waveform"], "wf-sta-1")
        self.assertEqual(reports["STA-2"]["waveform"], "wf-sta-2")
        self.assertEqual(second["status"], "published")

    def test_stale_expected_version_is_conflict(self):
        with self.assertRaises(ConflictError):
            self.service.apply_correction(
                station,
                self.event_id,
                {"station": "STA-2", "waveform": "wf-x"},
                expected_version=1,
            )

    def test_correction_requires_known_station(self):
        with self.assertRaises(ValidationError):
            self.service.apply_correction(
                station,
                self.event_id,
                {"station": "STA-X", "waveform": "wf-x"},
            )

    def test_viewer_cannot_correct(self):
        with self.assertRaises(PermissionDenied):
            self.service.apply_correction(
                Actor("viewer", "viewer"),
                self.event_id,
                {"station": "STA-2", "waveform": "wf-x"},
            )

    def test_empty_correction_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.apply_correction(
                station, self.event_id, {"station": "STA-2"}
            )

    def test_correction_on_wrong_kind(self):
        station_entity = self.service.create(
            admin, "station", {"code": "STA-9", "lat": 1.0, "lon": 2.0}
        )
        with self.assertRaises(InvalidTransition):
            self.service.apply_correction(
                admin,
                station_entity["id"],
                {"station": "STA-2", "waveform": "wf"},
            )

    def test_revision_chain_records_every_version(self):
        self.service.apply_correction(
            station,
            self.event_id,
            {"station": "STA-2", "waveform": "wf"},
        )
        revisions = self.service.revisions(self.event_id)
        statuses = [revision["status"] for revision in revisions]
        self.assertEqual(statuses[0], "candidate")
        self.assertEqual(statuses[-1], "published")
        self.assertEqual(len(revisions), revisions[-1]["version"])
        self.assertTrue(all(revision["note"] for revision in revisions))

    def test_list_by_station_and_pending_status(self):
        self.service.apply_correction(
            station,
            self.event_id,
            {"station": "STA-2", "magnitude": 4.8},
        )
        pending = self.service.list("event", status="pending_review")
        self.assertEqual([item["id"] for item in pending], [self.event_id])
        only_sta1 = self.service.list("event", station="STA-1")
        self.assertEqual(len(only_sta1), 1)
        self.assertEqual(self.service.list("event", station="STA-X"), [])


class ConcurrentRecomputeTest(unittest.TestCase):
    """模拟两站同时修改：第一次落地被外部抢先，重试必须基于新版本重算。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.rules = RuleEngine()
        self.service = DomainService(self.repo, self.rules)

    def tearDown(self):
        self.tmp.cleanup()

    def test_retry_recomputes_against_latest_version(self):
        service = self.service
        service.create(admin, "station", {"code": "STA-1", "lat": 1.0, "lon": 1.0})
        service.create(admin, "station", {"code": "STA-2", "lat": 2.0, "lon": 2.0})
        event = service.create(
            admin,
            "event",
            {
                "title": "E",
                "origin_time": "2026-01-01T00:00:00Z",
                "location": "R",
                "reports": [
                    {"station": "STA-1", "time_offset": 0, "distance_km": 0.5},
                    {"station": "STA-2", "time_offset": 1, "distance_km": 0.6},
                ],
            },
        )
        event_id = event["id"]
        service.transition(admin, event_id, "associate", {})
        service.transition(admin, event_id, "review", {"reviewer": "R", "magnitude": 3.0})
        service.transition(admin, event_id, "publish", {"communication_id": "C"})

        real_update = self.repo.update_entity
        state = {"snatched": False}

        def flaky_update(entity_id, expected_version, status, data):
            if not state["snatched"]:
                # 另一个台站的修订先落地，占用了版本；先标记，避免嵌套再次进入抢占分支
                state["snatched"] = True
                service.apply_correction(
                    Actor("sta-1", "station"),
                    event_id,
                    {"station": "STA-1", "waveform": "wf-first"},
                )
            return real_update(entity_id, expected_version, status, data)

        self.repo.update_entity = flaky_update
        updated = service.apply_correction(
            Actor("sta-2", "station"),
            event_id,
            {"station": "STA-2", "waveform": "wf-second"},
        )
        reports = {r["station"]: r for r in updated["data"]["reports"]}
        self.assertEqual(reports["STA-1"]["waveform"], "wf-first")
        self.assertEqual(reports["STA-2"]["waveform"], "wf-second")


class LegacyUpgradeTest(unittest.TestCase):
    """历史事件没有稳定报文编号，升级后仍能查询和继续修订，审计不可改写。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "legacy.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _legacy_payload(self):
        return json.dumps(
            {
                "title": "Legacy",
                "origin_time": "2020-05-01T00:00:00Z",
                "location": "Old-Region",
                "reports": [
                    {"station": "STA-1", "time_offset": 0, "distance_km": 1.0},
                    {"station": "STA-2", "time_offset": 1, "distance_km": 1.2},
                ],
            },
            sort_keys=True,
        )

    def test_legacy_event_keeps_queryable_revision_chain(self):
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE entities (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                version INTEGER NOT NULL, data TEXT NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT NOT NULL,
                actor_id TEXT NOT NULL, actor_role TEXT NOT NULL, action TEXT NOT NULL,
                from_status TEXT, to_status TEXT NOT NULL, detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE idempotency (
                actor_id TEXT NOT NULL, idem_key TEXT NOT NULL, entity_id TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(actor_id, idem_key)
            );
            """
        )
        connection.execute(
            "INSERT INTO entities VALUES (?, 'event', 'published', 7, ?, ?, ?, ?)",
            ("legacy-1", self._legacy_payload(), "old-system", "2020-05-01T00:01:00",
             "2020-05-01T00:09:00"),
        )
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status,"
            " to_status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-1", "old", "reviewer", "publish", "reviewed", "published",
             "{}", "2020-05-01T00:09:00"),
        )
        connection.commit()
        connection.close()

        repo = SQLiteRepository(self.db_path)
        service = DomainService(repo, RuleEngine())

        entity = service.get("legacy-1")
        self.assertEqual(entity["version"], 7)
        revisions = service.revisions("legacy-1")
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["version"], 7)
        self.assertEqual(revisions[0]["note"], "升级回填的历史版本")
        self.assertEqual(revisions[0]["data"]["location"], "Old-Region")

        # 升级幂等：再次打开不应产生重复回填
        repo2 = SQLiteRepository(self.db_path)
        self.assertEqual(len(repo2.list_event_revisions("legacy-1")), 1)

        # 历史事件可继续补波形修订
        service.create(admin, "station", {"code": "STA-1", "lat": 1.0, "lon": 1.0})
        service.create(admin, "station", {"code": "STA-2", "lat": 2.0, "lon": 2.0})
        updated = service.apply_correction(
            Actor("sta-2", "station"),
            "legacy-1",
            {"station": "STA-2", "waveform": "wf-legacy"},
        )
        self.assertEqual(updated["version"], 8)
        self.assertEqual(len(service.revisions("legacy-1")), 2)

        # 审计记录不能改写
        with self.assertRaises(sqlite3.Error):
            with repo._connect() as direct:
                direct.execute("UPDATE audit_log SET action = 'hacked' WHERE id = 1")
        with self.assertRaises(sqlite3.Error):
            with repo._connect() as direct:
                direct.execute("DELETE FROM audit_log WHERE id = 1")
        audit = service.audit_log("legacy-1")
        self.assertEqual(audit[0]["action"], "publish")


if __name__ == "__main__":
    unittest.main()
