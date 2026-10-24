"""Filtering, rate estimation and rate fusion (AGENTS.md section 7).

Rates are computed every 5 s of sample time on rolling windows from the ring buffers:
60 s for the reported breathing rate, 30 s for a display-only preliminary estimate,
15 s for heart rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import numpy as np
from scipy.signal import butter, detrend, find_peaks, sosfiltfilt

from server.config import Config, SignalsCfg
from server.ingest import CH, RingBuffer
from server.quality import OFF_STATUSES, posture, segment_masks

RESP_SOURCES = ("belt", "accel", "airflow")


@dataclass
class RateEstimate:
    bpm: float | None
    confidence: float = 0.0
    amplitude: float = 0.0      # robust half peak-to-peak of the filtered signal
    fft_bpm: float | None = None
    reason: str = ""            # why bpm is None or confidence is low


@dataclass
class FusedRate:
    bpm: float | None
    confidence: float
    sources: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- filtering

@lru_cache(maxsize=16)
def _bandpass_sos(low: float, high: float, fs: float, order: int) -> np.ndarray:
    return butter(order, [low, high], btype="bandpass", fs=fs, output="sos")


def bandpass(x: np.ndarray, fs: float, band: tuple[float, float], order: int,
             padlen: int | None = None) -> np.ndarray:
    """Zero-phase band-pass of a window (linear detrend first). `padlen` (samples) overrides
    scipy's very short default edge padding when values near the window edge matter."""
    if padlen is not None:
        padlen = min(padlen, len(x) - 1)
    return sosfiltfilt(_bandpass_sos(band[0], band[1], fs, order), detrend(x), padlen=padlen)


def fill_gaps(x: np.ndarray, min_valid_fraction: float) -> np.ndarray | None:
    """Linearly interpolate NaN samples; None if too little of the window is valid."""
    x = np.asarray(x, dtype=float)
    valid = ~np.isnan(x)
    if valid.mean() < min_valid_fraction or valid.sum() < 2:
        return None
    if valid.all():
        return x
    idx = np.arange(len(x))
    return np.interp(idx, idx[valid], x[valid])


def robust_amplitude(y: np.ndarray) -> float:
    lo, hi = np.percentile(y, [5, 95])
    return float(hi - lo) / 2.0


def accel_effort(axes: np.ndarray, fs: float, cfg: SignalsCfg) -> np.ndarray:
    """Band-pass each accel axis, then project onto the direction with the most breathing power."""
    filt = np.vstack([bandpass(a, fs, cfg.resp_band_hz, cfg.filter_order) for a in axes])
    cov = np.cov(filt)
    _, vecs = np.linalg.eigh(cov)
    return vecs[:, -1] @ filt


# --------------------------------------------------------------------------- rates

def dominant_bpm(y: np.ndarray, fs: float, lo_bpm: float, hi_bpm: float, pad: int) -> float | None:
    n = len(y)
    spec = np.abs(np.fft.rfft(y * np.hanning(n), n=n * pad)) ** 2
    freqs = np.fft.rfftfreq(n * pad, 1.0 / fs) * 60.0
    band = (freqs >= lo_bpm) & (freqs <= hi_bpm)
    if not band.any() or spec[band].max() <= 0:
        return None
    return float(freqs[band][np.argmax(spec[band])])


