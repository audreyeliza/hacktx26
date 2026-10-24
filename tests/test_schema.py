"""Packet schema (AGENTS.md section 4): every producer must pass the same validator."""

import copy
import json
import socket

import pytest

from server.schema import PacketError, encode_packet, parse_packet, validate_packet
from server.simulator import SCENARIO_NAMES, Simulator


@pytest.fixture
def packet(cfg):
    return Simulator("normal", cfg).next_packet()


@pytest.mark.parametrize("scenario", SCENARIO_NAMES)
def test_every_scenario_packet_is_valid(cfg, scenario):
    sim = Simulator(scenario, cfg)
    count = 0
    for p in sim.packets():
        assert validate_packet(p, cfg.contract) == [], f"seq {p['seq']}"
        assert len(p["pressure"]) == cfg.contract.batch_samples
        assert len(encode_packet(p)) <= cfg.contract.max_datagram_bytes
        count += 1
    assert count >= sim.duration_s * cfg.contract.sample_rate_hz / cfg.contract.batch_samples


def test_udp_round_trip(cfg):
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2.0)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sim = Simulator("motion", cfg)
    try:
        sent = [sim.next_packet() for _ in range(5)]
        for p in sent:
            tx.sendto(encode_packet(p), rx.getsockname())
        received = [parse_packet(rx.recv(65535), cfg.contract) for _ in sent]
    finally:
        rx.close()
        tx.close()
    assert received == sent


def test_serial_line_format(cfg, packet):
    line = encode_packet(packet).decode()
    assert "\n" not in line
    assert parse_packet(line + "\n", cfg.contract) == packet


def test_seq_and_t0_advance(cfg):
    sim = Simulator("normal", cfg)
    pkts = list(sim.packets(max_packets=10))
    batch_ms = cfg.contract.batch_samples * cfg.contract.dt_ms
    for a, b in zip(pkts, pkts[1:]):
        assert b["seq"] == a["seq"] + 1
        assert (b["t0_ms"] - a["t0_ms"]) % cfg.simulator.ticks_wrap == batch_ms


def test_adc_values_match_rp2_read_u16(cfg, packet):
    # MicroPython scales the 12-bit sample as (raw << 4) | (raw >> 8)
    for key in ("pressure", "pulse", "therm"):
        for v in packet[key]:
            assert v & 0xF == v >> 12


def test_accel_has_three_decimals(packet):
    for key in ("ax", "ay", "az"):
        assert all(round(v, 3) == v for v in packet[key])


BAD_CASES = [
    ("missing key", lambda p: p.pop("therm"), "missing keys"),
    ("wrong version", lambda p: p.update(v=2), "v must be"),
    ("wrong dt", lambda p: p.update(dt_ms=10), "dt_ms"),
    ("negative seq", lambda p: p.update(seq=-1), "seq"),
    ("bool seq", lambda p: p.update(seq=True), "seq"),
    ("float adc", lambda p: p["pressure"].__setitem__(0, 1.5), "pressure"),
    ("adc out of range", lambda p: p["pulse"].__setitem__(0, 70000), "pulse"),
    ("accel out of range", lambda p: p["az"].__setitem__(0, 2.5), "az"),
    ("unequal lengths", lambda p: p["ax"].pop(), "equal length"),
    ("empty device", lambda p: p.update(device=""), "device"),
    ("array not list", lambda p: p.update(ay="nope"), "ay must be a list"),
]


@pytest.mark.parametrize("name,mutate,expect", BAD_CASES, ids=[c[0] for c in BAD_CASES])
def test_invalid_packets_rejected(cfg, packet, name, mutate, expect):
    bad = copy.deepcopy(packet)
    mutate(bad)
    errors = validate_packet(bad, cfg.contract)
    assert any(expect in e for e in errors), errors
    with pytest.raises(PacketError):
        parse_packet(json.dumps(bad), cfg.contract)


def test_garbage_rejected(cfg):
    for data in (b"", b"not json", b"\xff\xfe", b"[1, 2]", b"null"):
        with pytest.raises(PacketError):
            parse_packet(data, cfg.contract)


def test_spec_example_shape_is_valid(cfg):
    """The literal example from AGENTS.md section 4, padded to equal-length arrays."""
    example = {
        "v": 1, "device": "patch-01", "seq": 1042, "t0_ms": 5210000, "dt_ms": 20,
        "pressure": [31200, 31350], "pulse": [28000, 28110], "therm": [40110, 40090],
        "ax": [0.012, 0.011], "ay": [-0.031, -0.03], "az": [0.981, 0.98],
    }
    assert validate_packet(example, cfg.contract) == []
