"""Milestone 9 prep: guided recording (tools/record_session.py) and training from its files
(server/train_quality.py). No hardware: the in-process simulator stands in for the wearable."""

import importlib.util
import json
import sys

import joblib
import numpy as np
import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.config import ROOT
from server.ingest import CHANNELS
from server.record import Recorder
from server.simulator import Simulator
from server.train_quality import read_recording, simulate_sessions, train
from tests.conftest import with_overrides

spec = importlib.util.spec_from_file_location("record_session", ROOT / "tools" / "record_session.py")
rs = importlib.util.module_from_spec(spec)
sys.modules["record_session"] = rs          # dataclasses need the module registered
spec.loader.exec_module(rs)


@pytest.fixture
def app_client(test_cfg, tmp_path):
    cfg = with_overrides(test_cfg, record={"dir": str(tmp_path / "recordings"), "labels": test_cfg.record.labels})
    app = create_app(cfg, source="udp")
    with TestClient(app) as c:
        c.post("/api/source", json={"source": "simulator:normal", "speed": 20, "loop": True})
        c.cfg = cfg
        c.rec_dir = tmp_path / "recordings"
        yield c


def test_protocol_has_the_requested_steps_and_labels(cfg):
    recorded = [(s.label, s.seconds) for s in rs.PROTOCOL if s.label]
    assert recorded == [("clean", 60), ("motion", 30), ("slow_breathing", 60), ("breath_hold_effort", 30),
                        ("breath_hold_no_effort", 25), ("sensor_off", 60)]
    assert {s.label for s in rs.PROTOCOL if s.label} <= set(cfg.record.labels)
    assert [s.label for s in rs.PROTOCOL if s.breath_hold] == ["breath_hold_effort", "breath_hold_no_effort"]
    # every breath-hold has a recovery rest after it
    for i, s in enumerate(rs.PROTOCOL):
        if s.breath_hold:
            assert rs.PROTOCOL[i + 1].label is None and rs.PROTOCOL[i + 1].seconds >= 30


def test_guided_session_end_to_end_then_train(app_client, capsys, tmp_path):
    server = rs.Server(app_client)
    manifest_file = rs.run_session(server, list(rs.PROTOCOL), "session-test-p01", "p01", "test notes",
                                   assume_yes=True, time_scale=0.03, allow_simulator=True)
    out = capsys.readouterr().out
    # the safety line comes right before each breath-hold step's prompt
    assert out.count(rs.SAFETY) == 2
    for title in ("Hold your breath while pushing your chest", "Hold your breath, fully relaxed"):
        start = out.index(title)
        assert start < out.index(rs.SAFETY, start) < out.index("Press Enter", start)
    manifest = json.loads(manifest_file.read_text())
    assert manifest_file.parent == app_client.rec_dir / "session-test-p01"
    labels = [seg["label"] for seg in manifest["segments"]]
    assert labels == ["clean", "motion", "slow_breathing", "breath_hold_effort", "breath_hold_no_effort", "sensor_off"]
    for seg in manifest["segments"]:
        f = manifest_file.parent / f"{seg['index']:02d}-{seg['step']}_{seg['label']}.csv"
        assert f.exists() and seg["file"].endswith(f.name)
        label, runs = read_recording(f)
        assert label == seg["label"] and sum(r.shape[1] for r in runs) > 200
    assert app_client.get("/api/admin/device").json()["recording"] is None
    # train directly from the session folder
    bundle = train(manifest_file.parent, tmp_path / "model.joblib", app_client.cfg)
    assert set(bundle["classes"]) == {"clean", "motion", "sensor_off"} and len(bundle["features"]) == 24
    assert joblib.load(tmp_path / "model.joblib")["features"] == bundle["features"]


def test_preflight_refuses_without_data(test_cfg):
    app = create_app(test_cfg, source="udp")          # listening, but nothing is sending
    with TestClient(app) as c:
        server = rs.Server(c)
        server.sign_in()
        with pytest.raises(SystemExit) as exc:
            rs.preflight(server, allow_simulator=False, wait_s=0.2)
        assert "no packets" in str(exc.value)


def test_record_session_names_are_safe(app_client):
    assert app_client.post("/api/record/start", json={"label": "clean", "session": "../../etc"}).status_code == 400
    assert app_client.post("/api/record/start", json={"label": "clean", "segment": "a/b"}).status_code == 400


def test_read_recording_splits_on_seq_gaps(cfg, tmp_path):
    rec = Recorder(cfg)
    rec.dir = tmp_path
    rec.start("clean", "s", "01-x")
    for p in Simulator("normal", cfg).packets(max_packets=10):
        if p["seq"] not in (4, 5):
            rec.on_packet(p)
    rec.stop()
    label, runs = read_recording(tmp_path / "s" / "01-x_clean.csv")
    assert label == "clean" and [r.shape for r in runs] == [(len(CHANNELS), 100), (len(CHANNELS), 100)]


def test_train_on_simulated_sessions(cfg, tmp_path, capsys):
    simulate_sessions(cfg, tmp_path / "rec")
    bundle = train(tmp_path / "rec", tmp_path / "m.joblib", cfg)
    out = capsys.readouterr().out
    assert "Confusion matrix" in out and "held-out session simulated-2" in out
    assert bundle["metrics"]["accuracy"] >= 0.9
    assert set(bundle["classes"]) == {"clean", "motion", "sensor_off"}
    model = joblib.load(tmp_path / "m.joblib")["model"]
    assert len(model.predict(np.zeros((1, len(bundle["features"]))))) == 1
