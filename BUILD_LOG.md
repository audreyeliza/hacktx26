# BUILD_LOG — which agent built what

Honest record of which tool or agent produced each part of the project.

## Workflow

- **Claude Code** (Anthropic, model Claude Opus 5.5) builds the server, simulator and processing
  (milestones 1–7).
- **Cursor's agent** builds the firmware (milestone 8) and polishes the dashboard later.
- Both agents read `AGENTS.md` as the single source of truth. Agreed decisions are written into
  `AGENTS.md` itself (not only `NOTES.md`) so both agents follow the same rules.
- The human team reviews each milestone's plan, makes the design calls and builds the hardware.

## Log

| When (2026-10-24) | Agent | Milestone | What |
|---|---|---|---|
| Sat afternoon | Claude Code | planning | Read AGENTS.md, proposed the M1–3 plan and flagged spec issues (filter corner at 6/min, window length, `rr_fused` undefined, `t0_ms` wrap, firmware timer ISR). Team accepted with changes (single-source fallback for `rr_fused`). |
| Sat afternoon | Claude Code | docs | Wrote the agreed decisions into AGENTS.md §4, §7, §13. |
| Sat afternoon | Claude Code | M1 | `config.yaml`, `server/config.py`, `server/schema.py`, `server/simulator.py` (8 scenarios + ground-truth labels), tests. |
| Sat afternoon | Claude Code | docs | AGENTS.md: Mac as main server, Pi optional (Trixie), serial source behaviour, timeline anchoring wording, vendored Chart.js, M10 "calibrate supine" control. |
| Sat afternoon | Claude Code | M2 | `server/ingest.py` (ring buffers, seq timeline with NaN gaps, UDP / USB serial / in-process simulator sources), `server/app.py` (REST, WebSocket, static), minimal `web/` dashboard, tests incl. serial over a pty. Checked in Chrome via the LAN address. |

| Sat afternoon | Claude Code | M3 | `server/signals.py` (filters, peak + FFT rates, accel PCA, `rr_fused` fusion incl. single-source rule), processing loop on sample time, vitals + trend on the WebSocket and dashboard, tests scoring against simulator ground truth. Checked `opioid_decline` at 10x in Chrome. |

## Prompts that worked

- "Read AGENTS.md fully. Before writing any code, give me a short plan for milestones 1–3: which
  files you'll create, how you'll test the packet schema, and anything in the spec that's
  ambiguous or that you'd change." — getting the plan reviewed first caught four spec issues
  before any code existed.
- "Make sure the USB serial input source works well" — led to a pty-based test that drives the
  real pyserial code path with split lines, CRLF and REPL noise, no hardware needed.
