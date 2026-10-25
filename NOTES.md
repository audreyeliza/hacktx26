# NOTES — decisions and open questions

Agent-maintained. Rules that every agent must follow are also written into AGENTS.md; this file
records the reasoning and smaller choices.

## Decisions (agreed with the team, Sat 2026-10-24)

1. **Respiratory band 0.05–1.0 Hz** (not 0.1). 6/min = 0.1 Hz would sit at the −3 dB corner.
   → AGENTS.md §7.
2. **Rate windows:** 60 s for the reported rate; 30 s only for a display-only `rr_prelim`.
   M3 tolerance ±1/min steady, ±2/min while the rate is changing. → AGENTS.md §7.
3. **`rr_fused`:** median of the largest group of clean sources agreeing within 3/min; one clean
   source → used with confidence capped at 0.6 and flag `single_source`; none → `None`.
   → AGENTS.md §7.
4. **Timeline from arrival time + `seq`;** `t0_ms` diagnostics only; `seq` gaps become NaN gaps,
   never compressed data. → AGENTS.md §4.
5. **Firmware samples in a `ticks_diff` polling loop**, not a hardware Timer callback.
   → AGENTS.md §13.
6. **"Byte-for-byte" (§6)** means same keys, types and value formats. JSON key order and
   whitespace are not part of the contract. The simulator encodes compact JSON
   (`separators=(",", ":")`).
7. **Repo root is `breathwatch/`** (no nested folder).

## Smaller choices (M1)

- **Python:** the dev Mac has 3.12 and 3.14 but not 3.11, so the local venv is 3.12. Code avoids
  3.12-only syntax. numpy 2.5 / scipy 1.18 dropped Python 3.11, so `requirements.txt` pins
  numpy 2.4.6 / scipy 1.17.1 (newest with 3.11 aarch64 wheels) so it installs on a Bookworm Pi.
- **Schema validator** is hand-written (`server/schema.py`), no `jsonschema` dependency.
  Array length is allowed 1..`max_samples` (100) so a partial batch from firmware is not
  dropped; the simulator always sends 25.
- **Simulator ADC values** mimic MicroPython's rp2 `read_u16` exactly: `(raw12 << 4) | (raw12 >> 8)`.
  Accel is quantized to 1/1024 g then rounded to 3 decimals. `t0_ms` wraps at 2**30 like rp2
  `ticks_ms`.
- **Accelerometer axes** (simulator convention, firmware should match or posture mapping must be
  adjusted): x = patient's left, y = toward head, z = out of the chest. Supine → az ≈ +1 g,
  left side → ax ≈ −1 g, right → ax ≈ +1 g, upright → ay ≈ +1 g.
- **Demo scenario decline goes to 5/min** (and holds 90 s), not 6, because the tier-3 rule is
  `rr_fused < 6`; a decline that stops at 6 could never reach tier 3. Then it recovers to 15/min
  (as if a nurse intervened) before the obstructive and central phases. The standalone
  `opioid_decline` scenario keeps the spec's 16 → 6.
- **Ground-truth labels** go to `data/sim_labels/<scenario>.json`: events (type, start, end,
  posture), quality segments (clean / motion / sensor_off) and true rate every 1 s.

## Smaller choices (M2)

- **Platform:** the Mac runs the server for dev and the demo; the Pi 4 (Trixie, Python 3.13) is an
  optional target. AGENTS.md §3/§15/§17 updated. Checked: every pin in `requirements.txt` resolves
  to Python 3.13 aarch64 wheels.
- **Default source is `udp`** (`ingest.default_source`), so both the external simulator and a WiFi
  Pico work with no clicks. Switch to `serial:auto` in the dashboard or config for USB.
- **Timeline anchoring:** sample time = first packet's arrival + sample index × 20 ms. With
  `--speed 10`, sample time runs ahead of the wall clock, which is fine: processing runs on sample
  time. Re-anchor happens on `seq` going back > 4 (`restart_seq_back`) or a device id change;
  smaller backward steps are late/duplicate packets and are dropped (not back-filled).
