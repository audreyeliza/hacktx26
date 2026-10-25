"""Quality model v1 in the server: loaded if usable, rules otherwise, never a simulated-only model."""

import csv

import joblib
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sklearn.dummy import DummyClassifier

from server.app import create_app
from server.ingest import CHANNELS, RingBuffer
from server.quality import window_features
from server.quality_model import QualityModel
from server.signals import analyze
from server.simulator import Simulator
from server.train_quality import SIMULATED_MODEL_PATH, simulate_sessions, train
from tests.conftest import with_overrides


def cfg_with_model(cfg, path, **quality):
    return with_overrides(cfg, quality={"model_path": str(path), **quality})


def features_list(cfg):
    ref = np.random.default_rng(0).normal(30000, 500, (6, 500))
    return sorted(window_features(ref, 50.0, cfg))


def constant_bundle(cfg, label, real_files=1):
    """A valid v1 bundle whose model always answers `label` (to test the wiring, not the model)."""
    feats = features_list(cfg)
    X = np.zeros((3, len(feats)))
    clf = DummyClassifier(strategy="constant", constant=label).fit(X, ["clean", "motion", "sensor_off"])
    return {"model": clf, "features": feats, "classes": ["clean", "motion", "sensor_off"], "version": 1,
            "provenance": {"real_files": real_files, "simulated_files": 0, "unknown_files": 0,
                           "real_sessions": ["s1"] if real_files else []},
            "n_windows": 3, "metrics": {"accuracy": 0.9, "split": "test"}, "trained_t": 1.0}


def make_real(rec_dir):
    """Pretend simulator recordings came from the wearable (rewrite the source column)."""
    for f in rec_dir.rglob("*.csv"):
        rows = list(csv.DictReader(f.open()))
        with f.open("w", newline="") as out:
            w = csv.DictWriter(out, fieldnames=list(rows[0]))
            w.writeheader()
            for r in rows:
                w.writerow({**r, "source": "serial:/dev/cu.usbmodem1101"})


# ---------------------------------------------------------------- loading and refusing

def test_no_model_file_means_rules(cfg, tmp_path):
    qm = QualityModel(cfg_with_model(cfg, tmp_path / "none.joblib"))
    assert not qm.active and qm.status["active"] == "rules" and "no trained model" in qm.status["reason"]


def test_corrupt_model_file_falls_back(cfg, tmp_path):
    bad = tmp_path / "m.joblib"
    bad.write_bytes(b"not a pickle")
    qm = QualityModel(cfg_with_model(cfg, bad))
    assert not qm.active and "failed to load" in qm.status["reason"]


def test_simulated_only_model_is_refused(cfg, tmp_path):
    simulate_sessions(cfg, tmp_path / "rec")
    bundle = train(tmp_path / "rec", tmp_path / "m.joblib", cfg)
    assert bundle["provenance"]["real_files"] == 0 and bundle["provenance"]["simulated_files"] > 0
    qm = QualityModel(cfg_with_model(cfg, tmp_path / "m.joblib"))
    assert not qm.active and "without real-wearable recordings" in qm.status["reason"]
    allowed = QualityModel(cfg_with_model(cfg, tmp_path / "m.joblib", allow_simulated_model=True))
    assert allowed.active                                       # only with an explicit opt-in
    assert SIMULATED_MODEL_PATH != cfg.quality.model_path       # --simulate never writes the active file


def test_model_with_real_recordings_is_activated(cfg, tmp_path):
    simulate_sessions(cfg, tmp_path / "rec")
    make_real(tmp_path / "rec")
    bundle = train(tmp_path / "rec", tmp_path / "m.joblib", cfg)
    assert bundle["provenance"]["real_files"] > 0
    qm = QualityModel(cfg_with_model(cfg, tmp_path / "m.joblib"))
    assert qm.active and qm.status["active"] == "model" and qm.status["real_files"] == bundle["provenance"]["real_files"]


@pytest.mark.parametrize("change,reason", [
    (lambda b: b.update(features=b["features"][:-1]), "different feature set"),
    (lambda b: b.pop("provenance"), "no record of where"),
    (lambda b: b.update(version=2), "not supported"),
    (lambda b: b.update(classes=["clean", "weird"]), "not usable"),
])
def test_unusable_bundles_fall_back(cfg, tmp_path, change, reason):
    b = constant_bundle(cfg, "clean")
    change(b)
    joblib.dump(b, tmp_path / "m.joblib")
    qm = QualityModel(cfg_with_model(cfg, tmp_path / "m.joblib"))
    assert not qm.active and reason in qm.status["reason"]


# ---------------------------------------------------------------- the model in the pipeline

def buffers_for(cfg, scenario, seconds=70):
    sim = Simulator(scenario, cfg)
    rb = RingBuffer(len(CHANNELS), 30000)
    for p in sim.packets(max_packets=int(seconds * 2)):
        rb.append(np.array([p[c] for c in CHANNELS], dtype=np.float32))
    return rb


def test_model_label_is_used_and_gates(cfg, tmp_path):
    joblib.dump(constant_bundle(cfg, "motion"), tmp_path / "m.joblib")
    c = cfg_with_model(cfg, tmp_path / "m.joblib")
    qm = QualityModel(c)
    rb = buffers_for(c, "normal")
    v, _ = analyze(rb, rb.total, 50.0, c, quality_model=qm)
    assert v["quality_source"] == "model" and v["quality"] == "motion" and v["quality_rules"] == "clean"
    assert any("rules said clean" in r for r in v["quality_reasons"])
    rules_only, _ = analyze(rb, rb.total, 50.0, c)
    assert rules_only["quality_source"] == "rules" and rules_only["quality"] == "clean"


def test_rules_channel_fault_overrides_the_model(cfg, tmp_path):
    joblib.dump(constant_bundle(cfg, "clean"), tmp_path / "m.joblib")
    c = cfg_with_model(cfg, tmp_path / "m.joblib")
    qm = QualityModel(c)
    sim = Simulator("sensor_off", c)
    seg = next(q for q in sim.labels()["quality"] if q["label"] == "sensor_off")
    rb = RingBuffer(len(CHANNELS), 30000)
    for p in sim.packets():
        rb.append(np.array([p[ch] for ch in CHANNELS], dtype=np.float32))
        if rb.total >= int((seg["start_s"] + 40) * 50):
            break
    v, _ = analyze(rb, rb.total, 50.0, c, quality_model=qm)
    assert v["quality_rules"] == "sensor_off" and v["quality"] == "sensor_off" and v["quality_source"] == "model"


# ---------------------------------------------------------------- admin page / API

def test_admin_device_shows_active_decider_and_reload(test_cfg, tmp_path):
    path = tmp_path / "m.joblib"
    app = create_app(cfg_with_model(test_cfg, path), source="udp")
    with TestClient(app) as c:
        qm = c.get("/api/admin/device").json()["quality_model"]
        assert qm["active"] == "rules" and qm["version"] == "v0" and qm["reason"]
        joblib.dump(constant_bundle(test_cfg, "clean"), path)
        r = c.post("/api/admin/quality-model/reload")
        assert r.status_code == 200 and r.json()["quality_model"]["active"] == "model"
        assert c.get("/api/admin/device").json()["quality_model"]["active"] == "model"
        rt = app.state.bw.ward.runtimes["p1"]
        assert rt.pipeline.quality_model.active                  # shared object: pipelines see it
        joblib.dump(constant_bundle(test_cfg, "clean", real_files=0), path)
        assert c.post("/api/admin/quality-model/reload").json()["quality_model"]["active"] == "rules"
