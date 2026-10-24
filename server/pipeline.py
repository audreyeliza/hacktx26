"""One patient's processing pipeline: vitals -> events (-> alerts in milestone 6).

The app keeps one Pipeline per patient (milestone 6.5); tests drive it directly.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from server.config import Config
from server.events import EventDetector
from server.ingest import RingBuffer
from server.signals import analyze

SampleTime = Callable[[int], "float | None"]
LIVE_PATIENT_ID = "p4"  # single-patient API until milestone 6.5 (AGENTS.md §19.8)


def public_event(e: dict[str, Any]) -> dict[str, Any]:
    """Event as exposed by the API (§19.3): without internal sample indices."""
    return {k: v for k, v in e.items() if k not in ("start_idx", "end_idx")}


@dataclass
class StepResult:
    vitals: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)   # events that ended this step
    ongoing: dict[str, Any] | None = None                        # low-airflow run in progress


class Pipeline:
    def __init__(self, cfg: Config, fs: float, patient_id: str) -> None:
        self.cfg = cfg
        self.fs = fs
        self.patient_id = patient_id
        self.detector = EventDetector(cfg, fs)
        self.events: deque[dict[str, Any]] = deque(maxlen=cfg.events.recent_max)
        self.ongoing: dict[str, Any] | None = None
        self._next_event_id = 1

    def step(self, buffers: RingBuffer, end: int, sample_time: SampleTime) -> StepResult:
        vitals, block = analyze(buffers, end, self.fs, self.cfg)
        vitals["t"] = sample_time(end)
        new = [self._finish(e, sample_time) for e in self.detector.update(block, end, vitals["posture"])]
        self.events.extend(new)
        ongoing = self.detector.ongoing()
        if ongoing is not None:
            ongoing = {**ongoing, "start_t": sample_time(ongoing["start_idx"])}
        self.ongoing = ongoing
        return StepResult(vitals, new, ongoing)

    def _finish(self, e: dict[str, Any], sample_time: SampleTime) -> dict[str, Any]:
        """Detector output -> the Event shape of AGENTS.md §19.3 (plus sample indices)."""
        event = {
            "id": self._next_event_id,
            "patient_id": self.patient_id,
            "type": e["type"],
            "start_t": sample_time(e["start_idx"]),
            "end_t": sample_time(e["end_idx"]),
            "duration_s": e["duration_s"],
            "confidence": e["confidence"],
            "posture": e["posture"],
            "airflow_pct": e["airflow_pct"],
            "effort_pct": e["effort_pct"],
            "start_idx": e["start_idx"],
            "end_idx": e["end_idx"],
        }
        self._next_event_id += 1
        return event

    def recent_events(self, limit: int = 10) -> list[dict[str, Any]]:
        return list(self.events)[-limit:][::-1]

    def baseline(self) -> dict[str, float] | None:
        return self.detector.baseline()
