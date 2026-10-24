"""Ingest: packet sources (UDP, USB serial, in-process simulator) -> timeline -> ring buffers.

Timeline rule (AGENTS.md section 4): the first packet's arrival time anchors the timeline;
after that `seq` decides the sample index, so a seq gap becomes NaN samples (a visible gap),
never compressed data. The stream re-anchors on arrival time when `seq` goes backwards or the
device id changes.

All buffer mutation happens on the asyncio event loop thread; the serial reader thread hands
lines over with `loop.call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import socket
import threading
import time
from dataclasses import asdict, dataclass
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np

from server.config import Config
from server.schema import PacketError, parse_packet, validate_packet
from server.simulator import SCENARIO_NAMES, Simulator, stream

log = logging.getLogger(__name__)

CHANNELS = ("pressure", "pulse", "therm", "ax", "ay", "az")
CH = {name: i for i, name in enumerate(CHANNELS)}


class RingBuffer:
    """Fixed-size per-channel sample buffer indexed by absolute sample number."""

    def __init__(self, n_channels: int, capacity: int) -> None:
        self.capacity = capacity
        self.data = np.full((n_channels, capacity), np.nan, dtype=np.float32)
        self.total = 0  # samples ever written; index of the next sample

    def clear(self) -> None:
        self.data.fill(np.nan)
        self.total = 0

    def _write(self, block: np.ndarray) -> None:
        n = block.shape[1]
        if n >= self.capacity:
            block = block[:, -self.capacity:]
            start = (self.total + n - self.capacity) % self.capacity
            n_keep = self.capacity
        else:
            start = self.total % self.capacity
            n_keep = n
        first = min(n_keep, self.capacity - start)
        self.data[:, start:start + first] = block[:, :first]
        self.data[:, :n_keep - first] = block[:, first:]
        self.total += n

    def append(self, block: np.ndarray) -> None:
        self._write(np.asarray(block, dtype=np.float32))

    def append_nan(self, n: int) -> None:
        if n <= 0:
            return
        if n >= self.capacity:
            self.data.fill(np.nan)
            self.total += n
        else:
            self._write(np.full((self.data.shape[0], n), np.nan, dtype=np.float32))

    def window(self, end: int, n: int) -> np.ndarray:
        """Samples [end - n, end) as (channels, n); NaN where unavailable."""
        out = np.full((self.data.shape[0], n), np.nan, dtype=np.float32)
        lo = max(end - n, self.total - self.capacity, 0)
        hi = min(end, self.total)
        if hi > lo:
            idx = np.arange(lo, hi) % self.capacity
            out[:, n - (end - lo):n - (end - hi)] = self.data[:, idx]
        return out


@dataclass
class StreamStats:
    packets: int = 0
    invalid: int = 0
    gaps: int = 0
    missing_packets: int = 0
    dropped_late: int = 0
    restarts: int = 0
    nonjson_lines: int = 0
    last_arrival: float | None = None
    last_seq: int | None = None
    last_t0_ms: int | None = None
    device: str | None = None
    addr: str | None = None


class Source(Protocol):
    name: str
    status: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class Stream:
    """One device's timeline and ring buffers (one per patient from milestone 6.5)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.fs = float(cfg.contract.sample_rate_hz)
        self.buffers = RingBuffer(len(CHANNELS), int(cfg.ingest.buffer_s * self.fs))
        self.stats = StreamStats()
        self.anchor: float | None = None  # epoch seconds of sample index 0
        self.expected_seq: int | None = None
        self.generation = 0               # bumps on every reset so consumers can resync

    def sample_time(self, idx: int | float) -> float | None:
        return None if self.anchor is None else self.anchor + idx / self.fs

    def reset(self) -> None:
        self.buffers.clear()
        self.stats = StreamStats()
        self.anchor = None
        self.expected_seq = None
        self.generation += 1

    def link_ok(self, now: float | None = None) -> bool:
        last = self.stats.last_arrival
        return last is not None and (now or time.time()) - last < self.cfg.ingest.link_timeout_s

    def _reanchor(self, n: int, arrival: float) -> None:
        """Start a new stream segment; insert a gap for the time that passed with no data."""
        if self.anchor is None:
            self.anchor = arrival - n / self.fs
            return
        now_idx = self.buffers.total
        gap = int(round((arrival - n / self.fs - self.sample_time(now_idx)) * self.fs))
        if gap > 0:
            self.buffers.append_nan(gap)

    def handle_packet(self, pkt: dict[str, Any], addr: Any = None, arrival: float | None = None,
                      validated: bool = False) -> None:
        if not validated:
            errors = validate_packet(pkt, self.cfg.contract)
            if errors:
                self.stats.invalid += 1
                log.warning("invalid packet from %s: %s", addr, "; ".join(errors))
                return
        arrival = time.time() if arrival is None else arrival
        st = self.stats
        n = len(pkt["pressure"])
        seq = pkt["seq"]

        if self.expected_seq is None or pkt["device"] != st.device:
            if st.device is not None:
                st.restarts += 1
                log.info("stream restart: device %s -> %s", st.device, pkt["device"])
            self._reanchor(n, arrival)
        elif seq > self.expected_seq:
            missing = seq - self.expected_seq
            st.gaps += 1
            st.missing_packets += missing
            log.warning("seq gap (%s): expected %d, got %d (%d packets / %.1f s missing)",
                        pkt["device"], self.expected_seq, seq, missing, missing * n / self.fs)
            self.buffers.append_nan(missing * n)
        elif seq < self.expected_seq:
            if self.expected_seq - seq <= self.cfg.ingest.restart_seq_back:
                st.dropped_late += 1
                return
            st.restarts += 1
            log.info("stream restart: seq went back from %d to %d", self.expected_seq - 1, seq)
            self._reanchor(n, arrival)

        self.buffers.append(np.array([pkt[c] for c in CHANNELS], dtype=np.float32))
        self.expected_seq = seq + 1
        st.packets += 1
        st.last_arrival = arrival
        st.last_seq = seq
        st.last_t0_ms = pkt["t0_ms"]
        st.device = pkt["device"]
        st.addr = None if addr is None else str(addr)


