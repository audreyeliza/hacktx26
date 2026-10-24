"""M10 "calibrate supine": posture relative to the gravity vector recorded while lying flat."""

import socket
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.quality import posture, supine_rotation
from server.schema import encode_packet
from server.simulator import Simulator
from tests.test_quality import synthetic_block
from tests.test_ward import wait_for

TILT = np.radians(60)     # patch taped on at 60° about the x axis
R_TILT = np.array([[1, 0, 0], [0, np.cos(TILT), -np.sin(TILT)], [0, np.sin(TILT), np.cos(TILT)]])


def tilted_packets(cfg, n, start_seq=0, motion=False):
    sim = Simulator("motion" if motion else "normal", cfg, device="patch-01")
    if motion:   # jump to the first motion burst
        burst = next(q for q in sim.labels()["quality"] if q["label"] == "motion")
        sim.n = int(burst["start_s"] * sim.fs) // sim.batch * sim.batch
    out = []
    for p in sim.packets(max_packets=n):
        acc = R_TILT @ np.array([p["ax"], p["ay"], p["az"]])
        p["ax"], p["ay"], p["az"] = ([round(float(v), 3) for v in row] for row in acc)
        p["seq"] += start_seq
        out.append(p)
    return out


@pytest.fixture
def client(test_cfg):
    app = create_app(test_cfg, source="udp")
    with TestClient(app) as c:
        c.st = app.state.bw
        yield c


def send(c, packets):
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    port = c.st.ingest.source.port
    for p in packets:
        tx.sendto(encode_packet(p), ("127.0.0.1", port))
        time.sleep(0.002)
    tx.close()


def test_rotation_maps_recorded_vector_to_supine():
    for g in ([0, 0, 1], [0.3, 0.2, 0.93], [0, -0.87, 0.5], [0, 0, -1]):
        r = supine_rotation(np.array(g))
        assert np.allclose(r @ (np.array(g) / np.linalg.norm(g)), [0, 0, 1], atol=1e-9)
        assert np.allclose(r @ r.T, np.eye(3))


def test_posture_uses_rotation(cfg):
    g = R_TILT @ np.array([0, 0, 1.0])
    block = synthetic_block(500, gravity=tuple(g))
    assert posture(block, cfg) == "unknown"
    assert posture(block, cfg, supine_rotation(g)) == "supine"


def test_calibrate_live_patient_end_to_end(client):
    live = client.st.ward.runtimes["p4"]
    send(client, tilted_packets(client.st.cfg, 12))
    wait_for(lambda: live.stream.stats.packets == 12)
    wait_for(lambda: live.vitals is not None)
    assert live.vitals["posture"] == "unknown"            # tilted patch, default axes
    r = client.post("/api/calibrate/supine")               # the path the frontend calls
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"patient_id", "gravity", "samples", "calibrated_t", "posture_now"}
    assert body["patient_id"] == "p4" and body["posture_now"] == "supine" and body["samples"] >= 135
    assert np.allclose(body["gravity"], R_TILT @ [0, 0, 1], atol=0.02)
    assert client.get("/api/patients/p4").json()["calibration"]["gravity"] == body["gravity"]
    send(client, tilted_packets(client.st.cfg, 12, start_seq=12))
    seq = live.vitals_seq
    wait_for(lambda: live.vitals_seq > seq and live.stream.stats.packets == 24)
    assert live.vitals["posture"] == "supine"              # now relative to the calibration
    r = client.delete("/api/patients/p4/calibration")
    assert r.status_code == 200 and r.json()["calibration"] is None


def test_calibration_refused_without_data_or_while_moving(client):
    r = client.post("/api/patients/p4/calibrate/supine")
    assert r.status_code == 409 and "accelerometer" in r.json()["detail"]
    live = client.st.ward.runtimes["p4"]
    send(client, tilted_packets(client.st.cfg, 10, motion=True))
    wait_for(lambda: live.stream.stats.packets == 10)
    r = client.post("/api/patients/p4/calibrate/supine")
    assert r.status_code == 409 and "moving" in r.json()["detail"]


def test_device_change_clears_calibration(client):
    rt = client.st.ward.runtimes["p1"]
    wait_for(lambda: rt.stream.buffers.total >= 200)
    assert client.post("/api/patients/p1/calibrate/supine").status_code == 200
    assert client.get("/api/patients/p1").json()["calibration"] is not None
    r = client.patch("/api/patients/p1", json={"device": {"kind": "simulator", "scenario": "central"}})
    assert r.status_code == 200 and r.json()["calibration"] is None