def peak_rate(y: np.ndarray, fs: float, lo_bpm: float, hi_bpm: float, cfg: SignalsCfg,
              min_amplitude: float) -> RateEstimate:
    """Rate from peak-to-peak intervals of an already filtered signal, confirmed by FFT."""
    amp = robust_amplitude(y)
    if amp < min_amplitude:
        return RateEstimate(None, 0.0, amp, reason="flat")
    peaks, _ = find_peaks(y, distance=max(1, int(fs * 60.0 / hi_bpm)),
                          prominence=cfg.peak_prominence_frac * 2 * amp)
    intervals = np.diff(peaks) / fs
    if len(intervals) < cfg.min_intervals:
        return RateEstimate(None, 0.0, amp, reason="too few peaks")
    bpm = 60.0 / float(np.median(intervals))
    if not lo_bpm <= bpm <= hi_bpm:
        return RateEstimate(None, 0.0, amp, reason="out of range")
    fft = dominant_bpm(y, fs, lo_bpm, hi_bpm, cfg.fft_pad_factor)
    reasons = []
    conf = 1.0
    if fft is None or abs(fft - bpm) > cfg.fft_confirm_bpm:
        diff = abs(fft - bpm) if fft is not None else np.inf
        conf *= float(np.clip(1.0 - (diff - cfg.fft_confirm_bpm) / (2 * cfg.fft_confirm_bpm), 0.3, 1.0))
        reasons.append("peaks/FFT disagree")
    cv = float(np.std(intervals) / np.mean(intervals))
    reg = float(np.clip(1.0 - cv / cfg.interval_cv_max, 0.0, 1.0))
    if reg < 1.0 and cv > cfg.interval_cv_max / 3:
        reasons.append("irregular")
    conf *= 0.5 + 0.5 * reg
    return RateEstimate(round(bpm, 2), round(conf, 3), amp, fft, ", ".join(reasons))


def breath_rates(block: np.ndarray, fs: float, cfg: SignalsCfg) -> dict[str, RateEstimate]:
    """Per-source breathing rate for one window. block: (channels, n) raw samples."""
    out: dict[str, RateEstimate] = {}
    lo, hi = cfg.rr_min_bpm, cfg.rr_max_bpm
    for name, ch in (("belt", "pressure"), ("airflow", "therm")):
        x = fill_gaps(block[CH[ch]], cfg.min_valid_fraction)
        if x is None:
            out[name] = RateEstimate(None, reason="missing data")
            continue
        y = bandpass(x, fs, cfg.resp_band_hz, cfg.filter_order)
        out[name] = peak_rate(y, fs, lo, hi, cfg, cfg.flat_std_counts)
    axes = [fill_gaps(block[CH[a]], cfg.min_valid_fraction) for a in ("ax", "ay", "az")]
    if any(a is None for a in axes):
        out["accel"] = RateEstimate(None, reason="missing data")
    else:
        y = accel_effort(np.vstack(axes), fs, cfg)
        out["accel"] = peak_rate(y, fs, lo, hi, cfg, cfg.accel_min_breath_g)
    return out


def heart_rate(pulse: np.ndarray, fs: float, cfg: SignalsCfg) -> RateEstimate:
    x = fill_gaps(pulse, cfg.min_valid_fraction)
    if x is None:
        return RateEstimate(None, reason="missing data")
    y = bandpass(x, fs, cfg.pulse_band_hz, cfg.filter_order)
    return peak_rate(y, fs, cfg.hr_min_bpm, cfg.hr_max_bpm, cfg, cfg.flat_std_counts)


# --------------------------------------------------------------------------- fusion

