"""Milestone 6.5: the clinical app shell backend (AGENTS.md §19) — auth, patients, ward,
per-patient endpoints, WebSockets, and the documented JSON shapes."""

import socket
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.app import create_app
from server.config import load_config
from server.ingest import Ingest
from server.patients import PatientCreate, Ward
from server.schema import encode_packet
from server.simulator import Simulator
from server.store import Store
from tests.conftest import with_overrides

# ---- §19.3 key sets (the contract the frontend is built against)
USER = {"username", "display_name", "role"}
PATIENT = {"id", "name", "bed", "risk_factors", "device", "notes", "status", "admitted_t", "discharged_t",
           "calibration"}
RISK = {"opioids", "supplemental_o2", "osa", "age_over_65"}
DEVICE_STATUS = {"kind", "device_id", "scenario", "link_ok", "source", "source_status", "packets", "gaps",
                 "missing_packets", "last_packet_t"}
VITALS = {"t", "rr_fused", "rr_conf", "rr_sources", "flags", "rr_belt", "rr_accel", "rr_airflow", "conf_belt",
          "conf_accel", "conf_airflow", "rr_prelim", "hr", "hr_conf", "quality", "quality_conf", "posture", "motion"}
EVENT = {"id", "patient_id", "type", "start_t", "end_t", "duration_s", "confidence", "posture", "airflow_pct",
         "effort_pct"}
ALERT = {"id", "patient_id", "kind", "tier", "reason", "status", "raised_t", "updated_t", "cleared_t", "escalated",
         "escalated_t", "acknowledged_by", "acknowledged_t", "ack_note"}
MONITORS = {"smart", "naive", "since_t"}
SUMMARY = {"id", "name", "bed", "device_kind", "scenario", "link_ok", "tier", "tier_reason", "rr", "rr_conf", "hr",
           "posture", "quality", "last_event", "unacked_alerts", "risk_factors", "updated_t"}
STATE = {"patient", "device", "vitals", "tier", "tier_reason", "baseline", "active_alerts", "recent_events",
         "monitors"}
ADMIN_DEVICE = {"source", "source_status", "link_ok", "stats", "assigned_patient_id", "unassigned_packets",
                "network", "recording"}

RF = {"opioids": True, "supplemental_o2": False, "osa": "none", "age_over_65": False}


def new_patient(bed="9", scenario="normal", speed=20.0, **kw):
    return {"name": "Test Patient", "bed": bed, "risk_factors": RF,
            "device": {"kind": "simulator", "scenario": scenario, "speed": speed, "loop": True}, **kw}


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.05)


@pytest.fixture
def client(test_cfg, tmp_path):
    cfg = with_overrides(test_cfg, record={"dir": str(tmp_path / "rec"), "labels": test_cfg.record.labels})
    app = create_app(cfg, source="udp")
    with TestClient(app) as c:
        c.app_state = app.state.bw
        yield c


@pytest.fixture
def secure(auth_cfg):
    app = create_app(auth_cfg, source="udp")
    with TestClient(app) as c:
        yield c


def sign_in(c, role):
    r = c.post("/api/auth/demo", json={"role": role})
    assert r.status_code == 200
    return r.json()["user"]


# ---------------------------------------------------------------- auth

def test_unauthenticated_requests_get_401(secure):
    for method, path in [("GET", "/api/patients"), ("GET", "/api/patients/p1/state"), ("GET", "/api/auth/me"),
                         ("GET", "/api/state"), ("POST", "/api/patients/p1/alerts/1/ack")]:
        r = secure.request(method, path)
        assert r.status_code == 401, path
        assert r.json() == {"detail": "not signed in"}


def test_login_logout_me(secure):
    assert secure.post("/api/auth/login", json={"username": "nurse", "password": "nope"}).status_code == 401
    r = secure.post("/api/auth/login", json={"username": "nurse", "password": "demo"})
    assert r.status_code == 200 and set(r.json()["user"]) == USER
    cookie = r.headers["set-cookie"]
    assert "bw_session=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert secure.get("/api/auth/me").json()["user"]["role"] == "nurse"
    assert secure.post("/api/auth/logout").status_code == 204
    assert secure.get("/api/auth/me").status_code == 401
    assert secure.post("/api/auth/logout").status_code == 204      # works when signed out too


def test_demo_accounts_never_expose_passwords(secure):
    accounts = secure.get("/api/auth/demo-accounts").json()
    assert [a["role"] for a in accounts] == ["nurse", "physician", "admin"]
    assert all(set(a) == {"role", "display_name"} for a in accounts)
    assert secure.post("/api/auth/demo", json={"role": "janitor"}).status_code == 400


