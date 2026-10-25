"""Signal quality (AGENTS.md section 7): v0 rules per 10 s segment, plus posture.

Labels: clean | motion | sensor_off (| unknown when there is too little data).
Low-confidence or non-clean windows can never raise alerts above tier 0 (design rule 4).

Note on "flat": an apnea makes the airflow (and for central apnea the effort) channel nearly
flat *physiologically*. So "sensor_off" here means dead-flat (below the ADC noise an attached
sensor shows), clipped at a rail, or **floating** (an unconnected pin: mid-range reading with
random jitter, moving together with the other floating pins), never merely "no breathing signal".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from scipy.signal import butter, sosfiltfilt

from server.config import Config
from server.ingest import CH

ADC_CHANNELS = ("pressure", "therm", "pulse")
OFF_STATUSES = ("flat", "clipped", "floating")
BREATHING_CHANNELS = ("pressure", "therm")   # an off breathing channel makes the window sensor_off
POSTURES = {"supine": (2, 1.0), "left": (0, -1.0), "right": (0, 1.0), "upright": (1, 1.0)}


@dataclass
class SegmentQuality:
    label: str                                # clean | motion | sensor_off | unknown
    confidence: float
    motion: float | None                      # max 2 s RMS of high-passed accel magnitude (g)
    channels: dict[str, str] = field(default_factory=dict)  # ok | flat | clipped | floating | missing
    reasons: list[str] = field(default_factory=list)


@lru_cache(maxsize=4)
def _highpass_sos(cutoff: float, fs: float, order: int) -> np.ndarray:
    return butter(order, cutoff, btype="highpass", fs=fs, output="sos")


def motion_energy(acc: np.ndarray, fs: float, cfg: Config) -> float | None:
    """Max over 2 s chunks of the RMS of the high-passed (> 1.5 Hz) accel magnitude."""
    if np.isnan(acc).any():
        valid = ~np.isnan(acc).any(axis=0)
        if valid.mean() < cfg.quality.min_valid_fraction:
            return None
        acc = acc[:, valid]
    sig = cfg.signals
    sos = _highpass_sos(sig.motion_highpass_hz, fs, sig.filter_order)
    if acc.shape[1] <= 3 * (2 * len(sos) + 1):
        return None
    hp = sosfiltfilt(sos, acc, axis=1)
    mag = np.sqrt((hp ** 2).sum(axis=0))
    n = max(1, int(sig.motion_rms_window_s * fs))
    chunks = [mag[i:i + n] for i in range(0, len(mag) - n + 1, n)] or [mag]
    return float(max(np.sqrt(np.mean(c ** 2)) for c in chunks))


def channel_status(x: np.ndarray, cfg: Config) -> str:
    q = cfg.quality
    valid = x[~np.isnan(x)]
    if len(valid) < q.min_valid_fraction * len(x):
        return "missing"
    clipped = (valid <= q.clip_margin_counts) | (valid >= cfg.contract.adc_max - q.clip_margin_counts)
    if clipped.mean() > q.clip_fraction:
        return "clipped"
    if valid.std() < q.flat_raw_std_counts:
        return "flat"
    return "ok"


def floating_channels(block: np.ndarray, fs: float, cfg: Config, candidates: list[str]) -> set[str]:
    """Unconnected ADC pins: median inside the floating band with real jitter, and either
    correlated with another such pin above float_hf_hz (floating pins move together) or, alone,
    mostly high-frequency jitter."""
    q = cfg.quality
    lo, hi = q.float_band_counts
    sos = _highpass_sos(q.float_hf_hz, fs, cfg.signals.filter_order)
    hf: dict[str, np.ndarray] = {}
    for c in candidates:
        x = block[CH[c]]
        if np.isnan(x).any() or len(x) <= 3 * (2 * len(sos) + 1):
            continue
        if not (lo <= float(np.median(x)) <= hi) or float(x.std()) < q.float_min_std_counts:
            continue
        hf[c] = sosfiltfilt(sos, x - x.mean())
    out: set[str] = set()
    names = list(hf)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if np.std(hf[a]) > 0 and np.std(hf[b]) > 0 and np.corrcoef(hf[a], hf[b])[0, 1] >= q.float_corr:
                out |= {a, b}
    for c in names:
        x = block[CH[c]]
        if c not in out and float(np.var(hf[c])) >= q.float_min_hf_share * float(np.var(x - x.mean())):
            out.add(c)
    return out


def assess_segment(block: np.ndarray, fs: float, cfg: Config) -> SegmentQuality:
    """v0 rules for one segment. block: (channels, n) raw samples."""
    q = cfg.quality
    channels = {c: channel_status(block[CH[c]], cfg) for c in ADC_CHANNELS}
    for c in floating_channels(block, fs, cfg, [c for c, st in channels.items() if st == "ok"]):
        channels[c] = "floating"
    motion = motion_energy(block[[CH["ax"], CH["ay"], CH["az"]]], fs, cfg)
    off = [c for c in BREATHING_CHANNELS if channels[c] in OFF_STATUSES]
    if all(channels[c] == "missing" for c in BREATHING_CHANNELS) or motion is None:
        return SegmentQuality("unknown", 0.0, motion, channels, ["not enough data"])
    if off:
        reasons = [f"{c} {channels[c]}" for c in off]
        return SegmentQuality("sensor_off", q.sensor_off_conf, motion, channels, reasons)
    if motion > q.motion_threshold_g:
        conf = float(min(1.0, 0.5 + 0.5 * (motion - q.motion_threshold_g) / q.motion_threshold_g))
        return SegmentQuality("motion", round(conf, 3), motion, channels, [f"motion {motion:.3f} g"])
    reasons = [f"pulse {channels['pulse']}"] if channels["pulse"] != "ok" else []
    return SegmentQuality("clean", 1.0, motion, channels, reasons)


def segment_masks(block: np.ndarray, fs: float, cfg: Config) -> tuple[np.ndarray, list[SegmentQuality]]:
    """Split a rate window into quality segments (newest last) and build a NaN mask.

    Motion masks every channel in that segment; an off channel masks only itself. Returns the
    masked copy of `block` and the per-segment results.
    """
    n_seg = int(cfg.quality.window_s * fs)
    out = block.astype(float, copy=True)
    results: list[SegmentQuality] = []
    end = block.shape[1]
    starts = list(range(end - n_seg, -1, -n_seg))[::-1]
    for s in starts:
        seg = assess_segment(block[:, s:s + n_seg], fs, cfg)
        results.append(seg)
        if seg.label == "motion":
            out[:, s:s + n_seg] = np.nan
        for c, status in seg.channels.items():
            if status in OFF_STATUSES:
                out[CH[c], s:s + n_seg] = np.nan
    return out, results


def supine_rotation(gravity: np.ndarray) -> np.ndarray:
    """Rotation that maps the gravity vector measured while the patient lies flat onto supine
    (0, 0, 1) — the "calibrate supine" control (M10). It corrects how the patch is tilted on the
    chest; a rotation of the patch *within* the chest plane cannot be seen from lying flat alone."""
    a = np.asarray(gravity, dtype=float)
    a = a / np.linalg.norm(a)
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(a, z)
    s, c = float(np.linalg.norm(v)), float(a @ z)
    if s < 1e-9:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * ((1 - c) / s ** 2)


def posture(block: np.ndarray, cfg: Config, rotation: np.ndarray | None = None) -> str:
    """Posture from the mean gravity vector (default axes: x = patient's left, y = head, z = out
    of chest), after the "calibrate supine" rotation when one is set."""
    acc = block[[CH["ax"], CH["ay"], CH["az"]]]
    valid = ~np.isnan(acc).any(axis=0)
    if valid.mean() < cfg.quality.min_valid_fraction:
        return "unknown"
    g = acc[:, valid].mean(axis=1)
    if rotation is not None:
        g = rotation @ g
    for name, (axis, sign) in POSTURES.items():
        if g[axis] * sign >= cfg.quality.posture_min_g:
            return name
    return "unknown"


# --------------------------------------------------------------------------- v1 features (ML)

FEATURE_CHANNELS = ("pressure", "therm", "pulse")


def _band_stats(x: np.ndarray, fs: float, band: tuple[float, float]) -> tuple[float, float]:
    """(dominant frequency in band [Hz], its power share of the band power)."""
    n = len(x)
    spec = np.abs(np.fft.rfft((x - x.mean()) * np.hanning(n), n=n * 4)) ** 2
    freqs = np.fft.rfftfreq(n * 4, 1.0 / fs)
    sel = (freqs >= band[0]) & (freqs <= band[1])
    if not sel.any() or spec[sel].sum() <= 0:
        return 0.0, 0.0
    k = int(np.argmax(spec[sel]))
    f0 = float(freqs[sel][k])
    near = sel & (np.abs(freqs - f0) <= 0.05)
    return f0, float(spec[near].sum() / spec[sel].sum())


def window_features(block: np.ndarray, fs: float, cfg: Config) -> dict[str, float]:
    """Features for the v1 quality model (AGENTS.md §7): per-channel std, range, clipping
    fraction, median level, dominant frequency and its power share; belt-accel and belt-airflow
    correlation; motion energy mean/max; belt vs accel rate disagreement. block: (channels, n)
    raw samples without NaN."""
    from server.signals import accel_effort, bandpass   # local: signals imports this module
    s, q = cfg.signals, cfg.quality
    feats: dict[str, float] = {}
    for c in FEATURE_CHANNELS:
        x = block[CH[c]].astype(float)
        lo, hi = np.percentile(x, [1, 99])
        clipped = (x <= q.clip_margin_counts) | (x >= cfg.contract.adc_max - q.clip_margin_counts)
        band = s.pulse_band_hz if c == "pulse" else s.resp_band_hz
        f0, share = _band_stats(x, fs, band)
        feats.update({f"{c}_std": float(x.std()), f"{c}_range": float(hi - lo), f"{c}_clip": float(clipped.mean()),
                      f"{c}_median": float(np.median(x)), f"{c}_dom_hz": f0, f"{c}_dom_share": share})
    acc = block[[CH["ax"], CH["ay"], CH["az"]]].astype(float)
    belt = bandpass(block[CH["pressure"]].astype(float), fs, s.resp_band_hz, s.filter_order)
    air = bandpass(block[CH["therm"]].astype(float), fs, s.resp_band_hz, s.filter_order)
    eff = accel_effort(acc, fs, s)

    def corr(a: np.ndarray, b: np.ndarray) -> float:
        return float(abs(np.corrcoef(a, b)[0, 1])) if a.std() > 0 and b.std() > 0 else 0.0

    feats["belt_accel_corr"] = corr(belt, eff)
    feats["belt_airflow_corr"] = corr(belt, air)
    feats["accel_effort_std"] = float(eff.std())
    sos = _highpass_sos(s.motion_highpass_hz, fs, s.filter_order)
    mag = np.sqrt((sosfiltfilt(sos, acc, axis=1) ** 2).sum(axis=0))
    m = max(1, int(s.motion_rms_window_s * fs))
    rms = [float(np.sqrt(np.mean(mag[i:i + m] ** 2))) for i in range(0, len(mag) - m + 1, m)] or [0.0]
    feats["motion_mean"] = float(np.mean(rms))
    feats["motion_max"] = float(np.max(rms))
    acc_f0, _ = _band_stats(eff, fs, s.resp_band_hz)
    feats["rate_disagreement_bpm"] = abs(feats["pressure_dom_hz"] - acc_f0) * 60.0
    return feats
