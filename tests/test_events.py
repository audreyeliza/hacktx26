"""Milestone 5: apnea / hypopnea detection and classification against simulator ground truth."""

import numpy as np
import pytest

from server.ingest import CH, CHANNELS, RingBuffer
from server.pipeline import Pipeline, public_event
from tests.helpers import run_pipeline


def score(events, truth):
    """(correct-type hits, false events) by time overlap."""
    hits = sum(any(e["type"] == t["type"] and e["start_s"] < t["end_s"] and t["start_s"] < e["end_s"]
                   for e in events) for t in truth)
    false = sum(not any(e["start_s"] < t["end_s"] and t["start_s"] < e["end_s"] for t in truth)
                for e in events)
    return hits, false


@pytest.mark.parametrize("scenario", ["obstructive", "central", "hypopnea"])
@pytest.mark.parametrize("seed", [42, 7])
def test_events_detected_with_right_type(cfg, scenario, seed):
    """Acceptance: >= 80 % of ground-truth events detected with the right type."""
    _, events, sim = run_pipeline(cfg, scenario, seed=seed)
    truth = sim.labels()["events"]
    hits, false = score(events, truth)
    assert hits >= 0.8 * len(truth), (hits, len(truth), events)
    assert false == 0


@pytest.mark.parametrize("scenario", ["normal", "motion", "sensor_off", "opioid_decline"])
def test_no_false_events(cfg, scenario):
    """Acceptance: <= 1 false event per 10 min on normal (we also hold motion, sensor-off and the
    slow opioid decline to it, since those must not create apneas)."""
    _, events, sim = run_pipeline(cfg, scenario)
    assert len(events) <= sim.duration_s / 600, events


def test_event_timing_matches_truth(cfg):
    _, events, sim = run_pipeline(cfg, "obstructive")
    for t in sim.labels()["events"]:
        e = next(e for e in events if e["start_s"] < t["end_s"] and t["start_s"] < e["end_s"])
        assert abs(e["start_s"] - t["start_s"]) <= 3.0
        assert abs(e["end_s"] - t["end_s"]) <= 3.0
        assert abs(e["duration_s"] - t["duration_s"]) <= 3.0


def test_event_fields(cfg):
    _, events, _ = run_pipeline(cfg, "central")
    e = public_event({k: v for k, v in events[0].items() if k not in ("start_s", "end_s")})  # helper-only keys
    assert set(e) == {"id", "patient_id", "type", "start_t", "end_t", "duration_s", "confidence",
                      "posture", "airflow_pct", "effort_pct"}
    assert e["posture"] == "supine" and e["airflow_pct"] < 10 and e["effort_pct"] < 20
    assert e["end_t"] - e["start_t"] == pytest.approx(e["duration_s"], abs=1.0)


def test_demo_long_apnea_reported_while_ongoing(cfg):
    """The >= 20 s apnea must be visible as 'in progress' before the final event is logged,
    so the tier-3 alert (milestone 6) does not wait for the 12 s final lag."""
    rows, events, sim = run_pipeline(cfg, "demo")
    long = next(t for t in sim.labels()["events"] if t["duration_s"] >= 20 and t["type"] == "obstructive")
    first = next(te for te, *_, og in rows if og and og["apnea"] and og["duration_s"] >= 20)
    logged = next(e for e in events if e["start_s"] < long["end_s"] and long["start_s"] < e["end_s"])
    assert first <= long["start_s"] + 30
    assert first < logged["end_s"] + cfg.events.lag_s
    assert logged["type"] == "obstructive" and logged["duration_s"] >= 20


def _breathing_block(fs, seconds, rng, airflow_scale, effort_scale):
    n = int(seconds * fs)
    t = np.arange(n) / fs
    b = np.zeros((len(CHANNELS), n))
    b[CH["pressure"]] = 31000 + 2400 * effort_scale(t) * np.sin(2 * np.pi * 0.25 * t) + rng.normal(0, 60, n)
    b[CH["therm"]] = 40000 + 1000 * airflow_scale(t) * np.sin(2 * np.pi * 0.25 * t) + rng.normal(0, 25, n)
    b[CH["pulse"]] = 28000 + 3000 * np.sin(2 * np.pi * 1.2 * t) + rng.normal(0, 80, n)
    b[CH["az"]] = 1.0 + 0.006 * effort_scale(t) * np.sin(2 * np.pi * 0.25 * t) + rng.normal(0, 0.002, n)
    b[CH["ay"]] = rng.normal(0, 0.002, n)
    b[CH["ax"]] = rng.normal(0, 0.002, n)
    return b


def _run_block(cfg, block, fs=50.0):
    rb = RingBuffer(len(CHANNELS), block.shape[1])
    pipe = Pipeline(cfg, fs, "p-test")
    events, step = [], int(cfg.signals.rate_interval_s * fs)
    for end in range(step, block.shape[1] + 1, step):
        rb.append(block[:, end - step:end])
        events += pipe.step(rb, end, lambda i: i / fs).events
    return events


def test_mixed_apnea(cfg):
    """Effort returns before airflow does -> mixed (not in the simulator, so synthetic)."""
    rng = np.random.default_rng(3)
    a0, a1, e1 = 150.0, 172.0, 161.0   # apnea 150-172 s; effort absent until 161 s

    def airflow(t):
        return np.where((t >= a0) & (t < a1), 0.03, 1.0)

    def effort(t):
        return np.where((t >= a0) & (t < e1), 0.03, 1.0)

    events = _run_block(cfg, _breathing_block(50.0, 240, rng, airflow, effort))
    assert [e["type"] for e in events] == ["mixed"]


def test_masked_data_never_becomes_an_apnea(cfg):
    """A data gap must abort, not look like 'no airflow'."""
    rng = np.random.default_rng(4)
    block = _breathing_block(50.0, 240, rng, lambda t: np.ones_like(t), lambda t: np.ones_like(t))
    block[:, int(150 * 50):int(170 * 50)] = np.nan
    assert _run_block(cfg, block) == []
