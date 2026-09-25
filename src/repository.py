from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, READING_STATUSES, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        reading_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in READING_STATUSES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    dose_limit REAL,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    instrument_id TEXT NOT NULL,
                    measured_at TEXT NOT NULL,
                    raw_dose REAL NOT NULL,
                    background REAL NOT NULL,
                    source TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    reason TEXT,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ({reading_statuses})),
                    supersedes INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, instrument_id, measured_at, version)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
        if "dose_limit" not in columns:
            with self.conn:
                self.conn.execute("ALTER TABLE items ADD COLUMN dose_limit REAL")
                self.conn.execute("UPDATE items SET dose_limit=threshold WHERE dose_limit IS NULL")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, dose_limit: Optional[float],
                    external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       dose_limit, status, version, external_ref, created_by, created_at,
                       updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, dose_limit,
                     STATES[0], 1, external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def add_reading(self, item_id: int, instrument_id: str, measured_at: str,
                    raw_dose: float, background: float, source: str,
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        with self._lock, self.conn:
            duplicate = self.conn.execute(
                """SELECT 1 FROM readings
                   WHERE item_id=? AND instrument_id=? AND measured_at=? LIMIT 1""",
                (item_id, instrument_id, measured_at),
            ).fetchone()
            if duplicate is not None:
                raise ConflictError("同一仪器同一时间的读数已存在，重复上传被拒绝")
            cur = self.conn.execute(
                """INSERT INTO readings(item_id, instrument_id, measured_at, raw_dose,
                   background, source, version, reason, status, supersedes, created_by,
                   created_at) VALUES(?,?,?,?,?,?,1,NULL,'pending',NULL,?,?)""",
                (item_id, instrument_id, measured_at, raw_dose, background, source,
                 actor, now),
            )
            reading_id = int(cur.lastrowid)
        return self.get_reading(reading_id)

    def get_reading(self, reading_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()
        if row is None:
            raise NotFoundError("读数不存在")
        return dict(row)

    def list_readings(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM readings WHERE item_id=?
                   ORDER BY instrument_id, measured_at, version""",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_group_version(self, item_id: int, instrument_id: str,
                             measured_at: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM readings
                   WHERE item_id=? AND instrument_id=? AND measured_at=?
                   ORDER BY version DESC LIMIT 1""",
                (item_id, instrument_id, measured_at),
            ).fetchone()
        if row is None:
            raise NotFoundError("读数不存在")
        return dict(row)

    def confirm_reading_group(self, item_id: int, instrument_id: str,
                              measured_at: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE readings SET status='confirmed'
                   WHERE item_id=? AND instrument_id=? AND measured_at=?""",
                (item_id, instrument_id, measured_at),
            )
        return self.latest_group_version(item_id, instrument_id, measured_at)

    def add_correction(self, item_id: int, instrument_id: str, measured_at: str,
                       raw_dose: float, background: float, source: str, reason: str,
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            latest = self.conn.execute(
                """SELECT * FROM readings
                   WHERE item_id=? AND instrument_id=? AND measured_at=?
                   ORDER BY version DESC LIMIT 1""",
                (item_id, instrument_id, measured_at),
            ).fetchone()
            if latest is None:
                raise NotFoundError("读数不存在")
            cur = self.conn.execute(
                """INSERT INTO readings(item_id, instrument_id, measured_at, raw_dose,
                   background, source, version, reason, status, supersedes, created_by,
                   created_at) VALUES(?,?,?,?,?,?,?,?, 'pending', ?, ?, ?)""",
                (item_id, instrument_id, measured_at, raw_dose, background, source,
                 int(latest["version"]) + 1, reason, int(latest["id"]), actor, now),
            )
            reading_id = int(cur.lastrowid)
        return self.get_reading(reading_id)

    def effective_readings(self, item_id: int) -> List[Dict[str, Any]]:
        best: Dict[str, Dict[str, Any]] = {}
        for row in self.list_readings(item_id):
            current = best.get(row["instrument_id"])
            if current is None or (row["measured_at"], row["version"]) > (
                    current["measured_at"], current["version"]):
                best[row["instrument_id"]] = row
        return [best[key] for key in sorted(best)]

    def cumulative_net(self, item_id: int) -> float:
        return sum(max(0.0, row["raw_dose"] - row["background"])
                   for row in self.effective_readings(item_id))

    def refresh_item_totals(self, item_id: int, quantity: float,
                            target_status: Optional[str]) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            if target_status:
                self.conn.execute(
                    """UPDATE items SET quantity=?, status=?, version=version+1,
                       updated_at=? WHERE id=?""",
                    (quantity, target_status, now, item_id),
                )
            else:
                self.conn.execute(
                    """UPDATE items SET quantity=?, version=version+1, updated_at=?
                       WHERE id=?""",
                    (quantity, now, item_id),
                )
        return self.get_item(item_id)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
