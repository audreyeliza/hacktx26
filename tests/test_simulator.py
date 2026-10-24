"""Simulator scenarios and ground-truth labels."""

import json

import numpy as np

from server.schema import parse_packet
from server.simulator import Simulator, main


def collect(sim: Simulator) -> dict[str, np.ndarray]:
    ch: dict[str, list] = {k: [] for k in ("pressure", "pulse", "therm", "ax", "ay", "az")}
    for p in sim.packets():
        for k in ch:
            ch[k].extend(p[k])
    return {k: np.array(v, dtype=float) for k, v in ch.items()}


def window(sim: Simulator, start_s: float, end_s: float) -> slice:
    return slice(int(start_s * sim.fs), int(end_s * sim.fs))


def test_deterministic_for_seed(cfg):
    a = list(Simulator("motion", cfg, seed=7).packets(max_packets=50))
    b = list(Simulator("motion", cfg, seed=7).packets(max_packets=50))
    c = list(Simulator("motion", cfg, seed=8).packets(max_packets=50))
    assert a == b
    assert a != c


def test_normal_rate_range(cfg):
    sim = Simulator("normal", cfg)
    rr = sim.true_rr(np.arange(0, sim.duration_s, 1.0))
    assert rr.min() >= 14 - 1e-9 and rr.max() <= 16 + 1e-9


def test_opioid_decline_truth(cfg):
    sim = Simulator("opioid_decline", cfg)
    assert abs(sim.true_rr(60.0)[0] - 16) < 1e-9
    assert abs(sim.true_rr(sim.duration_s - 1)[0] - 6) < 1e-9


def test_apnea_labels(cfg):
    for kind in ("obstructive", "central"):
        events = Simulator(kind, cfg).labels()["events"]
        assert len(events) == 6
        assert all(e["type"] == kind and 15 <= e["duration_s"] <= 25 for e in events)
    hyp = Simulator("hypopnea", cfg).labels()["events"]
    assert len(hyp) == 6 and all(e["duration_s"] == 15 for e in hyp)


def test_demo_contains_every_phase(cfg):
    lab = Simulator("demo", cfg).labels()
    types = {e["type"] for e in lab["events"]}
    assert types == {"obstructive", "central"}
    assert any(e["duration_s"] >= 20 for e in lab["events"])
    assert any(q["label"] == "motion" for q in lab["quality"])
    rr = np.array(lab["rr_truth"])[:, 1]
    assert rr.min() < 6


def test_obstructive_effort_continues_airflow_flat(cfg):
    sim = Simulator("obstructive", cfg)
    ch = collect(sim)
    ev = sim.labels()["events"][0]
    inner = window(sim, ev["start_s"] + 3, ev["end_s"] - 1)
    before = window(sim, ev["start_s"] - 30, ev["start_s"] - 2)
    assert ch["pressure"][inner].std() > 0.7 * ch["pressure"][before].std()
    assert ch["therm"][inner].std() < 0.15 * ch["therm"][before].std()


def test_central_effort_and_airflow_flat(cfg):
    sim = Simulator("central", cfg)
    ch = collect(sim)
    ev = sim.labels()["events"][0]
    inner = window(sim, ev["start_s"] + 3, ev["end_s"] - 1)
    before = window(sim, ev["start_s"] - 30, ev["start_s"] - 2)
    for k in ("pressure", "therm"):
        assert ch[k][inner].std() < 0.15 * ch[k][before].std()


def test_sensor_off_segments(cfg):
    sim = Simulator("sensor_off", cfg)
    ch = collect(sim)
    segs = [q for q in sim.labels()["quality"] if q["label"] == "sensor_off"]
    assert len(segs) == 3
    therm_flat, pressure_rail, therm_low = (window(sim, s["start_s"] + 2, s["end_s"]) for s in segs)
    assert ch["therm"][therm_flat].std() < 50
    assert np.all(ch["pressure"][pressure_rail] == 65535)
    assert np.all(ch["therm"][therm_low] == 0)


def test_motion_bursts_corrupt_signals(cfg):
    sim = Simulator("motion", cfg)
    ch = collect(sim)
    burst = next(q for q in sim.labels()["quality"] if q["label"] == "motion")
    inner = window(sim, burst["start_s"], burst["end_s"])
    calm = window(sim, 10, 100)
    for k in ("ax", "ay", "az"):
        assert ch[k][inner].std() > 10 * ch[k][calm].std()


def test_cli_stdout_and_labels(cfg, tmp_path, capsys):
    labels = tmp_path / "labels.json"
    main(["--scenario", "hypopnea", "--stdout", "--speed", "0", "--labels", str(labels)])
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) > 1000
    for line in lines[:: len(lines) // 20]:
        parse_packet(line, cfg.contract)
    data = json.loads(labels.read_text())
    assert data["scenario"] == "hypopnea" and len(data["events"]) == 6
