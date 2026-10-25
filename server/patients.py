"""Patients and the ward (AGENTS.md §19, milestone 6.5).

Every admitted patient has a PatientRuntime: its own Stream (buffers + timeline), its own
Pipeline (vitals -> events -> alerts) and, for simulated patients, its own in-process
simulator. The live wearable's packets are routed by device id to the patient it is assigned to.

Risk factors are stored and returned for display and LLM context only; they are never passed to
the pipeline, so they cannot change alert thresholds (design rule 8).
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections import deque
from typing import Annotated, Any, Literal, Union

import numpy as np
from pydantic import BaseModel, Field

from server.config import Config
from server.ingest import CH, Ingest, SimulatorSource, Stream
from server.pipeline import Pipeline, public_event
from server.public_demo import simulated_device
from server.quality import posture, supine_rotation
from server.simulator import SCENARIO_NAMES, Simulator
from server.store import Store

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- request models

class RiskFactors(BaseModel):
    opioids: bool
    supplemental_o2: bool
    osa: Literal["none", "suspected", "known"]
    age_over_65: bool


class LiveDevice(BaseModel):
    kind: Literal["live"]
    device_id: str = Field(min_length=1, max_length=64)


class SimDevice(BaseModel):
    kind: Literal["simulator"]
    scenario: Literal[SCENARIO_NAMES]  # type: ignore[valid-type]
    speed: float = Field(1.0, gt=0, le=100)
    loop: bool = True
    start_s: float | None = Field(None, ge=0)   # scenario time when the ward appears; None = random


Device = Annotated[Union[LiveDevice, SimDevice], Field(discriminator="kind")]


class PatientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    bed: str = Field(min_length=1, max_length=20)
    risk_factors: RiskFactors
    device: Device
    notes: str = Field("", max_length=1000)


class PatientUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    bed: str | None = Field(None, min_length=1, max_length=20)
    risk_factors: RiskFactors | None = None
    device: Device | None = None
    notes: str | None = Field(None, max_length=1000)


class WardError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def bed_key(bed: str) -> tuple[int, str]:
    m = re.match(r"(\d+)(.*)", bed)
    return (int(m.group(1)), m.group(2)) if m else (10**9, bed)


# --------------------------------------------------------------------------- runtime

class PatientRuntime:
    """One admitted patient's live processing state."""

    def __init__(self, ward: Ward, patient: dict[str, Any]) -> None:
        self.ward = ward
        self.cfg = ward.cfg
        self.patient = patient
        self.stream = Stream(self.cfg)
        self.fs = self.stream.fs
        self.sim: SimulatorSource | None = None
        self.generation = 0          # bumps when the pipeline restarts (sockets send "reset")
        self.discharged = False
        self._fresh()

    @property
    def id(self) -> str:
        return self.patient["id"]

    @property
    def is_live(self) -> bool:
        return self.patient["device"]["kind"] == "live"

    def _fresh(self) -> None:
        self.pipeline = Pipeline(self.cfg, self.fs, self.id, self.ward.store)
        cal = self.patient.get("calibration")
        self.pipeline.rotation = supine_rotation(np.array(cal["gravity"])) if cal else None
        self.tick = self.pipeline.watch_step
        self.next_idx = self.tick
        self.stream_generation = self.stream.generation
        self.vitals: dict[str, Any] | None = None
        self.vitals_seq = 0
        self.trend: deque[tuple[int, float, float | None]] = deque(
            maxlen=int(self.cfg.live.trend_window_s / self.cfg.signals.rate_interval_s))
        self.event_seq = 0
        self.event_log: deque[tuple[int, dict[str, Any]]] = deque(maxlen=self.cfg.events.recent_max)
        self.updated_t: float | None = None

    # ---- lifecycle
    async def start(self, backfill: bool = False) -> None:
        dev = self.patient["device"]
        if dev["kind"] == "simulator":
            num = int(self.id.lstrip("p"))
            seed = self.cfg.simulator.seed + num
            duration = Simulator(dev["scenario"], self.cfg, seed=seed).duration_s
            history = self.cfg.ward.backfill_s if backfill else 0.0
            if dev.get("start_s") is not None:      # staggered on purpose: be at start_s when the ward appears
                offset = (dev["start_s"] - history) % duration
            elif self.cfg.ward.random_offset:
                offset = self.ward.rng.uniform(0, duration)
            else:
                offset = 0.0
            self.sim = SimulatorSource(self.stream, dev["scenario"], dev.get("speed", 1.0), dev.get("loop", True),
                                       device=f"sim-{self.id}", seed=seed, start_s=offset)
            if history > 0:
                self._backfill(history)
            await self.sim.start()

    def _backfill(self, seconds: float) -> None:
        """Give a simulated bed recent history at once (demo: rates, baseline and trend on the
        ward right after start/reset instead of a blank first minute)."""
        assert self.sim is not None
        sim = self.sim.sim
        period = sim.batch / sim.fs
        n = int(seconds / period)
        now = time.time()
        for k in range(n):
            if sim.done:
                break
            self.stream.handle_packet(sim.next_packet(), addr="simulator",
                                      arrival=now - (n - 1 - k) * period, validated=True)
        self.process_pending(max_ticks=n * 2)
        start = self.stream.sample_time(0)
        if start is not None and (self.patient.get("admitted_t") or now) > start:
            # the history is part of this stay: summaries and the chart start from it
            self.patient = {**self.patient, "admitted_t": start}
            self.ward.store.save_patient(self.patient)

    async def stop(self) -> None:
        if self.sim is not None:
            await self.sim.stop()
            self.sim = None

    def restart_pipeline(self) -> None:
        """Clear runtime state (buffers, pipeline); stored history is kept."""
        self.stream.reset()
        self._fresh()
        self.generation += 1

    # ---- processing
    def process_pending(self, max_ticks: int = 50) -> int:
        """Tick the pipeline at every 1 s boundary of this patient's sample time."""
        if self.stream.generation != self.stream_generation:   # stream reset (live source switch)
            self._fresh()
            self.generation += 1
        buf = self.stream.buffers
        total = buf.total
        if total - self.next_idx > buf.capacity:
            self.next_idx = (total // self.tick) * self.tick
        done = 0
        while total >= self.next_idx and done < max_ticks:
            res = self.pipeline.tick(buf, self.next_idx, self.stream.sample_time)
            for e in res.events:
                self.event_seq += 1
                self.event_log.append((self.event_seq, public_event(e)))
            for a in res.alerts:
                self.ward.log_alert(self.id, a)
            if res.vitals is not None:
                v = res.vitals
                self.vitals_seq += 1
                self.vitals = v
                self.updated_t = v["t"]
                self.trend.append((self.vitals_seq, v["t"], v["rr_fused"]))
            self.next_idx += self.tick
            done += 1
        return done

    def now_t(self) -> float:
        return self.stream.sample_time(self.stream.buffers.total) or time.time()

    def calibrate_supine(self) -> dict[str, Any]:
        """"Calibrate supine" (M10): average gravity over the last few seconds while the patient
        lies flat; posture is then computed relative to it. Refuses moving or missing data."""
        q = self.cfg.quality
        n = int(q.calib_window_s * self.fs)
        acc = self.stream.buffers.window(self.stream.buffers.total, n)[[CH["ax"], CH["ay"], CH["az"]]]
        valid = ~np.isnan(acc).any(axis=0)
        if valid.sum() < 0.9 * n:
            raise WardError(409, f"need {q.calib_window_s:.0f} s of accelerometer data from this patient's "
                                 "device; is it connected?")
        acc = acc[:, valid]
        if float(acc.std(axis=1).max()) > q.calib_max_std_g:
            raise WardError(409, "the patient seems to be moving; lie still and flat, then try again")
        g = acc.mean(axis=1)
        mag = float(np.linalg.norm(g))
        if not q.calib_g_range[0] <= mag <= q.calib_g_range[1]:
            raise WardError(409, f"measured gravity {mag:.2f} g is implausible; check the accelerometer")
        calibration = {"gravity": [round(float(x), 4) for x in g], "samples": int(valid.sum()),
                       "calibrated_t": self.now_t()}
        self.patient = {**self.patient, "calibration": calibration}
        self.ward.store.save_patient(self.patient)
        self.pipeline.rotation = supine_rotation(g)
        block = self.stream.buffers.window(self.stream.buffers.total, n)
        return {"patient_id": self.id, **calibration,
                "posture_now": posture(block, self.cfg, self.pipeline.rotation)}

    def clear_calibration(self) -> dict[str, Any]:
        self.patient = {**self.patient, "calibration": None}
        self.ward.store.save_patient(self.patient)
        self.pipeline.rotation = None
        return self.patient

    def acknowledge(self, alert_id: int, user: dict[str, Any] | None, note: str | None) -> dict[str, Any] | None:
        alert = self.pipeline.alerts.acknowledge(alert_id, user, note, self.now_t())
        if alert is not None:
            self.pipeline.alerts.take_changes()
            self.ward.log_alert(self.id, alert)
        return alert

    # ---- views (AGENTS.md §19.3)
    def trend_since(self, seq: int) -> list[tuple[float, float | None]]:
        return [(t, rr) for s, t, rr in self.trend if s > seq]

    def events_since(self, seq: int) -> list[dict[str, Any]]:
        return [e for s, e in self.event_log if s > seq]

    def recent_events(self, limit: int = 10) -> list[dict[str, Any]]:
        return [e for _, e in self.event_log][-limit:][::-1]

    def device_status(self) -> dict[str, Any]:
        st = self.stream.stats
        dev = self.patient["device"]
        common = {"link_ok": self.stream.link_ok(), "packets": st.packets, "gaps": st.gaps,
                  "missing_packets": st.missing_packets, "last_packet_t": st.last_arrival}
        if dev["kind"] == "live":
            ing = self.ward.ingest
            return {"kind": "live", "device_id": dev["device_id"], "scenario": None,
                    "source": ing.source_spec, "source_status": ing.source.status if ing.source else "stopped",
                    **common}
        return {"kind": "simulator", "device_id": None, "scenario": dev["scenario"],
                "source": f"simulator:{dev['scenario']}", "source_status": self.sim.status if self.sim else "stopped",
                **common}

    def tier(self) -> tuple[int, str | None]:
        return self.pipeline.alerts.tier()

    def summary(self) -> dict[str, Any]:
        tier, reason = self.tier()
        v = self.vitals or {}
        last = self.recent_events(1)
        dev = self.patient["device"]
        return {
            "id": self.id, "name": self.patient["name"], "bed": self.patient["bed"],
            "device_kind": dev["kind"], "scenario": dev.get("scenario"),
            "link_ok": self.stream.link_ok(),
            "tier": tier, "tier_reason": reason,
            "rr": v.get("rr_fused"), "rr_conf": v.get("rr_conf"), "hr": v.get("hr"),
            "posture": v.get("posture"), "quality": v.get("quality"),
            "last_event": ({"type": last[0]["type"], "start_t": last[0]["start_t"],
                            "duration_s": last[0]["duration_s"]} if last else None),
            "unacked_alerts": len(self.pipeline.alerts.active_alerts()),
            "risk_factors": self.patient["risk_factors"],
            "updated_t": self.updated_t,
        }

    def state(self) -> dict[str, Any]:
        tier, reason = self.tier()
        return {
            "patient": self.patient,
            "device": self.device_status(),
            "vitals": self.vitals,
            "tier": tier,
            "tier_reason": reason,
            "baseline": self.pipeline.alerts.baseline(),
            "active_alerts": self.pipeline.alerts.active_alerts(),
            "recent_events": self.recent_events(10),
            "monitors": self.pipeline.monitors(),
        }

    def bedside_command(self) -> dict[str, Any]:
        tier, _ = self.tier()
        return {"cmd": "status", "color": ("green", "yellow", "orange", "red")[tier],
                "buzzer": self.pipeline.alerts.buzzer()}


# --------------------------------------------------------------------------- ward

class Ward:
    def __init__(self, cfg: Config, store: Store, ingest: Ingest, public: bool = False) -> None:
        self.cfg = cfg
        self.store = store
        self.ingest = ingest
        self.public = public          # PUBLIC_DEMO (§20): simulated beds only, capped ward size
        self.runtimes: dict[str, PatientRuntime] = {}
        self.rng = random.Random(cfg.simulator.seed)
        self.alert_seq = 0
        self.alert_log: deque[tuple[int, str, dict[str, Any]]] = deque(maxlen=1000)
        ingest.router = self.route
        ingest.on_source_change = self._live_source_changed

    # ---- lifecycle
    async def start(self) -> None:
        if not self.store.patients():
            await self.seed()
        if self.public:   # a database from a non-public run may still have the live bed
            for p in self.store.patients("admitted"):
                if p["device"]["kind"] == "live":
                    self.store.save_patient({**p, "device": simulated_device(self.cfg)})
                    log.info("public demo: %s (bed %s) now simulated", p["id"], p["bed"])
        for p in self.store.patients("admitted"):
            await self._start_runtime(p, backfill=True)

    async def stop(self) -> None:
        for rt in list(self.runtimes.values()):
            await rt.stop()

    async def seed(self) -> None:
        for rec in self.cfg.ward.seed:
            if self.public and rec["device"]["kind"] == "live":
                rec = {**rec, "device": simulated_device(self.cfg)}
            self._insert(PatientCreate.model_validate(rec))
        log.info("seeded default ward (%d patients)", len(self.cfg.ward.seed))

    async def _start_runtime(self, patient: dict[str, Any], backfill: bool = False) -> PatientRuntime:
        rt = PatientRuntime(self, patient)
        self.runtimes[patient["id"]] = rt
        await rt.start(backfill)
        return rt

    # ---- routing (live wearable)
    def route(self, device_id: str) -> Stream | None:
        for rt in self.runtimes.values():
            if rt.is_live and rt.patient["device"]["device_id"] == device_id:
                return rt.stream
        return None

    def _live_source_changed(self) -> None:
        for rt in self.runtimes.values():
            if rt.is_live:
                rt.stream.reset()

    def live_runtime(self) -> PatientRuntime | None:
        return next((rt for rt in self.runtimes.values()
                     if rt.is_live and rt.patient["device"]["device_id"] == self.cfg.ward.live_device_id), None)

    # ---- alerts feed for the sockets
    def log_alert(self, patient_id: str, alert: dict[str, Any]) -> None:
        self.alert_seq += 1
        self.alert_log.append((self.alert_seq, patient_id, dict(alert)))
        if alert["tier"] >= 1:
            log.info("alert %s (%s) tier %d %s: %s", alert["id"], patient_id, alert["tier"],
                     alert["status"], alert["reason"])

    def alerts_since(self, seq: int, patient_id: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        return [(pid, a) for s, pid, a in self.alert_log if s > seq and (patient_id is None or pid == patient_id)]

    # ---- CRUD
    def _check_conflicts(self, bed: str, device: dict[str, Any], exclude: str | None = None) -> None:
        if self.public and device["kind"] == "live":
            raise WardError(400, "the live wearable is disabled in the public demo; use a simulator scenario")
        for p in self.store.patients("admitted"):
            if p["id"] == exclude:
                continue
            if p["bed"] == bed:
                raise WardError(409, f"bed {bed} is already occupied by {p['id']}")
            if device["kind"] == "live" and p["device"]["kind"] == "live" and p["device"]["device_id"] == device["device_id"]:
                raise WardError(409, f"device {device['device_id']} is already assigned to {p['id']} (bed {p['bed']})")

    def _insert(self, body: PatientCreate) -> dict[str, Any]:
        device = body.device.model_dump()
        self._check_conflicts(body.bed, device)
        if self.public and len(self.store.patients("admitted")) >= self.cfg.public_demo.max_patients:
            raise WardError(429, f"the public demo ward is full ({self.cfg.public_demo.max_patients} patients); "
                                 "discharge someone first")
        patient = {
            "id": self.store.next_patient_id(), "name": body.name, "bed": body.bed,
            "risk_factors": body.risk_factors.model_dump(), "device": device, "notes": body.notes,
            "status": "admitted", "admitted_t": time.time(), "discharged_t": None, "calibration": None,
        }
        self.store.save_patient(patient)
        return patient

    async def create(self, body: PatientCreate) -> dict[str, Any]:
        patient = self._insert(body)
        await self._start_runtime(patient)
        return patient

    def get(self, patient_id: str) -> dict[str, Any]:
        patient = self.store.patient(patient_id)
        if patient is None:
            raise WardError(404, f"no patient {patient_id}")
        return patient

    def runtime(self, patient_id: str) -> PatientRuntime:
        self.get(patient_id)
        rt = self.runtimes.get(patient_id)
        if rt is None:
            raise WardError(400, f"patient {patient_id} is discharged")
        return rt

    async def update(self, patient_id: str, body: PatientUpdate) -> dict[str, Any]:
        patient = self.get(patient_id)
        if patient["status"] != "admitted":
            raise WardError(400, f"patient {patient_id} is discharged")
        changes = body.model_dump(exclude_unset=True)
        if "device" in changes and changes["device"] is not None:
            changes["device"] = body.device.model_dump()  # type: ignore[union-attr]
        if "risk_factors" in changes and changes["risk_factors"] is not None:
            changes["risk_factors"] = body.risk_factors.model_dump()  # type: ignore[union-attr]
        changes = {k: v for k, v in changes.items() if v is not None}
        new = {**patient, **changes}
        if new["device"] != patient["device"]:
            new["calibration"] = None          # a new placement needs a new calibration
        self._check_conflicts(new["bed"], new["device"], exclude=patient_id)
        self.store.save_patient(new)
        rt = self.runtimes[patient_id]
        if new["device"] != patient["device"]:   # new input: new runtime (open sockets send "reset")
            await rt.stop()
            del self.runtimes[patient_id]
            await self._start_runtime(new)
        else:
            rt.patient = new
        return new

    async def discharge(self, patient_id: str) -> dict[str, Any]:
        patient = self.get(patient_id)
        if patient["status"] == "discharged":
            return patient
        patient = {**patient, "status": "discharged", "discharged_t": time.time()}
        self.store.save_patient(patient)
        rt = self.runtimes.pop(patient_id, None)
        if rt is not None:
            rt.patient = patient
            rt.discharged = True
            await rt.stop()
        return patient

    # ---- views
    def state_for(self, patient_id: str) -> dict[str, Any]:
        """PatientState; for a discharged patient, built from stored history."""
        patient = self.get(patient_id)
        rt = self.runtimes.get(patient_id)
        if rt is not None:
            return rt.state()
        vitals = self.store.vitals(patient_id, limit=1)
        dev = patient["device"]
        return {
            "patient": patient,
            "device": {"kind": dev["kind"], "device_id": dev.get("device_id"), "scenario": dev.get("scenario"),
                       "link_ok": False, "source": None, "source_status": "discharged", "packets": 0,
                       "gaps": 0, "missing_packets": 0, "last_packet_t": None},
            "vitals": vitals[0] if vitals else None, "tier": 0, "tier_reason": None, "baseline": None,
            "active_alerts": [], "recent_events": self.store.events(patient_id, limit=10),
            "monitors": {"smart": {"alarms": 0, "per_hour": 0.0}, "naive": {"alarms": 0, "per_hour": 0.0},
                         "since_t": patient.get("admitted_t")},
        }

    def _discharged_summary(self, p: dict[str, Any]) -> dict[str, Any]:
        events = self.store.events(p["id"], limit=1)
        e = events[0] if events else None
        return {
            "id": p["id"], "name": p["name"], "bed": p["bed"], "device_kind": p["device"]["kind"],
            "scenario": p["device"].get("scenario"), "link_ok": False, "tier": 0, "tier_reason": None,
            "rr": None, "rr_conf": None, "hr": None, "posture": None, "quality": None,
            "last_event": {"type": e["type"], "start_t": e["start_t"], "duration_s": e["duration_s"]} if e else None,
            "unacked_alerts": 0, "risk_factors": p["risk_factors"], "updated_t": p.get("discharged_t"),
        }

    def listing(self, status: str = "admitted") -> dict[str, Any]:
        if status not in ("admitted", "discharged", "all"):
            raise WardError(400, "status must be admitted, discharged or all")
        rows = []
        for p in self.store.patients(None if status == "all" else status):
            rt = self.runtimes.get(p["id"])
            rows.append(rt.summary() if rt is not None else self._discharged_summary(p))
        rows.sort(key=lambda r: (-r["tier"], -r["unacked_alerts"], bed_key(r["bed"])))
        counts = {str(t): sum(1 for r in rows if r["tier"] == t) for t in range(4)}
        return {"patients": rows, "counts": counts, "generated_t": time.time()}

    def process_all(self) -> None:
        for rt in list(self.runtimes.values()):
            try:
                rt.process_pending()
            except Exception:  # one patient's bad window must not stop the others
                log.exception("processing failed for %s", rt.id)

    async def reset(self, reseed: bool = False) -> None:
        """Demo reset: clear runtime state and stored vitals/events/alerts; restart simulators.
        With reseed, delete every patient and recreate the default ward."""
        await self.stop()
        self.runtimes.clear()
        if reseed:
            self.store.delete_all_patients()
            await self.seed()
        else:
            for p in self.store.patients():
                self.store.clear_patient(p["id"])
        self.alert_log.clear()
        for p in self.store.patients("admitted"):
            await self._start_runtime(p, backfill=True)   # new runtime objects: open sockets send "reset"
