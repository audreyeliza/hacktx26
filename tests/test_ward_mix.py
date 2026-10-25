"""The seeded ward shows a mix of states within 30 s of appearing (staggered start_s per bed)."""

import asyncio
import time

import pytest

from server.ingest import Ingest
from server.patients import Ward
from server.store import Store
from tests.conftest import with_overrides


def first_30_s(cfg, public: bool, reset: bool = False):
    """Start the seeded ward (with its real backfill), then play 30 s of every simulated bed
    synchronously. Returns per-bed observations."""
    async def run():
        ward = Ward(cfg, Store(":memory:"), Ingest(cfg), public=public)
        await ward.start()
        if reset:
            await ward.reset()
        for rt in ward.runtimes.values():          # take over the real-time simulators
            if rt.sim:
                await rt.sim.stop()
        seen = {pid: {"rr": [], "quality": set(), "tiers": set(), "kinds": set(), "apnea_live": False,
                      "events": len(rt.event_log)} for pid, rt in ward.runtimes.items()}
        t0 = time.time()
        for k in range(60):                          # 30 s = 60 packets per bed
            for rt in ward.runtimes.values():
                if rt.sim:
                    rt.stream.handle_packet(rt.sim.sim.next_packet(), addr="simulator",
                                            arrival=t0 + (k + 1) * 0.5, validated=True)
            ward.process_all()
            for pid, rt in ward.runtimes.items():
                s = seen[pid]
                v = rt.vitals or {}
                if v.get("rr_fused") is not None:
                    s["rr"].append(v["rr_fused"])
                s["quality"].add(v.get("quality"))
                for a in rt.pipeline.alerts.active_alerts():
                    s["tiers"].add(a["tier"])
                    s["kinds"].add(a["kind"])
                og = rt.pipeline.ongoing
                s["apnea_live"] |= bool(og and og.get("apnea"))
        for pid, rt in ward.runtimes.items():
            seen[pid]["new_events"] = len(rt.event_log) - seen[pid]["events"]
            seen[pid]["scenario"] = rt.patient["device"].get("scenario")
        listing = ward.listing()
        await ward.stop()
        return seen, listing
    return asyncio.run(run())


@pytest.mark.parametrize("public,reset", [(False, False), (True, False), (True, True)])
def test_ward_shows_a_mix_within_30_s(cfg, public, reset):
    cfg = with_overrides(cfg, store={"path": ":memory:"})
    seen, listing = first_30_s(cfg, public, reset)
    by = {s["scenario"]: s for s in seen.values()}
    # stable: a bed breathing normally with nothing active
    assert by["normal"]["rr"] and all(13 <= r <= 18 for r in by["normal"]["rr"]) and not by["normal"]["tiers"]
    # motion: artefacts the smart monitor sits out
    assert "motion" in by["motion"]["quality"] and not by["motion"]["tiers"]
    # slowing: low and falling rate with an rr alert already raised
    slow = by["opioid_decline"]
    assert slow["rr"] and max(slow["rr"]) < 10 and slow["rr"][-1] <= slow["rr"][0] + 0.3
    assert "rr_low" in slow["kinds"]
    # apneas: live in at least two beds, and a tier-3 apnea alarm within the 30 s
    live = [s for s in seen.values() if s["apnea_live"]]
    assert len(live) >= 2
    assert 3 in by["central"]["tiers"] and 3 in by["obstructive"]["tiers"]
    # the ward listing itself shows more than one tier
    assert len({p["tier"] for p in listing["patients"]}) >= 3