Router = Callable[[str], "Stream | None"]
PacketHook = Callable[[dict[str, Any]], None]


class Ingest:
    """The live input: owns the active source (UDP / serial / simulator-as-live) and hands each
    packet to a Stream.

    Without a router (single-patient use and most tests) every packet goes to `self.default`, and
    `buffers`, `stats`, `sample_time`, … refer to it. With a router (the ward, M6.5) packets go
    to the stream of the patient assigned that device id; unassigned devices are counted and
    dropped.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.fs = float(cfg.contract.sample_rate_hz)
        self.default = Stream(cfg)
        self.router: Router | None = None
        self.on_packet: PacketHook | None = None     # record mode
        self.on_source_change: Callable[[], None] | None = None
        self.source: Source | None = None
        self.source_spec = "none"
        self.pico_addr: tuple[str, int] | None = None  # for bedside commands (milestone 6)
        self.invalid = 0
        self.unassigned_packets = 0
        self.nonjson_lines = 0
        self.packets = 0
        self.last_arrival: float | None = None
        self.last_device: str | None = None

    # ---- single-stream conveniences (the default stream)
    @property
    def buffers(self) -> RingBuffer:
        return self.default.buffers

    @property
    def stats(self) -> StreamStats:
        return self.default.stats

    @property
    def generation(self) -> int:
        return self.default.generation

    def sample_time(self, idx: int | float) -> float | None:
        return self.default.sample_time(idx)

    def reset(self) -> None:
        self.default.reset()
        self.invalid = self.unassigned_packets = self.nonjson_lines = self.packets = 0
        self.last_arrival = None

    # ---- packets
    def _count_invalid(self) -> None:
        self.invalid += 1
        if self.router is None:
            self.default.stats.invalid += 1

    def count_nonjson(self) -> None:
        self.nonjson_lines += 1
        if self.router is None:
            self.default.stats.nonjson_lines += 1

    def handle_raw(self, data: bytes | str, addr: Any = None, arrival: float | None = None) -> bool:
        try:
            pkt = parse_packet(data, self.cfg.contract)
        except PacketError as exc:
            self._count_invalid()
            if self.invalid <= 5 or self.invalid % 100 == 0:
                log.warning("invalid packet #%d from %s: %s", self.invalid, addr, exc)
            return False
        self.handle_packet(pkt, addr, arrival, validated=True)
        return True

    def handle_packet(self, pkt: dict[str, Any], addr: Any = None, arrival: float | None = None,
                      validated: bool = False) -> None:
        if not validated:
            errors = validate_packet(pkt, self.cfg.contract)
            if errors:
                self._count_invalid()
                log.warning("invalid packet from %s: %s", addr, "; ".join(errors))
                return
        arrival = time.time() if arrival is None else arrival
        self.packets += 1
        self.last_arrival = arrival
        self.last_device = pkt["device"]
        if isinstance(addr, tuple):
            self.pico_addr = addr
        if self.on_packet is not None:
            self.on_packet(pkt)
        stream = self.default if self.router is None else self.router(pkt["device"])
        if stream is None:
            self.unassigned_packets += 1
            if self.unassigned_packets in (1, 100) or self.unassigned_packets % 1000 == 0:
                log.warning("packets from unassigned device %r (%d so far)", pkt["device"], self.unassigned_packets)
            return
        stream.handle_packet(pkt, addr, arrival, validated=True)

    # ---- sources
    async def set_source(self, spec: str, speed: float | None = None, loop: bool = False) -> None:
        """Switch input. spec: udp | serial:<port|auto> | simulator:<scenario>."""
        new = self._make_source(spec, speed, loop)  # raises ValueError before stopping the old one
        await self.stop()
        self.reset()
        if self.on_source_change is not None:
            self.on_source_change()
        self.source, self.source_spec = new, spec
        await new.start()
        log.info("source: %s (%s)", spec, new.status)

    def _make_source(self, spec: str, speed: float | None, loop: bool) -> Source:
        kind, _, arg = spec.partition(":")
        if kind == "udp" and not arg:
            return UdpSource(self, self.cfg.network.udp_host, self.cfg.network.udp_port)
        if kind == "serial":
            return SerialSource(self, arg or "auto")
        if kind == "simulator" and arg in SCENARIO_NAMES:
            return SimulatorSource(self, arg, self.cfg.ingest.default_speed if speed is None else speed, loop)
        raise ValueError(f"bad source {spec!r}: use udp, serial:<port|auto>, "
                         f"simulator:<{'|'.join(SCENARIO_NAMES)}>")

    async def stop(self) -> None:
        if self.source is not None:
            await self.source.stop()
            self.source = None
            self.source_spec = "none"

    def send_command(self, cmd: dict[str, Any]) -> bool:
        """Bedside status to the Pico over the link the data arrives on (AGENTS.md §4):
        UDP to the learned Pico address on the command port, or one JSON line on serial."""
        src = self.source
        if isinstance(src, SerialSource):
            return src.write_line(json.dumps(cmd))
        if isinstance(src, UdpSource) and self.pico_addr is not None:
            return src.send(json.dumps(cmd).encode(), (self.pico_addr[0], self.cfg.network.command_port))
        return False

    def link_ok(self, now: float | None = None) -> bool:
        last = self.last_arrival
        return last is not None and (now or time.time()) - last < self.cfg.ingest.link_timeout_s

    def snapshot(self) -> dict[str, Any]:
        return {
            "source": self.source_spec,
            "source_status": self.source.status if self.source else "stopped",
            "link_ok": self.link_ok(),
            "samples": self.buffers.total,
            "sample_time": self.sample_time(self.buffers.total),
            "stats": asdict(self.stats),
        }


# --------------------------------------------------------------------------- sources

class PacketTarget(Protocol):
    cfg: Config

    def handle_packet(self, pkt: dict[str, Any], addr: Any = None, arrival: float | None = None,
                      validated: bool = False) -> None: ...


class SimulatorSource:
    """In-process simulator feeding an Ingest (simulator-as-live) or a patient's own Stream."""

    def __init__(self, target: PacketTarget, scenario: str, speed: float, loop: bool,
                 device: str | None = None, seed: int | None = None, start_s: float = 0.0) -> None:
        self.target = target
        self.name = f"simulator:{scenario}"
        self.sim = Simulator(scenario, target.cfg, seed=seed, loop=loop, device=device, start_s=start_s)
        self.speed = speed
        self.status = "starting"
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.sim.write_labels()
        self.status = f"running at {self.speed:g}x"
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        def sink(pkt: dict[str, Any]) -> None:
            self.target.handle_packet(pkt, addr="simulator", validated=True)
        await stream(self.sim, sink, self.speed, self._stop)
        if not self._stop.is_set():
            self.status = "finished"

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, ingest: Ingest) -> None:
        self.ingest = ingest

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.ingest.handle_raw(data, addr)


