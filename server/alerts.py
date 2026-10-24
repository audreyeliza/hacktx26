"""Alerts (AGENTS.md section 8): baseline, persistence, tiers, cooldown, escalation, plus the
naive comparison monitor.

Deterministic rules only — the LLM never decides whether to alarm (design rule 1). Patient risk
factors are never an input here (design rule 8).

Policy choices beyond §8 (NOTES.md, M6):
- Alerts at or above `latch_tier` (2) stay until acknowledged even if the condition clears:
  after it has been clear for `clear_s` they move to status `awaiting_ack` ("resolved, awaiting
  acknowledgment": still counted in the patient's tier and still escalated, but the buzzer
  stops); if the condition returns they go back to `active`. Lower tiers resolve on their own.
- A step that fails the quality gate resets a condition's persistence timer (conservative).
- "Sensor off > 60 s -> tier 1" is the one rule that fires on non-clean data: it is about the
  device, not the patient.
- A condition that would alarm but fails the gate is logged as a tier-0 alert ("not alarmed:
  signal …"), at most once per cooldown, so the timeline shows what was suppressed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from server.config import Config
from server.ingest import CH
from scipy.signal import find_peaks

from server.signals import bandpass, fill_gaps

APNEA_TYPES = ("obstructive", "central", "mixed")
PRE_AUTH_USER = {"username": "bedside", "display_name": "Bedside (no login yet)", "role": "nurse"}


@dataclass
class _Cool:
    t: float
    tier: int


def _conf_word(conf: float, high: float) -> str:
    return "high" if conf >= high else "moderate"


class AlertEngine:
    """One patient's alert state. `update` is called every 5 s with that step's results."""

    def __init__(self, cfg: Config, patient_id: str, store: Any = None) -> None:
        self.cfg = cfg
        self.a = cfg.alerts
        self.patient_id = patient_id
        self.store = store
        self.alerts: dict[int, dict[str, Any]] = {}
        self.active: dict[str, int] = {}               # kind -> alert id
        self.cool: dict[str, _Cool] = {}
        self.since: dict[str, float | None] = {}       # condition -> first step time it held
        self.clear_since: dict[str, float | None] = {}
        self.start_t: float | None = None
        self.baseline_vals: list[float] = []
        self.recent_rr: deque[tuple[float, float]] = deque()
        self.apnea_times: deque[tuple[float, str]] = deque()
        self.alerted_apnea_start: float | None = None
        self.tier3_since: dict[int, float] = {}
        self.smart_alarms = 0
        self.suppressed = 0
        self.last_t: float | None = None
        self._local_id = 0
        self._changed: list[dict[str, Any]] = []

    # ---- public state
    @property
    def baseline_rr(self) -> float | None:
        if len(self.baseline_vals) * self.cfg.signals.rate_interval_s < self.a.baseline_min_s:
            return None
        return float(np.median(self.baseline_vals))

    def baseline(self) -> dict[str, Any] | None:
        rr = self.baseline_rr
        if rr is None:
            return None
        return {"rr": round(rr, 1), "since_t": self.start_t, "n": len(self.baseline_vals)}

    def active_alerts(self) -> list[dict[str, Any]]:
        """Unacknowledged alerts (status active or awaiting_ack), highest tier first."""
        return sorted((self.alerts[i] for i in self.active.values()), key=lambda x: (-x["tier"], -x["raised_t"]))

    def tier(self) -> tuple[int, str | None]:
        act = self.active_alerts()
        return (act[0]["tier"], act[0]["reason"]) if act else (0, None)

    def buzzer(self) -> bool:
        return any(a["tier"] >= 3 and a["status"] == "active" for a in self.active_alerts())

    def counts(self, naive_alarms: int) -> dict[str, Any]:
        hours = max((self.last_t or 0) - (self.start_t or 0), 1.0) / 3600.0
        return {"smart": {"alarms": self.smart_alarms, "per_hour": round(self.smart_alarms / hours, 1)},
                "naive": {"alarms": naive_alarms, "per_hour": round(naive_alarms / hours, 1)},
                "since_t": self.start_t}

    # ---- lifecycle helpers
    def _save(self, alert: dict[str, Any]) -> None:
        if self.store is not None:
            self.store.save_alert(alert)
        elif alert["id"] is None:
            self._local_id += 1
            alert["id"] = self._local_id
        self.alerts[alert["id"]] = alert
        self._changed.append(alert)

    def _new(self, kind: str, tier: int, reason: str, t: float, status: str = "active") -> dict[str, Any]:
        alert = {
            "id": None, "patient_id": self.patient_id, "kind": kind, "tier": tier, "reason": reason,
            "status": status, "raised_t": t, "updated_t": t, "cleared_t": None,
            "escalated": False, "escalated_t": None,
            "acknowledged_by": None, "acknowledged_t": None, "ack_note": None,
        }
        self._save(alert)
        return alert

    def _raise(self, kind: str, tier: int, reason: str, t: float, refresh: bool = False) -> None:
        """Raise, escalate or (with refresh) re-word the alert of this kind."""
        aid = self.active.get(kind)
        if aid is not None:
            alert = self.alerts[aid]
            changed = False
            if alert["status"] == "awaiting_ack":        # condition is back
                alert.update(status="active", cleared_t=None, updated_t=t)
                changed = True
            if tier > alert["tier"] or (refresh and tier == alert["tier"] and reason != alert["reason"]):
                if tier > alert["tier"]:
                    self.smart_alarms += 1
                alert.update(tier=max(tier, alert["tier"]), reason=reason, updated_t=t)
                changed = True
            if changed:
                self._save(alert)
            self.clear_since[kind] = None
            return
        c = self.cool.get(kind)
        if c is not None and t - c.t < self.a.cooldown_s and tier <= c.tier:
            self.suppressed += 1
            return
        alert = self._new(kind, tier, reason, t)
        self.active[kind] = alert["id"]
        self.cool[kind] = _Cool(t, tier)
        self.clear_since[kind] = None
        if tier >= 1:
            self.smart_alarms += 1

    def _log_suppressed(self, kind: str, reason: str, t: float) -> None:
        key = f"{kind}:log"
        c = self.cool.get(key)
        if c is not None and t - c.t < self.a.cooldown_s:
            return
        self.cool[key] = _Cool(t, 0)
        self._new(kind, 0, reason, t, status="resolved")

    def _condition_clear(self, kind: str, t: float) -> None:
        """Condition for `kind` is clear now. After clear_s: a non-latching alert resolves; a
        latching one moves to awaiting_ack (it stays until acknowledged)."""
        aid = self.active.get(kind)
        if aid is None:
            return
        alert = self.alerts[aid]
        if alert["status"] != "active":
            return
        start = self.clear_since.get(kind) or t
        self.clear_since[kind] = start
        if t - start < self.a.clear_s:
            return
        if alert["tier"] >= self.a.latch_tier:
            alert.update(status="awaiting_ack", cleared_t=t, updated_t=t)
        else:
            alert.update(status="resolved", cleared_t=t, updated_t=t)
            del self.active[kind]
            self.cool[kind] = _Cool(t, alert["tier"])
        self._save(alert)

    def _held(self, key: str, cond: bool, t: float) -> float:
        """How long (s) a condition has held on consecutive steps; 0 when it does not hold."""
        if not cond:
            self.since[key] = None
            return 0.0
        if self.since.get(key) is None:
            self.since[key] = t
        return t - self.since[key]

    def acknowledge(self, alert_id: int, user: dict[str, Any] | None, note: str | None, t: float) -> dict[str, Any] | None:
        alert = self.alerts.get(alert_id)
        if alert is None:
            return None
        if alert["status"] not in ("active", "awaiting_ack"):
            return alert
        alert.update(status="acknowledged", acknowledged_by=user or PRE_AUTH_USER, acknowledged_t=t,
                     ack_note=note, updated_t=t)
        if self.active.get(alert["kind"]) == alert_id:
            del self.active[alert["kind"]]
        self.cool[alert["kind"]] = _Cool(t, alert["tier"])
        self._save(alert)
        return alert

    def take_changes(self) -> list[dict[str, Any]]:
        out, self._changed = self._changed, []
        return out

    # ---- the rules
    def _clock(self, t: float) -> None:
        if self.start_t is None:
            self.start_t = t
        self.last_t = max(t, self.last_t or t)

    def update(self, v: dict[str, Any], ongoing: dict[str, Any] | None, new_events: list[dict[str, Any]],
               t: float) -> list[dict[str, Any]]:
        """Rate rules and apnea rules for one step (convenience for tests)."""
        changed = self.update_rates(v, t)
        quality_ok = v.get("quality") == "clean" and (v.get("quality_conf") or 0) >= self.a.gate_quality_conf
        return changed + self.update_apnea(ongoing, new_events, quality_ok, t)

    def update_apnea(self, ongoing: dict[str, Any] | None, new_events: list[dict[str, Any]],
                     quality_ok: bool, t: float) -> list[dict[str, Any]]:
        """Apnea rules; called every second by the apnea watch. Returns changed alerts."""
        self._clock(t)
        self._rule_apnea(ongoing, new_events, quality_ok, t)
        self._escalate(t)
        return self.take_changes()

    def update_rates(self, v: dict[str, Any], t: float) -> list[dict[str, Any]]:
        """Rate, trend and sensor rules for one 5 s vitals step. Returns changed alerts."""
        a = self.a
        self._clock(t)
        rr = v.get("rr_fused")
        quality_ok = v.get("quality") == "clean" and (v.get("quality_conf") or 0) >= a.gate_quality_conf
        rr_ok = quality_ok and rr is not None and (v.get("rr_conf") or 0) >= a.gate_rr_conf

        # baseline (first 10 min of eligible data) and recent history for the trend rule
        if rr_ok and t - self.start_t <= a.baseline_window_s:
            self.baseline_vals.append(rr)
        if rr_ok:
            self.recent_rr.append((t, rr))
        while self.recent_rr and self.recent_rr[0][0] < t - a.trend_window_s:
            self.recent_rr.popleft()
        base = self.baseline_rr

        self._rule_rr_low(v, rr, rr_ok, base, t)
        self._rule_trend(rr, rr_ok, base, t)
        self._rule_sensor(v, t)
        self._escalate(t)
        return self.take_changes()

    def _rule_rr_low(self, v: dict[str, Any], rr: float | None, rr_ok: bool, base: float | None, t: float) -> None:
        a = self.a
        levels = ((1, a.rr_tier1_below, a.rr_tier1_persist_s), (2, a.rr_tier2_below, a.rr_tier2_persist_s),
                  (3, a.rr_tier3_below, a.rr_tier3_persist_s))
        tier, held_for = 0, 0.0
        for lvl, below, persist in levels:
            held = self._held(f"rr<{below}", rr_ok and rr is not None and rr < below, t)
            if held >= persist:
                tier, held_for = lvl, held
        if tier:
            conf = _conf_word(v["rr_conf"], a.conf_high)
            extra = ", single sensor" if "single_source" in (v.get("flags") or []) else ""
            base_txt = f"baseline {base:.0f}/min" if base is not None else "no baseline yet"
            reason = (f"Breathing rate {rr:.0f}/min for {held_for + self.cfg.signals.rate_interval_s:.0f} s "
                      f"({base_txt}), signal confidence {conf}{extra}")
            self._raise("rr_low", tier, reason, t)
        elif rr is not None and rr < a.rr_tier1_below and not rr_ok:
            if v.get("quality") != "clean" or (v.get("quality_conf") or 0) < a.gate_quality_conf:
                why = f"signal {v.get('quality')} (confidence {v.get('quality_conf') or 0:.2f})"
            else:
                flags = ", ".join(v.get("flags") or []) or "low agreement"
                why = f"rate confidence {v.get('rr_conf') or 0:.2f} ({flags})"
            self._log_suppressed("rr_low", f"Not alarmed: breathing rate {rr:.0f}/min but {why}", t)
        if rr_ok and rr is not None and rr >= a.rr_tier1_below:
            self._condition_clear("rr_low", t)

    def _rule_trend(self, rr: float | None, rr_ok: bool, base: float | None, t: float) -> None:
        a = self.a
        if base is None:
            return
        limit = (1 - a.trend_drop_frac) * base
        recent_max = max((x for _, x in self.recent_rr), default=0.0)
        cond = rr_ok and rr is not None and rr <= limit and recent_max >= a.trend_recent_frac * base
        if self._held("trend", cond, t) >= a.trend_persist_s:
            first = next(tt for tt, x in self.recent_rr if x >= a.trend_recent_frac * base)
            pct = 100 * (base - rr) / base
            reason = (f"Declining trend: breathing rate {rr:.0f}/min is {pct:.0f}% below baseline "
                      f"{base:.0f}/min, fell within {max(1.0, (t - first) / 60):.0f} min")
            self._raise("rr_trend", 2, reason, t)
        if rr_ok and rr is not None and rr > (1 - a.trend_drop_frac / 2) * base:
            self._condition_clear("rr_trend", t)

    def _rule_apnea(self, ongoing: dict[str, Any] | None, new_events: list[dict[str, Any]],
                    quality_ok: bool, t: float) -> None:
        a = self.a
        in_progress = bool(ongoing and ongoing.get("apnea"))
        # in progress: tier 3 as soon as an apnea reaches 20 s, before the final event is logged
        if in_progress and ongoing["duration_s"] >= a.apnea_tier3_s and quality_ok:
            if self.alerted_apnea_start is None or abs(ongoing["start_t"] - self.alerted_apnea_start) > a.apnea_tier1_s:
                self.alerted_apnea_start = ongoing["start_t"]
                self._raise("apnea", 3, f"Apnea in progress for {ongoing['duration_s']:.0f} s "
                            f"({ongoing['type']}, {ongoing['posture']})", t, refresh=True)
        for e in new_events:
            if e["type"] not in APNEA_TYPES:
                continue
            self.apnea_times.append((e["end_t"], e["type"]))
            desc = f"Apnea {e['duration_s']:.0f} s ({e['type']}, {e['posture']})"
            already = self.alerted_apnea_start is not None and abs(e["start_t"] - self.alerted_apnea_start) <= a.apnea_tier1_s
            if e["duration_s"] >= a.apnea_tier3_s:
                self._raise("apnea", 3, desc, t, refresh=True)
            elif e["duration_s"] >= a.apnea_tier1_s and not already:
                self._raise("apnea", 1, desc, t, refresh=True)
        while self.apnea_times and self.apnea_times[0][0] < t - a.cluster_window_s:
            self.apnea_times.popleft()
        if len(self.apnea_times) >= a.cluster_count and new_events:
            kinds = {k: sum(1 for _, x in self.apnea_times if x == k) for k in APNEA_TYPES}
            mix = ", ".join(f"{k} {n}" for k, n in kinds.items() if n)
            self._raise("apnea_cluster", 2, f"{len(self.apnea_times)} apneas in the last "
                        f"{a.cluster_window_s / 60:.0f} min ({mix})", t, refresh=True)
        if not in_progress and not new_events:
            self._condition_clear("apnea", t)
        if len(self.apnea_times) < a.cluster_count:
            self._condition_clear("apnea_cluster", t)

    def _rule_sensor(self, v: dict[str, Any], t: float) -> None:
        held = self._held("sensor_off", v.get("quality") == "sensor_off", t)
        if held >= self.a.sensor_off_s:
            what = ", ".join(f"{c} {s}" for c, s in (v.get("channels") or {}).items() if s in ("flat", "clipped", "floating"))
            self._raise("sensor_check", 1, f"Check sensor: {what or 'signal lost'} for "
                        f"{held + self.cfg.signals.rate_interval_s:.0f} s", t)
        elif v.get("quality") != "sensor_off":
            self._condition_clear("sensor_check", t)

    def _escalate(self, t: float) -> None:
        for alert in self.active_alerts():   # active or awaiting_ack: nobody has acknowledged it
            if alert["tier"] < 3 or alert["escalated"]:
                continue
            start = self.tier3_since.setdefault(alert["id"], t)
            if t - start >= self.a.escalate_s:
                alert.update(escalated=True, escalated_t=t, updated_t=t,
                             reason=alert["reason"] + " — escalated to charge nurse (not acknowledged)")
                self._save(alert)


class NaiveMonitor:
    """The comparison monitor (§8), modelled on a simple fixed-threshold bedside monitor:
    rates are plain peak counts over a short window (no range checks, no FFT confirmation,
    no quality gating), and every onset of a condition counts as one alarm (no persistence,
    no cooldown)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.n = cfg.naive
        self.active: dict[str, bool] = {}
        self.alarms = 0
        self.by_kind: dict[str, int] = {}

    def _count_rate(self, x: np.ndarray | None, fs: float, band: tuple[float, float], window_s: float,
                    prominence: float, min_gap_s: float) -> float | None:
        if x is None:
            return None
        y = bandpass(x, fs, band, self.cfg.signals.filter_order)
        peaks, _ = find_peaks(y, prominence=prominence, distance=max(1, int(min_gap_s * fs)))
        return 60.0 * len(peaks) / window_s

    def update(self, raw: np.ndarray, fs: float, t: float) -> list[str]:
        """raw: unmasked window (channels, n) ending now. Returns conditions that just started."""
        s, n = self.cfg.signals, self.n
        p = fill_gaps(raw[CH["pressure"], -int(n.rr_window_s * fs):], s.min_valid_fraction)
        pu = fill_gaps(raw[CH["pulse"], -int(n.hr_window_s * fs):], s.min_valid_fraction)
        th = fill_gaps(raw[CH["therm"], -int(n.flat_s * fs):], s.min_valid_fraction)
        rr = self._count_rate(p, fs, s.resp_band_hz, n.rr_window_s, n.peak_prominence_counts, n.rr_min_gap_s)
        hr = self._count_rate(pu, fs, s.pulse_band_hz, n.hr_window_s, n.peak_prominence_counts, n.hr_min_gap_s)
        flat = th is not None and float(np.std(bandpass(th, fs, s.resp_band_hz, s.filter_order))) < n.flat_std_counts
        conds = {
            "rr_low": rr is not None and rr < n.rr_low,
            "rr_high": rr is not None and rr > n.rr_high,
            "hr_low": hr is not None and hr < n.hr_low,
            "hr_high": hr is not None and hr > n.hr_high,
            "airflow_flat": flat,
        }
        started = [k for k, c in conds.items() if c and not self.active.get(k)]
        self.active = conds
        for k in started:
            self.alarms += 1
            self.by_kind[k] = self.by_kind.get(k, 0) + 1
        return started
