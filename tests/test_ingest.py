"""Ingest: ring buffer, seq-based timeline with gaps, UDP and USB serial sources."""

import asyncio
import os
import socket
import threading
import time

import numpy as np
import pytest

from server.ingest import CH, Ingest, RingBuffer
from server.schema import encode_packet
from server.simulator import Simulator

BATCH = 25


# ---------------------------------------------------------------- ring buffer

def test_ring_buffer_wraps_and_windows():
    rb = RingBuffer(1, 10)
    rb.append(np.arange(7, dtype=float)[None])
    rb.append(np.arange(7, 14, dtype=float)[None])
    assert rb.total == 14
    np.testing.assert_array_equal(rb.window(14, 5)[0], [9, 10, 11, 12, 13])
    w = rb.window(14, 12)[0]  # older than capacity -> NaN
    assert np.isnan(w[:2]).all()
    np.testing.assert_array_equal(w[2:], np.arange(4, 14))
    w = rb.window(16, 4)[0]   # beyond the newest sample -> NaN
    np.testing.assert_array_equal(w[:2], [12, 13])
    assert np.isnan(w[2:]).all()


def test_ring_buffer_block_larger_than_capacity():
    rb = RingBuffer(1, 10)
    rb.append(np.arange(3, dtype=float)[None])
    rb.append(np.arange(100, 125, dtype=float)[None])
    assert rb.total == 28
    np.testing.assert_array_equal(rb.window(28, 10)[0], np.arange(115, 125))


def test_ring_buffer_nan_gap_advances_index():
    rb = RingBuffer(1, 10)
    rb.append(np.ones((1, 4)))
    rb.append_nan(3)
    rb.append(np.full((1, 2), 2.0))
    assert rb.total == 9
    w = rb.window(9, 9)[0]
    assert np.isnan(w[4:7]).all() and (w[:4] == 1).all() and (w[7:] == 2).all()
    rb.append_nan(50)
    assert rb.total == 59 and np.isnan(rb.window(59, 10)).all()


# ---------------------------------------------------------------- timeline

def packets(cfg, n, scenario="normal", device=None):
    return list(Simulator(scenario, cfg, device=device).packets(max_packets=n))


def test_in_order_packets_fill_buffer(cfg):
    ing = Ingest(cfg)
    pk = packets(cfg, 8)
    for i, p in enumerate(pk):
        ing.handle_packet(p, arrival=1000.0 + 0.5 * i)
    assert ing.buffers.total == 8 * BATCH
    assert ing.stats.packets == 8 and ing.stats.gaps == 0
    assert ing.buffers.window(ing.buffers.total, BATCH)[CH["pressure"]].tolist() == pk[-1]["pressure"]
    assert ing.sample_time(0) == pytest.approx(1000.0 - 0.5)


def test_seq_gap_becomes_nan_gap_not_compressed(cfg):
    ing = Ingest(cfg)
    pk = packets(cfg, 10)
    for i, p in enumerate(pk):
        if i in (4, 5, 6):  # lose three packets
            continue
        ing.handle_packet(p, arrival=1000.0 + 0.5 * i)
    assert ing.buffers.total == 10 * BATCH  # timeline length preserved
    assert ing.stats.gaps == 1 and ing.stats.missing_packets == 3
    w = ing.buffers.window(ing.buffers.total, 10 * BATCH)[CH["pressure"]]
    assert np.isnan(w[4 * BATCH:7 * BATCH]).all()
    assert not np.isnan(w[:4 * BATCH]).any() and not np.isnan(w[7 * BATCH:]).any()
    # samples after the gap sit at their true index
    assert w[7 * BATCH:8 * BATCH].tolist() == pk[7]["pressure"]


def test_duplicate_and_late_packets_dropped(cfg):
    ing = Ingest(cfg)
    pk = packets(cfg, 5)
    for p in pk:
        ing.handle_packet(p)
    ing.handle_packet(pk[4])
    ing.handle_packet(pk[2])
    assert ing.stats.dropped_late == 2 and ing.buffers.total == 5 * BATCH