def fuse(estimates: dict[str, RateEstimate], cfg: SignalsCfg,
         clean: dict[str, bool] | None = None) -> FusedRate:
    """Combine per-source rates (AGENTS.md section 7, `rr_fused`).

    `clean` lets the quality stage (milestone 4) exclude a source; by default a source is
    usable when it has a rate with confidence >= source_min_conf.
    """
    usable = {
        k: e for k, e in estimates.items()
        if e.bpm is not None and e.confidence >= cfg.source_min_conf and (clean or {}).get(k, True)
    }
    if not usable:
        return FusedRate(None, 0.0, [], ["no_clean_source"])
    if len(usable) == 1:
        (name, e), = usable.items()
        return FusedRate(e.bpm, min(e.confidence, cfg.single_source_conf_cap), [name], ["single_source"])

    # largest group of sources whose rates lie within agree_bpm of each other
    items = sorted(usable.items(), key=lambda kv: kv[1].bpm)
    best: list[tuple[str, RateEstimate]] = []
    for i in range(len(items)):
        group = [kv for kv in items[i:] if kv[1].bpm - items[i][1].bpm <= cfg.agree_bpm]
        better = len(group) > len(best) or (
            len(group) == len(best) and sum(e.confidence for _, e in group) > sum(e.confidence for _, e in best))
        if better:
            best = group
    if len(best) < 2:
        name, e = max(usable.items(), key=lambda kv: kv[1].confidence)
        return FusedRate(e.bpm, min(e.confidence, cfg.disagree_conf_cap), [name], ["sources_disagree"])
    names = [k for k, _ in best]
    bpm = float(np.median([e.bpm for _, e in best]))
    conf = float(np.mean([e.confidence for _, e in best]))
    flags = []
    excluded = sorted(set(usable) - set(names))
    if excluded:
        flags.append("outlier:" + ",".join(excluded))
    return FusedRate(round(bpm, 2), round(conf, 3), names, flags)


# --------------------------------------------------------------------------- vitals

def compute_vitals(buffers: RingBuffer, end: int, fs: float, cfg: Config) -> dict[str, Any]:
    return analyze(buffers, end, fs, cfg)[0]


def analyze(buffers: RingBuffer, end: int, fs: float, cfg: Config,
            rotation: np.ndarray | None = None) -> tuple[dict[str, Any], np.ndarray]:
    """All rates, quality and posture for the windows ending at sample index `end`, plus the
    quality-masked 60 s block (for event detection).

    The 60 s rate window is split into 10 s quality segments; motion segments (all channels)
    and off channels are masked as missing data before rates are estimated, so an artifact
    from 40 s ago cannot corrupt a rate reported under a "clean" label.
    """
    s = cfg.signals
    n_rate = int(s.rate_window_s * fs)
    n_prelim = int(s.prelim_window_s * fs)
    n_seg = int(cfg.quality.window_s * fs)
    raw = buffers.window(end, n_rate)
    block, segments = segment_masks(raw, fs, cfg)
    current = segments[-1]
    off = {c for c, st in current.channels.items() if st in OFF_STATUSES}
    clean = {"belt": "pressure" not in off, "airflow": "therm" not in off, "accel": True}

    rates = breath_rates(block, fs, s)
    fused = fuse(rates, s, clean)
    prelim = fuse(breath_rates(block[:, -n_prelim:], fs, s), s, clean)
    hr = heart_rate(block[CH["pulse"], -int(s.hr_window_s * fs):], fs, s)

    label, qconf, reasons = current.label, current.confidence, list(current.reasons)
    belt, acc = rates["belt"].bpm, rates["accel"].bpm
    if label == "clean" and belt is not None and acc is not None and abs(belt - acc) > s.agree_bpm:
        qconf = cfg.quality.disagree_conf
        reasons.append(f"belt {belt:.0f} vs accel {acc:.0f}/min disagree")

    v: dict[str, Any] = {"idx": end}
    for name in RESP_SOURCES:
        e = rates[name]
        v[f"rr_{name}"] = e.bpm
        v[f"conf_{name}"] = e.confidence
        v[f"amp_{name}"] = round(e.amplitude, 4)
        v[f"why_{name}"] = e.reason
    v.update({
        "rr_fused": fused.bpm, "rr_conf": fused.confidence, "rr_sources": fused.sources,
        "flags": fused.flags, "rr_prelim": prelim.bpm, "hr": hr.bpm, "hr_conf": hr.confidence,
        "quality": label, "quality_conf": qconf, "quality_reasons": reasons,
        "channels": current.channels,
        "motion": None if current.motion is None else round(current.motion, 4),
        "clean_fraction": round(sum(g.label == "clean" for g in segments) / len(segments), 3),
        "posture": posture(raw[:, -n_seg:], cfg, rotation),
    })
    return v, block
