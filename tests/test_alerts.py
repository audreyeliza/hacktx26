"""Milestone 6: alert rules, naive monitor comparison, storage, acknowledge, bedside command."""

import asyncio
import os
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from server.alerts import AlertEngine
from server.app import create_app
from server.ingest import Ingest
from server.schema import encode_packet
from server.simulator import Simulator
from server.store import Store
from tests.conftest import with_overrides
from tests.helpers import run_pipeline_full

STEP = 5.0


# ---------------------------------------------------------------- demo acceptance

@pytest.fixture(scope="module")
def demo(cfg):
    return run_pipeline_full(cfg, "demo")


def phase_bounds(sim, label):
    segs = [q for q in sim.labels()["quality"] if q["label"] == label]
    return segs[0]["start_s"], segs[-1]["end_s"]


def test_demo_motion_phase_smart_far_fewer_than_naive(demo):
    _, _, alerts, naive, sim, _ = demo
    lo, hi = phase_bounds(sim, "motion")
    hi += 30  # artifacts can still be in the 60 s window just after the last burst
    smart = [a for a in alerts if a["tier"] >= 1 and lo <= a["changed_s"] <= hi]
    naive_here = [k for t, k in naive if lo <= t <= hi]
    assert smart == []
    assert len(naive_here) >= 5


def test_demo_reaches_tier3_on_decline(demo):
    _, _, alerts, _, sim, _ = demo
    decline_end = next(e for e in sim.labels()["events"])["start_s"]  # first apnea comes after recovery
    tier3 = [a for a in alerts if a["kind"] == "rr_low" and a["tier"] == 3 and a["changed_s"] < decline_end]
    assert tier3, "decline to 5/min must reach tier 3"
    assert "Breathing rate" in tier3[0]["reason"] and "baseline" in tier3[0]["reason"]
    assert any(a["kind"] == "rr_trend" and a["tier"] == 2 for a in alerts)


def test_demo_reaches_tier3_on_long_apnea(demo):
    _, _, alerts, _, sim, _ = demo
    long = next(t for t in sim.labels()["events"] if t["duration_s"] >= 20)
    tier3 = [a for a in alerts if a["kind"] == "apnea" and a["tier"] == 3
             and long["start_s"] <= a["changed_s"] <= long["end_s"] + 15]
    assert tier3
    # raised while the apnea was still in progress or right after it ended, not 12 s+ later
    assert tier3[0]["changed_s"] <= long["start_s"] + 30


def test_demo_smart_total_well_below_naive(demo):
    *_, pipe = demo
    counts = pipe.monitors()
    assert counts["smart"]["alarms"] < counts["naive"]["alarms"] / 2
    assert counts["naive"]["per_hour"] > counts["smart"]["per_hour"]


def test_every_alert_has_a_reason(demo):
    _, _, alerts, *_ = demo
    assert alerts and all(isinstance(a["reason"], str) and len(a["reason"]) > 10 for a in alerts)


@pytest.mark.parametrize("scenario", ["normal", "motion"])
def test_no_smart_alarms_on_normal_or_motion(cfg, scenario):
    _, _, alerts, _, _, pipe = run_pipeline_full(cfg, scenario)
    assert [a for a in alerts if a["tier"] >= 1] == []


def test_sensor_off_raises_check_sensor_tier1(cfg):
    _, _, alerts, *_ = run_pipeline_full(cfg, "sensor_off")
    tiers = {a["tier"] for a in alerts if a["kind"] == "sensor_check"}
    assert tiers == {1}
    assert all(a["kind"] == "sensor_check" for a in alerts if a["tier"] >= 1)


# ---------------------------------------------------------------- rule units

def vit(rr=15.0, quality="clean", qconf=1.0, rr_conf=0.95, flags=None, channels=None):
    return {"rr_fused": rr, "rr_conf": rr_conf, "quality": quality, "quality_conf": qconf,
            "flags": flags or [], "channels": channels or {"pressure": "ok", "therm": "ok", "pulse": "ok"},
            "posture": "supine"}


class Clock:
    """Drives an AlertEngine with a vitals sequence, 5 s per step."""

    def __init__(self, cfg, baseline_rr=15.0, baseline_s=300):
        self.e = AlertEngine(cfg, "p-test")
        self.t = 0.0
        self.changes = []
        self.run(vit(baseline_rr), baseline_s)

    def run(self, v, seconds, ongoing=None, events=()):
        for _ in range(int(seconds / STEP)):
            self.t += STEP
            self.changes += self.e.update(v, ongoing, list(events), self.t)
            events = ()
        return self

    def raised(self, kind=None, min_tier=1):
        return [a for a in self.changes if a["tier"] >= min_tier and (kind is None or a["kind"] == kind)]


def rr_low_tier(c):
    aid = c.e.active.get("rr_low")
    return c.e.alerts[aid]["tier"] if aid else 0


