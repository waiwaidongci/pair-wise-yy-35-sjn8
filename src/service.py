from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text, require_timestamp)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, READING_CONFIRM_ROLES,
                    READING_CORRECT_ROLES, READING_UPLOAD_ROLES, RECORD_ROLES,
                    TERMINAL_STATES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, escalation_target, follow_up_required,
                    priority_score, remaining_report_hours,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        dose_limit_raw = payload.get("dose_limit")
        dose_limit = (require_number(dose_limit_raw, "dose_limit", 0.000001)
                      if dose_limit_raw is not None else threshold)
        if dose_limit < threshold:
            from .domain import ValidationError
            raise ValidationError("dose_limit不能小于threshold")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, dose_limit, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "threshold": threshold, "dose_limit": dose_limit,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def _ensure_item_open(self, item_id: int) -> Dict[str, Any]:
        item = self.repository.get_item(item_id)
        if item["status"] in TERMINAL_STATES:
            raise ConflictError("事件已关闭，不能登记或更正读数")
        return item

    def _refresh_after_reading_change(self, item_id: int, actor: str) -> Dict[str, Any]:
        item = self.repository.get_item(item_id)
        total = round(self.repository.cumulative_net(item_id), 6)
        dose_limit = item["dose_limit"] if item["dose_limit"] is not None else item["threshold"]
        target = escalation_target(item["status"], total, item["threshold"], dose_limit)
        updated = self.repository.refresh_item_totals(item_id, total, target)
        self.repository.append_audit("recompute", ENTITY, item_id, actor, {
            "quantity": total, "threshold": item["threshold"],
            "dose_limit": dose_limit, "auto_escalation": target,
        })
        return updated

    def add_reading(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, READING_UPLOAD_ROLES)
        actor = require_text(actor, "actor", 100)
        instrument_id = require_text(payload.get("instrument_id"), "instrument_id", 100)
        measured_at = require_timestamp(payload.get("measured_at"), "measured_at")
        raw_dose = require_number(payload.get("raw_dose"), "raw_dose")
        background = require_number(payload.get("background"), "background")
        source = require_text(payload.get("source"), "source", 100)
        self._ensure_item_open(item_id)
        reading = self.repository.add_reading(item_id, instrument_id, measured_at,
                                              raw_dose, background, source, actor)
        self.repository.append_audit("reading_upload", ENTITY, item_id, actor, {
            "reading_id": reading["id"], "instrument_id": instrument_id,
            "measured_at": measured_at, "raw_dose": raw_dose,
            "background": background, "source": source, "version": 1,
        })
        self._refresh_after_reading_change(item_id, actor)
        return self._reading_view(reading)

    def confirm_reading(self, item_id: int, reading_id: int, actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, READING_CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        self._ensure_item_open(item_id)
        reading = self.repository.get_reading(reading_id)
        if reading["item_id"] != item_id:
            from .domain import NotFoundError
            raise NotFoundError("读数不存在")
        latest = self.repository.confirm_reading_group(
            item_id, reading["instrument_id"], reading["measured_at"])
        self.repository.append_audit("reading_confirm", ENTITY, item_id, actor, {
            "reading_id": reading_id, "instrument_id": reading["instrument_id"],
            "measured_at": reading["measured_at"], "version": latest["version"],
        })
        return self._reading_view(latest)

    def correct_reading(self, item_id: int, reading_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, READING_CORRECT_ROLES)
        actor = require_text(actor, "actor", 100)
        reason = require_text(payload.get("reason"), "reason", 500)
        raw_dose = require_number(payload.get("raw_dose"), "raw_dose")
        background = require_number(payload.get("background"), "background")
        self._ensure_item_open(item_id)
        reading = self.repository.get_reading(reading_id)
        if reading["item_id"] != item_id:
            from .domain import NotFoundError
            raise NotFoundError("读数不存在")
        source = payload.get("source")
        source = (require_text(source, "source", 100) if source is not None
                  else reading["source"])
        correction = self.repository.add_correction(
            item_id, reading["instrument_id"], reading["measured_at"], raw_dose,
            background, source, reason, actor)
        self.repository.append_audit("reading_correct", ENTITY, item_id, actor, {
            "reading_id": correction["id"], "corrects_reading_id": reading_id,
            "instrument_id": reading["instrument_id"],
            "measured_at": reading["measured_at"], "version": correction["version"],
            "raw_dose": raw_dose, "background": background, "reason": reason,
        })
        self._refresh_after_reading_change(item_id, actor)
        return self._reading_view(correction)

    def list_readings(self, item_id: int, role: str,
                      effective_only: bool = False) -> list:
        self._view(role)
        rows = (self.repository.effective_readings(item_id) if effective_only
                else self.repository.list_readings(item_id))
        return [self._reading_view(row) for row in rows]

    @staticmethod
    def _reading_view(row: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(row)
        result["net_dose"] = max(0.0, row["raw_dose"] - row["background"])
        return result

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        items = [self.enrich(item) for item in self.repository.list_items(status)]
        items.sort(key=lambda entry: (-entry["priority"], entry["deadline_hours"],
                                      -entry["id"]))
        return items

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        dose_limit = item["dose_limit"] if item.get("dose_limit") is not None else item["threshold"]
        result["dose_limit"] = dose_limit
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["follow_up_required"] = follow_up_required(
            item["quantity"], dose_limit)
        result["remaining_hours"] = remaining_report_hours(
            item["created_at"], result["deadline_hours"])
        return result
