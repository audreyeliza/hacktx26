"""Shared test helpers: run a simulator scenario through the real pipeline."""

import numpy as np

from server.ingest import Ingest
from server.pipeline import Pipeline
from server.simulator import Simulator


def run_pipeline(cfg, name, drop=None, seed=None):
    """Feed a whole scenario through ingest + Pipeline, stepping every 5 s of sample time.

    Returns (rows, events, sim). rows = [(window_end_s, truth_window_mean, truth_at_end,
    vitals, ongoing)]; events carry start_s / end_s in scenario seconds. `drop` is an
    optional set of packet seq numbers to lose (to create gaps).
    """
    sim = Simulator(name, cfg, seed=seed)
    ing = Ingest(cfg)
    pipe = Pipeline(cfg, ing.fs, "p-test")
    s = cfg.signals
    step = int(s.rate_interval_s * ing.fs)
    nxt, rows, events = step, [], []
    to_s = lambda idx: idx / ing.fs  # scenario seconds (no gaps before the first packet)
    for i, p in enumerate(sim.packets()):
        if drop and p["seq"] in drop:
            continue
        ing.handle_packet(p, arrival=1000 + 0.5 * i, validated=True)
        while ing.buffers.total >= nxt:
            te = nxt / ing.fs
            t = np.arange(te - s.rate_window_s, te, 1 / ing.fs)
            res = pipe.step(ing.buffers, nxt, ing.sample_time)
            for e in res.events:
                events.append({**e, "start_s": to_s(e["start_idx"]), "end_s": to_s(e["end_idx"])})
            rows.append((te, float(sim.true_rr(t).mean()), float(sim.true_rr(te)[0]), res.vitals, res.ongoing))
            nxt += step
    return rows, events, sim


def run_scenario(cfg, name, drop=None, seed=None):
    """[(window_end_s, truth_window_mean, truth_at_end, vitals)] for a scenario."""
    rows, _, _ = run_pipeline(cfg, name, drop, seed)
    return [r[:4] for r in rows]


def truth_quality(labels, a, b):
    """Ground-truth quality label if [a, b] lies inside one segment, else 'mixed'."""
    for q in labels["quality"]:
        if q["start_s"] <= a and b <= q["end_s"]:
            return q["label"]
    return "mixed"


def overlaps_event(labels, a, b):
    return any(e["start_s"] < b and a < e["end_s"] for e in labels["events"])