- **Serial source:** pyserial in a daemon thread, lines handed to the event loop with
  `call_soon_threadsafe`, so buffers are only touched on one thread (no locks). Handles lines split
  across reads, CRLF, REPL banners/debug prints (counted as `nonjson_lines`, not errors), unplug and
  re-plug (reconnect every 1 s), and port-busy (opened with `exclusive=True`, error message says
  "is Thonny/mpremote holding the port?"). `serial:auto` picks the Pico by USB VID 0x2E8A, else the
  first `usbmodem`/`ttyACM` port. Tested against a real pseudo-terminal.
- **Phone access:** HTTP binds `0.0.0.0:8000`. On startup the server logs every LAN IPv4 as
  "Dashboard: http://<ip>:8000 (Pico SERVER_IP=<ip>)"; the dashboard status line shows them too.
  When the Mac is on a phone hotspot (iPhone: 172.20.10.x) that address is the Pico's `SERVER_IP`.
- **Bedside command address:** the Pico's address is learned from the source address of its UDP
  packets (`Ingest.pico_addr`) instead of being configured.
- **Chart.js 4.5.1 vendored** in `web/vendor/` (not a CDN) so the page works with no internet.
  4.6.1 was 2 days old at the time, so the previous release was used. AGENTS.md §12 updated.
- **Waveforms on the socket are raw ADC counts**, decimated 50 → 25 Hz, NaN → `null` (a visible
  gap). Raw (not filtered) so rails, clipping and motion are visible while debugging hardware.
  Messages are incremental (only new samples), aligned to the global sample index.
- **Static files are sent with `Cache-Control: no-cache`** so dashboard edits show up on a normal
  reload (found while testing: the browser kept a stale `style.css`).
- **Known warning:** Starlette warns that `httpx` in `TestClient` is deprecated in favour of
  `httpx2`. Harmless for now; tests pass.

## Smaller choices (M3)

- **Rate method:** band-pass (0.05–1.0 Hz breathing, 0.7–3.5 Hz pulse), `find_peaks` with a minimum
  spacing (from the max rate) and a prominence of 30 % of the window's robust peak-to-peak
  amplitude; rate = 60 / median peak-to-peak interval. A zero-padded FFT confirms it: confidence
  drops when peak and FFT rates differ by > 2/min, and with irregular intervals.
- **Accel effort:** each axis band-passed, then projected on the largest principal component.
  Below 0.0015 g (`accel_min_breath_g`) it reports `None` ("flat") instead of a noise rate.
- **Fusion decisions beyond the agreed rules (please review):**
  - "Clean" in M3 means "the source produced a rate with confidence ≥ 0.3"
    (`source_min_conf`). M4 quality will pass a per-source `clean` map into `fuse()`.
  - **2+ usable sources with no two agreeing** (e.g. belt 10, airflow 20): the most confident
    one is shown, flagged `sources_disagree`, with confidence capped at **0.4**
    (`disagree_conf_cap`), i.e. below the alert gate. Rationale: conflicting sensors should
    not raise an alarm on their own; the spec already says disagreement lowers confidence.
  - A source outside the agreeing group is listed in the flag `outlier:<name>`.
- **Warm-up:** rates appear once the 60 s window is ≥ 80 % valid (`min_valid_fraction`), i.e. at
  50 s. Gaps up to 20 % of a window are linearly interpolated; longer gaps give `None` with reason
  "missing data", never a "flat" reading.
- **How M3 is scored:** each 60 s estimate is compared with the *mean* true rate over its window
  (that is what a windowed estimator measures); during the decline it is also checked against
  the true rate at the window end (±2). Results on simulator data: `normal` worst error
  0.20/min, `opioid_decline` worst 0.16/min; HR within ±1.5 of 70; ~4 ms per 5 s update.
  **Caveat:** the simulator is idealized; real sensors will be noisier, and thresholds need
  checking against recordings (M9).
- **HR resolution** is limited by 50 Hz sampling (~1.6 bpm steps at 70 bpm with a median
  interval). Fine for display; can be refined with peak interpolation if needed.
