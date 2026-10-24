"""Apnea / hypopnea detection and classification (AGENTS.md section 7, Events).

Every 5 s the detector looks at the quality-masked 60 s window and steps a state machine at
1 s resolution. Amplitude = smoothed Hilbert envelope of the band-passed signal; ratios are
against a rolling baseline (median amplitude over the last 2 min of valid data, excluding
seconds inside an apnea so a long apnea cannot erase itself; sustained shallow breathing does
enter the baseline, as "the previous 2 min" in §7 implies).

Measurement choices (see NOTES.md, M5):
- **Event extent** = the run of seconds with airflow below the hypopnea limit (70 %), i.e.
  from the start of the drop to recovery. The 10 % level is not used for boundaries because
  the envelope cannot be sharp there: the simulated (and real) airflow decays over ~2-3 s, and
  the Hilbert transform leaks ~10 % of a breath 2 s away.
- **Type**: apnea if the median airflow over the event's interior (edges trimmed) is < 10 % of
  baseline, else hypopnea; apneas are then split by effort (obstructive / central / mixed).
- **Two lags**: a second is *final* once it is `lag_s` (8 s) behind the newest sample (values
  near a window edge are distorted when breathing resumes right at the edge). Seconds between
  `provisional_lag_s` and `lag_s` are evaluated *provisionally* each update, only to report an
  apnea in progress (`ongoing()`), so alerts are not delayed by the final lag. Edge distortion
  only ever makes airflow look higher, so the provisional tail can only under-call an apnea.
- Seconds that were masked (motion, sensor off, data gap) abort any event in progress instead
  of being read as "no airflow".
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.signal import hilbert

from server.config import Config
from server.ingest import CH
from server.signals import bandpass


@dataclass
class _Point:
    idx: int          # sample index at the end of this second
    ra: float         # airflow amplitude / baseline
    re: float         # effort amplitude / baseline


@dataclass
class _Run:
    """Consecutive final seconds with airflow below the hypopnea limit."""
    posture: str
    points: list[_Point] = field(default_factory=list)


def envelope(x: np.ndarray, fs: float, cfg: Config) -> np.ndarray:
    """Smoothed Hilbert envelope, with long reflective padding at the window edges."""
    pad = min(int(cfg.events.edge_pad_s * fs), len(x) - 1)
    y = bandpass(x, fs, cfg.signals.resp_band_hz, cfg.signals.filter_order, padlen=pad)
    yp = np.pad(y, pad, mode="reflect")
    env = np.abs(hilbert(yp))[pad:pad + len(y)]
    return uniform_filter1d(env, max(1, int(cfg.events.env_smooth_s * fs)), mode="nearest")


def _fill(x: np.ndarray) -> np.ndarray:
    valid = ~np.isnan(x)
    if valid.all():
        return x
    idx = np.arange(len(x))
    return np.interp(idx, idx[valid], x[valid])


class EventDetector:
    """Stateful per-patient detector. Feed it each new quality-masked window."""

    def __init__(self, cfg: Config, fs: float) -> None:
        self.cfg = cfg
        self.ev = cfg.events
        self.fs = fs
        self.step = int(round(self.ev.point_s * fs))
        n_base = int(self.ev.baseline_window_s / self.ev.point_s)
        self.base_air: deque[float] = deque(maxlen=n_base)
        self.base_eff: deque[float] = deque(maxlen=n_base)
        self.next_idx: int | None = None
        self.run: _Run | None = None
        self.provisional: list[_Point] = []   # seconds after the final ones, re-evaluated each update
        self.posture = "unknown"

    @property
    def baseline_ready(self) -> bool:
        return len(self.base_air) * self.ev.point_s >= self.ev.baseline_min_s

    def baseline(self) -> dict[str, float] | None:
        if not self.baseline_ready:
            return None
        return {"airflow": float(np.median(self.base_air)), "effort": float(np.median(self.base_eff))}

    # ---- main update
    def update(self, block: np.ndarray, end: int, posture: str) -> list[dict[str, Any]]:
        """block: quality-masked window (channels, n) ending at sample `end`. Returns events that
        ended in this update, each with start_idx / end_idx in samples."""
        self.posture = posture
        n = block.shape[1]
        start = end - n
        final_stop = end - int(self.ev.lag_s * self.fs)
        prov_stop = end - int(self.ev.provisional_lag_s * self.fs)
        first = start + self.step
        if self.next_idx is None or self.next_idx < first:
            self.next_idx = ((first + self.step - 1) // self.step) * self.step
        self.provisional = []

        air_raw, eff_raw = block[CH["therm"]], block[CH["pressure"]]
        invalid = np.isnan(air_raw) | np.isnan(eff_raw)
        if invalid.all():
            self.run = None
            self.next_idx = max(self.next_idx, ((final_stop // self.step) + 1) * self.step)
            return []
        env_air = envelope(_fill(air_raw), self.fs, self.cfg)
        env_eff = envelope(_fill(eff_raw), self.fs, self.cfg)

        def measure(idx: int) -> tuple[float, float] | None:
            sl = slice(idx - self.step - start, idx - start)
            if invalid[sl].any():
                return None
            return float(env_air[sl].mean()), float(env_eff[sl].mean())

        events: list[dict[str, Any]] = []
        while self.next_idx <= final_stop:
            idx = self.next_idx
            self.next_idx += self.step
            m = measure(idx)
            if m is None:
                self.run = None          # masked second: abort, never read as "no airflow"
                continue
            events += self._final_point(idx, *m)

        base = self.baseline()
        if base is not None:
            idx = self.next_idx
            while idx <= prov_stop:
                m = measure(idx)
                if m is None:
                    break
                self.provisional.append(_Point(idx, *self._ratios(*m, base)))
                idx += self.step
        return events

    def _ratios(self, a: float, e: float, base: dict[str, float]) -> tuple[float, float]:
        ra = a / base["airflow"] if base["airflow"] > 0 else 1.0
        re = e / base["effort"] if base["effort"] > 0 else 1.0
        return ra, re

    def _final_point(self, idx: int, a: float, e: float) -> list[dict[str, Any]]:
        base = self.baseline()
        if base is None:
            self.base_air.append(a)
            self.base_eff.append(e)
            return []
        ra, re = self._ratios(a, e, base)
        if ra >= self.ev.apnea_frac:
            self.base_air.append(a)
            self.base_eff.append(e)
        if ra >= self.ev.hypopnea_frac:
            out = self._close(self.run) if self.run is not None else []
            self.run = None
            return out
        if self.run is None:
            self.run = _Run(self.posture)
        self.run.points.append(_Point(idx, ra, re))
        if len(self.run.points) * self.ev.point_s >= self.ev.max_event_s:
            out = self._close(self.run)
            self.run = None
            return out
        return []

    # ---- classification
    def _interior(self, pts: list[_Point]) -> list[_Point]:
        trim = int(round(self.ev.edge_trim_s / self.ev.point_s))
        return pts[trim:len(pts) - trim] if len(pts) > 2 * trim + 2 else pts

    def _classify(self, pts: list[_Point]) -> tuple[str, float, bool]:
        """(type, confidence, is_apnea) for a low-airflow run."""
        ev = self.ev
        inner = self._interior(pts)
        if float(np.median([p.ra for p in inner])) >= ev.apnea_frac:
            return "hypopnea", ev.clear_conf, False
        re = np.array([p.re for p in inner])
        third = max(1, len(re) // 3)
        if np.median(re[:third]) < ev.central_effort_frac and np.median(re[-third:]) >= ev.obstructive_effort_frac:
            return "mixed", ev.clear_conf, True
        med = float(np.median(re))
        if med >= ev.obstructive_effort_frac:
            return "obstructive", ev.clear_conf, True
        if med < ev.central_effort_frac:
            return "central", ev.clear_conf, True
        mid = (ev.obstructive_effort_frac + ev.central_effort_frac) / 2
        return ("obstructive" if med >= mid else "central"), ev.ambiguous_conf, True

    def _event(self, pts: list[_Point], posture: str) -> dict[str, Any]:
        kind, conf, _ = self._classify(pts)
        inner = self._interior(pts)
        return {
            "type": kind,
            "start_idx": pts[0].idx - self.step,
            "end_idx": pts[-1].idx,
            "duration_s": round(len(pts) * self.ev.point_s, 1),
            "confidence": conf,
            "posture": posture,
            "airflow_pct": round(100 * float(np.median([p.ra for p in inner])), 1),
            "effort_pct": round(100 * float(np.median([p.re for p in inner])), 1),
        }

    def _close(self, run: _Run) -> list[dict[str, Any]]:
        if len(run.points) * self.ev.point_s < self.ev.min_event_s:
            return []
        return [self._event(run.points, run.posture)]

    def ongoing(self) -> dict[str, Any] | None:
        """The low-airflow run in progress (final + provisional seconds), for alerts that must
        fire before the event ends."""
        pts = list(self.run.points) if self.run else []
        for p in self.provisional:
            if p.ra >= self.ev.hypopnea_frac:
                break
            pts.append(p)
        if not pts:
            return None
        kind, conf, is_apnea = self._classify(pts)
        return {"type": kind, "apnea": is_apnea, "start_idx": pts[0].idx - self.step,
                "duration_s": round(len(pts) * self.ev.point_s, 1), "confidence": conf,
                "posture": self.run.posture if self.run else self.posture}
