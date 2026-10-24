"""Milestone 3: breathing/heart rates, and rr_fused rules (AGENTS.md section 7)."""

import numpy as np
import pytest

from server.ingest import CH, Ingest
from server.signals import FusedRate, RateEstimate, breath_rates, compute_vitals, fuse
from server.simulator import Simulator


def run_scenario(cfg, name, drop=None):
    """Feed a whole scenario through ingest and compute vitals every 5 s of sample time.

    Returns [(window_end_s, truth_window_mean, truth_at_end, vitals), ...]. `drop` is an
    optional set of packet seq numbers to lose (to create gaps).
    """
    sim = Simulator(name, cfg)
    ing = Ingest(cfg)
    s = cfg.signals
    step = int(s.rate_interval_s * ing.fs)
    nxt, rows = step, []
    for i, p in enumerate(sim.packets()):
        if drop and p["seq"] in drop:
            continue
        ing.handle_packet(p, arrival=1000 + 0.5 * i, validated=True)
        while ing.buffers.total >= nxt:
            te = nxt / ing.fs
            t = np.arange(te - s.rate_window_s, te, 1 / ing.fs)
            rows.append((te, float(sim.true_rr(t).mean()), float(sim.true_rr(te)[0]),
                         compute_vitals(ing.buffers, nxt, ing.fs, s)))
            nxt += step
    return rows


@pytest.fixture(scope="module")
def normal_rows(cfg):
    return run_scenario(cfg, "normal")


@pytest.fixture(scope="module")
def decline_rows(cfg):
    return run_scenario(cfg, "opioid_decline")


# ---------------------------------------------------------------- acceptance

def test_normal_rr_fused_within_1(cfg, normal_rows):
    rows = [r for r in normal_rows if r[0] >= cfg.signals.rate_window_s]
    assert len(rows) > 100
    for te, truth, _, v in rows:
        assert v["rr_fused"] is not None, te
        assert abs(v["rr_fused"] - truth) <= 1.0, (te, v["rr_fused"], truth)
        assert v["rr_conf"] >= 0.6
        assert v["flags"] == []
        assert len(v["rr_sources"]) == 3


def test_normal_each_source_within_1(cfg, normal_rows):
    for te, truth, _, v in normal_rows:
        if te < cfg.signals.rate_window_s:
            continue
        for src in ("belt", "accel", "airflow"):
            assert abs(v[f"rr_{src}"] - truth) <= 1.0, (te, src, v[f"rr_{src}"], truth)


def test_normal_preliminary_and_heart_rate(cfg, normal_rows):
    for te, truth, _, v in normal_rows:
        if te >= cfg.signals.rate_window_s:
            assert abs(v["rr_prelim"] - truth) <= 2.0
        if te >= cfg.signals.hr_window_s:
            assert abs(v["hr"] - cfg.simulator.hr_bpm) <= 3.0, (te, v["hr"])


def test_opioid_decline_is_tracked(cfg, decline_rows):
    win = cfg.signals.rate_window_s
    for te, truth_win, truth_now, v in decline_rows:
        if te < win:
            continue
        rr = v["rr_fused"]
        assert rr is not None, te
        changing = 120 < te <= 720 + win  # decline phase, plus window lag
        tol = 2.0 if changing else 1.0
        assert abs(rr - truth_win) <= tol, (te, rr, truth_win)
        if changing and te < 840:
            assert abs(rr - truth_now) <= 2.0, (te, rr, truth_now)
    start = next(v["rr_fused"] for te, *_, v in decline_rows if te >= win)
    end = decline_rows[-2][3]["rr_fused"]
    assert start >= 15 and end <= 7


def test_rates_none_before_window_fills(cfg, normal_rows):
    first = normal_rows[0]
    assert first[0] == cfg.signals.rate_interval_s
    assert first[3]["rr_fused"] is None and first[3]["flags"] == ["no_clean_source"]


# ---------------------------------------------------------------- gaps and flat signals