- **Processing runs on sample time:** the server computes vitals for every 5 s boundary of
  sample time the buffers pass (up to 10 per 100 ms tick), so `--speed 10` replays work. The
  trend (last 60 min) is kept in memory; SQLite storage comes with alerts.
- **Dashboard tile** shows `rr_fused`, confidence, flags, per-source rates and the 30 s
  preliminary value; the trend chart plots `rr_fused` (baseline line comes with alerts, M6).

## Smaller choices (M4)

- **"Flat" ≠ "no breathing".** During an apnea the airflow (and, central, the effort) channel is
  nearly flat *physiologically*. So `sensor_off` = dead-flat raw signal (std < 15 counts, below
  the noise an attached sensor shows) or > 20 % of samples within 64 counts of a rail. A test
  checks that obstructive, central and hypopnea windows are never labelled sensor_off or motion.
  **Real-hardware risk:** a thermistor that slips off the face may read like a long apnea (both
  cool toward room temperature). The rules cannot fully tell these apart; tune with recordings
  (M9) and consider a DC-level check against the patient's breathing baseline.
- **Which channels decide the label:** belt (pressure) and airflow (therm). The pulse sensor being
  off only marks the pulse channel (HR → None); it does not block breathing alarms.
- **Priority:** sensor_off, then motion, then clean. `unknown` when the segment lacks data.
- **Motion threshold 0.03 g** (max 2 s RMS of the > 1.5 Hz accel magnitude); simulator noise is
  ~0.0035 g, bursts ~0.35 g. Needs a check on the real MMA8452Q.
- **Rate windows are cleaned with the quality segments:** the 60 s window is split into 10 s
  segments; motion segments are masked on every channel, off channels on themselves, then rates
  are computed (≤ 20 % masked is interpolated, more gives None). Without this, motion from 40 s
  ago would corrupt a rate reported under a "clean" label. Result: on `motion`, no
  alert-eligible rate is off by more than 2/min.
- **Quality confidence:** clean = 1.0, or 0.5 (`disagree_conf`) when belt and accel rates differ by
  > 3/min; motion 0.5–1.0 by energy; sensor_off 0.9. Alerts (M6) will gate on
  `quality == "clean"` and `quality_conf ≥ 0.6`.
- **Vitals gained** `quality`, `quality_conf`, `quality_reasons`, `channels` (per-channel ok / flat /
  clipped / missing), `motion` (g), `clean_fraction` (of the 60 s window) and `posture`
  (dominant gravity axis ≥ 0.6 g, else "unknown"; prone maps to "unknown").
- **Results on simulator data:** every 10 s window fully inside a motion or sensor-off segment is
  labelled correctly, every clean one is clean (normal 109/109, motion 42/42 clean + 7/7 motion,
  sensor_off 46/46 clean + 36/36 off).

## Smaller choices (M5)

- **Amplitude** = Hilbert envelope of the band-passed signal, 1 s moving average, sampled once
  per second. Baseline = median over the last 2 min of valid seconds, **excluding seconds inside
  an apnea** (< 10 %); shallow breathing does update it ("previous 2 min of clean data"). An
  earlier version froze the baseline below 70 % and produced false hypopneas after the demo's
  slow decline.
- **Event extent uses the 70 % crossing; the 10 % rule decides the type.** Measured at the 10 %
  level, apneas came out 7–9 s short (the airflow decay ramp plus the Hilbert transform's
  non-local tail, which leaks ~10 % of a breath 2 s away), so a 24 s apnea measured 14 s and
  could never reach the tier-3 rule. Now: extent = seconds with airflow < 70 % of baseline;
  apnea if the median airflow over the event interior (2 s trimmed at each end) is < 10 %;
  hypopnea otherwise; ≥ 10 s for both. Effort over the interior: ≥ 30 % obstructive, < 20 %
  central, starts < 20 % and ends ≥ 30 % mixed; 20–30 % → nearer class with confidence 0.5.
  **This is a measurement interpretation of §7, flagged for review.**
