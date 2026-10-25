"""One patient's processing pipeline: vitals -> events -> alerts (+ the naive monitor).

Call `tick()` at every 1 s boundary of sample time:
- every second, the **apnea watch** runs event detection and the apnea alert rules, so a tier-3
  alarm fires as soon as an in-progress apnea passes 20 s (not at the next 5 s update);
- every 5 s, the **vitals step** computes rates/quality/posture, the naive monitor and the
  rate, trend and sensor alert rules.

The app keeps one Pipeline per patient (milestone 6.5); tests drive it directly.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from server.alerts import AlertEngine, NaiveMonitor, count_since
from server.config import Config
from server.events import EventDetector
from server.ingest import RingBuffer
from server.quality import segment_masks
from server.signals import analyze

SampleTime = Callable[[int], "float | None"]
LIVE_PATIENT_ID = "p4"  # single-patient API until milestone 6.5 (AGENTS.md §19.8)


def public_event(e: dict[str, Any]) -> dict[str, Any]:
    """Event as exposed by the API (§19.3): without internal sample indices."""
    return {k: v for k, v in e.items() if k not in ("start_idx", "end_idx")}


@dataclass
class StepResult:
    vitals: dict[str, Any] | None                                # only on 5 s steps
    events: list[dict[str, Any]] = field(default_factory=list)   # events that ended this tick
    ongoing: dict[str, Any] | None = None                        # low-airflow run in progress
    alerts: list[dict[str, Any]] = field(default_factory=list)   # alerts created/changed this step
    naive: list[str] = field(default_factory=list)               # naive-monitor alarms that started


class Pipeline:
    def __init__(self, cfg: Config, fs: float, patient_id: str, store: Any = None, quality_model: Any = None) -> None:
        self.cfg = cfg
        self.fs = fs
        self.patient_id = patient_id
        self.store = store
        self.detector = EventDetector(cfg, fs)
        self.alerts = AlertEngine(cfg, patient_id, store)
        self.naive = NaiveMonitor(cfg)
        self.events: deque[dict[str, Any]] = deque(maxlen=cfg.events.recent_max)
        self.ongoing: dict[str, Any] | None = None
        self.posture = "unknown"
        self.rotation: Any = None   # "calibrate supine" rotation (numpy 3x3) or None
        self.quality_model = quality_model   # shared QualityModel (v1) or None → v0 rules
        self.model_quality: str | None = None   # latest model verdict, also gates the apnea watch
        self.vitals_step = int(round(cfg.signals.rate_interval_s * fs))
        self.watch_step = int(round(cfg.events.watch_interval_s * fs))
        self._next_event_id = 1

    def tick(self, buffers: RingBuffer, end: int, sample_time: SampleTime) -> StepResult:
        """Run the apnea watch, plus the vitals step when `end` is on a 5 s boundary."""
        res = self.watch(buffers, end, sample_time)
        if end % self.vitals_step == 0:
            step = self.step(buffers, end, sample_time)
            res.vitals, res.naive = step.vitals, step.naive
            res.alerts += step.alerts
        return res

    def watch(self, buffers: RingBuffer, end: int, sample_time: SampleTime) -> StepResult:
        """Apnea watch (every second): event detection + apnea alert rules."""
        raw = buffers.window(end, int(self.cfg.signals.rate_window_s * self.fs))
        block, segments = segment_masks(raw, self.fs, self.cfg)
        seg = segments[-1]
        quality_ok = seg.label == "clean" and seg.confidence >= self.cfg.alerts.gate_quality_conf
        if self.model_quality is not None and self.model_quality != "clean":
            quality_ok = False      # the trained model's latest verdict (5 s step) also gates apneas
        new = [self._finish(e, sample_time) for e in self.detector.update(block, end, self.posture)]
        self.events.extend(new)
        ongoing = self.detector.ongoing()
        if ongoing is not None:
            ongoing = {**ongoing, "start_t": sample_time(ongoing["start_idx"])}
        self.ongoing = ongoing
        changed = self.alerts.update_apnea(ongoing, new, quality_ok, sample_time(end))
        return StepResult(None, new, ongoing, changed)

    def step(self, buffers: RingBuffer, end: int, sample_time: SampleTime) -> StepResult:
        """Vitals step (every 5 s): rates, quality, posture, naive monitor, rate/sensor rules."""
        vitals, _ = analyze(buffers, end, self.fs, self.cfg, self.rotation, self.quality_model)
        self.model_quality = vitals["quality"] if vitals["quality_source"] == "model" else None
        t = sample_time(end)
        vitals["t"] = t
        self.posture = vitals["posture"]
        naive = self.naive.update(buffers.window(end, int(self.cfg.signals.rate_window_s * self.fs)), self.fs, t)
        changed = self.alerts.update_rates(vitals, t)
        if self.store is not None:
            self.store.add_vitals(self.patient_id, vitals)
        return StepResult(vitals, [], self.ongoing, changed, naive)

    def _finish(self, e: dict[str, Any], sample_time: SampleTime) -> dict[str, Any]:
        """Detector output -> the Event shape of AGENTS.md §19.3 (plus sample indices)."""
        event = {
            "id": None,
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
        if self.store is not None:
            event["id"] = self.store.add_event(public_event(event))
        else:
            event["id"] = self._next_event_id
            self._next_event_id += 1
        return event

    def recent_events(self, limit: int = 10) -> list[dict[str, Any]]:
        return list(self.events)[-limit:][::-1]

    def monitors(self) -> dict[str, Any]:
        return self.alerts.counts(self.naive.alarms)

    def alarms_since(self, since_t: float) -> dict[str, int]:
        """Smart and naive alarm counts since a time (e.g. the start of the shift)."""
        return {"smart": count_since(self.alerts.alarm_times, since_t),
                "naive": count_since(self.naive.alarm_times, since_t)}