def test_rr_low_persistence_and_tiers(cfg):
    c = Clock(cfg).run(vit(9.0), 55)
    assert c.raised("rr_low") == []
    c.run(vit(9.0), 10)
    assert rr_low_tier(c) == 1
    assert c.raised("rr_trend")      # 9 vs baseline 15 is also a >= 30 % drop (tier 2, separate kind)
    c.run(vit(5.0), 40)
    assert rr_low_tier(c) == 3 and c.e.tier()[0] == 3
    reason = c.e.tier()[1]
    assert reason.startswith("Breathing rate 5/min") and "baseline 15/min" in reason


def test_low_quality_never_raises_above_tier0(cfg):
    c = Clock(cfg)
    c.run(vit(4.0, quality="motion", qconf=0.9), 300)
    assert c.raised(min_tier=1) == []
    logs = [a for a in c.changes if a["tier"] == 0]
    assert 1 <= len(logs) <= 300 / cfg.alerts.cooldown_s + 1
    assert all(a["status"] == "resolved" and "Not alarmed" in a["reason"] for a in logs)
    c.run(vit(4.0, rr_conf=0.4, flags=["sources_disagree"]), 300)
    assert c.raised(min_tier=1) == []


def test_single_source_still_alarms(cfg):
    """Decided rule: one clean source, confidence capped at 0.6, still meets the gate."""
    c = Clock(cfg).run(vit(5.0, rr_conf=0.6, flags=["single_source"]), 70)
    assert c.e.tier()[0] == 3
    assert "single sensor" in c.e.tier()[1]


def test_quality_dip_resets_persistence(cfg):
    c = Clock(cfg)
    for _ in range(4):
        c.run(vit(9.0), 40).run(vit(9.0, quality="motion"), 5)
    assert c.raised("rr_low") == []


def test_cooldown_and_tier_increase(cfg):
    a = cfg.alerts
    c = Clock(cfg, baseline_rr=11.0).run(vit(9.0), 65)      # tier 1 (non-latching; 18 % drop: no trend)
    first = c.raised("rr_low")[0]
    c.run(vit(11.0), a.clear_s + 5)                         # clears -> resolved
    assert c.e.alerts[first["id"]]["status"] == "resolved"
    n = len([x for x in c.changes if x["status"] == "active"])
    c.run(vit(9.0), 65)                                     # same tier within cooldown: suppressed
    assert len([x for x in c.changes if x["status"] == "active"]) == n
    c.run(vit(7.0), 65)                                     # tier increases: new alert
    assert rr_low_tier(c) == 2
    assert c.e.active["rr_low"] != first["id"]


def test_latching_ack_and_escalation(cfg):
    a = cfg.alerts
    c = Clock(cfg).run(vit(5.0), 35)
    alert = c.e.active_alerts()[0]
    assert alert["tier"] == 3 and c.e.buzzer()
    c.run(vit(15.0), 30)                                    # condition clears, but tier 3 latches
    assert c.e.alerts[alert["id"]]["status"] == "active"
    c.run(vit(15.0), a.escalate_s)
    assert c.e.alerts[alert["id"]]["escalated"] is True
    acked = c.e.acknowledge(alert["id"], {"username": "n", "display_name": "N", "role": "nurse"}, "roused", c.t)
    assert acked["status"] == "acknowledged" and acked["acknowledged_by"]["username"] == "n"
    assert acked["ack_note"] == "roused" and not c.e.buzzer() and "rr_low" not in c.e.active
    assert c.e.tier()[0] == 2           # the separate declining-trend alert is still latched
    assert c.e.acknowledge(alert["id"], None, None, c.t + 5) == acked   # idempotent


def test_apnea_rules(cfg):
    c = Clock(cfg)
    ongoing = {"type": "obstructive", "apnea": True, "start_t": c.t + 5, "duration_s": 21.0,
               "confidence": 0.9, "posture": "supine"}
    c.run(vit(), 5, ongoing=ongoing)
    assert c.e.tier()[0] == 3 and "in progress" in c.e.tier()[1]
    n_raised = c.e.smart_alarms
    final = {"type": "obstructive", "start_t": ongoing["start_t"], "end_t": ongoing["start_t"] + 24,
             "duration_s": 24.0, "posture": "supine"}
    c.run(vit(), 5, events=[final])
    assert c.e.smart_alarms == n_raised               # same apnea: reworded, not a second alarm
    assert c.e.tier()[1].startswith("Apnea 24 s")


def test_short_apnea_tier1_and_cluster_tier2(cfg):
    c = Clock(cfg)
    for i in range(5):
        ev = {"type": "central", "start_t": c.t, "end_t": c.t + 12, "duration_s": 12.0, "posture": "left"}
        c.run(vit(), 60, events=[ev])
    assert c.raised("apnea")[0]["tier"] == 1
    cluster = c.raised("apnea_cluster")
    assert cluster and cluster[0]["tier"] == 2 and "5 apneas" in cluster[0]["reason"]


def test_hypopnea_does_not_alarm(cfg):
    c = Clock(cfg)
    ev = {"type": "hypopnea", "start_t": c.t, "end_t": c.t + 30, "duration_s": 30.0, "posture": "supine"}
    c.run(vit(), 10, events=[ev])
    assert c.raised() == []