- **Final vs provisional seconds:** a second is final 12 s behind the newest sample. Closer to
  the edge, breathing that resumes right at the window edge (mirrored by the edge padding)
  inflated airflow 3–4× and turned short apneas into hypopneas. Seconds 3–12 s behind are
  evaluated provisionally every update, only to report an apnea *in progress*
  (`ongoing_event`) for alerts. Result: the demo's 24 s apnea is reported "≥ 20 s in progress"
  at ~25 s of apnea (cadence-limited); final events appear ~12–17 s after they end.
- **Masked seconds abort** an event in progress (motion, sensor off, gaps) instead of being read
  as "no airflow". Effort = belt only for now; if the belt is off, no events are scored (accel
  fallback is a possible M10 item).
- **Results:** seeds 42 + 1–5: obstructive 36/36, central 36/36, hypopnea 36/36 with the right
  type; 0 false events in ~3.5 h of normal/motion/sensor_off/opioid_decline data; start/end within
  ±3 s of truth. Mixed is not in the simulator; a synthetic test covers it.
- **New module `server/pipeline.py`**: one patient's vitals → events (→ alerts) step, so M6.5 can
  run one per patient. Events carry `patient_id` (`p4` for the single-patient API) and follow
  the §19.3 Event shape; `GET /api/events` and WebSocket `event` messages added; the dashboard
  lists events and shows an apnea in progress.

## Smaller choices (M6)

- **Alert policy beyond §8 (please review):**
  - **Latching:** tier ≥ 2 alerts stay active until acknowledged, even if the condition clears;
    tier 1 resolves on its own after the condition has been clear for 30 s (`clear_s`).
  - **A gate failure resets persistence** (rather than pausing it): conservative per rule 4.
  - **`sensor_check` (tier 1) is the one rule that fires on non-clean data**: it is about the
    device, not the patient, so it cannot be gated on signal quality.
  - **Tier-0 log entries:** when a rate condition would alarm but fails the gate, a tier-0
    "Not alarmed: … but signal motion (confidence 0.9)" alert is stored (status `resolved`, at
    most once per cooldown), so the timeline shows what the smart monitor suppressed.
  - **One active alert per kind;** a higher tier escalates it in place (counted as a new
    alarm); the same tier only re-words it for apneas ("Apnea in progress 21 s" → "Apnea 25 s").
    Cooldown (120 s) runs from the last raise, acknowledge or resolve.
  - **Apnea tier 3 fires from the in-progress report** (M5 provisional tail), at ~25 s of a
    24 s apnea in the demo; the final event then re-words the same alert. Hypopneas do not alarm
    (§8 has no hypopnea rule).
  - **Trend rule:** rr ≤ 70 % of baseline, and rr was ≥ 90 % of baseline somewhere in the last
    30 min (so the drop happened within 30 min), held 30 s → tier 2.
  - **Baseline** = median eligible rr of the first 10 min (median so far after 2 min). In the
    demo the decline starts inside the first 10 min, so the baseline is 14/min, not 15–16.
  - **Escalation** after 60 s unacknowledged at tier 3, timed in sample time (equal to wall time
    at 1×). Acknowledge time = current sample time.
  - **Before M6.5 there is no login:** acknowledgements record `acknowledged_by =
    {"username": "bedside", "display_name": "Bedside (no login yet)", "role": "nurse"}`.
- **Naive monitor** = a plain fixed-threshold monitor: rate = peak count over 15 s (RR) / 10 s
  (HR) with a fixed 200-count prominence, no range checks, no quality gating; every onset of
  RR < 8 or > 25, HR < 50 or > 120, airflow flat (band-passed std < 150 counts over 10 s) counts
  as one alarm. An earlier version reused our rate estimator (with its range checks) and was
  unrealistically quiet during motion.
- **Results on `demo` (20 min):** smart 7 alarms vs naive 23; motion phase smart 0 vs naive 7;
  tier 3 on the decline (rate 5/min for 35 s) and on the 24 s apnea, both escalated when not
  acknowledged; tier 2 cluster after 5 apneas; normal 0 vs 0.
- **Storage:** stdlib `sqlite3` in WAL mode, `data/breathwatch.db`; vitals every 5 s, events,
  alerts (every version saved), `llm_cache` table created for M7. JSON blob per row plus indexed
  columns. Tests use `:memory:`.
