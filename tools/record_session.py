#!/usr/bin/env python3
"""Guided recording session for the real BreathWatch wearable (milestone 9).

Walks the wearer through a protocol step by step in the terminal, counts down, and labels each
segment automatically through the server's record mode. Files land in
data/recordings/<session>/<NN-step>_<label>.csv plus a manifest.json; train with
`python -m server.train_quality`.

Before you start:
  1. Server running on this Mac:          python -m server.app
  2. Wearable streaming to it:            Admin → Device & data → Source serial:auto (or udp)
  3. Run from the repo root:              python tools/record_session.py [--participant p01]

Options:
  --list                 print the protocol and exit
  --skip LABEL           skip steps with this label (repeatable), e.g. --skip breath_hold_effort
  --server URL           default http://localhost:8000
  --notes TEXT           free text stored in the manifest (placement, posture, …)

Safety: only for a healthy adult volunteer, sitting or lying down. Skip the breath-hold steps if
you have any heart or lung condition. Stop any breath-hold early if uncomfortable (Ctrl+C) — the
recording up to that point is kept.

Research prototype — not a medical device.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
SAFETY = ("SAFETY: stop early if you feel at all uncomfortable — press Ctrl+C and breathe normally. "
          "The recording up to that point is kept.")


@dataclass(frozen=True)
class Step:
    key: str                 # short name, used in the file name
    label: str | None        # record-mode label; None = not recorded (rest / setup)
    seconds: float
    title: str
    instructions: str
    breath_hold: bool = False
    pace: float | None = None  # breathing pacer: seconds per half-breath (in / out)


PROTOCOL: tuple[Step, ...] = (
    Step("settle", None, 20, "Get settled",
         "Lie down on your back (or sit back), belt snug, thermistor under the nose, finger clip on. "
         "Breathe normally."),
    Step("clean", "clean", 60, "Lie still and breathe normally",
         "Stay still and breathe the way you normally would. Don't talk."),
    Step("motion", "motion", 30, "Twist and move around",
         "Twist side to side, shift your hips, reach your arms over your head, tug the belt gently. "
         "Keep breathing."),
    Step("rest1", None, 20, "Rest", "Lie still and breathe normally."),
    Step("slow", "slow_breathing", 60, "Slow breathing",
         "Breathe slowly and steadily: in for 5 seconds, out for 5 seconds (about 6 breaths/min). "
         "Follow the in / out cue.", pace=5.0),
    Step("rest2", None, 30, "Rest", "Breathe normally and recover."),
    Step("hold_effort", "breath_hold_effort", 30, "Hold your breath while pushing your chest",
         "Breathe out normally, then close your mouth and pinch your nose. Keep gently trying to "
         "breathe in and out with your chest and belly (no air moving). This looks like an "
         "obstructive apnea.", breath_hold=True),
    Step("rest3", None, 45, "Rest", "Breathe normally and recover fully before the next hold."),
    Step("hold_relaxed", "breath_hold_no_effort", 25, "Hold your breath, fully relaxed",
         "Breathe out normally, then simply hold with chest and belly relaxed and still — no "
         "effort at all. This looks like a central apnea.", breath_hold=True),
    Step("rest4", None, 45, "Rest", "Breathe normally and recover."),
    Step("therm_off", "sensor_off", 60, "Unplug the thermistor",
         "Unplug the thermistor module (or its signal wire) and leave everything else on. Keep "
         "breathing normally. Plug it back in when this step ends."),
)


# --------------------------------------------------------------------------- terminal helpers

class Quit(Exception):
    pass


def say(text: str = "") -> None:
    print(text, flush=True)


def ask(prompt: str, assume_yes: bool) -> str:
    """Enter = go, s = skip, q = quit."""
    if assume_yes:
        say(prompt + "(auto)")
        return ""
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return ""
    if answer.startswith("q"):
        raise Quit()
    return answer


def countdown_line(step: Step, left: float, extra: str) -> str:
    cue = ""
    if step.pace:
        elapsed = step.seconds - left
        cue = "  breathe IN " if int(elapsed // step.pace) % 2 == 0 else "  breathe OUT"
    bar_n = 30
    done = int(bar_n * (1 - left / step.seconds)) if step.seconds else bar_n
    return f"\r  [{'#' * done}{'.' * (bar_n - done)}] {left:4.0f} s left{cue}  {extra}   "


# --------------------------------------------------------------------------- server API

class Server:
    def __init__(self, client: Any) -> None:
        self.c = client

    def sign_in(self) -> None:
        r = self.c.post("/api/auth/demo", json={"role": "admin"})
        if r.status_code != 200:
            raise SystemExit(f"could not sign in as the demo admin ({r.status_code}): {r.text[:200]}")

    def device(self) -> dict[str, Any]:
        r = self.c.get("/api/admin/device")
        r.raise_for_status()
        return r.json()

    def start(self, label: str, session: str, segment: str) -> dict[str, Any]:
        r = self.c.post("/api/record/start", json={"label": label, "session": session, "segment": segment})
        if r.status_code != 200:
            raise SystemExit(f"record start failed ({r.status_code}): {r.json().get('detail', r.text)}")
        return r.json()["recording"]

    def stop(self) -> str | None:
        r = self.c.post("/api/record/stop")
        r.raise_for_status()
        return r.json().get("file")


def preflight(server: Server, allow_simulator: bool, wait_s: float = 2.0) -> dict[str, Any]:
    """The wearable must be streaming to the server (and not the simulator, unless allowed)."""
    first = server.device()
    if first.get("recording"):
        server.stop()
    time.sleep(wait_s)
    dev = server.device()
    grew = dev["stats"]["packets"] > first["stats"]["packets"]
    problems = []
    if not dev["source"].startswith(("serial", "udp")):
        if not (allow_simulator and dev["source"].startswith("simulator")):
            problems.append(f"source is '{dev['source']}': switch to serial:auto or udp in Admin → Device & data")
    if not dev["link_ok"] or not grew:
        problems.append("no packets are arriving: is the wearable on and connected?")
    if dev.get("assigned_patient_id") is None:
        problems.append("no admitted patient has the live device assigned (bed 4 by default)")
    if problems:
        raise SystemExit("Not ready to record:\n  - " + "\n  - ".join(problems))
    return dev


# --------------------------------------------------------------------------- the session

def run_session(server: Server, steps: list[Step], session: str, participant: str | None, notes: str,
                assume_yes: bool = False, time_scale: float = 1.0, allow_simulator: bool = False) -> Path:
    server.sign_in()
    dev = preflight(server, allow_simulator, wait_s=2.0 * time_scale if time_scale < 1 else 2.0)
    say(f"\nBreathWatch guided recording — session {session}")
    say(f"Source {dev['source']} · {dev['stats']['packets']} packets so far · patient {dev['assigned_patient_id']}")
    say("Healthy adult volunteer only. Skip the breath-holds if you have any heart or lung condition.")
    total = sum(s.seconds for s in steps)
    say(f"{len(steps)} steps, about {total / 60:.0f} min. Enter = start a step, s = skip it, q = quit.\n")

    manifest: dict[str, Any] = {
        "session": session, "participant": participant, "notes": notes, "source": dev["source"],
        "started_t": time.time(), "protocol_version": 1, "segments": [],
    }
    recorded = 0
    try:
        for i, step in enumerate(steps, 1):
            say(f"── Step {i}/{len(steps)}: {step.title} ({step.seconds:.0f} s)"
                + (f"  [label: {step.label}]" if step.label else "  [not recorded]"))
            say(f"   {step.instructions}")
            if step.breath_hold:
                say(f"   ⚠ {SAFETY}")
            if ask("   Press Enter when ready (s = skip, q = quit) ", assume_yes).startswith("s"):
                say("   skipped\n")
                continue
            for n in (3, 2, 1):
                print(f"\r   starting in {n} ", end="", flush=True)
                time.sleep(1.0 * time_scale)
            print("\r   GO              ", flush=True)
            segment = f"{i:02d}-{step.key}"
            rec = server.start(step.label, session, segment) if step.label else None
            started = time.time()
            early = False
            packets0 = server.device()["stats"]["packets"] if rec else 0
            try:
                end = started + step.seconds * time_scale
                last_check = 0.0
                link = ""
                while (left := end - time.time()) > 0:
                    if rec and time.time() - last_check > 2.0 * max(time_scale, 0.05):
                        d = server.device()
                        last_check = time.time()
                        link = (f"recording · +{d['stats']['packets'] - packets0} packets"
                                + ("" if d["link_ok"] else " · LINK LOST — check the wearable"))
                    print(countdown_line(step, left / time_scale, link if rec else "rest"), end="", flush=True)
                    time.sleep(min(0.25 * time_scale, max(left, 0.0)))
            except KeyboardInterrupt:
                early = True
                print("\n   stopped early — that's fine, breathe normally", flush=True)
            actual = time.time() - started
            file = server.stop() if rec else None
            print("\r" + " " * 90 + "\r", end="")
            if rec:
                recorded += 1
                manifest["segments"].append({
                    "index": i, "step": step.key, "label": step.label, "title": step.title,
                    "planned_s": step.seconds, "actual_s": round(actual / time_scale, 1),
                    "stopped_early": early, "started_t": started, "file": file,
                })
                say(f"   saved {file}" + ("  (stopped early)" if early else ""))
            if step.label == "sensor_off":
                say("   Plug the thermistor back in now.")
            say()
    except Quit:
        say("\nQuitting: segments recorded so far are kept.")
    except KeyboardInterrupt:
        server.stop()
        say("\nInterrupted: segments recorded so far are kept.")
    manifest["ended_t"] = time.time()
    path = manifest_path(session, manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=1))
    say(f"Done: {recorded} recorded segments. Manifest: {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    say("Train the signal-quality model with:  python -m server.train_quality")
    return path


def manifest_path(session: str, manifest: dict[str, Any]) -> Path:
    """Next to the CSVs when the server is on this machine, else data/recordings/<session>/ here."""
    for seg in manifest["segments"]:
        f = seg.get("file")
        if f:
            p = Path(f) if Path(f).is_absolute() else ROOT / f
            if p.exists():
                return p.parent / "manifest.json"
    return ROOT / "data" / "recordings" / session / "manifest.json"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Guided BreathWatch recording session (real wearable).")
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--participant", default=None, help="short id, e.g. p01 (no names)")
    ap.add_argument("--notes", default="")
    ap.add_argument("--skip", action="append", default=[], metavar="LABEL", help="skip steps with this label")
    ap.add_argument("--list", action="store_true", help="print the protocol and exit")
    ap.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)               # tests
    ap.add_argument("--time-scale", type=float, default=1.0, help=argparse.SUPPRESS)    # tests
    ap.add_argument("--allow-simulator", action="store_true", help=argparse.SUPPRESS)   # tests
    args = ap.parse_args(argv)

    steps = [s for s in PROTOCOL if not (s.label and s.label in args.skip)]
    if args.list:
        for i, s in enumerate(steps, 1):
            say(f"{i:2d}. {s.title:42s} {s.seconds:4.0f} s  {s.label or '(not recorded)'}")
        say(f"    total {sum(s.seconds for s in steps) / 60:.1f} min")
        return
    participant = re.sub(r"[^A-Za-z0-9_-]", "-", args.participant)[:20] if args.participant else None
    session = time.strftime("session-%Y%m%d-%H%M%S") + (f"-{participant}" if participant else "")
    with httpx.Client(base_url=args.server, timeout=10) as client:
        try:
            run_session(Server(client), steps, session, participant, args.notes,
                        assume_yes=args.yes, time_scale=args.time_scale, allow_simulator=args.allow_simulator)
        except httpx.ConnectError:
            sys.exit(f"cannot reach the server at {args.server}: start it with `python -m server.app`")


if __name__ == "__main__":
    main()