def test_role_permissions(secure):
    sign_in(secure, "physician")
    assert secure.get("/api/patients").status_code == 200
    assert secure.post("/api/patients", json=new_patient()).status_code == 403
    assert secure.post("/api/patients/p1/discharge").status_code == 403
    assert secure.get("/api/admin/device").status_code == 403
    sign_in(secure, "nurse")
    assert secure.post("/api/patients", json=new_patient()).status_code == 201
    for method, path in [("GET", "/api/admin/device"), ("POST", "/api/demo/reset"), ("GET", "/api/serial/ports"),
                         ("POST", "/api/record/stop")]:
        assert secure.request(method, path).status_code == 403, path
    assert secure.post("/api/source", json={"source": "udp"}).status_code == 403
    sign_in(secure, "admin")
    assert secure.get("/api/admin/device").status_code == 200


def test_websocket_requires_login(secure):
    for path in ("/ws/ward", "/ws/patients/p1", "/ws/live"):
        with secure.websocket_connect(path) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4401, path
    sign_in(secure, "nurse")
    with secure.websocket_connect("/ws/ward") as ws:
        assert ws.receive_json()["type"] == "ward"


# ---------------------------------------------------------------- ward and patients

def test_default_ward_seeded(client):
    body = client.get("/api/patients").json()
    assert set(body) == {"patients", "counts", "generated_t"}
    assert set(body["counts"]) == {"0", "1", "2", "3"} and sum(body["counts"].values()) == 6
    rows = body["patients"]
    assert all(set(r) == SUMMARY for r in rows)
    by_bed = {r["bed"]: r for r in rows}
    assert set(by_bed) == {"1", "2", "3", "4", "5", "6"}
    assert by_bed["4"]["id"] == "p4" and by_bed["4"]["device_kind"] == "live"
    assert {by_bed[b]["scenario"] for b in "12356"} == {"normal", "motion", "obstructive", "opioid_decline", "central"}
    assert all(set(r["risk_factors"]) == RISK for r in rows)
    keys = [(-r["tier"], -r["unacked_alerts"], int(r["bed"])) for r in rows]
    assert keys == sorted(keys)


def test_patient_crud_and_conflicts(client):
    r = client.post("/api/patients", json=new_patient(notes="hi"))
    assert r.status_code == 201
    p = r.json()
    assert set(p) == PATIENT and p["id"] == "p7" and p["status"] == "admitted" and p["notes"] == "hi"
    assert p["device"] == {"kind": "simulator", "scenario": "normal", "speed": 20.0, "loop": True, "start_s": None}
    assert client.get("/api/patients/p7").json() == p
    # conflicts
    assert client.post("/api/patients", json=new_patient(bed="9")).status_code == 409
    live = new_patient(bed="10")
    live["device"] = {"kind": "live", "device_id": "patch-01"}
    r = client.post("/api/patients", json=live)
    assert r.status_code == 409 and "patch-01" in r.json()["detail"]
    assert client.post("/api/patients", json={"name": "x"}).status_code == 422
    bad = new_patient(bed="11")
    bad["device"]["scenario"] = "nope"
    assert client.post("/api/patients", json=bad).status_code == 422
    assert client.get("/api/patients/p99").status_code == 404
    # update
    r = client.patch("/api/patients/p7", json={"name": "Renamed", "risk_factors": {**RF, "osa": "known"}})
    assert r.status_code == 200 and r.json()["name"] == "Renamed" and r.json()["risk_factors"]["osa"] == "known"
    assert client.patch("/api/patients/p7", json={"bed": "1"}).status_code == 409
    # discharge (idempotent), then PATCH is refused
    r = client.post("/api/patients/p7/discharge")
    assert r.status_code == 200 and r.json()["status"] == "discharged" and r.json()["discharged_t"]
    assert client.post("/api/patients/p7/discharge").json()["status"] == "discharged"
    assert client.patch("/api/patients/p7", json={"name": "x"}).status_code == 400
    assert "p7" not in [x["id"] for x in client.get("/api/patients").json()["patients"]]
    assert "p7" in [x["id"] for x in client.get("/api/patients?status=discharged").json()["patients"]]
    assert len(client.get("/api/patients?status=all").json()["patients"]) == 7
    assert set(client.get("/api/patients/p7/state").json()) == STATE
    # the bed is free again
    assert client.post("/api/patients", json=new_patient(bed="9")).status_code == 201