- **Bedside command** every 2 s: color from tier, buzzer while an unacknowledged tier-3 alert is
  active. Sent over the link the data arrives on: UDP to the Pico's source address on port 5006,
  or one JSON line on the serial port (AGENTS.md §4 updated: firmware must also read stdin).
- **Dashboard:** Cursor's agent is mid-way through the §19 frontend in the working tree
  (`index.html` is now the ward overview), so the M6 UI (alert panel, ack button, naive-vs-smart
  counter, baseline line) was **not** added to the old single-patient page. The data is ready:
  `/api/state` now has `tier`, `tier_reason`, `baseline`, `active_alerts`, `monitors`,
  `bedside`; `/ws/live` sends `alert` messages; `GET /api/alerts`, `POST /api/alerts/{id}/ack`.

## Follow-ups after M6 (approved decisions 1, 2, 4)

- **`awaiting_ack` status** ("resolved, awaiting acknowledgment"): a latched tier 2–3 alert
  whose condition has been clear for 30 s. Still counted in the tier and still escalated (nobody
  acknowledged it), but the buzzer stops; condition back → `active` again, same id. New field
  `cleared_t`. `GET …/alerts?status=unacked` returns active + awaiting_ack. §19.3 updated —
  **Cursor: the dashboard should show `awaiting_ack` differently from `active`.**
  For apnea alerts the condition is "an apnea is in progress"; for clusters, "≥ 5 apneas in
  the last 15 min".
- **Apnea alarm latency (measured):** an apnea watch now runs every **1 s** (event detection +
  apnea rules; vitals stay every 5 s) and the provisional lag is **1 s** (was 3 s; edge
  distortion only makes airflow look higher, so a short lag cannot create a false alarm). The
  tier-3 alarm fires **21.0–22.1 s into an apnea, i.e. +1.0 to +2.1 s after the 20 s mark**
  (1 s provisional lag + up to 1 s cadence + onset measured at the 70 % crossing), across 6
  seeded runs. Apneas of 20–22 s therefore alarm ~1 s after they end; that is the floor for
  airflow-based detection here.
- **Demo apneas lengthened**: obstructive 24 → **32 s**, central 22 → **26 s**; the tier-3 alarm
  fires at 21.8 s and 21.6 s, well before they end (test asserts ≥ 4 s before the end).
- `pytest -q` from the repo root now also collects `tools/sensor_test.py` (an untracked
  MicroPython script that imports `machine`) and fails; run `pytest -q tests`, or add
  `testpaths = tests` to a pytest config (not done: outside server/ and tests/).

## Smaller choices (M6.5)

- **Structure:** `Ingest` (the live source) now hands packets to a per-patient `Stream` (buffers +
  seq timeline). Without a router everything goes to a default stream (single-patient tests);
  the `Ward` sets a router that maps the packet's `device` to the admitted patient assigned that
  device id. Each `PatientRuntime` = Stream + Pipeline (+ its own in-process simulator for
  simulated beds). One processing loop ticks every runtime every 100 ms.
- **Auth (demo-grade):** in-memory sessions (`secrets.token_urlsafe`), cookie `bw_session`
  (HttpOnly, SameSite=Lax), passwords compared with `compare_digest`. `auth.enabled: false`
  makes every request the first admin (tests use this; dedicated tests run with auth on).
  WebSockets: accept, then close 4401 when not signed in (a close before accept would show up
  as an HTTP 403 handshake failure, not the documented code).
- **Live source switch** resets the live patient's stream and pipeline (stored history kept).
  Packets from a device no admitted patient has are counted (`unassigned_packets`) and dropped.
- **Patients:** pydantic models validate PatientCreate/PatientUpdate (422 on bad input; device is
  a discriminated union on `kind`). Ids `p<n>` from the max stored number + 1. Conflicts (409):
  same bed or same live device among admitted patients. Changing `device` replaces the runtime
  (sockets see a `reset`). Discharged patients: `GET …/state` is built from stored history.
- **Backfill** (not in the original plan): simulated beds get 150 s of history on ward start /
  demo reset (≈1.6 s startup for 5 beds) so the ward is not blank for the first minute. New
  admissions are not backfilled (no history before admission); the live bed never is.
