import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class ReadingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "worker dose event", "description": "multi detector monitoring",
             "severity": "high", "quantity": 0, "threshold": 10, "dose_limit": 20,
             "external_ref": "RD-1"}, "creator", "dosimetrist")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def upload(self, instrument, measured_at, raw, background, source="TLD",
               actor="reader", role="dosimetrist", item_id=None):
        return self.service.add_reading(
            item_id if item_id is not None else self.item["id"],
            {"instrument_id": instrument, "measured_at": measured_at,
             "raw_dose": raw, "background": background, "source": source},
            actor, role)

    def test_upload_registers_fields_and_recomputes_cumulative(self):
        first = self.upload("DET-1", "2026-09-25T08:00:00Z", 5.0, 1.0, source="EPD")
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["version"], 1)
        self.assertEqual(first["source"], "EPD")
        self.assertAlmostEqual(first["net_dose"], 4.0)
        self.upload("DET-2", "2026-09-25T08:05:00+00:00", 3.0, 0.5)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertAlmostEqual(item["quantity"], 6.5)
        self.assertIn("remaining_hours", item)
        self.assertEqual(item["dose_limit"], 20.0)

    def test_duplicate_instrument_and_time_rejected(self):
        self.upload("DET-1", "2026-09-25T08:00:00Z", 5.0, 1.0)
        with self.assertRaises(ConflictError):
            self.upload("DET-1", "2026-09-25T08:00:00+00:00", 9.0, 1.0)
        with self.assertRaises(ValidationError):
            self.upload("DET-1", "not-a-time", 9.0, 1.0)

    def test_confirmed_reading_corrected_by_append_only(self):
        reading = self.upload("DET-1", "2026-09-25T08:00:00Z", 5.0, 1.0)
        confirmed = self.service.confirm_reading(
            self.item["id"], reading["id"], "officer", "radiation_officer")
        self.assertEqual(confirmed["status"], "confirmed")
        with self.assertRaises(ValidationError):
            self.service.correct_reading(
                self.item["id"], reading["id"],
                {"raw_dose": 6.0, "background": 1.0}, "officer", "radiation_officer")
        correction = self.service.correct_reading(
            self.item["id"], reading["id"],
            {"raw_dose": 6.0, "background": 1.0, "reason": "仪器校准错误"},
            "officer", "radiation_officer")
        self.assertEqual(correction["version"], 2)
        self.assertEqual(correction["reason"], "仪器校准错误")
        self.assertEqual(correction["supersedes"], reading["id"])
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertAlmostEqual(item["quantity"], 5.0)
        history = self.service.list_readings(self.item["id"], "viewer")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["raw_dose"], 5.0)
        self.assertEqual(history[0]["version"], 1)
        self.assertEqual(history[0]["status"], "confirmed")

    def test_cumulative_uses_latest_version_per_instrument(self):
        self.upload("DET-1", "2026-09-25T08:00:00Z", 5.0, 1.0)
        later = self.upload("DET-1", "2026-09-25T09:00:00Z", 10.0, 1.0)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertAlmostEqual(item["quantity"], 9.0)
        self.service.correct_reading(
            self.item["id"], later["id"],
            {"raw_dose": 4.0, "background": 1.0, "reason": "读数漂移"},
            "officer", "radiation_officer")
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertAlmostEqual(item["quantity"], 3.0)
        effective = self.service.list_readings(self.item["id"], "viewer",
                                               effective_only=True)
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0]["version"], 2)

    def test_auto_escalation_to_investigation_and_follow_up(self):
        self.upload("DET-1", "2026-09-25T08:00:00Z", 13.0, 1.0)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["status"], "investigation")
        self.assertTrue(item["escalation_required"])
        self.assertFalse(item["follow_up_required"])
        self.upload("DET-2", "2026-09-25T08:05:00Z", 10.0, 0.0)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertAlmostEqual(item["quantity"], 22.0)
        self.assertEqual(item["status"], "follow_up")
        self.assertTrue(item["follow_up_required"])
        actions = [event["action"] for event in
                   self.service.audit("viewer", self.item["id"])]
        self.assertIn("reading_upload", actions)
        self.assertIn("recompute", actions)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_reading_permissions_and_closed_guard(self):
        with self.assertRaises(PermissionDenied):
            self.upload("DET-1", "2026-09-25T08:00:00Z", 5.0, 1.0, role="viewer")
        reading = self.upload("DET-1", "2026-09-25T08:00:00Z", 5.0, 1.0)
        with self.assertRaises(PermissionDenied):
            self.service.confirm_reading(
                self.item["id"], reading["id"], "reader", "dosimetrist")
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"], "closed")
        with self.assertRaises(ConflictError):
            self.upload("DET-2", "2026-09-25T09:00:00Z", 5.0, 1.0)
        with self.assertRaises(ConflictError):
            self.service.correct_reading(
                self.item["id"], reading["id"],
                {"raw_dose": 6.0, "background": 1.0, "reason": "已关闭"},
                "officer", "radiation_officer")

    def test_list_order_recomputed_from_cumulative(self):
        other = self.service.create_item(
            {"title": "second event", "description": "lower priority initially",
             "severity": "elevated", "quantity": 0, "threshold": 10,
             "dose_limit": 20, "external_ref": "RD-2"}, "creator", "dosimetrist")
        first_id = self.item["id"]
        ids = [entry["id"] for entry in self.service.list_items("viewer")]
        self.assertEqual(ids[0], first_id)
        self.upload("DET-9", "2026-09-25T08:00:00Z", 30.0, 0.0,
                    item_id=other["id"])
        ordered = self.service.list_items("viewer")
        self.assertEqual(ordered[0]["id"], other["id"])
        self.assertGreater(ordered[0]["priority"], ordered[1]["priority"])
        self.assertEqual(ordered[1]["id"], first_id)


if __name__ == "__main__":
    unittest.main()
