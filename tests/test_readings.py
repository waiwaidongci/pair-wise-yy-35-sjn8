import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


class ReadingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "worker dose event", "description": "multi detector round",
             "severity": "low", "quantity": 0, "threshold": 1.0, "dose_limit": 2.0,
             "external_ref": "RD-READ-1"}, "creator", "dosimetrist")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _upload(self, instrument="TLD-01", measured="2026-09-25T08:00:00Z",
                raw=1.2, background=0.2, source="auto-tld",
                actor="reader", role="dosimetrist"):
        return self.service.add_reading(self.item["id"], {
            "instrument_id": instrument, "measured_at": measured,
            "raw_dose": raw, "background": background, "source": source}, actor, role)

    def _confirm_all(self):
        for reading in self.service.list_readings(self.item["id"], "viewer"):
            if reading["status"] == "pending":
                self.service.confirm_reading(
                    self.item["id"], reading["id"], "officer", "radiation_officer")

    def test_upload_confirm_accumulate_and_flags(self):
        reading = self._upload()
        self.assertEqual(reading["version"], 1)
        self.assertEqual(reading["status"], "pending")
        self.assertFalse(reading["effective"])
        self.assertAlmostEqual(reading["net_dose"], 1.0)
        # 待确认读数不计入累计
        view = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(view["accumulated_dose"], 0)
        self.assertFalse(view["escalation_required"])
        # 第二路探测器读数，确认两路后按扣本底累计
        self._upload(instrument="TLD-02", raw=0.9, background=0.1)
        self._confirm_all()
        view = self.service.get_item(self.item["id"], "viewer")
        self.assertAlmostEqual(view["accumulated_dose"], 1.8)
        self.assertAlmostEqual(view["dose_current"], 1.8)
        self.assertEqual(view["confirmed_readings"], 2)
        self.assertTrue(view["escalation_required"])   # 超过调查水平1.0
        self.assertFalse(view["follow_up_required"])   # 未达剂量限值2.0
        self.assertLess(view["deadline_hours"], 72)    # 报告期限按累计值重算
        self.assertGreaterEqual(view["remaining_hours"], 0)
        self.assertLessEqual(view["remaining_hours"], view["deadline_hours"])

    def test_duplicate_rejected_and_timezone_normalized(self):
        self._upload()
        with self.assertRaises(ConflictError):
            self._upload()
        # 同一时刻的不同时区写法规范化后仍算重复
        with self.assertRaises(ConflictError):
            self._upload(measured="2026-09-25T16:00:00+08:00")
        # 不同仪器同一时间、同一仪器不同时间允许
        self._upload(instrument="TLD-02")
        self._upload(measured="2026-09-25T09:00:00Z")
        self.assertEqual(len(self.service.list_readings(self.item["id"], "viewer")), 3)

    def test_correction_appends_version_and_keeps_history(self):
        first = self._upload(raw=1.2, background=0.2)
        # 未确认的读数不能更正
        with self.assertRaises(ConflictError):
            self.service.correct_reading(self.item["id"], {
                "instrument_id": "TLD-01", "measured_at": "2026-09-25T08:00:00Z",
                "raw_dose": 2.2, "background": 0.1, "source": "manual-review",
                "reason": "本底台账录入错误"}, "officer", "radiation_officer")
        self.service.confirm_reading(self.item["id"], first["id"], "officer",
                                     "radiation_officer")
        # 确认后只能追加更正版本，且必须填写原因
        with self.assertRaises(ValidationError):
            self.service.correct_reading(self.item["id"], {
                "instrument_id": "TLD-01", "measured_at": "2026-09-25T08:00:00Z",
                "raw_dose": 2.2, "background": 0.1, "source": "manual-review"},
                "officer", "radiation_officer")
        corrected = self.service.correct_reading(self.item["id"], {
            "instrument_id": "TLD-01", "measured_at": "2026-09-25T08:00:00Z",
            "raw_dose": 2.2, "background": 0.1, "source": "manual-review",
            "reason": "本底台账录入错误"}, "officer", "radiation_officer")
        self.assertEqual(corrected["version"], 2)
        self.assertEqual(corrected["status"], "pending")
        self.assertFalse(corrected["effective"])
        # 更正版本确认前仍按v1累计
        view = self.service.get_item(self.item["id"], "viewer")
        self.assertAlmostEqual(view["accumulated_dose"], 1.0)
        self.service.confirm_reading(self.item["id"], corrected["id"], "officer",
                                     "radiation_officer")
        view = self.service.get_item(self.item["id"], "viewer")
        self.assertAlmostEqual(view["accumulated_dose"], 2.1)
        self.assertTrue(view["follow_up_required"])  # 达到剂量限值2.0
        # 历史版本与有效版本都可查
        history = self.service.list_readings(self.item["id"], "viewer")
        self.assertEqual(len(history), 2)
        self.assertEqual([r["version"] for r in history], [1, 2])
        effective = self.service.list_readings(self.item["id"], "viewer",
                                               effective_only=True)
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0]["version"], 2)
        # 审计链包含全部读数动作且可校验
        actions = [e["action"] for e in self.service.audit("viewer", self.item["id"])]
        for expected in ("reading", "reading_confirm", "reading_correct"):
            self.assertIn(expected, actions)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_confirm_guards(self):
        reading = self._upload()
        self.service.confirm_reading(self.item["id"], reading["id"], "officer",
                                     "radiation_officer")
        with self.assertRaises(ConflictError):
            self.service.confirm_reading(self.item["id"], reading["id"], "officer",
                                         "radiation_officer")
        with self.assertRaises(NotFoundError):
            self.service.confirm_reading(self.item["id"], 9999, "officer",
                                         "radiation_officer")
        with self.assertRaises(NotFoundError):
            self.service.correct_reading(self.item["id"], {
                "instrument_id": "GHOST", "measured_at": "2026-09-25T08:00:00Z",
                "raw_dose": 1.0, "background": 0.1, "source": "manual",
                "reason": "无原始读数"}, "officer", "radiation_officer")

    def test_permissions_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self._upload(role="viewer")
        with self.assertRaises(PermissionDenied):
            self._upload(role="health_physicist")
        reading = self._upload()
        with self.assertRaises(PermissionDenied):
            self.service.confirm_reading(self.item["id"], reading["id"], "reader",
                                         "dosimetrist")
        with self.assertRaises(PermissionDenied):
            self.service.correct_reading(self.item["id"], {
                "instrument_id": "TLD-01", "measured_at": "2026-09-25T08:00:00Z",
                "raw_dose": 1.0, "background": 0.1, "source": "manual",
                "reason": "越权更正"}, "reader", "dosimetrist")
        bad_payloads = [
            {"instrument_id": "", "measured_at": "2026-09-25T08:00:00Z",
             "raw_dose": 1.0, "background": 0.1, "source": "auto"},
            {"instrument_id": "TLD-09", "measured_at": "not-a-time",
             "raw_dose": 1.0, "background": 0.1, "source": "auto"},
            {"instrument_id": "TLD-09", "measured_at": "2026-09-25T08:00:00Z",
             "background": 0.1, "source": "auto"},
            {"instrument_id": "TLD-09", "measured_at": "2026-09-25T08:00:00Z",
             "raw_dose": 1.0, "background": -0.1, "source": "auto"},
            {"instrument_id": "TLD-09", "measured_at": "2026-09-25T08:00:00Z",
             "raw_dose": 1.0, "background": 0.1, "source": ""},
        ]
        for payload in bad_payloads:
            with self.assertRaises(ValidationError):
                self.service.add_reading(self.item["id"], payload, "reader",
                                         "dosimetrist")

    def test_list_order_follows_recomputed_priority(self):
        other = self.service.create_item(
            {"title": "quiet event", "description": "no readings",
             "severity": "low", "quantity": 0, "threshold": 1.0,
             "external_ref": "RD-READ-2"}, "creator", "dosimetrist")
        # 无读数事件回退到登记总量，且新事件排在同优先级前面
        items = self.service.list_items("viewer")
        self.assertEqual(items[0]["id"], other["id"])
        view = self.service.get_item(other["id"], "viewer")
        self.assertEqual(view["dose_current"], other["quantity"])
        # 本事件确认读数后累计值上升，优先级重算并排到列表前
        reading = self._upload(raw=3.0, background=0.0)
        self.service.confirm_reading(self.item["id"], reading["id"], "officer",
                                     "radiation_officer")
        items = self.service.list_items("viewer")
        self.assertEqual(items[0]["id"], self.item["id"])
        self.assertGreater(items[0]["priority"], items[1]["priority"])


if __name__ == "__main__":
    unittest.main()
