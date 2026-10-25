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
    float_dc: float
    float_wander: float
    float_wander_tau_s: float
    float_jitter: float
    float_indep: float
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
    hr_window_s: float
    min_valid_fraction: float
    rr_min_bpm: float
    rr_max_bpm: float
    hr_min_bpm: float
    hr_max_bpm: float
    peak_prominence_frac: float
    min_intervals: int
    interval_cv_max: float
    fft_pad_factor: int
    fft_confirm_bpm: float
    source_min_conf: float
    agree_bpm: float
    single_source_conf_cap: float
    disagree_conf_cap: float
    flat_std_counts: float
    accel_min_breath_g: float


@dataclass(frozen=True)
class QualityCfg:
    window_s: float
    min_valid_fraction: float
    flat_raw_std_counts: float
    clip_margin_counts: float
    clip_fraction: float
    motion_threshold_g: float
    disagree_conf: float
    sensor_off_conf: float
    posture_min_g: float
    calib_window_s: float
    calib_max_std_g: float
    calib_g_range: tuple[float, float]
    float_band_counts: tuple[float, float]
    float_min_std_counts: float
    float_hf_hz: float
    float_corr: float
    float_min_hf_share: float
    model_path: str


@dataclass(frozen=True)
class EventsCfg:
    point_s: float
    lag_s: float
    provisional_lag_s: float
    watch_interval_s: float
    env_smooth_s: float
    edge_pad_s: float
    baseline_window_s: float
    baseline_min_s: float
    apnea_frac: float
    hypopnea_frac: float
    min_event_s: float
    edge_trim_s: float
    max_event_s: float
    obstructive_effort_frac: float
    central_effort_frac: float
    clear_conf: float
    ambiguous_conf: float
    recent_max: int


@dataclass(frozen=True)
class AlertsCfg:
    gate_quality_conf: float
    gate_rr_conf: float
    rr_tier1_below: float
    rr_tier1_persist_s: float
    rr_tier2_below: float
    rr_tier2_persist_s: float
    rr_tier3_below: float
    rr_tier3_persist_s: float
    baseline_window_s: float
    baseline_min_s: float
    trend_drop_frac: float
    trend_window_s: float
    trend_recent_frac: float
    trend_persist_s: float
    apnea_tier1_s: float
    apnea_tier3_s: float
    cluster_count: int
    cluster_window_s: float
    sensor_off_s: float
    cooldown_s: float
    escalate_s: float
    latch_tier: int
    clear_s: float
    conf_high: float


@dataclass(frozen=True)
class NaiveCfg:
    rr_low: float
    rr_high: float
    hr_low: float
    hr_high: float
    flat_s: float
    flat_std_counts: float
    rr_window_s: float
    hr_window_s: float
    peak_prominence_counts: float
    rr_min_gap_s: float
    hr_min_gap_s: float


@dataclass(frozen=True)
class BedsideCfg:
    command_interval_s: float


@dataclass(frozen=True)
class StoreCfg:
    path: str


@dataclass(frozen=True)
class AccountCfg:
    username: str
    display_name: str
    role: str
    password: str


@dataclass(frozen=True)
class AuthCfg:
    enabled: bool
    cookie_name: str
    session_ttl_h: float
    accounts: list[AccountCfg]


@dataclass(frozen=True)
class WardCfg:
    live_device_id: str
    random_offset: bool
    ward_push_s: float
    backfill_s: float
    seed: list[dict[str, Any]]   # PatientCreate records, validated by server.patients


@dataclass(frozen=True)
class RecordCfg:
    dir: str
    labels: list[str]


@dataclass(frozen=True)
class LLMCfg:
    provider: str
    model: str
    handoff_model: str
    fallback_models: list[str]
    attempts_per_model: int
    api_key_env: str
    timeout_s: float
    total_timeout_s: float
    temperature: float
    max_output_tokens: int
    explain_window_s: float
    overnight_window_s: float
    max_events_listed: int
    max_alerts_listed: int
    notes_max_chars: int


@dataclass(frozen=True)
class PublicDemoCfg:
    enabled: bool
    live_bed_scenario: str
    live_bed_start_s: float
    http_host: str
    proxy_ips: str
    reset_cooldown_s: float
    summary_per_session_per_h: int
    summary_global_per_h: int
    max_patients: int
    banner: str


@dataclass(frozen=True)
class DemoCfg:
    fast_warmup: bool
    warmup_min_s: float
    warmup_conf_cap: float


@dataclass(frozen=True)
class DisplayCfg:
    timezone: str
    shift_change_hours: list[int]
    rates_after_hours: float


@dataclass(frozen=True)
class Config:
    contract: ContractCfg
    network: NetworkCfg
    ingest: IngestCfg
    live: LiveCfg
    simulator: SimulatorCfg
    signals: SignalsCfg
    quality: QualityCfg
    events: EventsCfg
    alerts: AlertsCfg
    naive: NaiveCfg
    bedside: BedsideCfg
    store: StoreCfg
    auth: AuthCfg
    ward: WardCfg
    record: RecordCfg
    llm: LLMCfg
    public_demo: PublicDemoCfg
    demo: DemoCfg
    display: DisplayCfg


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
        origin = getattr(hint, "__origin__", None)
        if dataclasses.is_dataclass(hint):
            kwargs[name] = _build(hint, value, f"{where}.{name}" if where else name)
        elif origin is list:
            item = hint.__args__[0]
            if dataclasses.is_dataclass(item):
                kwargs[name] = [_build(item, v, f"{where}.{name}[{i}]") for i, v in enumerate(value)]
            else:
                kwargs[name] = list(value)
        elif origin is tuple:
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
