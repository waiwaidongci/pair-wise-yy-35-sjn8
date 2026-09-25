from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text, require_timestamp)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DEFAULT_DOSE_LIMIT, ENTITY,
                    READING_CONFIRM_ROLES, READING_CORRECT_ROLES, READING_ROLES,
                    RECORD_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, follow_up_required, net_dose,
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
        dose_limit = payload.get("dose_limit")
        if dose_limit is None:
            dose_limit = DEFAULT_DOSE_LIMIT
        dose_limit = require_number(dose_limit, "dose_limit", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, dose_limit, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "dose_limit": dose_limit,
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

    def _reading_payload(self, payload: Dict[str, Any]) -> tuple:
        instrument_id = require_text(payload.get("instrument_id"), "instrument_id", 100)
        measured_at = require_timestamp(payload.get("measured_at"), "measured_at")
        raw_dose = require_number(payload.get("raw_dose"), "raw_dose")
        background = require_number(payload.get("background"), "background")
        source = require_text(payload.get("source"), "source", 100)
        return instrument_id, measured_at, raw_dose, background, source

    @staticmethod
    def _reading_view(reading: Dict[str, Any], summary: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(reading)
        result["net_dose"] = net_dose(reading["raw_dose"], reading["background"])
        result["effective"] = reading["id"] in summary["effective_ids"]
        return result

    def add_reading(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, READING_ROLES)
        actor = require_text(actor, "actor", 100)
        instrument_id, measured_at, raw_dose, background, source = \
            self._reading_payload(payload)
        reading = self.repository.add_reading(item_id, instrument_id, measured_at,
                                              raw_dose, background, source, actor)
        summary = self.repository.dose_summary(item_id)
        self.repository.append_audit("reading", ENTITY, item_id, actor, {
            "reading_id": reading["id"], "instrument_id": instrument_id,
            "measured_at": measured_at, "raw_dose": raw_dose, "background": background,
            "source": source, "version": reading["version"],
            "accumulated_dose": summary["accumulated"],
        })
        return self._reading_view(reading, summary)

    def confirm_reading(self, item_id: int, reading_id: int, actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, READING_CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        reading = self.repository.confirm_reading(item_id, reading_id, actor)
        summary = self.repository.dose_summary(item_id)
        self.repository.append_audit("reading_confirm", ENTITY, item_id, actor, {
            "reading_id": reading["id"], "instrument_id": reading["instrument_id"],
            "measured_at": reading["measured_at"], "version": reading["version"],
            "accumulated_dose": summary["accumulated"],
        })
        return self._reading_view(reading, summary)

    def correct_reading(self, item_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, READING_CORRECT_ROLES)
        actor = require_text(actor, "actor", 100)
        instrument_id, measured_at, raw_dose, background, source = \
            self._reading_payload(payload)
        reason = require_text(payload.get("reason"), "reason", 500)
        reading = self.repository.correct_reading(item_id, instrument_id, measured_at,
                                                  raw_dose, background, source, reason,
                                                  actor)
        summary = self.repository.dose_summary(item_id)
        self.repository.append_audit("reading_correct", ENTITY, item_id, actor, {
            "reading_id": reading["id"], "instrument_id": instrument_id,
            "measured_at": measured_at, "raw_dose": raw_dose, "background": background,
            "source": source, "version": reading["version"], "reason": reason,
            "accumulated_dose": summary["accumulated"],
        })
        return self._reading_view(reading, summary)

    def list_readings(self, item_id: int, role: str,
                      effective_only: bool = False) -> list:
        self._view(role)
        readings = self.repository.list_readings(item_id)
        summary = self.repository.dose_summary(item_id)
        views = [self._reading_view(reading, summary) for reading in readings]
        if effective_only:
            views = [view for view in views if view["effective"]]
        return views

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
            raise ConflictError("；".join(blockers))
        enriched = self.enrich(item)
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": enriched["escalation_required"],
            "accumulated_dose": enriched["accumulated_dose"],
            "follow_up_required": enriched["follow_up_required"],
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        items = [self.enrich(item) for item in self.repository.list_items(status)]
        items.sort(key=lambda entry: (-entry["priority"], -entry["id"]))
        return items

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        summary = self.repository.dose_summary(item["id"])
        accumulated = summary["accumulated"]
        dose_current = accumulated if summary["effective_count"] > 0 else item["quantity"]
        result["accumulated_dose"] = accumulated
        result["dose_current"] = round(dose_current, 6)
        result["confirmed_readings"] = summary["effective_count"]
        result["reading_versions"] = summary["version_count"]
        result["priority"] = priority_score(
            item["severity"], dose_current, item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], dose_current, item["threshold"])
        result["remaining_hours"] = remaining_report_hours(
            result["deadline_hours"], item["created_at"])
        result["escalation_required"] = escalation_required(
            item["severity"], dose_current, item["threshold"])
        result["follow_up_required"] = follow_up_required(
            dose_current, item.get("dose_limit"))
        return result