def test_device_restart_reanchors_on_arrival_time(cfg):
    ing = Ingest(cfg)
    pk = packets(cfg, 20)
    for i, p in enumerate(pk[:10]):
        ing.handle_packet(p, arrival=1000.0 + 0.5 * i)
    # device reboots: seq starts over, and 6 s of wall time passed with no data
    ing.handle_packet(pk[0], arrival=1000.0 + 0.5 * 9 + 6.0)
    assert ing.stats.restarts == 1
    gap = ing.buffers.total - 11 * BATCH
    assert gap == pytest.approx(5.5 * 50, abs=1)
    assert ing.sample_time(ing.buffers.total) == pytest.approx(1000.0 + 4.5 + 6.0, abs=0.05)


def test_device_change_restarts_stream(cfg):
    ing = Ingest(cfg)
    for p in packets(cfg, 3, device="patch-01"):
        ing.handle_packet(p, arrival=1000.0)
    for p in packets(cfg, 3, device="patch-02"):
        ing.handle_packet(p, arrival=1001.5)
    assert ing.stats.restarts == 1 and ing.stats.device == "patch-02"


def test_invalid_raw_packets_counted_not_fatal(cfg):
    ing = Ingest(cfg)
    assert not ing.handle_raw(b"garbage")
    assert not ing.handle_raw(b'{"v": 1}')
    assert ing.handle_raw(encode_packet(packets(cfg, 1)[0]))
    assert ing.stats.invalid == 2 and ing.stats.packets == 1


# ---------------------------------------------------------------- sources

async def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


def test_udp_source_receives(test_cfg):
    async def run():
        ing = Ingest(test_cfg)
        await ing.set_source("udp")
        port = ing.source.port
        assert port != 0, ing.source.status
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for p in packets(test_cfg, 6):
            tx.sendto(encode_packet(p), ("127.0.0.1", port))
        tx.sendto(b"not a packet", ("127.0.0.1", port))
        tx.close()
        await wait_for(lambda: ing.stats.packets == 6 and ing.stats.invalid == 1)
        assert ing.pico_addr is not None and ing.pico_addr[0] == "127.0.0.1"
        await ing.stop()
    asyncio.run(run())


def test_simulator_source_streams(test_cfg):
    async def run():
        ing = Ingest(test_cfg)
        await ing.set_source("simulator:normal", speed=50)
        await wait_for(lambda: ing.stats.packets >= 20)
        await ing.stop()
        assert ing.stats.gaps == 0
    asyncio.run(run())


def test_bad_source_rejected_without_stopping_current(test_cfg):
    async def run():
        ing = Ingest(test_cfg)
        await ing.set_source("udp")
        with pytest.raises(ValueError):
            await ing.set_source("bogus")
        with pytest.raises(ValueError):
            await ing.set_source("simulator:nope")
        assert ing.source_spec == "udp"
        await ing.stop()
    asyncio.run(run())


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pseudo-terminal")
def test_serial_source_over_pty(test_cfg):
    """Real pyserial port (a pty): split writes, REPL noise and CRLF line endings."""
    master, slave = os.openpty()
    port = os.ttyname(slave)
    pk = packets(test_cfg, 12)

    def writer():
        time.sleep(0.3)
        os.write(master, b"MicroPython v1.24 on 2026-01-01; Raspberry Pi Pico 2 W\r\n>>> \r\n")
        for p in pk:
            data = encode_packet(p) + b"\r\n"
            mid = len(data) // 2
            os.write(master, data[:mid])   # a line split across reads
            time.sleep(0.005)
            os.write(master, data[mid:])
        os.write(master, b"debug: wifi failed, using USB\r\n")

    async def run():
        ing = Ingest(test_cfg)
        await ing.set_source(f"serial:{port}")
        t = threading.Thread(target=writer)
        t.start()
        await wait_for(lambda: ing.stats.packets == len(pk) and ing.stats.nonjson_lines >= 3)
        t.join()
        assert ing.stats.invalid == 0 and ing.stats.gaps == 0
        assert ing.buffers.total == len(pk) * BATCH
        assert ing.source.status.startswith("connected")
        await ing.stop()

    try:
        asyncio.run(run())
    finally:
        os.close(master)
        os.close(slave)


def test_serial_source_missing_port_keeps_retrying(test_cfg):
    async def run():
        ing = Ingest(test_cfg)
        await ing.set_source("serial:/dev/does-not-exist")
        await wait_for(lambda: ing.source.status.startswith("disconnected"))
        await ing.stop()
    asyncio.run(run())