- **Record mode** implemented minimally now because §19 promises the admin endpoints: raw live
  packets → `data/recordings/<time>_<label>.csv` (device, seq, k, t0_ms, 6 channels, label).
  Training (M9) is still to do.
- **Pages:** `/`, `/login`, `/patients/new`, `/patients/{id}`, `/admin/device` serve Cursor's
  files from `web/`; everything outside `/api/` is sent `no-cache`.
- **Legacy aliases** (`/api/state`, `/api/events`, `/api/alerts`, `/api/alerts/{id}/ack`, `/ws/live`)
  follow the patient wearing `ward.live_device_id`; they require sign-in like everything else.
  `POST /api/source` and `/api/demo/reset` now return the §19 bodies.
- **Tests:** shape checks against the §19.3 key sets for every endpoint and socket message, 401
  / 403 / 404 / 409 / 422 paths, WebSocket 4401 / 4404 / discharge, routing, record CSV, reset /
  reseed, and design rule 8 (same data, opposite risk factors → identical alerts).

## Smaller choices (M7)

- **Model ids:** `gemini-3.8-flash` for explain, overnight and handoff (configurable;
  `handoff_model` can point at a stronger model such as `gemini-4-argon`). Taken from the Gemini
  models page (Oct 2026) — **not yet verified with a real key**; the first real call will show
  whether the id is accepted (a wrong id falls back to the cache and logs the API error).
- **SDK:** `google-genai` 2.33: `Client(api_key=…, http_options=HttpOptions(timeout=20 000 ms))`,
  `system_instruction`, image as `Part.from_bytes(png)`, handoff with `response_mime_type=
  application/json` + a pydantic schema (SBAR + `chart_observation` + `discrepancies`). The SDK
  call runs in a worker thread with an extra asyncio timeout as a backstop.
- **Digest** (never waveforms): bed, risk factors, notes (≤ 300 chars), data source (simulated vs
  live), time range, rate stats + baseline + first/last-quarter means + minutes without a
  reliable rate, HR stats, quality/posture/flag fractions, events by type/posture + the last 15,
  alerts (tier ≥ 1) with reasons. No patient name is sent. Explain uses 15 min before the alert;
  overnight/handoff the stay so far, at most 12 h.
- **Chart** (overnight + handoff, also `GET …/summary/chart.png`): matplotlib `Figure` (thread-safe,
  Agg), rate line, dashed baseline, faint 10/8/6 lines, event spans by type, tier ≥ 2 alert
  triangles. The prompt asks Gemini to describe it first and report chart-vs-digest
  disagreements rather than resolve them.
- **Cache / offline:** every response stored in `llm_cache`; on failure (no key, bad key, network,
  timeout, malformed SBAR JSON) the latest cached response for that patient + kind (explain:
  that alert) is returned with `cached: true`; nothing cached → 503. Demo reset keeps the cache.
  Checked once against the real API with a deliberately invalid key: `API_KEY_INVALID` → cached
  response in 0.2 s.

## Gemini key and real-API check (Sat night)

- **Key loading:** `GEMINI_API_KEY` from the environment, else from `.env` at the repo root
  (python-dotenv `dotenv_values`, so `.env` does not leak into `os.environ`). Only the source
  ("environment" / ".env") is logged; errors are passed through `_redact` before logging or
  returning. `.env` is in `.gitignore` (checked with `git check-ignore`).
- **Model id verified** with `models.list()`: `gemini-3.8-flash` exists and supports
  generateContent (also available: `gemini-4-argon`, `gemini-3.7-flash`, `gemini-3.5-flash`,
  `gemini-3.5-flash-lite`, …). No change needed.
- **But at test time the newest models were overloaded:** `gemini-3.8-flash` returned 504
  DEADLINE_EXCEEDED even for "Reply OK" (28.6 s), `gemini-3.7-flash` / `gemini-flash-latest` 503
  "high demand"; `gemini-3.5-flash` answered in 1.4 s, `gemini-3.5-flash-lite` in 0.7 s.
  Added **fallback models** tried on 5xx / 429 only (auth errors do not walk the list):
  `[gemini-3.5-flash, gemini-3.5-flash-lite]`, one SDK attempt each, 15 s per request, 40 s total
  deadline enforced inside the provider. `Summary.model` records the model that answered.
  `max_output_tokens` raised to 4096 because thinking tokens can count toward it.