class UdpSource:
    def __init__(self, ingest: Ingest, host: str, port: int) -> None:
        self.ingest = ingest
        self.host, self.port = host, port
        self.name = "udp"
        self.status = "starting"
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _UdpProtocol(self.ingest), local_addr=(self.host, self.port))
        except OSError as exc:
            self.status = f"error: cannot listen on {self.host}:{self.port} ({exc})"
            log.error(self.status)
            return
        self.port = self._transport.get_extra_info("sockname")[1]
        self.status = f"listening on {self.host}:{self.port}"

    async def stop(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None

    def send(self, data: bytes, addr: tuple[str, int]) -> bool:
        if self._transport is None:
            return False
        self._transport.sendto(data, addr)
        return True


def list_serial_ports(pico_vid: int) -> list[dict[str, Any]]:
    from serial.tools import list_ports
    return [
        {"device": p.device, "description": p.description, "vid": p.vid, "pid": p.pid,
         "is_pico": p.vid == pico_vid}
        for p in list_ports.comports()
    ]


def find_pico_port(pico_vid: int) -> str | None:
    """Pico by USB vendor id first, else the first usbmodem (macOS) / ttyACM (Linux) port."""
    ports = list_serial_ports(pico_vid)
    for p in ports:
        if p["is_pico"]:
            return p["device"]
    for p in ports:
        if "usbmodem" in p["device"] or "ttyACM" in p["device"]:
            return p["device"]
    return None


class SerialSource:
    """Reads JSON lines from USB serial in a thread; reconnects after unplug or Pico reset."""

    def __init__(self, ingest: Ingest, port: str) -> None:
        self.ingest = ingest
        self.port_spec = port
        self.name = f"serial:{port}"
        self.status = "starting"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._outbox: queue.Queue[bytes] = queue.Queue(maxsize=16)
        self._connected = False

    def write_line(self, line: str) -> bool:
        """Queue one line for the Pico (written by the reader thread, which owns the port)."""
        if not self._connected:
            return False
        try:
            self._outbox.put_nowait(line.encode() + b"\n")
        except queue.Full:
            return False
        return True

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._run, name="serial-reader", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._thread:
            await asyncio.to_thread(self._thread.join, 2.0)

    def _resolve(self) -> str | None:
        if self.port_spec == "auto":
            return find_pico_port(self.ingest.cfg.ingest.serial_pico_vid)
        return self.port_spec

    def _run(self) -> None:
        import serial  # pyserial

        icfg = self.ingest.cfg.ingest
        while not self._stop.is_set():
            port = self._resolve()
            if port is None:
                self.status = "waiting for device (no Pico/usbmodem port found)"
                self._stop.wait(icfg.serial_reconnect_s)
                continue
            try:
                with serial.Serial(port, self.ingest.cfg.network.serial_baud,
                                   timeout=icfg.serial_read_timeout_s, exclusive=True) as ser:
                    self.status = f"connected to {port}"
                    log.info("serial: %s", self.status)
                    self._connected = True
                    try:
                        self._read_lines(ser, port)
                    finally:
                        self._connected = False
            except (serial.SerialException, OSError) as exc:
                msg = str(exc)
                if "lock" in msg.lower() or "busy" in msg.lower():
                    msg += " (is Thonny/mpremote holding the port?)"
                if not self.status.endswith(msg):
                    log.warning("serial %s: %s", port, msg)
                self.status = f"disconnected from {port}: {msg}"
            self._stop.wait(icfg.serial_reconnect_s)

    def _read_lines(self, ser: Any, port: str) -> None:
        max_len = self.ingest.cfg.ingest.serial_max_line_bytes
        buf = bytearray()
        while not self._stop.is_set():
            while not self._outbox.empty():
                ser.write(self._outbox.get_nowait())
            chunk = ser.read(max(1, ser.in_waiting))
            if not chunk:
                continue
            buf.extend(chunk)
            while (nl := buf.find(b"\n")) >= 0:
                line = bytes(buf[:nl]).strip()
                del buf[:nl + 1]
                if line:
                    assert self._loop is not None
                    self._loop.call_soon_threadsafe(self._on_line, line, port, time.time())
            if len(buf) > max_len:
                log.warning("serial: discarding %d bytes without newline", len(buf))
                buf.clear()

    def _on_line(self, line: bytes, port: str, arrival: float) -> None:
        if not line.startswith(b"{"):
            self.ingest.count_nonjson()
            if self.ingest.nonjson_lines <= 5:
                log.info("serial: ignoring non-packet line: %r", line[:120])
            return
        self.ingest.handle_raw(line, port, arrival)


# --------------------------------------------------------------------------- network info

def local_ips() -> list[str]:
    """Best-effort list of this machine's LAN IPv4 addresses (primary first)."""
    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; picks the default-route interface
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]
