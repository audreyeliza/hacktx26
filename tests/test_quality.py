"""Milestone 4: signal quality v0 rules and posture."""

import numpy as np
import pytest

from server.ingest import CH, CHANNELS
from server.quality import assess_segment, channel_status, posture
from server.signals import compute_vitals
from server.ingest import RingBuffer
from server.simulator import Simulator
from tests.helpers import overlaps_event, run_scenario, truth_quality

QWIN = 10.0


def labelled(cfg, scenario):
    """[(ground-truth label of the last 10 s, in-event?, vitals)] for windows after warm-up."""
    labels = Simulator(scenario, cfg).labels()
    return [(truth_quality(labels, te - QWIN, te), overlaps_event(labels, te - QWIN, te), v)
            for te, _, _, v in run_scenario(cfg, scenario) if te >= cfg.signals.rate_window_s]


@pytest.fixture(scope="module")
def motion_rows(cfg):
    return labelled(cfg, "motion")


@pytest.fixture(scope="module")
def sensor_rows(cfg):
    return labelled(cfg, "sensor_off")


# ---------------------------------------------------------------- acceptance

def test_motion_windows_labelled_motion(motion_rows):
    motion = [v for tl, _, v in motion_rows if tl == "motion"]
    clean = [v for tl, _, v in motion_rows if tl == "clean"]
    assert motion and clean
    assert all(v["quality"] == "motion" for v in motion)
    assert all(v["quality"] == "clean" for v in clean)


def test_sensor_off_windows_labelled_sensor_off(sensor_rows):
    off = [v for tl, _, v in sensor_rows if tl == "sensor_off"]
    clean = [v for tl, _, v in sensor_rows if tl == "clean"]
    assert len(off) >= 20 and clean
    assert all(v["quality"] == "sensor_off" for v in off)
    assert all(v["quality"] == "clean" for v in clean)


@pytest.mark.parametrize("scenario", ["obstructive", "central", "hypopnea"])
def test_apneas_are_not_mistaken_for_sensor_off(cfg, scenario):
    """A flat airflow (or effort) channel during an apnea is physiology, not a loose sensor;
    labelling it sensor_off would silence exactly the alarms that matter."""
    rows = labelled(cfg, scenario)
    in_event = [v for _, ev, v in rows if ev]
    assert in_event
    assert all(v["quality"] == "clean" for v in in_event)


def test_normal_is_clean_with_full_confidence(cfg):
    rows = labelled(cfg, "normal")
    assert all(v["quality"] == "clean" and v["quality_conf"] == 1.0 for _, _, v in rows)
    assert all(v["posture"] == "supine" for _, _, v in rows)


def test_alert_eligible_rates_stay_accurate_through_motion(cfg):
    """Masking motion segments keeps garbage out of rr_fused when the label says clean."""
    for te, truth, _, v in run_scenario(cfg, "motion"):
        if te >= 60 and v["quality"] == "clean" and v["rr_conf"] >= 0.6 and v["rr_fused"] is not None:
            assert abs(v["rr_fused"] - truth) <= 2.0, (te, v["rr_fused"], truth)


def test_sensor_off_channel_excluded_from_fusion(sensor_rows):
    therm_off = [v for tl, _, v in sensor_rows
                 if tl == "sensor_off" and v["channels"]["therm"] in ("flat", "clipped")]
    assert therm_off
    assert all("airflow" not in v["rr_sources"] for v in therm_off)


# ---------------------------------------------------------------- units

def test_channel_status(cfg):
    rng = np.random.default_rng(0)
    ok = 30000 + 2000 * np.sin(np.linspace(0, 20, 500)) + rng.normal(0, 50, 500)
    assert channel_status(ok, cfg) == "ok"
    assert channel_status(np.full(500, 65535.0), cfg) == "clipped"
    assert channel_status(np.zeros(500), cfg) == "clipped"
    assert channel_status(np.full(500, 36500.0) + rng.normal(0, 3, 500), cfg) == "flat"
    missing = ok.copy()
    missing[:400] = np.nan
    assert channel_status(missing, cfg) == "missing"


def synthetic_block(n, fs=50.0, rr_belt=15.0, rr_accel=15.0, gravity=(0.0, 0.0, 1.0)):
    t = np.arange(n) / fs
    rng = np.random.default_rng(1)
    block = np.zeros((len(CHANNELS), n))
    block[CH["pressure"]] = 31000 + 2400 * np.sin(2 * np.pi * rr_belt / 60 * t) + rng.normal(0, 60, n)
    block[CH["therm"]] = 40000 + 1100 * np.sin(2 * np.pi * rr_belt / 60 * t) + rng.normal(0, 25, n)
    block[CH["pulse"]] = 28000 + 3000 * np.sin(2 * np.pi * 70 / 60 * t) + rng.normal(0, 80, n)
    breath = 0.006 * np.sin(2 * np.pi * rr_accel / 60 * t)
    for i, a in enumerate(("ax", "ay", "az")):
        block[CH[a]] = gravity[i] + breath * (1.0 if a == "ay" else 0.3) + rng.normal(0, 0.002, n)
    return block


def test_belt_accel_disagreement_lowers_confidence(cfg):
    n = int(cfg.signals.rate_window_s * 50)
    rb = RingBuffer(len(CHANNELS), n)
    rb.append(synthetic_block(n, rr_belt=12.0, rr_accel=24.0))
    v = compute_vitals(rb, n, 50.0, cfg)
    assert v["quality"] == "clean"
    assert v["quality_conf"] == cfg.quality.disagree_conf
    assert any("disagree" in r for r in v["quality_reasons"])


def test_motion_burst_detected(cfg):
    block = synthetic_block(500)
    rng = np.random.default_rng(2)
    block[CH["ax"], 200:350] += rng.normal(0, 0.3, 150)
    seg = assess_segment(block, 50.0, cfg)
    assert seg.label == "motion" and seg.motion > cfg.quality.motion_threshold_g


def test_pulse_off_does_not_block_breathing_quality(cfg):
    block = synthetic_block(500)
    block[CH["pulse"]] = 0.0
    seg = assess_segment(block, 50.0, cfg)
    assert seg.label == "clean" and seg.channels["pulse"] == "clipped"


@pytest.mark.parametrize("gravity,expected", [
    ((0, 0, 1), "supine"), ((-1, 0, 0), "left"), ((1, 0, 0), "right"),
    ((0, 1, 0), "upright"), ((0, 0, -1), "unknown"), ((0.5, 0.5, 0.5), "unknown"),
])
def test_posture_from_gravity(cfg, gravity, expected):
    assert posture(synthetic_block(500, gravity=gravity), cfg) == expected


def test_posture_follows_roll_in_motion_scenario(cfg, motion_rows):
    postures = {v["posture"] for tl, _, v in motion_rows if tl == "clean"}
    assert {"supine", "left"} <= postures