def test_patient_state_vitals_events_alerts_shapes(client):
    pid = client.post("/api/patients", json=new_patient(bed="12", scenario="central", speed=50)).json()["id"]
    rt = client.app_state.ward.runtimes[pid]
    wait_for(lambda: rt.vitals is not None and rt.event_seq >= 1, timeout=30)
    state = client.get(f"/api/patients/{pid}/state").json()
    assert set(state) == STATE
    assert set(state["device"]) == DEVICE_STATUS and state["device"]["kind"] == "simulator"
    assert VITALS <= set(state["vitals"])
    assert set(state["monitors"]) == MONITORS
    vitals = client.get(f"/api/patients/{pid}/vitals").json()["vitals"]
    assert vitals and VITALS <= set(vitals[0]) and vitals[0]["t"] <= vitals[-1]["t"]   # oldest first
    events = client.get(f"/api/patients/{pid}/events").json()["events"]
    assert events and all(set(e) == EVENT for e in events) and events[0]["type"] == "central"
    wait_for(lambda: rt.pipeline.alerts.active_alerts(), timeout=30)
    alerts = client.get(f"/api/patients/{pid}/alerts?status=unacked").json()["alerts"]
    assert alerts and all(set(a) == ALERT for a in alerts)
    a = alerts[0]
    r = client.post(f"/api/patients/{pid}/alerts/{a['id']}/ack", json={"note": "seen"})
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"
    assert set(r.json()["acknowledged_by"]) == USER and r.json()["ack_note"] == "seen"
    assert client.post(f"/api/patients/p1/alerts/{a['id']}/ack").status_code == 404   # other patient
    assert client.post(f"/api/patients/{pid}/alerts/999999/ack").status_code == 404


def test_device_change_restarts_pipeline(client):
    pid = client.post("/api/patients", json=new_patient(bed="13", speed=50)).json()["id"]
    wait_for(lambda: client.app_state.ward.runtimes[pid].vitals is not None)
    r = client.patch(f"/api/patients/{pid}", json={"device": {"kind": "simulator", "scenario": "motion"}})
    assert r.status_code == 200 and r.json()["device"]["scenario"] == "motion"
    rt = client.app_state.ward.runtimes[pid]
    assert rt.patient["device"]["scenario"] == "motion" and rt.vitals is None


# ---------------------------------------------------------------- live wearable routing

def test_live_packets_routed_to_assigned_patient(client):
    port = client.app_state.ingest.source.port
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cfg = client.app_state.cfg
    for p in Simulator("normal", cfg, device="patch-01").packets(max_packets=4):
        tx.sendto(encode_packet(p), ("127.0.0.1", port))
    for p in Simulator("normal", cfg, device="patch-99").packets(max_packets=3):
        tx.sendto(encode_packet(p), ("127.0.0.1", port))
    tx.close()
    live = client.app_state.ward.runtimes["p4"]
    wait_for(lambda: live.stream.stats.packets == 4 and client.app_state.ingest.unassigned_packets == 3)
    dev = client.get("/api/admin/device").json()
    assert set(dev) == ADMIN_DEVICE and dev["assigned_patient_id"] == "p4" and dev["unassigned_packets"] == 3
    status = client.get("/api/patients/p4/state").json()["device"]
    assert status["kind"] == "live" and status["device_id"] == "patch-01" and status["packets"] == 4
    assert status["link_ok"] is True


def test_record_mode_writes_csv(client):
    r = client.post("/api/record/start", json={"label": "motion"})
    assert r.status_code == 200 and r.json()["recording"]["label"] == "motion"
    assert client.post("/api/record/start", json={"label": "bogus"}).status_code == 400
    client.post("/api/record/start", json={"label": "clean"})
    port = client.app_state.ingest.source.port
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for p in Simulator("normal", client.app_state.cfg).packets(max_packets=4):
        tx.sendto(encode_packet(p), ("127.0.0.1", port))
    tx.close()
    wait_for(lambda: client.app_state.recorder.samples == 100)
    r = client.post("/api/record/stop").json()
    assert r["recording"] is None and r["file"].endswith("_clean.csv")
    from server.config import ROOT
    lines = (ROOT / r["file"]).read_text().splitlines()   # absolute paths join as themselves
    assert lines[0].startswith("device,seq,k,t0_ms,pressure") and len(lines) == 101 and lines[1].endswith(",clean")


def test_demo_reset_and_reseed(client):
    client.post("/api/patients", json=new_patient(bed="14"))
    body = client.post("/api/demo/reset", json={"reseed_ward": False}).json()
    assert len(body["patients"]) == 7 and set(body) == {"patients", "counts", "generated_t"}
    body = client.post("/api/demo/reset", json={"reseed_ward": True}).json()
    assert sorted(p["bed"] for p in body["patients"]) == ["1", "2", "3", "4", "5", "6"]


def test_legacy_aliases_follow_live_patient(client):
    s = client.get("/api/state").json()
    assert s["patient"] == "Bed 4" and "ingest" in s and "tier" in s
    assert client.get("/api/alerts").json() == {"alerts": []}
    assert client.get("/api/events").json() == {"events": []}