def test_long_gap_gives_none_not_flat_reading(cfg):
    # lose 30 s of packets (60 packets) starting at 200 s: the 60 s window is < 80 % valid
    lost = set(range(400, 460))
    rows = run_scenario(cfg, "normal", drop=lost)
    during = [v for te, *_, v in rows if 235 <= te <= 255]
    assert during and all(v["rr_fused"] is None for v in during)
    assert all(v["why_belt"] == "missing data" for v in during)
    after = [r for r in rows if r[0] >= 300]
    assert all(abs(v["rr_fused"] - truth) <= 1.0 for te, truth, _, v in after)


def test_short_gap_is_interpolated(cfg):
    rows = run_scenario(cfg, "normal", drop={400, 401})  # 1 s lost
    for te, truth, _, v in rows:
        if te >= 60:
            assert v["rr_fused"] is not None and abs(v["rr_fused"] - truth) <= 1.0


def test_flat_and_weak_channels_report_none(cfg):
    sim = Simulator("normal", cfg)
    pk = list(sim.packets(max_packets=130))
    block = np.array([sum((p[c] for p in pk), []) for c in ("pressure", "pulse", "therm", "ax", "ay", "az")],
                     dtype=float)[:, -3000:]
    block[CH["therm"]] = 36500.0                      # thermistor off the face
    block[CH["ax"]] = 0.0                             # accelerometer with no breathing signal
    block[CH["ay"]] = 0.0
    block[CH["az"]] = 1.0
    rates = breath_rates(block, 50.0, cfg.signals)
    assert rates["airflow"].bpm is None and rates["airflow"].reason == "flat"
    assert rates["accel"].bpm is None and rates["accel"].reason == "flat"
    assert rates["belt"].bpm is not None


# ---------------------------------------------------------------- fusion rules

def est(bpm, conf=0.95):
    return RateEstimate(bpm, conf)


def test_fuse_all_agree_uses_median(cfg):
    f = fuse({"belt": est(15.0), "accel": est(15.6), "airflow": est(14.8)}, cfg.signals)
    assert f.bpm == 15.0 and f.flags == [] and sorted(f.sources) == ["accel", "airflow", "belt"]
    assert f.confidence >= 0.9


def test_fuse_excludes_outlier(cfg):
    f = fuse({"belt": est(15.0), "accel": est(28.0), "airflow": est(15.4)}, cfg.signals)
    assert f.bpm == pytest.approx(15.2) and f.flags == ["outlier:accel"]


def test_fuse_single_source_capped_and_flagged(cfg):
    f = fuse({"belt": est(12.0, 0.95), "accel": RateEstimate(None, reason="flat"),
              "airflow": RateEstimate(None, reason="flat")}, cfg.signals)
    assert f == FusedRate(12.0, cfg.signals.single_source_conf_cap, ["belt"], ["single_source"])
    # still meets the >= 0.6 alert gate on purpose (decided)
    assert f.confidence >= 0.6


def test_fuse_low_confidence_source_ignored(cfg):
    f = fuse({"belt": est(12.0, 0.95), "accel": est(20.0, 0.1)}, cfg.signals)
    assert f.flags == ["single_source"] and f.bpm == 12.0


def test_fuse_quality_can_exclude_source(cfg):
    f = fuse({"belt": est(15.0), "accel": est(15.2), "airflow": est(15.1)}, cfg.signals,
             clean={"belt": False})
    assert "belt" not in f.sources and f.bpm == pytest.approx(15.15)


def test_fuse_disagreement_below_gate(cfg):
    f = fuse({"belt": est(10.0, 0.9), "airflow": est(20.0, 0.8)}, cfg.signals)
    assert f.flags == ["sources_disagree"] and f.bpm == 10.0
    assert f.confidence < 0.6


def test_fuse_none(cfg):
    f = fuse({"belt": RateEstimate(None), "accel": RateEstimate(None)}, cfg.signals)
    assert f.bpm is None and f.flags == ["no_clean_source"] and f.confidence == 0.0
