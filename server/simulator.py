"""Synthetic BreathWatch sensor data in the exact section 4 packet format.

Usage:
    python -m server.simulator --scenario normal --udp            # real-time UDP to 127.0.0.1:5005
    python -m server.simulator --scenario demo --udp --speed 10    # 10x faster
    python -m server.simulator --scenario motion --stdout          # JSON lines, like USB serial

Each scenario is a timeline of phases. Ground-truth labels (events, quality segments, true
breathing rate) are written to data/sim_labels/<scenario>.json so tests can score detection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import lfilter, lfilter_zi

from server.config import ROOT, Config, load_config
from server.schema import encode_packet

log = logging.getLogger(__name__)

# Gravity vector (device axes: x = patient's left, y = toward head, z = out of the chest).
POSTURE_GRAVITY: dict[str, tuple[float, float, float]] = {
    "supine": (0.0, 0.0, 1.0),
    "left": (-1.0, 0.0, 0.0),
    "right": (1.0, 0.0, 0.0),
    "upright": (0.0, 1.0, 0.0),
}
# Direction of breathing-induced tilt in device axes; non-degenerate for every posture.
BREATH_TILT = np.array([0.4, -1.0, 0.3])

SENSOR_MODES = ("therm_flat", "therm_rail_low", "pressure_rail_high", "therm_floating", "all_floating")
# Which ADC channels float (unconnected pin) in each floating mode.
FLOATING = {"therm_floating": ("therm",), "all_floating": ("pressure", "therm", "pulse")}


@dataclass(frozen=True)
class Phase:
    duration_s: float
    rr: tuple[float, float] = (15.0, 15.0)   # breaths/min at phase start and end (linear)
    amp: tuple[float, float] = (1.0, 1.0)    # overall breath amplitude at start and end
    effort: float = 1.0                       # extra multiplier on effort (belt + accel)
    airflow: float = 1.0                      # extra multiplier on airflow (thermistor)
    wobble: bool = False                      # normal 14-16/min wander
    motion: bool = False
    sensor: str | None = None                 # one of SENSOR_MODES
    event: str | None = None                  # obstructive | central | hypopnea
    posture: str = "supine"


# --------------------------------------------------------------------------- scenarios

def _normal(d: float, rr: float = 15.0, posture: str = "supine", wobble: bool = True) -> Phase:
    return Phase(d, rr=(rr, rr), wobble=wobble, posture=posture)


def _motion_block(rng: np.random.Generator, bursts: int) -> list[Phase]:
    phases: list[Phase] = []
    posture = "supine"
    for i in range(bursts):
        phases.append(Phase(float(rng.uniform(8, 20)), motion=True, posture=posture))
        if i == 1:
            posture = "left"       # patient rolls over during a burst
        elif i == 3:
            posture = "supine"
        phases.append(_normal(float(rng.uniform(25, 45)), posture=posture))
    return phases


def _apnea_block(rng: np.random.Generator, kind: str, durations: list[float], residual: float,
                 hypopnea_level: float) -> list[Phase]:
    if kind == "obstructive":
        effort, airflow = 1.1, residual
    elif kind == "central":
        effort, airflow = residual, residual
    else:
        effort, airflow = hypopnea_level, hypopnea_level
    phases: list[Phase] = []
    for d in durations:
        phases.append(Phase(d, effort=effort, airflow=airflow, event=kind))
        phases.append(_normal(float(rng.uniform(40, 70))))
    return phases


def build_scenario(name: str, rng: np.random.Generator, residual: float,
                   hypopnea_level: float) -> list[Phase]:
    def apneas(kind: str, durations: list[float]) -> list[Phase]:
        return _apnea_block(rng, kind, durations, residual, hypopnea_level)

    if name == "normal":
        return [_normal(600)]
    if name == "motion":
        return [_normal(120)] + _motion_block(rng, 6)
    if name == "opioid_decline":
        return [
            _normal(120, rr=16, wobble=False),
            Phase(600, rr=(16, 6), amp=(1.0, 0.55)),
            Phase(120, rr=(6, 6), amp=(0.55, 0.55)),
        ]
    if name in ("obstructive", "central"):
        return [_normal(120)] + apneas(name, [float(rng.uniform(15, 25)) for _ in range(6)])
    if name == "hypopnea":
        return [_normal(120)] + apneas("hypopnea", [15.0] * 6)
    if name == "sensor_off":
        return [
            _normal(120),
            Phase(75, sensor="therm_flat"), _normal(60),
            Phase(75, sensor="pressure_rail_high"), _normal(60),
            Phase(45, sensor="therm_rail_low"), _normal(60),
            Phase(70, sensor="therm_floating"), _normal(60),
            Phase(70, sensor="all_floating"), _normal(60),
        ]
    if name == "demo":
        # normal -> motion -> opioid decline (to 5/min so tier 3 is reachable) -> recovery
        # -> obstructive (one 32 s) -> central (one 26 s). The long apneas are well past 20 s so
        # the tier-3 alarm (~21-22 s into an apnea) visibly fires while the apnea is still going.
        return (
            [_normal(180)]
            + _motion_block(rng, 4)
            + [
                Phase(300, rr=(16, 5), amp=(1.0, 0.5)),
                Phase(90, rr=(5, 5), amp=(0.5, 0.5)),
                Phase(30, rr=(5, 15), amp=(0.5, 1.0)),
                _normal(60),
            ]
            + apneas("obstructive", [14.0, 32.0, 16.0])
            + apneas("central", [26.0, 15.0])
        )
    raise ValueError(f"unknown scenario {name!r}; choose from {SCENARIO_NAMES}")


SCENARIO_NAMES = ("normal", "motion", "opioid_decline", "obstructive", "central", "hypopnea",
                  "sensor_off", "demo")


# --------------------------------------------------------------------------- generator

@dataclass
class _MotionComponents:
    freqs: np.ndarray   # (channels, k)
    amps: np.ndarray
    phases: np.ndarray


@dataclass
class _GenState:
    breath_phase: float = 0.0
    hr_phase: float = 0.0
    env_zi: dict[str, np.ndarray] = field(default_factory=dict)
    therm_zi: np.ndarray | None = None
    float_zi: np.ndarray | None = None


def adc_u16(values: np.ndarray) -> list[int]:
    """Quantize like the RP2 ADC: 12-bit sample scaled by MicroPython's read_u16."""
    raw12 = np.clip(np.rint(values / 16.0), 0, 4095).astype(np.int64)
    return ((raw12 << 4) | (raw12 >> 8)).tolist()