# ---------------------------------------------------------------- websockets

def test_patient_socket_messages(client):
    pid = client.post("/api/patients", json=new_patient(bed="15", speed=20)).json()["id"]
    with client.websocket_connect(f"/ws/patients/{pid}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert set(hello) == {"type", "patient", "state", "trend", "window_s", "trend_window_s"}
        assert set(hello["patient"]) == PATIENT and set(hello["state"]) == STATE
        seen = {}
        deadline = time.monotonic() + 15
        while {"wave", "status", "vitals"} - set(seen) and time.monotonic() < deadline:
            m = ws.receive_json()
            seen.setdefault(m["type"], m)
        w, st, v = seen["wave"], seen["status"], seen["vitals"]
        assert set(w) == {"type", "start", "step", "fs", "end", "t_end", "effort", "airflow", "pulse"}
        assert set(st) == {"type", "device", "tier", "monitors"} and set(st["device"]) == DEVICE_STATUS
        assert set(v) == {"type", "vitals", "trend"} and VITALS <= set(v["vitals"])
        client.post(f"/api/patients/{pid}/discharge")
        while True:
            m = ws.receive_json()
            if m["type"] == "discharged":
                assert m["patient"]["status"] == "discharged"
                break
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1000


def test_unknown_patient_socket_closes_4404(client):
    with client.websocket_connect("/ws/patients/p404") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4404


def test_ward_socket_summaries_and_alert_push(client):
    with client.websocket_connect("/ws/ward") as ws:
        first = ws.receive_json()
        assert first["type"] == "ward" and set(first) == {"type", "patients", "counts", "generated_t"}
        rt = client.app_state.ward.runtimes["p1"]
        eng = rt.pipeline.alerts
        eng.start_t = 0.0
        eng._raise("rr_low", 2, "Breathing rate 7/min for 65 s (baseline 15/min), signal confidence high", 100.0)
        for a in eng.take_changes():
            client.app_state.ward.log_alert(rt.id, a)
        msg = ws.receive_json()
        while msg["type"] != "alert":
            msg = ws.receive_json()
        assert msg["patient_id"] == "p1" and set(msg["alert"]) == ALERT and msg["alert"]["tier"] == 2
        while True:                       # the next ward push shows the new tier, sorted first
            m = ws.receive_json()
            if m["type"] == "ward":
                break
        assert m["patients"][0]["id"] == "p1" and m["patients"][0]["tier"] == 2 and m["counts"]["2"] >= 1


# ---------------------------------------------------------------- design rule 8

def test_risk_factors_never_change_alerts(cfg):
    """Same data, opposite risk factors -> identical alerts (design rule 8)."""
    cfg = with_overrides(cfg, store={"path": ":memory:"}, ward={"seed": [], "random_offset": False})
    ward = Ward(cfg, Store(":memory:"), Ingest(cfg))
    low = {"opioids": False, "supplemental_o2": False, "osa": "none", "age_over_65": False}
    high = {"opioids": True, "supplemental_o2": True, "osa": "known", "age_over_65": True}
    rts = []
    for bed, rf, dev in (("1", low, "dev-a"), ("2", high, "dev-b")):
        patient = ward._insert(PatientCreate.model_validate(
            {"name": "x", "bed": bed, "risk_factors": rf, "device": {"kind": "live", "device_id": dev}}))
        from server.patients import PatientRuntime
        rt = PatientRuntime(ward, patient)
        ward.runtimes[patient["id"]] = rt
        rts.append(rt)
    for i, p in enumerate(Simulator("demo", cfg).packets()):
        for rt in rts:
            rt.stream.handle_packet(p, arrival=1000 + 0.5 * i, validated=True)
            rt.process_pending()
    strip = lambda alerts: [(a["kind"], a["tier"], a["status"], a["reason"], a["raised_t"]) for a in alerts]
    a0, a1 = (strip(rt.pipeline.alerts.alerts.values()) for rt in rts)
    assert a0 and a0 == a1
    assert any(t == 3 for _, t, *_ in a0)


def test_backfill_gives_seeded_beds_rates_at_start(test_cfg):
    cfg = with_overrides(test_cfg, ward={"backfill_s": 90})
    app = create_app(cfg, source="udp")
    with TestClient(app) as c:
        rows = {p["bed"]: p for p in c.get("/api/patients").json()["patients"]}
        assert rows["1"]["rr"] is not None and 12 <= rows["1"]["rr"] <= 18   # simulated, normal
        assert rows["4"]["rr"] is None                                       # live: no fake history
        assert len(c.get("/api/patients/p1/vitals").json()["vitals"]) >= 17