def test_sensor_check(cfg):
    off = vit(None, quality="sensor_off", qconf=0.9, channels={"pressure": "ok", "therm": "flat", "pulse": "ok"})
    c = Clock(cfg).run(off, 55)
    assert c.raised() == []
    c.run(off, 15)
    assert c.e.tier() == (1, c.e.tier()[1]) and "therm flat" in c.e.tier()[1]
    c.run(vit(), cfg.alerts.clear_s + 5)
    assert c.e.tier()[0] == 0


def test_baseline_from_first_10_min(cfg):
    c = Clock(cfg, baseline_rr=16.0, baseline_s=600)
    c.run(vit(12.0), 600)
    assert c.e.baseline_rr == 16.0


# ---------------------------------------------------------------- storage

def test_store_round_trip(tmp_path):
    st = Store(tmp_path / "bw.db")
    st.add_vitals("p1", {"t": 1.0, "rr_fused": 14.0, "rr_conf": 0.9, "hr": 70, "quality": "clean",
                         "quality_conf": 1.0, "posture": "supine", "flags": []})
    eid = st.add_event({"id": None, "patient_id": "p1", "type": "central", "start_t": 1.0, "end_t": 13.0,
                        "duration_s": 12.0, "confidence": 0.9, "posture": "supine", "airflow_pct": 4.0,
                        "effort_pct": 5.0})
    a = {"id": None, "patient_id": "p1", "kind": "apnea", "tier": 1, "reason": "r", "status": "active",
         "raised_t": 13.0, "updated_t": 13.0, "escalated": False, "escalated_t": None,
         "acknowledged_by": None, "acknowledged_t": None, "ack_note": None}
    aid = st.save_alert(a)
    a["status"] = "acknowledged"
    st.save_alert(a)
    st.close()
    st = Store(tmp_path / "bw.db")   # persisted across reopen
    assert st.vitals("p1")[0]["rr_fused"] == 14.0
    assert st.events("p1")[0]["id"] == eid
    assert st.alert(aid)["status"] == "acknowledged"
    assert st.alerts("p1", status="active") == []
    st.clear_patient("p1")
    assert st.alerts("p1") == [] and st.vitals("p1") == []


# ---------------------------------------------------------------- app: alerts API + bedside

def test_alerts_api_and_ack(test_cfg):
    app = create_app(test_cfg, source="udp")
    with TestClient(app) as c:
        state = app.state.bw
        eng = state.pipeline.alerts
        eng.start_t = 0.0
        eng._raise("apnea", 3, "Apnea 25 s (obstructive, supine)", 100.0)
        for a in eng.take_changes():
            state._log_alert(a)
        aid = eng.active["apnea"]
        s = c.get("/api/state").json()
        assert s["tier"] == 3 and s["bedside"] == {"cmd": "status", "color": "red", "buzzer": True}
        assert c.get("/api/alerts?status=active").json()["alerts"][0]["id"] == aid
        r = c.post(f"/api/alerts/{aid}/ack", json={"note": "checked"})
        assert r.status_code == 200 and r.json()["status"] == "acknowledged"
        assert r.json()["acknowledged_by"]["role"] == "nurse" and r.json()["ack_note"] == "checked"
        assert c.get("/api/state").json()["bedside"]["buzzer"] is False
        assert c.post("/api/alerts/99999/ack").status_code == 404


def test_bedside_command_over_udp(test_cfg):
    """The Pico's address is learned from its packets; commands go to its command port."""
    pico_cmd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pico_cmd.bind(("127.0.0.1", 0))
    pico_cmd.settimeout(5)
    cfg = with_overrides(test_cfg, network={"udp_port": 0, "command_port": pico_cmd.getsockname()[1]},
                         bedside={"command_interval_s": 0.2})
    app = create_app(cfg, source="udp")
    try:
        with TestClient(app):
            port = app.state.bw.ingest.source.port
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tx.sendto(encode_packet(Simulator("normal", cfg).next_packet()), ("127.0.0.1", port))
            data, _ = pico_cmd.recvfrom(1024)
            tx.close()
        import json
        assert json.loads(data) == {"cmd": "status", "color": "green", "buzzer": False}
    finally:
        pico_cmd.close()


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pseudo-terminal")
def test_bedside_command_over_serial(test_cfg):
    master, slave = os.openpty()
    try:
        async def run():
            ing = Ingest(test_cfg)
            await ing.set_source(f"serial:{os.ttyname(slave)}")
            end = time.monotonic() + 5
            while not ing.source.status.startswith("connected"):
                assert time.monotonic() < end
                await asyncio.sleep(0.02)
            assert ing.send_command({"cmd": "status", "color": "red", "buzzer": True})
            await asyncio.sleep(0.5)
            await ing.stop()

        asyncio.run(run())
        out = os.read(master, 4096)
        assert b'{"cmd": "status", "color": "red", "buzzer": true}\n' in out
    finally:
        os.close(master)
        os.close(slave)
