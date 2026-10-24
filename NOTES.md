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

## Open questions

- SpaceXSI track: does Claude Code count as part of the multi-agent workflow? (team is asking MLH)