def accel_g(values: np.ndarray, max_g: float) -> list[float]:
    """Quantize like the MMA8452Q at +/-2 g (1024 counts/g), rounded to 3 decimals."""
    lim = max_g - 1.0 / 1024
    return [round(float(v), 3) for v in np.clip(np.rint(values * 1024) / 1024, -lim, lim)]


class Simulator:
    """Generates section 4 packets for a scenario, one batch at a time."""

    def __init__(self, scenario: str, cfg: Config, seed: int | None = None, loop: bool = False,
                 device: str | None = None, start_s: float = 0.0) -> None:
        self.scenario = scenario
        self.cfg = cfg
        self.sc = cfg.simulator
        self.loop = loop
        self.device = device or self.sc.device
        seed = self.sc.seed if seed is None else seed
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.phases = build_scenario(scenario, np.random.default_rng(seed + 1),
                                     self.sc.apnea_residual, self.sc.hypopnea_level)
        self.fs = float(cfg.contract.sample_rate_hz)
        self.batch = cfg.contract.batch_samples
        self.starts = np.concatenate([[0.0], np.cumsum([p.duration_s for p in self.phases])])
        self.duration_s = float(self.starts[-1])
        self.total_samples = int(round(self.duration_s * self.fs))
        # start part-way into the scenario (whole packets), e.g. so a simulated ward is not in lockstep
        self.n = int(start_s * self.fs) // self.batch * self.batch % max(self.total_samples, 1)
        self.seq = 0
        self._st = _GenState()
        self._tab = self._phase_table()
        self._motion = [self._motion_components() if p.motion else None for p in self.phases]

    # ---- per-phase lookup tables
    def _phase_table(self) -> dict[str, np.ndarray]:
        ps = self.phases
        return {
            "r0": np.array([p.rr[0] for p in ps]), "r1": np.array([p.rr[1] for p in ps]),
            "a0": np.array([p.amp[0] for p in ps]), "a1": np.array([p.amp[1] for p in ps]),
            "effort": np.array([p.effort for p in ps]), "airflow": np.array([p.airflow for p in ps]),
            "wobble": np.array([p.wobble for p in ps]), "motion": np.array([p.motion for p in ps]),
            "sensor": np.array([SENSOR_MODES.index(p.sensor) if p.sensor else -1 for p in ps]),
            "gravity": np.array([POSTURE_GRAVITY[p.posture] for p in ps]),
        }

    def _motion_components(self) -> _MotionComponents:
        # channels: ax, ay, az, pressure, therm, pulse. Low frequencies on pressure/therm are what
        # make motion look like breathing; that is the point of the scenario.
        k = 4
        freqs = np.empty((6, k))
        freqs[:3] = self.rng.uniform(1.5, 6.0, (3, k))
        freqs[3:] = self.rng.uniform(0.2, 4.0, (3, k))
        amps = self.rng.uniform(0.3, 1.0, (6, k))
        amps /= amps.sum(axis=1, keepdims=True)
        return _MotionComponents(freqs, amps, self.rng.uniform(0, 2 * np.pi, (6, k)))

    # ---- ground truth
    def _phase_index(self, t_local: np.ndarray) -> np.ndarray:
        idx = np.searchsorted(self.starts, t_local, side="right") - 1
        return np.clip(idx, 0, len(self.phases) - 1)

    def true_rr(self, t_s: np.ndarray | float) -> np.ndarray:
        """True breathing rhythm (breaths/min) at scenario time t_s."""
        t = np.atleast_1d(np.asarray(t_s, dtype=float)) % self.duration_s
        i = self._phase_index(t)
        tab = self._tab
        u = (t - self.starts[i]) / np.array([p.duration_s for p in self.phases])[i]
        rr = tab["r0"][i] + (tab["r1"][i] - tab["r0"][i]) * u
        wob = self.sc.rr_wobble_bpm * np.sin(2 * np.pi * t / self.sc.rr_wobble_period_s)
        return rr + np.where(tab["wobble"][i], wob, 0.0)

    def labels(self) -> dict[str, Any]:
        events = [
            {"type": p.event, "start_s": float(self.starts[i]), "end_s": float(self.starts[i + 1]),
             "duration_s": p.duration_s, "posture": p.posture}
            for i, p in enumerate(self.phases) if p.event
        ]
        quality: list[dict[str, Any]] = []
        for i, p in enumerate(self.phases):
            label = "motion" if p.motion else "sensor_off" if p.sensor else "clean"
            if quality and quality[-1]["label"] == label:
                quality[-1]["end_s"] = float(self.starts[i + 1])
            else:
                quality.append({"label": label, "start_s": float(self.starts[i]),
                                "end_s": float(self.starts[i + 1])})
        t = np.arange(0.0, self.duration_s, 1.0)
        return {
            "scenario": self.scenario, "seed": self.seed, "duration_s": self.duration_s,
            "sample_rate_hz": self.fs, "events": events, "quality": quality,
            "rr_truth": [[float(a), round(float(b), 3)] for a, b in zip(t, self.true_rr(t))],
        }

    def write_labels(self, path: str | Path | None = None) -> Path:
        path = Path(path) if path else ROOT / self.sc.labels_dir / f"{self.scenario}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.labels(), indent=1))
        return path

    # ---- packets
    @property
    def done(self) -> bool:
        return not self.loop and self.n >= self.total_samples

    def _smooth(self, x: np.ndarray, tau_s: float, zi: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
        alpha = (1.0 / self.fs) / (tau_s + 1.0 / self.fs)
        b, a = [alpha], [1.0, alpha - 1.0]
        if zi is None:
            zi = lfilter_zi(b, a) * x[0]
        return lfilter(b, a, x, zi=zi)

    def _shape(self, phase_cycles: np.ndarray) -> np.ndarray:
        x = 2 * np.pi * phase_cycles
        return np.sin(x + self.sc.breath_skew * np.sin(x))

    def next_packet(self) -> dict[str, Any]:
        sc, fs, n = self.sc, self.fs, self.batch
        k = np.arange(self.n, self.n + n)
        t_abs = k / fs
        t = t_abs % self.duration_s
        i = self._phase_index(t)
        tab = self._tab
        st = self._st

        # breathing rhythm and amplitude envelopes
        rr = self.true_rr(t)
        u = (t - self.starts[i]) / np.array([p.duration_s for p in self.phases])[i]
        amp = tab["a0"][i] + (tab["a1"][i] - tab["a0"][i]) * u
        bphase = st.breath_phase + np.cumsum(rr / 60.0 / fs)
        st.breath_phase = float(bphase[-1] % 1.0)
        env_e, st.env_zi["e"] = self._smooth(amp * tab["effort"][i], sc.envelope_tau_s, st.env_zi.get("e"))
        env_a, st.env_zi["a"] = self._smooth(amp * tab["airflow"][i], sc.envelope_tau_s, st.env_zi.get("a"))
        effort = env_e * self._shape(bphase)
        air_raw = env_a * self._shape(bphase - sc.therm_lag_s * rr / 60.0)
        airflow, st.therm_zi = self._smooth(air_raw, sc.therm_tau_s, st.therm_zi)

        # heart
        hr = sc.hr_bpm + sc.hr_wobble_bpm * np.sin(2 * np.pi * t_abs / sc.hr_wobble_period_s)
        hphase = st.hr_phase + np.cumsum(hr / 60.0 / fs)
        st.hr_phase = float(hphase[-1] % 1.0)
        x = hphase % 1.0
        ppg = np.exp(-(((x - 0.2) / 0.06) ** 2)) + 0.45 * np.exp(-(((x - 0.5) / 0.09) ** 2))

        rnd = self.rng.standard_normal
        drift = sc.pressure_drift_amp * np.sin(2 * np.pi * sc.pressure_drift_hz * t_abs)
        pressure = sc.pressure_dc + sc.pressure_amp * effort + drift + sc.pressure_noise * rnd(n)
        therm = sc.therm_dc - sc.therm_amp * airflow + sc.therm_noise * rnd(n)
        pulse = sc.pulse_dc + sc.pulse_amp * ppg + sc.pulse_resp_mod * effort + sc.pulse_noise * rnd(n)
        acc = (tab["gravity"][i] + np.outer(effort * sc.accel_breath_g, BREATH_TILT)
               + sc.accel_noise_g * rnd((n, 3)))

        # motion artifacts
        for j in np.unique(i[tab["motion"][i]]):
            m = self._motion[j]
            sel = i == j
            ts = t_abs[sel][:, None]
            comp = (m.amps[:, None, :] * np.sin(2 * np.pi * m.freqs[:, None, :] * ts[None] + m.phases[:, None, :])).sum(-1)
            comp += 0.3 * rnd(comp.shape)
            acc[sel] += sc.motion_accel_g * comp[:3].T
            pressure[sel] += sc.motion_pressure_amp * comp[3]
            therm[sel] += sc.motion_therm_amp * comp[4]
            pulse[sel] += sc.pulse_amp * comp[5]

        # sensor faults
        mode = tab["sensor"][i]
        therm = np.where(mode == SENSOR_MODES.index("therm_flat"), sc.therm_ambient + 8 * rnd(n), therm)
        therm = np.where(mode == SENSOR_MODES.index("therm_rail_low"), 0.0, therm)
        pressure = np.where(mode == SENSOR_MODES.index("pressure_rail_high"), 70000.0, pressure)
        floating = {m: mode == SENSOR_MODES.index(m) for m in FLOATING}
        if any(f.any() for f in floating.values()):
            # unconnected pins: one shared wander + shared jitter (they move together), tiny per-pin noise
            wander, st.float_zi = self._smooth(rnd(n), sc.float_wander_tau_s, st.float_zi)
            common = sc.float_dc + sc.float_wander * wander * 3 + sc.float_jitter * rnd(n)
            chans = {"pressure": pressure, "therm": therm, "pulse": pulse}
            for m, sel in floating.items():
                for c in FLOATING[m]:
                    chans[c] = np.where(sel, common + sc.float_indep * rnd(n), chans[c])
            pressure, therm, pulse = chans["pressure"], chans["therm"], chans["pulse"]

        max_g = self.cfg.contract.accel_max_g
        packet = {
            "v": self.cfg.contract.version,
            "device": self.device,
            "seq": self.seq,
            "t0_ms": int((sc.start_ticks_ms + self.n * self.cfg.contract.dt_ms) % sc.ticks_wrap),
            "dt_ms": self.cfg.contract.dt_ms,
            "pressure": adc_u16(pressure),
            "pulse": adc_u16(pulse),
            "therm": adc_u16(therm),
            "ax": accel_g(acc[:, 0], max_g),
            "ay": accel_g(acc[:, 1], max_g),
            "az": accel_g(acc[:, 2], max_g),
        }
        self.n += n
        self.seq += 1
        return packet

    def packets(self, max_packets: int | None = None) -> Iterator[dict[str, Any]]:
        count = 0
        while not self.done and (max_packets is None or count < max_packets):
            yield self.next_packet()
            count += 1


# --------------------------------------------------------------------------- runners

PacketSink = Callable[[dict[str, Any]], Awaitable[None] | None]


async def stream(sim: Simulator, sink: PacketSink, speed: float = 1.0,
                 stop: asyncio.Event | None = None) -> None:
    """Feed packets to `sink` paced at `speed` x real time (in-process source)."""
    period = sim.batch / sim.fs / speed if speed > 0 else 0.0
    start = time.monotonic()
    sent = 0
    while not sim.done and not (stop and stop.is_set()):
        result = sink(sim.next_packet())
        if asyncio.iscoroutine(result):
            await result
        sent += 1
        delay = start + sent * period - time.monotonic()
        await asyncio.sleep(max(0.0, delay))


def _run_cli(args: argparse.Namespace, cfg: Config) -> None:
    sim = Simulator(args.scenario, cfg, seed=args.seed, loop=args.loop)
    labels_path = sim.write_labels(args.labels)
    print(f"scenario={args.scenario} duration={sim.duration_s:.0f}s labels={labels_path}", file=sys.stderr)

    sock = None
    if args.udp:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        print(f"sending UDP to {args.host}:{args.port} at {args.speed}x", file=sys.stderr)

    period = sim.batch / sim.fs / args.speed if args.speed > 0 else 0.0
    start = time.monotonic()
    sent = 0
    try:
        while not sim.done:
            data = encode_packet(sim.next_packet())
            if sock:
                sock.sendto(data, (args.host, args.port))
            if args.stdout:
                sys.stdout.write(data.decode() + "\n")
                sys.stdout.flush()
            sent += 1
            time.sleep(max(0.0, start + sent * period - time.monotonic()))
    except KeyboardInterrupt:
        pass
    finally:
        print(f"sent {sent} packets", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    cfg = load_config()
    p = argparse.ArgumentParser(description="BreathWatch sensor simulator")
    p.add_argument("--scenario", default="normal", choices=SCENARIO_NAMES)
    p.add_argument("--udp", action="store_true", help="send packets over UDP")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=cfg.network.udp_port)
    p.add_argument("--stdout", action="store_true", help="print JSON lines (serial format)")
    p.add_argument("--speed", type=float, default=1.0, help="playback speed; 0 = as fast as possible")
    p.add_argument("--loop", action="store_true", help="repeat the scenario forever")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--labels", default=None, help="ground-truth labels output path")
    args = p.parse_args(argv)
    if not (args.udp or args.stdout):
        p.error("choose at least one output: --udp and/or --stdout")
    _run_cli(args, cfg)


if __name__ == "__main__":
    main()
