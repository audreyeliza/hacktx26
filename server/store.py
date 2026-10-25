"""SQLite storage (AGENTS.md section 9): vitals every 5 s, events, alerts, LLM cache.

Raw waveforms are never stored here (only in record mode). One connection, used from the
event loop thread; writes are small, and WAL mode keeps readers unblocked.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from server.config import ROOT

SCHEMA = """
CREATE TABLE IF NOT EXISTS vitals (
    id INTEGER PRIMARY KEY,
    patient_id TEXT NOT NULL,
    t REAL,
    rr_fused REAL, rr_conf REAL, hr REAL,
    quality TEXT, quality_conf REAL, posture TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS vitals_patient_t ON vitals (patient_id, t);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    patient_id TEXT NOT NULL,
    type TEXT, start_t REAL, end_t REAL, duration_s REAL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_patient_t ON events (patient_id, start_t);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    patient_id TEXT NOT NULL,
    kind TEXT, tier INTEGER, status TEXT, raised_t REAL, acknowledged_at REAL,
    reason TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_patient_t ON alerts (patient_id, raised_t);

CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY,
    num INTEGER NOT NULL,
    status TEXT, bed TEXT,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_cache (
    id INTEGER PRIMARY KEY,
    patient_id TEXT, kind TEXT, key TEXT, created_t REAL,
    data TEXT NOT NULL
);
"""

VITALS_COLUMNS = ("t", "rr_fused", "rr_conf", "hr", "quality", "quality_conf", "posture")


class Store:
    def __init__(self, path: str | Path) -> None:
        path = str(path)
        if path != ":memory:":
            p = Path(path)
            if not p.is_absolute():
                p = ROOT / p
            p.parent.mkdir(parents=True, exist_ok=True)
            path = str(p)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- vitals
    def add_vitals(self, patient_id: str, v: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO vitals (patient_id, t, rr_fused, rr_conf, hr, quality, quality_conf, posture, data)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (patient_id, *(v.get(c) for c in VITALS_COLUMNS), json.dumps(v)))
        self.db.commit()

    def vitals(self, patient_id: str, since_t: float | None = None, limit: int = 720) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT data FROM vitals WHERE patient_id = ? AND (? IS NULL OR t >= ?)"
            " ORDER BY t DESC, id DESC LIMIT ?", (patient_id, since_t, since_t, limit)).fetchall()
        return [json.loads(r["data"]) for r in reversed(rows)]

    # ---- events
    def add_event(self, e: dict[str, Any]) -> int:
        cur = self.db.execute(
            "INSERT INTO events (patient_id, type, start_t, end_t, duration_s, data) VALUES (?, ?, ?, ?, ?, ?)",
            (e["patient_id"], e["type"], e["start_t"], e["end_t"], e["duration_s"], "{}"))
        eid = int(cur.lastrowid)
        e["id"] = eid
        self.db.execute("UPDATE events SET data = ? WHERE id = ?", (json.dumps(e), eid))
        self.db.commit()
        return eid

    def events(self, patient_id: str, since_t: float | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT data FROM events WHERE patient_id = ? AND (? IS NULL OR start_t >= ?)"
            " ORDER BY start_t DESC, id DESC LIMIT ?", (patient_id, since_t, since_t, limit)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    # ---- alerts
    def save_alert(self, a: dict[str, Any]) -> int:
        """Insert (a["id"] is None) or update an alert; sets a["id"]."""
        cols = (a["patient_id"], a["kind"], a["tier"], a["status"], a["raised_t"], a["acknowledged_t"], a["reason"])
        if a.get("id") is None:
            cur = self.db.execute(
                "INSERT INTO alerts (patient_id, kind, tier, status, raised_t, acknowledged_at, reason, data)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, '{}')", cols)
            a["id"] = int(cur.lastrowid)
        self.db.execute(
            "UPDATE alerts SET patient_id = ?, kind = ?, tier = ?, status = ?, raised_t = ?, acknowledged_at = ?,"
            " reason = ?, data = ? WHERE id = ?", (*cols, json.dumps(a), a["id"]))
        self.db.commit()
        return a["id"]

    def alerts(self, patient_id: str, status: str | None = None, since_t: float | None = None,
               limit: int = 100) -> list[dict[str, Any]]:
        """`status`: None/"all", one status, or "unacked" (active + awaiting_ack)."""
        statuses = {None: None, "all": None, "unacked": ("active", "awaiting_ack")}.get(status, (status,))
        status_sql = "" if statuses is None else f" AND status IN ({','.join('?' * len(statuses))})"
        rows = self.db.execute(
            "SELECT data FROM alerts WHERE patient_id = ?" + status_sql +
            " AND (? IS NULL OR raised_t >= ?) ORDER BY raised_t DESC, id DESC LIMIT ?",
            (patient_id, *(statuses or ()), since_t, since_t, limit)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def alert(self, alert_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT data FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    # ---- patients
    def next_patient_id(self) -> str:
        row = self.db.execute("SELECT COALESCE(MAX(num), 0) + 1 AS n FROM patients").fetchone()
        return f"p{row['n']}"

    def save_patient(self, patient: dict[str, Any]) -> None:
        num = int(patient["id"].lstrip("p"))
        self.db.execute(
            "INSERT INTO patients (id, num, status, bed, data) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET status = excluded.status, bed = excluded.bed, data = excluded.data",
            (patient["id"], num, patient["status"], patient["bed"], json.dumps(patient)))
        self.db.commit()

    @staticmethod
    def _patient(data: str) -> dict[str, Any]:
        p = json.loads(data)
        p.setdefault("calibration", None)   # records from before "calibrate supine" existed
        return p

    def patient(self, patient_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT data FROM patients WHERE id = ?", (patient_id,)).fetchone()
        return self._patient(row["data"]) if row else None

    def patients(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT data FROM patients WHERE (? IS NULL OR status = ?) ORDER BY num",
            (status, status)).fetchall()
        return [self._patient(r["data"]) for r in rows]

    def delete_all_patients(self) -> None:
        for table in ("patients", "vitals", "events", "alerts", "llm_cache"):
            self.db.execute(f"DELETE FROM {table}")
        self.db.commit()

    # ---- LLM cache (milestone 7)
    def cache_summary(self, patient_id: str, kind: str, key: str, summary: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO llm_cache (patient_id, kind, key, created_t, data) VALUES (?, ?, ?, ?, ?)",
                        (patient_id, kind, key, summary["created_t"], json.dumps(summary)))
        self.db.commit()

    def cached_summary(self, patient_id: str, kind: str, key: str | None = None) -> dict[str, Any] | None:
        """Latest cached summary for this patient and kind (and key, e.g. the alert id, if given)."""
        row = self.db.execute(
            "SELECT data FROM llm_cache WHERE patient_id = ? AND kind = ? AND (? IS NULL OR key = ?)"
            " ORDER BY created_t DESC, id DESC LIMIT 1", (patient_id, kind, key, key)).fetchone()
        return json.loads(row["data"]) if row else None

    def clear_summaries(self) -> int:
        """Delete every cached LLM summary (e.g. after changing how times are formatted)."""
        n = self.db.execute("DELETE FROM llm_cache").rowcount
        self.db.commit()
        return n

    # ---- housekeeping
    def clear_patient(self, patient_id: str) -> None:
        """Delete a patient's runtime history (vitals, events, alerts); keeps the record and
        cached summaries (the demo must still have something to show offline)."""
        for table in ("vitals", "events", "alerts"):
            self.db.execute(f"DELETE FROM {table} WHERE patient_id = ?", (patient_id,))
        self.db.commit()


def main(argv: list[str] | None = None) -> None:
    """Maintenance: python -m server.store --clear-summaries [--db data/breathwatch.db]"""
    import argparse
    from server.config import load_config
    ap = argparse.ArgumentParser(description="BreathWatch database maintenance")
    ap.add_argument("--db", default=None, help="database path (default: store.path in config.yaml)")
    ap.add_argument("--clear-summaries", action="store_true", help="delete all cached LLM summaries")
    args = ap.parse_args(argv)
    store = Store(args.db or load_config().store.path)
    if args.clear_summaries:
        print(f"deleted {store.clear_summaries()} cached summaries")
    store.close()


if __name__ == "__main__":
    main()
