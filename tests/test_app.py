"""HTTP API, static dashboard and the live WebSocket."""

import socket
import time

from fastapi.testclient import TestClient

from server.app import create_app
from server.schema import encode_packet
from server.simulator import Simulator


def recv_until(ws, kind, limit=200):
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == kind:
            return msg
    raise AssertionError(f"no {kind} message")


def test_dashboard_and_static_files(test_cfg):
    with TestClient(create_app(test_cfg, source="udp")) as c:
        r = c.get("/")
        assert r.status_code == 200 and "BreathWatch" in r.text
        assert "not a medical device" in r.text
        for path in ("/static/app.js", "/static/style.css", "/static/vendor/chart.umd.min.js"):
            assert c.get(path).status_code == 200, path


def test_state_and_source_switching(test_cfg):
    with TestClient(create_app(test_cfg, source="udp")) as c:
        s = c.get("/api/state").json()
        assert s["patient"] == "Bed 4" and s["ingest"]["source"] == "udp"
        r = c.post("/api/source", json={"source": "simulator:normal", "speed": 20})
        assert r.status_code == 200 and r.json()["ingest"]["source"] == "simulator:normal"
        assert c.post("/api/source", json={"source": "nonsense"}).status_code == 400
        assert c.get("/api/state").json()["ingest"]["source"] == "simulator:normal"
        assert isinstance(c.get("/api/serial/ports").json(), list)


def test_websocket_streams_waveforms(test_cfg):
    with TestClient(create_app(test_cfg, source="simulator:normal")) as c:
        with c.websocket_connect("/ws/live") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and "normal" in hello["scenarios"]
            seen = 0
            for _ in range(5):
                w = recv_until(ws, "wave")
                assert w["step"] == test_cfg.live.wave_decimate
                assert len(w["effort"]) == len(w["airflow"]) == len(w["pulse"])
                assert all(v is None or 0 <= v <= 65535 for v in w["effort"])
                seen += len(w["effort"])
            assert seen > 0


def test_udp_to_websocket_end_to_end(test_cfg):
    """Milestone 2 acceptance: simulator packets over real UDP reach the dashboard socket."""
    app = create_app(test_cfg, source="udp")
    with TestClient(app) as c:
        port = app.state.bw.ingest.source.port
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with c.websocket_connect("/ws/live") as ws:
            ws.receive_json()
            for p in Simulator("normal", test_cfg).packets(max_packets=10):
                tx.sendto(encode_packet(p), ("127.0.0.1", port))
            time.sleep(0.3)
            w = recv_until(ws, "wave")
            assert any(v is not None for v in w["effort"])
            status = recv_until(ws, "status")
            assert status["ingest"]["stats"]["packets"] == 10
        tx.close()


def test_reset_clears_buffers(test_cfg):
    app = create_app(test_cfg, source="simulator:normal")
    with TestClient(app) as c:
        time.sleep(0.6)
        assert c.post("/api/demo/reset").json()["ingest"]["stats"]["packets"] <= 2