- **Real outputs** (demo scenario, simulated): overnight summary and SBAR both answered by
  `gemini-3.5-flash-lite` after the two busier models failed, ~30 s each (mostly waiting on the
  busy models). The chart description matched the rendered chart.
  **Demo-day choice for the team:** keep `gemini-3.8-flash` first (best model, but ~15 s extra
  per summary while it is overloaded) or put `gemini-3.5-flash` first in `config.yaml`.

## Floating pins (Sat night)

- New channel status `floating` (treated like flat/clipped: channel masked, breathing channel →
  `sensor_off`, after 60 s tier-1 "Check sensor: therm floating …"). Rules in AGENTS.md §7.
- Simulator: new sensor faults `therm_floating` and `all_floating` (shared wander + shared jitter
  around 11 400, range ≈ 10 400–12 300, std ≈ 285 — matching the hardware report). The
  `sensor_off` scenario gained a 70 s phase of each; both are labelled `sensor_off` in every
  window inside them, and all earlier tests (apneas never sensor_off, clean stays clean) pass.
- **Uncertain:** the jitter spectrum of the real pins is a guess. If the real floating signal is
  mostly slow (< 1.5 Hz) the single-pin rule would miss it (the multi-pin correlation rule needs
  some fast jitter too). Recording 1–2 min of disconnected pins with record mode would settle it.

## M11 deployment prep (Sat night)

- `deploy/pi_setup.sh`, `deploy/breathwatch.service`, `deploy/push_to_pi.sh` (see AGENTS.md M11, §17).
  Written and syntax-checked only; **not run against the Pi**, nothing deployed anywhere.
- The service is sandboxed (`ProtectSystem=strict`, `ProtectHome=read-only`, only `data/` writable,
  `NoNewPrivileges`), so bytecode caching is off and matplotlib's cache lives in `data/`.
- `.env` is never rsynced; `push_to_pi.sh --env` copies it separately with mode 600.
- **PUBLIC_DEMO:** the 7-point proposal was approved and is built (AGENTS.md §20). Three additions
  were needed to make it safe: `Secure` cookie, a 12-patient ward cap (each patient runs a
  simulator — protects a 1 vCPU server), and `GET /api/public-config` for the frontend. The
  banner is injected server-side into every page so it holds without frontend changes.
  Pins checked for the VPS: `requirements.txt` resolves on Python 3.12 and 3.14 x86_64.

## M10 polish (Sun early)

