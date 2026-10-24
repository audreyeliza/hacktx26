"""Typed loader for config.yaml. Unknown or missing keys fail loudly at startup."""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_type_hints

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config.yaml"


@dataclass(frozen=True)
class ContractCfg:
    version: int
    sample_rate_hz: int
    dt_ms: int
    batch_samples: int
    max_samples: int
    adc_max: int
    accel_max_g: float
    max_datagram_bytes: int


@dataclass(frozen=True)
class NetworkCfg:
    server_ip: str
    http_host: str
    http_port: int
    udp_host: str
    udp_port: int
    command_port: int
    serial_baud: int


@dataclass(frozen=True)
class IngestCfg:
    default_source: str
    default_speed: float
    buffer_s: int
    restart_seq_back: int
    serial_reconnect_s: float
    serial_read_timeout_s: float
    serial_max_line_bytes: int
    serial_pico_vid: int
    link_timeout_s: float


@dataclass(frozen=True)
class LiveCfg:
    push_hz: float
    wave_decimate: int
    wave_window_s: float
    status_interval_s: float
    trend_window_s: float


@dataclass(frozen=True)
class SimulatorCfg:
    seed: int
    device: str
    ticks_wrap: int
    start_ticks_ms: int
    pressure_dc: float
    pressure_amp: float
    pressure_noise: float
    pressure_drift_amp: float
    pressure_drift_hz: float
    therm_dc: float
    therm_amp: float
    therm_noise: float
    therm_lag_s: float
    therm_tau_s: float
    therm_ambient: float
    pulse_dc: float
    pulse_amp: float
    pulse_noise: float
    accel_breath_g: float
    accel_noise_g: float
    motion_accel_g: float
    motion_pressure_amp: float
    motion_therm_amp: float
    envelope_tau_s: float
    apnea_residual: float
    hypopnea_level: float
    hr_bpm: float
    hr_wobble_bpm: float
    hr_wobble_period_s: float
    pulse_resp_mod: float
    rr_wobble_bpm: float
    rr_wobble_period_s: float
    breath_skew: float
    labels_dir: str


@dataclass(frozen=True)
class SignalsCfg:
    filter_order: int
    resp_band_hz: tuple[float, float]
    pulse_band_hz: tuple[float, float]
    motion_highpass_hz: float
    motion_rms_window_s: float
    rate_interval_s: float
    rate_window_s: float
    prelim_window_s: float
    min_valid_fraction: float
    rr_min_bpm: float
    rr_max_bpm: float
    hr_min_bpm: float
    hr_max_bpm: float
    peak_prominence_frac: float
    fft_confirm_bpm: float
    agree_bpm: float
    single_source_conf_cap: float
    flat_std_counts: float
    accel_min_breath_g: float


@dataclass(frozen=True)
class Config:
    contract: ContractCfg
    network: NetworkCfg
    ingest: IngestCfg
    live: LiveCfg
    simulator: SimulatorCfg
    signals: SignalsCfg


def _build(cls: type, raw: dict[str, Any], where: str) -> Any:
    hints = get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}
    extra = set(raw) - names
    missing = names - set(raw)
    if extra or missing:
        raise ValueError(f"config [{where}]: unknown keys {sorted(extra)}, missing keys {sorted(missing)}")
    kwargs: dict[str, Any] = {}
    for name in names:
        hint = hints[name]
        value = raw[name]
        if dataclasses.is_dataclass(hint):
            kwargs[name] = _build(hint, value, f"{where}.{name}" if where else name)
        elif getattr(hint, "__origin__", None) is tuple:
            kwargs[name] = tuple(float(v) for v in value)
        elif hint is float:
            kwargs[name] = float(value)
        else:
            kwargs[name] = value
    return cls(**kwargs)


def load_config(path: str | Path | None = None) -> Config:
    """Load config from `path`, $BREATHWATCH_CONFIG, or the repo's config.yaml."""
    path = Path(path or os.environ.get("BREATHWATCH_CONFIG") or DEFAULT_PATH)
    with path.open() as f:
        raw = yaml.safe_load(f)
    return _build(Config, raw, "")
