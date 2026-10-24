# BUILD_LOG — which agent built what

Honest record of which tool or agent produced each part of the project.

## Workflow

- **Claude Code** (Anthropic, model Claude Opus 5.5) builds the server, simulator and processing
  (milestones 1–7).
- **Cursor** (Grok, in the Cursor IDE) builds the clinical frontend against §19 with mock data, in parallel with the processing milestones. Firmware (milestone 8) is still later.
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
| Sat evening | Claude Code | docs | AGENTS.md §19 "Clinical app shell": API contract (auth, patients, per-patient endpoints, ward + patient WebSockets, pages, permissions, config) so Cursor's agent can build the frontend against mocks in parallel; milestone 6.5 added; design rule 8 (risk factors never change thresholds). |
| Sat evening | Claude Code | M4 | `server/quality.py` (v0 rules per 10 s segment: flat/clipped channels, motion energy, belt/accel disagreement; posture), motion/off segments masked out of the rate window, tests vs ground truth incl. "apneas are never sensor_off". |
| Sat evening | Claude Code | M5 | `server/events.py` (Hilbert-envelope state machine at 1 s, rolling baseline, obstructive/central/mixed/hypopnea, final + provisional lags), `server/pipeline.py`, events on API/WebSocket/dashboard, tests scored against ground truth over several seeds. Two iterations driven by per-second traces (baseline freeze, window-edge leakage). |
| Sat evening | Cursor (Grok) | frontend | Clinical shell in `web/` against §19: `mock.js` (`USE_MOCK`), login, ward census, patient detail, add-patient, admin Device & data. Six scripted beds with live rates and waveforms. Did not edit `server/`. |
| Sat evening | Claude Code | M6 | `server/alerts.py` (tiers, persistence, baseline, trend, apnea + cluster, sensor check, cooldown, latching, escalation; naive comparison monitor), `server/store.py` (SQLite), bedside LED/buzzer command over UDP or serial, alerts API + WebSocket messages, tests incl. demo acceptance. Committed only backend files: Cursor's agent had uncommitted §19 frontend work in `web/`, left untouched. |
| Sat night | Claude Code | follow-ups | `awaiting_ack` alert status; 1 s apnea watch + 1 s provisional lag (tier 3 at 21–22 s into an apnea, measured); demo apneas 32 s / 26 s; §19 Alert shape updated. |
| Sat night | Claude Code | M6.5 | Multi-patient backend for §19: `server/auth.py`, `server/patients.py` (ward, per-patient runtimes, routing), `server/record.py`, Stream/Ingest split, all §19 endpoints + `/ws/ward` + `/ws/patients/{id}` + page routes + legacy aliases, ward backfill; `tests/test_ward.py` checks every documented shape. Read Cursor's `web/mock.js` (read-only) to match its real-API path. |
| Sat night | Claude Code | M7 | `server/llm.py` (Gemini provider via google-genai, fake provider, digest builder, matplotlib trend chart sent as an image, SBAR structured output, cache fallback), per-patient + legacy summary endpoints, `tests/test_llm.py` (no network); bad-key fallback checked once against the real API. |
| Sat night | Cursor (Grok) | frontend | Committed the clinical shell in `web/`: login, ward census, patient detail, add-patient, and admin Device & data. Built by Cursor against §19 with `mock.js` (`USE_MOCK` still on). Did not edit `server/`. |

## Prompts that worked

- "Read AGENTS.md fully. Before writing any code, give me a short plan for milestones 1–3: which
  files you'll create, how you'll test the packet schema, and anything in the spec that's
  ambiguous or that you'd change." — getting the plan reviewed first caught four spec issues
  before any code existed.
- "Make sure the USB serial input source works well" — led to a pty-based test that drives the
  real pyserial code path with split lines, CRLF and REPL noise, no hardware needed.
- "Build the clinical app frontend in web/ … against the §19 API contract. Use a mock data module
  (web/mock.js) … behind a single switch." — Cursor built the pages to the JSON shapes in §19
  without waiting for the M6.5 backend, and without editing `server/`.

## Smaller choices (clinical frontend, Cursor)

- `USE_MOCK` at the top of `web/mock.js` is the only switch. `true` uses the in-memory ward
  (sessionStorage stands in for the HttpOnly `bw_session` cookie, which page script cannot set).
  `false` uses same-origin `fetch` with credentials and the §19 WebSockets.
- Rates and waveforms are functions of the clock, so moving between pages does not reset the story.
  Demo reset clears alerts, events and rate history. "Restore the default six-bed ward" puts the
  scripted findings back. The mock does not run the Python detector, so it cannot re-raise alerts
  on its own after a plain reset.
- Red is used only for tier 3 (the urgent count, the urgent row, the tier-3 alert, and the
  "escalated to charge nurse" line). Discharge and form errors use ink and teal.
- Page links are the §19 paths. Until M6.5, FastAPI still serves only `/` and `/static/`, so
  `/login`, `/patients/…` and `/admin/device` 404 on the current server. A static file server that
  maps those paths is enough to click through the mock.
- "If the alarm fires at ~25 s into a 24 s apnea, it fires after the apnea ends … tell me the
  real latency" — forced a measurement against ground truth instead of a claim, which showed
  most of the delay was update cadence, not detection.