- **Calibrate supine:** Rodrigues rotation from the 3 s mean gravity vector to (0, 0, 1), applied
  to the gravity vector before the posture rule. Matches the frontend's call
  (`POST /api/calibrate/supine` → `{gravity, samples, …}`, read from Cursor's `web/patient.js`).
  Permission nurse/admin. Tilt only — see AGENTS.md M10 limitation.

- **Fast warm-up:** first rate at 15 s (was 50 s) after a start/reset; worst error 0.15/min vs the
  true rate over the data so far (normal, opioid_decline, demo, obstructive, two seeds). Only in
  the first 60 s of a stream (`end < rate window`), so it never papers over a missing rate later
  (e.g. during a central apnea or after motion). `demo.fast_warmup: false` restores 50 s.

- **DEMO.md** (repo root): checklist, exact startup commands, the live script with expected alarms
  and timings, the simulated fallback timeline (measured from the `demo` scenario), and a
  failure table. UI labels taken from Cursor's pages (read-only). Volunteer safety notes for the
  breath-holds are included.

## Public demo deployed (Sun 2026-10-25)

- **URL: https://149-28-251-200.sslip.io** (Vultr, Ubuntu 24.04.5, Dallas, 1 vCPU / 955 MB).
  Steps as run are in deploy/PUBLIC_DEMO.md. Users: `breathwatch` (system, runs the app),
  `audreybolyard` (SSH key only, passwordless sudo). SSH password and keyboard-interactive auth off
  (drop-in `00-breathwatch-hardening.conf`, verified a password attempt gets "Permission denied
  (publickey)"). Root SSH login **disabled** (`PermitRootLogin no` in the same drop-in, after checking
  `audreybolyard` key login + sudo); admin is via `audreybolyard` + sudo only.
  ufw: 22, 80, 443/tcp. App on 127.0.0.1:8000 only; no UDP listener.
- Gemini key **not set** (empty placeholder `/opt/breathwatch/.env`, mode 600): summaries return
  503 "GEMINI_API_KEY is not set … and no cached … summary yet".
- Verified from the Mac over HTTPS: valid Let's Encrypt cert, HTTP→HTTPS 308, HSTS/nosniff/DENY,
  banner on pages, `public-config`, six simulated beds (bed 4 = `demo`), password login 403,
  source/serial/record 403, live-device patient 400, legacy live endpoints 404, second reset 429,
  `wss://` ward + patient sockets, ports 8000/5005 closed from outside. Cursor's pages render.
- Observed: right after a reset bed 4 can sit in the `demo` scenario's 3-minute motion block,
  during which its rate is withheld ("missing data": > 20 % of the window masked as motion) even
  between bursts. Correct behaviour, but it looks like a blank tile for up to ~3 min.
- Deploy gotcha written into the doc: `sudo caddy validate` as root creates a root-owned log
  file and the reload fails.

## Clinical app shell contract (§19) — choices made while writing it

- **Both WebSockets:** a light `/ws/ward` (summaries + alert pushes) for the overview and a heavy
  `/ws/patients/{id}` (waveforms, vitals, events, alerts) for the detail page, so six patients'
  waveforms are never streamed to the overview.
- **Times are epoch seconds** (`*_t`), matching what the server already sends; the frontend
  multiplies by 1000 for JS `Date`.
- **Permissions:** nurses can admit/update/discharge (they do on a ward); physicians view, ack and
  generate summaries; admin-only for source/record/reset. Everyone signed in can acknowledge.
- **Ward tier** = highest tier among *unacknowledged* active alerts, so acknowledging calms the
  card but the alert stays visible in history.
- **Patient ids** are server-assigned `p<n>`; the live patient in the default ward is `p4`.
  Events (M5) and alerts (M6) already carry `patient_id` so M6.5 does not reshape them.
- **One live wearable**, routed by the packet's `device` field; packets from unassigned devices
  are counted and dropped (shown on the admin page).
- **Added endpoints not in the request but needed by the pages:** `GET /api/patients/{id}/vitals`
  (trend chart history), `GET /api/patients/{id}/summary/chart.png`, `GET /api/admin/device`,
  `GET /api/auth/demo-accounts` (login buttons without exposing passwords).
- **Bed uniqueness:** two admitted patients cannot share a bed (409).

## Smaller choices (clinical frontend, Cursor)

- **`USE_MOCK`** at the top of `web/mock.js` is the only switch. `true`: in-memory ward, with
  sessionStorage standing in for the HttpOnly `bw_session` cookie (page script cannot set that
  cookie). `false`: same-origin fetch with credentials and the §19 WebSockets.
- **Clock-based physiology.** Rates and waveforms are functions of time, so opening a patient does
  not reset the ward. Plain demo reset clears alerts, events and rate history; "restore the default
  ward" puts the six scripted beds back. The mock does not run the Python detector.
- **Red is only tier 3** — the urgent count, urgent row, tier-3 alert, and the charge-nurse line.
  Discharge and form errors use ink and teal.
- **Routes.** Links use the §19 paths. Until M6.5, FastAPI still serves only `/` and `/static/`,
  so `/login`, `/patients/…` and `/admin/device` 404 there. `GET /` now returns the ward shell
  (the single-patient M2–M5 page is the patient detail view). Click-through of the other pages
  needs a static server that maps those paths, or the M6.5 routes.

## Open questions

- SpaceXSI track: does Claude Code count as part of the multi-agent workflow? (team is asking MLH)
