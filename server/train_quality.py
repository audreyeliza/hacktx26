"""Train the v1 signal-quality model (AGENTS.md §7, milestone 9) from record-mode CSVs.

    python -m server.train_quality                       # data/recordings/**.csv -> data/quality_model.joblib
    python -m server.train_quality --dir data/recordings/session-20261025-…
    python -m server.train_quality --simulate             # make labelled recordings from the simulator first

Recordings come from record mode, usually via the guided session (tools/record_session.py): one
CSV per labelled segment (device, seq, k, t0_ms, pressure, pulse, therm, ax, ay, az, label). Each
file is cut into 10 s windows (5 s hop; edges trimmed; never across a seq gap), recording labels
are mapped to quality classes (train.label_map: breath-holds and slow breathing are physiology, so
"clean"), and a RandomForest is trained on the §7 features. Evaluation is honest about overlap:
with >= 2 sessions the newest session is held out; with one session the last 30 % of every
recording is the test set (windows straddling the cut are dropped). A confusion matrix is printed;
the final model is trained on everything and saved with its feature list. The server keeps using
the v0 rules until it is wired to load this file.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from server.config import ROOT, Config, load_config
from server.ingest import CHANNELS
from server.quality import window_features
from server.record import Recorder
from server.simulator import Simulator

log = logging.getLogger(__name__)


@dataclass
class Window:
    features: dict[str, float]
    label: str            # quality class
    source_label: str     # recording label
    file: str
    session: str
    t: float              # seconds from the start of the file (for the time-based split)
    frac: float           # position in the file, 0..1


def read_recording(path: Path) -> tuple[str, list[np.ndarray]]:
    """(recording label, contiguous runs as (channels, n) arrays). A seq gap starts a new run."""
    runs: list[list[list[float]]] = []
    labels: Counter[str] = Counter()
    last_seq: int | None = None
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            seq, k = int(row["seq"]), int(row["k"])
            if last_seq is None or (k == 0 and seq != last_seq + 1 and seq != last_seq):
                runs.append([])
            if k == 0:
                last_seq = seq
            runs[-1].append([float(row[c]) for c in CHANNELS])
            labels[row["label"]] += 1
    if not labels:
        return "", []
    label = labels.most_common(1)[0][0]
    return label, [np.array(r, dtype=float).T for r in runs if r]


def windows_from_file(path: Path, cfg: Config, root: Path) -> list[Window]:
    t = cfg.train
    fs = float(cfg.contract.sample_rate_hz)
    label, runs = read_recording(path)
    cls = t.label_map.get(label)
    if cls is None:
        log.warning("skipping %s: label %r not in train.label_map", path.name, label)
        return []
    n_win, hop = int(t.window_s * fs), int(t.hop_s * fs)
    total = sum(r.shape[1] for r in runs)
    # trim the edges (getting into / out of the task), but less for short segments so a 15-30 s
    # step still yields windows
    trim = max(0, min(int(t.trim_s * fs), (total - n_win) // 4))
    out: list[Window] = []
    offset = 0
    rel = path.relative_to(root) if path.is_relative_to(root) else path
    session = rel.parts[0] if len(rel.parts) > 1 else "(no session)"
    for run in runs:
        n = run.shape[1]
        lo, hi = (trim if offset == 0 else 0), n - (trim if offset + n == total else 0)
        for s0 in range(lo, hi - n_win + 1, hop):
            mid = offset + s0 + n_win / 2
            out.append(Window(window_features(run[:, s0:s0 + n_win], fs, cfg), cls, label, str(rel), session,
                              (offset + s0) / fs, mid / total))
        offset += n
    return out


def load_windows(root: Path, cfg: Config) -> list[Window]:
    files = sorted(root.rglob("*.csv"))
    windows: list[Window] = []
    for f in files:
        windows += windows_from_file(f, cfg, root)
    return windows


def split(windows: list[Window], cfg: Config) -> tuple[list[Window], list[Window], str]:
    sessions = sorted({w.session for w in windows})
    if len(sessions) >= 2:
        test_s = sessions[-1]
        return ([w for w in windows if w.session != test_s], [w for w in windows if w.session == test_s],
                f"held-out session {test_s} (trained on {len(sessions) - 1} other session(s))")
    cut = 1 - cfg.train.test_fraction
    span = cfg.train.window_s  # windows overlapping the cut are dropped from both sides
    train, test = [], []
    by_file: dict[str, list[Window]] = {}
    for w in windows:
        by_file.setdefault(w.file, []).append(w)
    for ws in by_file.values():
        end = max(w.t for w in ws) + span
        cut_t = cut * end
        tr = [w for w in ws if w.t + span <= cut_t]
        te = [w for w in ws if w.t >= cut_t]
        if not tr or not te:          # too short to split without overlap: training only
            tr, te = ws, []
        train += tr
        test += te
    return train, test, f"single session: last {cfg.train.test_fraction:.0%} of each recording held out (no overlap)"


def matrix(features: list[str], ws: list[Window]) -> np.ndarray:
    return np.array([[w.features[f] for f in features] for w in ws], dtype=float)


def train(root: Path, out: Path, cfg: Config) -> dict[str, Any]:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import classification_report, confusion_matrix

    windows = load_windows(root, cfg)
    if not windows:
        raise SystemExit(f"no usable recordings under {root} (record some with tools/record_session.py)")
    counts = Counter(w.label for w in windows)
    print(f"{len(windows)} windows from {len({w.file for w in windows})} files, "
          f"{len({w.session for w in windows})} session(s): {dict(counts)}")
    print("by recording label:", dict(Counter(w.source_label for w in windows)))
    low = [c for c, n in counts.items() if n < cfg.train.min_windows_per_class]
    if len(counts) < 2:
        raise SystemExit(f"need at least two quality classes, have {dict(counts)}")
    if low:
        print(f"warning: few windows for {low} (< {cfg.train.min_windows_per_class}); results will be shaky")
    features = sorted(windows[0].features)
    classes = sorted(counts)

    def model() -> Any:
        return RandomForestClassifier(n_estimators=cfg.train.n_estimators, max_depth=cfg.train.max_depth,
                                      class_weight="balanced", min_samples_leaf=2, random_state=0)

    tr, te, how = split(windows, cfg)
    metrics: dict[str, Any] = {"split": how}
    print(f"\nEvaluation: {how}; {len(tr)} train / {len(te)} test windows")
    untested = [c for c in classes if not any(w.label == c for w in te)]
    if untested:
        print(f"warning: no test windows for {untested}: their scores below mean nothing — record a "
              "second session so one can be held out")
    if tr and te and len({w.label for w in tr}) >= 2:
        clf = model().fit(matrix(features, tr), [w.label for w in tr])
        pred = clf.predict(matrix(features, te))
        truth = [w.label for w in te]
        cm = confusion_matrix(truth, pred, labels=classes)
        print("\nConfusion matrix (rows = true, columns = predicted):")
        print("".join(f"{'':>12s}") + "".join(f"{c:>12s}" for c in classes))
        for c, row in zip(classes, cm):
            print(f"{c:>12s}" + "".join(f"{v:>12d}" for v in row))
        print("\n" + classification_report(truth, pred, labels=classes, zero_division=0))
        metrics.update(accuracy=float(np.mean(np.array(pred) == np.array(truth))),
                       confusion_matrix=cm.tolist(), test_windows=len(te))
    else:
        print("not enough data for a held-out evaluation; training on everything without one")
    final = model().fit(matrix(features, windows), [w.label for w in windows])
    importances = sorted(zip(features, final.feature_importances_), key=lambda x: -x[1])[:8]
    print("top features:", ", ".join(f"{f} {v:.2f}" for f, v in importances))
    bundle = {"model": final, "features": features, "classes": classes, "label_map": cfg.train.label_map,
              "window_s": cfg.train.window_s, "trained_t": time.time(), "n_windows": len(windows),
              "class_counts": dict(counts), "files": sorted({w.file for w in windows}), "metrics": metrics,
              "version": 1}
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, out)
    print(f"\nsaved {out}")
    return bundle


# --------------------------------------------------------------------------- simulated recordings

SIMULATED = (  # (recording label, scenario, which part of it)
    ("clean", "normal", "all"),
    ("motion", "motion", "motion"),
    ("sensor_off", "sensor_off", "sensor_off"),
    ("slow_breathing", "opioid_decline", "tail"),
    ("breath_hold_effort", "obstructive", "events"),
    ("breath_hold_no_effort", "central", "events"),
)


def simulate_recordings(cfg: Config, out_dir: Path, seed: int = 0, session: str = "simulated") -> Path:
    """One simulated session; see simulate_sessions."""
    rec = Recorder(cfg)
    rec.dir = out_dir
    for i, (label, scenario, part) in enumerate(SIMULATED, 1):
        sim = Simulator(scenario, cfg, seed=cfg.simulator.seed + seed + i)
        lab = sim.labels()
        if part == "all":
            spans = [(0.0, sim.duration_s)]
        elif part == "tail":
            spans = [(sim.duration_s - 180, sim.duration_s)]
        elif part == "events":
            spans = [(e["start_s"], e["end_s"]) for e in lab["events"]]
        else:
            spans = [(q["start_s"], q["end_s"]) for q in lab["quality"] if q["label"] == part]
        for j, (a, b) in enumerate(spans):
            rec.start(label, session, f"{i:02d}-{j:02d}")
            for p in sim.packets():
                t = (sim.n - sim.batch) / sim.fs
                if a <= t < b:
                    rec.on_packet(p)
                if t >= b:
                    break
            rec.stop()
            sim = Simulator(scenario, cfg, seed=cfg.simulator.seed + seed + i)
    return out_dir / session


def simulate_sessions(cfg: Config, out_dir: Path, n: int = 2) -> Path:
    """Labelled CSVs from the simulator in the record-mode format, as n sessions with different
    seeds (so the held-out-session evaluation is used, as with real recordings)."""
    for k in range(n):
        simulate_recordings(cfg, out_dir, seed=100 * k, session=f"simulated-{k + 1}")
    return out_dir


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Train the Respiro signal-quality model (v1).")
    ap.add_argument("--dir", default=cfg.train.recordings_dir, help="recordings folder (searched recursively)")
    ap.add_argument("--out", default=cfg.quality.model_path)
    ap.add_argument("--simulate", action="store_true", help="train on simulator-made recordings (no hardware)")
    args = ap.parse_args(argv)
    out = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    if args.simulate:
        tmp = Path(tempfile.mkdtemp(prefix="bw-sim-recordings-"))
        print(f"writing simulated recordings to {tmp}")
        simulate_sessions(cfg, tmp)
        root = tmp
    else:
        root = ROOT / args.dir if not Path(args.dir).is_absolute() else Path(args.dir)
    if not root.exists():
        sys.exit(f"{root} does not exist")
    train(root, out, cfg)


if __name__ == "__main__":
    main()
