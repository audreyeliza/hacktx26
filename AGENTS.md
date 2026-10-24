# BreathWatch — agent instructions

Read this whole file before writing code. It is the single source of truth for this project.
If something here conflicts with your assumptions, follow this file. If something is
ambiguous, pick the simplest option that satisfies the acceptance checks and note it in
`NOTES.md`.

## 1. What we are building

A 24-hour hackathon prototype: a wearable breathing monitor for post-surgical patients on
opioids on a general hospital ward, with smart alarms that avoid alarm fatigue and AI-written
clinical summaries.

**Problem:** On general wards, vitals are checked every 4–6 hours. Opioids can slow or stop
breathing between checks. Pulse oximetry misses it when patients are on supplemental oxygen.
Existing continuous monitors cause alarm fatigue (false alarms from motion and loose sensors).
Many patients have undiagnosed obstructive sleep apnea.

**Solution:**

1. Measure breathing **effort** (chest pressure belt + accelerometer), **airflow** (thermistor
   under the nose), **posture/motion** (accelerometer) and **heart rate** (finger pulse sensor).
2. Judge **signal quality** first; low-confidence data never triggers an alarm.
3. Track breathing rate and trend against the patient's own baseline.
4. Detect and classify breathing events: obstructive apnea, central apnea, hypopnea.
5. Tiered alerts with a stated reason for every alert.
6. LLM-generated plain-language summaries and SBAR handoff notes.

**Users:** ward nurses (primary), charge nurse, physicians reviewing summaries.

**This is a prototype, not a medical device.** All thresholds are demo values. Say so in the UI
footer.

## 2. Non-negotiable design rules

1. **The LLM never decides whether to alarm.** Alarm logic is deterministic, rule-based Python.
   The LLM only explains and summarizes data the rules already produced.
2. **Everything must run with no hardware attached**, using the simulator (section 6). Never
   block software work on hardware.
3. **One data contract** (section 4) between the Pico and the Pi. Do not change it without
   updating this file.
4. **Low-confidence windows cannot raise alerts above tier 0** (log only).
5. Keep it light: the server runs on a Raspberry Pi 4 with **1 GB RAM**. No heavy frameworks,
   no local LLMs, no build step for the frontend.
6. Every alert stores a human-readable `reason` string.
7. Demo must survive no internet: cache the last LLM outputs and show them if the API fails.
8. **Patient risk factors (opioids, O2, OSA, age) are display and LLM context only.** They never
   change alert thresholds, tiers, persistence or cooldowns (§19).

## 3. Hardware (for context; the human builds this)

- **Wearable:** Raspberry Pi Pico 2W (MicroPython), clipped to a chest belt, USB power bank.
- **Server (decided):** a **Mac laptop** runs the server for development and the demo, bound to
  `0.0.0.0` so phones on the same network can open the dashboard. The Pico connects by **USB
  serial** (likely the main link) or by UDP over a shared network (e.g. a phone hotspot; the
  Pico's `SERVER_IP` must be set to the Mac's address on that network).
- **Optional deployment target:** Raspberry Pi 4 Model B 1 GB, Raspberry Pi OS Lite 64-bit
  (Debian Trixie), can act as WiFi hotspot. Keep code compatible; no Pi-specific work.
- **Sensors:** Keyestudio 48-in-1 kit modules, all powered from the Pico's **3.3 V** pin.

| Pico pin | Module | Signal |
|---|---|---|
| GP26 / ADC0 | Thin-film pressure sensor under chest belt (with rigid puck) | breathing effort |
| GP27 / ADC1 | Pulse rate monitor (finger, IR LED + phototransistor) | heart rate |
| GP28 / ADC2 | Analog temperature (thermistor) module taped under the nostrils | airflow |
| GP4 SDA / GP5 SCL (I2C0) | MMA8452Q 3-axis accelerometer at the sternum | motion, posture, effort |
| GP15 | Active buzzer | bedside urgent alarm |
| GP16 / GP17 / GP18 | RGB LED module (R/G/B) | bedside status color |

MMA8452Q notes: I2C address is usually `0x1C` or `0x1D` (scan the bus). WHO_AM_I register
`0x0D` returns `0x2A`. Set range ±2 g via XYZ_DATA_CFG `0x0E` = `0x00`, then set active via
CTRL_REG1 `0x2A`. Data registers `0x01`–`0x06`, 12-bit left-justified, 1024 counts/g at ±2 g.

## 4. Data contract

### Pico → Pi (sensor data)

- Transport: **UDP** to the Pi at `10.42.0.1:5005` (default address of a NetworkManager
  hotspot on the Pi; configurable via `SERVER_IP` in `secrets.py` and `config.yaml`).
  Fallback: the same JSON, one object per line, over **USB serial at 115200 baud**.
- Sample rate: **50 Hz**. Send a batch every **0.5 s** (25 samples).
- Packet (one JSON object per datagram / line):

```json
{
  "v": 1,
  "device": "patch-01",
  "seq": 1042,
  "t0_ms": 5210000,
  "dt_ms": 20,
  "pressure": [31200, 31350, ...],
  "pulse":    [28000, 28110, ...],
  "therm":    [40110, 40090, ...],
  "ax": [0.012, ...], "ay": [-0.031, ...], "az": [0.981, ...]
}
```

- `pressure`, `pulse`, `therm`: raw `ADC.read_u16()` values, 0–65535.
- `ax/ay/az`: acceleration in g (float, 3 decimals).
- `t0_ms`: Pico `ticks_ms()` of the first sample. `seq` increments per packet; the server must
  detect and log gaps.
- All arrays in one packet have equal length.
- **Server timing (decided):** the server builds its timeline from **arrival time + `seq`**,
  not from `t0_ms` (`ticks_ms` wraps and the Pico clock drifts). Concretely: the arrival time of
  the first packet anchors the timeline; after that, sample time = anchor + sample index × dt,
  where `seq` decides the sample index. The stream re-anchors on arrival time when it restarts
  (`seq` goes backwards, or the device id changes). `t0_ms` is kept for diagnostics only.
  Late/duplicate packets (`seq` at or behind the newest) are dropped and counted. A `seq` gap
  must appear as a **gap in the timeline** (missing samples are marked NaN for the gap
  duration), never as compressed data. Processing treats NaN spans as
  missing data, not as a flat signal.

### Pi → Pico (bedside commands)

- UDP to the Pico at port `5006`, JSON: `{"cmd": "status", "color": "green|yellow|orange|red", "buzzer": false}`.
- **Over USB serial (decided):** when the Pico's data arrives over serial, the server writes the
  same JSON object as **one line** (`\n`-terminated) to the serial port; the firmware must read
  stdin (non-blocking, e.g. `select.poll` on `sys.stdin`) as well as UDP 5006.
- The server sends the current status every 2 s (`bedside.command_interval_s`): color from the
  live patient's tier (0 green, 1 yellow, 2 orange, 3 red), `buzzer` true while an
  unacknowledged tier-3 alert is active. The UDP target is the source address of the Pico's
  packets (no configuration).
- Pico applies the latest command; if no command for 10 s, it blinks blue (link lost).

## 5. Repository layout

```
breathwatch/
├── AGENTS.md               # this file
├── CLAUDE.md               # points to AGENTS.md
├── NOTES.md                # agent-maintained decisions and open questions
├── BUILD_LOG.md            # which agent/tool built what (SpaceXSI track evidence)
├── config.yaml             # all thresholds, ports, model name — no magic numbers in code
├── firmware/               # MicroPython for the Pico 2W
│   ├── main.py
│   ├── mma8452q.py
│   └── secrets_example.py  # WIFI_SSID, WIFI_PASS, SERVER_IP
├── server/
│   ├── app.py              # FastAPI app, WebSocket, REST endpoints, static files
│   ├── ingest.py           # UDP listener, serial reader, simulator source, ring buffers
│   ├── simulator.py        # synthetic data in the exact packet format
│   ├── signals.py          # filtering, rate estimation, feature extraction
│   ├── quality.py          # signal quality: rules (v0) and ML model (v1)
│   ├── events.py           # apnea / hypopnea detection and classification
│   ├── pipeline.py         # one patient's step: vitals → events → alerts (added in M5)
│   ├── alerts.py           # baseline, trends, tiers, escalation, naive monitor
│   ├── store.py            # SQLite
│   ├── llm.py              # Gemini API summaries with caching
│   ├── record.py           # record mode: raw data + labels to CSV
│   ├── train_quality.py    # trains the signal-quality model from recordings
│   ├── auth.py             # demo accounts, sessions, role checks (§19, M6.5)
│   └── patients.py         # patient registry + one pipeline per patient (§19, M6.5)
├── web/
│   ├── index.html          # nurses' station dashboard (from M6.5: ward overview, §19)
│   ├── app.js
│   ├── style.css
│   └── …                   # from M6.5: login.html, patient.html, patient-new.html, admin.html, common.js
├── data/                   # recordings, model file, sqlite db (gitignored)
├── tests/                  # pytest, mostly run against simulator data
└── requirements.txt
```

## 6. Simulator (build this first)

`server/simulator.py` produces packets **byte-for-byte in the section 4 format** at real-time
speed (or faster with `--speed`). It can feed the server directly (in-process source) or send
UDP to `127.0.0.1:5005` so the real ingest path is exercised.

Scenarios (CLI: `python -m server.simulator --scenario <name>`), each a timeline of phases:

| Scenario | Content |
|---|---|
| `normal` | 14–16 breaths/min, HR 70, small noise |
| `motion` | normal breathing with bursts of large accel noise and corrupted pressure/therm |
| `opioid_decline` | breathing rate falls 16 → 6/min over ~10 min (accelerated), amplitude shrinks |
| `obstructive` | normal, then 15–25 s periods where effort continues but airflow goes flat |
| `central` | normal, then 15–25 s periods where effort and airflow both go flat |
| `hypopnea` | periods where effort and airflow drop to ~40% amplitude for 15 s |
| `sensor_off` | therm or pressure goes flat/clipped at a rail |
| `demo` | the full demo script: normal → motion → opioid_decline → obstructive → central |

Breathing waveform: sinusoid-ish with slight asymmetry plus noise; thermistor signal lags effort
slightly and has slower rise/fall; pulse is a PPG-like waveform at the HR. Values must sit in
realistic raw ADC ranges with a DC offset.

Ground-truth event labels are written alongside (JSON) so tests can score detection.

## 7. Processing pipeline (`signals.py`, `quality.py`, `events.py`)

Work on rolling windows from per-channel ring buffers (keep ~10 min in memory, ~300 MB budget
is far more than needed — stay small).

**Filtering**

- Effort (pressure) and airflow (therm): detrend, band-pass **0.05–1.0 Hz** (2nd-order
  Butterworth, `scipy.signal.sosfiltfilt` on windows, or causal `sosfilt` for live).
  (Decided: low cutoff is 0.05 Hz, not 0.1 Hz, because 6 breaths/min = 0.1 Hz would sit at
  the −3 dB corner and lose amplitude exactly where apnea/hypopnea thresholds matter. The
  cutoffs live in `config.yaml`.)
- Accelerometer effort: band-pass 0.05–1.0 Hz on each axis, then take the projection with most
  power in that band (PCA on the 3 filtered axes over the window).
- Motion energy: magnitude of high-pass (>1.5 Hz) accel, RMS over 2 s.
- Pulse: band-pass **0.7–3.5 Hz**.
- NeuroKit2 (`nk.rsp_clean`, `nk.rsp_peaks`, `nk.ppg_process`) may be used if it's simpler and
  fits in memory; otherwise use scipy directly. Do not add other heavy dependencies.

**Rates** (every 5 s)

- **Windows (decided):** the reported breathing rate uses a **60 s** window (a 30 s window
  holds only 3 breaths at 6/min). A 30 s window may be computed as a *preliminary* estimate
  (`rr_prelim`) for display only; it never feeds alerts.
- Breathing rate from each source by peak detection; confirm with dominant FFT frequency.
- Heart rate from pulse peaks.
- Report `rr_belt`, `rr_accel`, `rr_airflow`, `rr_fused`, `hr`.
- **`rr_fused` (decided):**
  - ≥ 2 clean sources: median of the clean sources that agree within 3 breaths/min of each
    other (largest agreeing group); confidence from agreement.
  - Exactly 1 clean source (likely on real hardware if the accelerometer breathing signal is
    weak): use it, cap confidence at **0.6**, and set flag `single_source`. This still meets
    the ≥ 0.6 alert gate on purpose, so losing one sensor does not disable every alarm.
  - 0 clean sources: `None`.
- M3 acceptance tolerance: ±1/min against truth when the rate is steady; **±2/min while the
  rate is changing** (window lag).
- **Demo fast warm-up (M10, `demo.fast_warmup`, on by default):** in the first 60 s after a start
  or reset, once ≥ 15 s of data exist, `rr_fused` is computed from the data so far, flagged
  `warming_up`, with confidence capped at 0.5 — below the 0.6 alert gate, so it is display-only and
  can never raise an alert. First rate ~15 s after a reset instead of ~50 s.

**Signal quality** (`quality.py`), per 10 s window, output `clean | motion | sensor_off` plus a
confidence 0–1:

- **v0 (rules, build first):** sensor_off if a channel is flat (std below threshold) or clipped
  near 0/65535; motion if motion energy above threshold; confidence lowered when `rr_belt` and
  `rr_accel` disagree by more than 3 breaths/min.
  - **Floating pins (decided, from real hardware):** an unconnected analog pin reads ~10 000–12 800
    with random jitter, and all floating pins move together. A channel is `floating` (→
    sensor_off) when its median is in that band with jitter (std ≥ 150 counts) and either it is
    correlated (r ≥ 0.6) with another such channel **above 1.5 Hz** (breathing has no energy
    there, so connected belt + thermistor, which correlate through breathing, are not flagged),
    or, alone, most of its variance is above 1.5 Hz. A quiet attached sensor (apnea) never
    qualifies. All values in `config.yaml` (`quality.float_*`); check against a recording of
    disconnected pins (record mode, label `sensor_off`).
- **v1 (ML):** RandomForest (scikit-learn) trained on recorded, labeled windows. Features:
  per-channel std, range, clipping fraction, dominant frequency and its power share, belt–accel
  correlation, belt–airflow correlation, motion energy mean/max, rate disagreement. Fall back to
  v0 if no model file exists. Print a confusion matrix when training.

**Events** (`events.py`), using rolling baseline amplitude = median breath amplitude over the
previous 2 min of clean data:

- **Apnea:** airflow amplitude < 10% of baseline for ≥ 10 s.
  - **Obstructive:** effort amplitude stays ≥ 30% of baseline during the apnea.
  - **Central:** effort amplitude < 20% of baseline during the apnea.
  - **Mixed:** starts central, effort returns before airflow does.
- **Hypopnea (simplified):** airflow amplitude 10–70% of baseline for ≥ 10 s. Note in UI that
  this is simplified because SpO2 is not measured.
- Each event: start, end, duration, type, confidence, posture at onset.
- Posture from the accelerometer gravity vector: supine, left, right, upright.

## 8. Alerts (`alerts.py`)

Tiers:

| Tier | Name | Output |
|---|---|---|
| 0 | Log | stored only, visible in timeline |
| 1 | Watch | patient tile turns yellow |
| 2 | Notify | tile orange + nurse notification panel entry with reason |
| 3 | Urgent | tile red + bedside buzzer + escalates to "charge nurse" if not acknowledged in 60 s |

Rules (all values in `config.yaml`; demo values, not clinical):

- Only windows with quality `clean` and confidence ≥ 0.6 can raise tier ≥ 1.
- A condition must persist for its `persist_s` before firing.
- `rr_fused` < 10 for 60 s → tier 1; < 8 for 60 s → tier 2; < 6 for 30 s → tier 3.
- `rr_fused` dropped ≥ 30% from patient baseline (median of first 10 min, or since admission)
  over ≤ 30 min → tier 2 "declining trend".
- Any apnea ≥ 20 s → tier 3. Apnea 10–20 s → tier 1; ≥ 5 apneas in 15 min → tier 2.
  (Decided: the tier-3 alarm fires from the apnea *in progress*; an apnea watch runs every 1 s,
  so the alarm fires ~1–2 s after an apnea passes 20 s, i.e. 21–22 s into it. The demo's long
  apneas are 32 s and 26 s so this is visibly mid-apnea.)
- Sensor off for > 60 s → tier 1 "check sensor" (not an emergency).
- Cooldown: same alert type not re-raised within 120 s unless tier increases.
- Acknowledge endpoint clears the buzzer and records who/when.
- Every alert has `reason`, e.g. `"Breathing rate 7/min for 75 s (baseline 15/min), signal confidence high"`.

**Naive monitor** runs in parallel on the same data for the demo comparison: RR < 8 or > 25,
HR < 50 or > 120, airflow flat > 10 s — fixed thresholds, no quality gating, no persistence,
no cooldown. Count its alarms per hour vs ours.

## 9. Storage (`store.py`)

SQLite in `data/breathwatch.db`. Tables: `vitals` (every 5 s: rates, quality, posture),
`events`, `alerts` (with tier, reason, acknowledged_at), `llm_cache`. Raw waveforms are not
stored except in record mode.

## 10. LLM summaries (`llm.py`) — Google Gemini API

We are entering MLH "Best Use of Gemini API", so Gemini is the LLM provider.

- Use the official `google-genai` Python SDK. Key from env `GEMINI_API_KEY` (or `.env` at the repo
  root via python-dotenv; the environment wins; never logged); model name from
  `config.yaml` (`llm.model`). Check the current Gemini API docs for the right model id
  (use a fast "flash" model for explanations, optionally a "pro" model for the handoff note).
  Never hardcode the key.
- Wrap the provider behind a small interface (`summarize(kind, digest, images) -> text`) so the
  rest of the code does not care which LLM is used.
- Input: a **structured JSON digest** built in Python (time range, rate stats, events by type
  and posture, alerts with reasons, quality stats). Never send raw waveforms as numbers.
- **Multimodal (this is our "best use" angle):** for the overnight summary and handoff note,
  also render a PNG of the breathing-rate trend with events marked (matplotlib, Agg backend,
  small figure) and send it with the digest. The prompt asks Gemini to describe the pattern
  it sees in the chart and cross-check it against the digest; any disagreement is reported,
  not resolved by guessing.
- Optional stretch: structured output (JSON schema) for the SBAR note so the dashboard can
  render the four sections separately.
- System prompt rules: use only the provided data; state uncertainty; no diagnoses; no
  medication or dosing advice; "Recommendation" in SBAR is limited to monitoring/clinician
  review suggestions; mention that the device is a prototype.
- Endpoints: `POST /api/summary/explain/{alert_id}`, `POST /api/summary/overnight`,
  `POST /api/summary/handoff` (SBAR format).
- Cache every response in `llm_cache`; on API failure, return the latest cached response with
  a "cached" flag. Timeout 20 s.
- The dashboard AI panel shows a small "Powered by Gemini" label.
- **Decided (M7):** model ids live in `config.yaml` (`llm.model`, `llm.handoff_model`; default
  `gemini-3.5-flash` first for demo reliability, then fallbacks `gemini-3.5-flash-lite`,
  `gemini-3.8-flash` on overload/quota errors; all verified with `models.list()`). `llm.provider: fake` gives a
  deterministic offline provider (tests). The digest identifies the patient by bed only (no
  name) and states when data is simulated. If the API fails and nothing is cached for that
  patient and kind, the endpoint returns **503** `{"detail": "..."}`; the explain cache is per
  alert, overnight/handoff per patient.

## 11. Server API (`app.py`)

Single-patient API used up to M6. **§19 is the multi-patient contract that replaces it from
M6.5**; these endpoints then stay as deprecated aliases for the live patient (§19.8).

- `GET /` serves `web/index.html`.
- `WS /ws/live`: pushes at 10 Hz the latest downsampled waveforms (effort, airflow, pulse),
  and every 5 s the vitals, quality, posture, tier, and new events/alerts.
- `GET /api/state`, `GET /api/events`, `GET /api/alerts`, `POST /api/alerts/{id}/ack`.
- `POST /api/source` switch input: `simulator:<scenario>`, `udp`, `serial:<port>`, or
  `serial:auto` (finds the Pico by USB VID `0x2E8A`, else the first `usbmodem`/`ttyACM` port).
  The serial source reconnects automatically after unplug/reset and ignores non-JSON lines
  (REPL banners, debug prints). `GET /api/serial/ports` lists ports.
- `POST /api/record/start` with label, `POST /api/record/stop`.
- `POST /api/demo/reset` clears state for a fresh demo run.
- Run with `uvicorn server.app:app --host 0.0.0.0 --port 8000` (or `python -m server.app`,
  which also prints the dashboard URL for phones and the `SERVER_IP` to use).

## 12. Dashboard (`web/`)

From M6.5 this single-patient dashboard becomes the **patient detail page** of the clinical
app shell (§19.5), and the Source/record/reset controls move to the admin "Device & data" page.

Vanilla HTML/JS/CSS, Chart.js, no build step. Chart.js is vendored in `web/vendor/` (decided:
the demo network may have no internet, and a CDN script would leave the page blank). Single page, works on a laptop and a
phone. Sections:

1. Patient tile: name "Bed 4", tier color, rr_fused, HR, posture, quality badge.
2. Live waveforms: effort, airflow, pulse (last 30 s).
3. Breathing-rate trend (last 60 min) with baseline line.
4. Event timeline with type badges (obstructive / central / hypopnea).
5. Alert panel: tier, reason, time, Acknowledge button.
6. Naive vs smart counter: alarms so far and alarms/hour for each.
7. AI panel: buttons "Explain latest alert", "Overnight summary", "Handoff note (SBAR)".
8. Controls: source selector, scenario selector, record mode with label buttons
   (`clean`, `motion`, `sensor_off`, `slow_breathing`, `breath_hold_effort`, `breath_hold_no_effort`), demo reset.
9. Footer: "Research prototype — not a medical device. Thresholds are for demonstration."

Readable at a glance, calm colors, red only for tier 3.

## 13. Firmware (`firmware/main.py`)

MicroPython on Pico 2W:

- Connect to WiFi from `secrets.py`; if it fails, keep running and write packets to USB serial.
- Sample at 50 Hz with a **polling loop timed with `time.ticks_diff`** (decided: not a hardware
  `Timer` callback, because MicroPython timer callbacks cannot allocate memory). Schedule each
  sample at `next += 20` ms so jitter does not accumulate; fill buffers; send a packet every
  25 samples.
- Read MMA8452Q via `machine.I2C(0, sda=Pin(4), scl=Pin(5), freq=400000)`.
- Listen on UDP 5006 for status commands; drive RGB LED and buzzer.
- Onboard LED blinks once per sent packet.
- Keep the sampling loop simple and non-blocking; never let a network error stop sampling.

## 14. Build order and acceptance checks

Work in this order. Each milestone must pass its check before moving on. Commit after each.

1. **Skeleton + simulator.** `python -m server.simulator --scenario normal --udp` sends valid
   packets; a test validates the schema.
2. **Ingest + live dashboard.** Server receives simulator UDP; waveforms move on the page.
3. **Rates.** On `normal`, `rr_fused` within ±1 of truth; on `opioid_decline`, it tracks the
   decline.
4. **Quality v0.** On `motion`, motion windows are labeled `motion`; on `sensor_off`, labeled
   `sensor_off`.
5. **Events.** On `obstructive`/`central`/`hypopnea`, ≥ 80% of ground-truth events detected
   with the right type; ≤ 1 false event per 10 min on `normal`.
6. **Alerts + naive monitor.** On `demo`, smart monitor raises far fewer alarms than naive on
   the motion phase and still reaches tier 3 on the decline and the long apnea.

   **6.5. Multi-patient backend (§19).** Auth (login/demo/logout/me, roles enforced: tests for
   401 and 403), patients in SQLite with the default 6-bed ward seeded, one pipeline per
   admitted patient (Bed 4 live, others simulated), every §19.5 endpoint and §19.6 WebSocket
   returns the documented shapes (tests check the keys), device-assignment conflicts give 409,
   and a test shows risk factors do not change alerts. Old single-patient endpoints still work
   as aliases. The frontend is built in parallel by Cursor's agent against §19.
7. **LLM (Gemini).** Summary endpoints work, including the chart image; cache fallback works
   with the network unplugged (simulate with a bad key).
8. **Firmware.** Pico packets pass the same schema test; switching source to `udp` shows real
   waveforms.
9. **Record mode + quality v1.** Recordings saved as CSV with labels; `train_quality.py`
   trains, saves `data/quality_model.joblib`, prints a confusion matrix; server uses it.
10. **Polish:** demo script works end to end from `POST /api/demo/reset`.
    - **"Calibrate supine" control:** a dashboard button (and `POST /api/calibrate/supine`) records
      the mean gravity vector over a few seconds while the patient lies flat; posture is then
      computed relative to that calibration (rotation that maps the recorded vector to supine).
      **Built:** 3 s window, refused (409) when moving / no data / implausible |g|; stored on the
      patient (`calibration`), cleared on device change. Limitation: lying flat only reveals the
      patch's tilt; a rotation of the patch within the chest plane (which swaps left/right) would
      need a second "calibrate left side" step.
      Before calibration, use the default axis convention: x = patient's left, y = toward head,
      z = out of the chest (supine az ≈ +1 g, left ax ≈ −1 g, right ax ≈ +1 g, upright ay ≈ +1 g).
11. **Deployment prep (Pi).** `deploy/pi_setup.sh` (run on the Pi in `~/hacktx` as `audreybolyard`:
    Python ≥ 3.11 check — the Pi has 3.13.5 — apt `python3-venv`, venv + `requirements.txt`,
    `dialout` group, `.env` mode 600, installs/starts the systemd unit), `deploy/breathwatch.service`
    (runs `.venv/bin/python -m server.app`, restarts on failure, code read-only, only `data/`
    writable), `deploy/push_to_pi.sh` (Mac → `audreybolyard@audreybolyard.local:~/hacktx` via rsync;
    `.env` only with `--env`). **Do not deploy to any cloud server yet.**
    - **PUBLIC_DEMO mode (§20)** for a public server: `deploy/PUBLIC_DEMO.md` (Vultr Ubuntu,
      systemd `deploy/breathwatch-public.service`, Caddy `deploy/Caddyfile.example`, .TECH DNS).

## 15. Conventions

- Python 3.11+ (dev Mac: 3.12; Pi on Trixie: 3.13), type hints, small modules, no global mutable state outside an app `State` class.
- Dependencies: fastapi, uvicorn, numpy, scipy, scikit-learn, joblib, pyyaml, pyserial,
  google-genai, matplotlib, pytest; neurokit2 optional; psycopg (only for the Tiger Data stretch). Pin versions in `requirements.txt`.
- All tunable numbers in `config.yaml`.
- Log with the `logging` module, not print (except CLI tools).
- Tests run without hardware or network: `pytest -q`.

## 16. Do not

- Do not let the LLM output change alert tiers.
- Do not add a database server, message broker, React build, Docker, or a local ML model larger
  than the RandomForest.
- Do not change the data contract silently.
- Do not claim clinical accuracy anywhere in the UI or docs.
- Do not store API keys in the repo.

## 17. Raspberry Pi 4 setup notes (optional deployment target, for the human)

- Flash Raspberry Pi OS Lite 64-bit (Trixie); enable SSH.
- Hotspot (NetworkManager):
  `sudo nmcli device wifi hotspot ifname wlan0 ssid BreathWatch password <pass>` — the Pi is
  usually `10.42.0.1` on its own hotspot; confirm with `ip addr` and set `SERVER_IP` to match.
- Internet for the Gemini API via Ethernet.
- Host `audreybolyard.local`, user `audreybolyard`, project in `~/hacktx`, Python 3.13.5.
- From the Mac: `./deploy/push_to_pi.sh --env --setup` (first time: copies code + `.env`, then runs
  `deploy/pi_setup.sh` on the Pi, which creates the venv, installs the `breathwatch` systemd
  service and starts it). Later: `./deploy/push_to_pi.sh --restart`.
- On the Pi: `systemctl status breathwatch`, `journalctl -u breathwatch -f`.

## 18. Hackathon context (HackTX '26)

- **All code must be written during the hacking period.** This file is a plan; generate code
  only after hacking starts.
- Soft submission Saturday 11:30 PM; final Devpost submission Sunday 11:30 AM; judging Sunday
  2–5 PM. Keep the app demoable at every commit after milestone 2.
- Target tracks (max 3 besides Best Overall/Novice — confirm with organizers whether MLH prizes
  count toward that limit):

  1. **Best Hardware** (main target).
  2. **MLH Best Use of Gemini API** — section 10.
  3. **MLH "Your Hackathon A-Team with SpaceXSI"** — we build with Cursor + Grok Bot. Keep
     `BUILD_LOG.md` updated: which agent built which module, prompts that worked, how the
     multi-agent workflow was set up. This is evidence for that track.
  Also: register a **.TECH** domain (MLH Best Domain), and write a DEV post after the event.
- **Stretch — Tiger Data (MLH):** vitals are time-series data. If time allows, add an optional
  sink that mirrors the `vitals`, `events` and `alerts` tables to a Tiger Cloud (TimescaleDB)
  hypertable via `psycopg`, enabled only when `TIGER_DATABASE_URL` is set. SQLite stays the
  local source of truth so the demo works offline. Do not start this before milestone 9.

## 19. Clinical app shell (multi-patient ward) — API contract

Built in **milestone 6.5** (backend, Claude Code). The frontend (Cursor's agent) is built **in
parallel against this contract with mock data**, so the shapes below are binding. Change them
only by editing this section first, and say so in `NOTES.md`.

### 19.1 Scope and rules

- **Demo login:** accounts in `config.yaml`, roles `nurse`, `physician`, `admin`, a session
  cookie, and a one-click **"Demo sign-in"** per role. **Demo-grade only**: plaintext demo
  passwords in config, in-memory sessions (a server restart signs everyone out), no lockout,
  no CSRF tokens (relies on `SameSite=Lax`), plain HTTP on the LAN. The login page must say
  "Demo accounts — do not use real credentials."
- **Multiple patients.** Each patient has an id, name, bed, risk factors and a **device
  assignment**: either the **live wearable** (by device id) or a **simulator scenario**.
  Each admitted patient runs **its own pipeline** (buffers → rates → quality → events →
  alerts). Patients are stored in SQLite (`patients` table).
- **Default ward:** 6 beds, seeded on first start when the `patients` table is empty
  (`ward.seed` in `config.yaml`). Bed 4 = live wearable `patch-01`; the others are simulated
  with a mix of scenarios (looping, real-time). All names are fictional.
- **Risk factors are display and LLM context only. They never change alert thresholds, tiers,
  persistence or cooldowns.** (Also design rule 8 in §2.) A test must show that the same data
  with different risk factors gives identical alerts.
- **Live wearable input** (UDP / serial / simulator-as-live) is still chosen with the
  admin-only `POST /api/source`; packets with `device` = `patch-01` go to the patient who has
  that device assigned. Packets for a device nobody is assigned to are counted and dropped.
- A device id can be assigned to **at most one admitted patient** (409 otherwise).
- Discharging stops the patient's pipeline; their stored vitals/events/alerts are kept.

### 19.2 Conventions

- JSON everywhere. **All times are Unix epoch seconds (float, UTC)**, in fields ending in `_t`
  (or `t`). Durations end in `_s`. Rates are breaths/min (`rr*`) and beats/min (`hr`).
- Missing/unknown values are `null`, never omitted.
- Errors: `{"detail": "<human-readable message>"}` with status `400` bad input, `401` not
  signed in, `403` role not allowed, `404` unknown id, `409` conflict, `422` schema
  validation (FastAPI's default 422 body is acceptable).
- Auth: session cookie `bw_session` (HttpOnly, `SameSite=Lax`, `Path=/`). WebSockets use the
  same cookie (same origin); an unauthenticated socket is closed with code **4401**, a
  forbidden one with **4403**.
- `auth.enabled: false` in config (tests/dev only) treats every request as the first admin
  account.

### 19.3 Shared objects

**User**
```json
{"username": "nurse", "display_name": "Demo Nurse", "role": "nurse"}
```

**RiskFactors** (all required on create; `osa` is a string, the rest booleans)
```json
{"opioids": true, "supplemental_o2": false, "osa": "suspected", "age_over_65": true}
```
`osa` ∈ `"none" | "suspected" | "known"`.

**Device** (assignment), one of:
```json
{"kind": "live", "device_id": "patch-01"}
{"kind": "simulator", "scenario": "opioid_decline", "speed": 1.0, "loop": true}
```
`scenario` ∈ `normal | motion | opioid_decline | obstructive | central | hypopnea |
sensor_off | demo`.

**Patient**
```json
{
  "id": "p4",
  "name": "Jordan Avery",
  "bed": "4",
  "risk_factors": {"opioids": true, "supplemental_o2": true, "osa": "suspected", "age_over_65": false},
  "device": {"kind": "live", "device_id": "patch-01"},
  "notes": "Post-op day 1, PCA morphine",
  "status": "admitted",
  "admitted_t": 1792870000.0,
  "discharged_t": null,
  "calibration": null
}
```
`calibration` is `null` or `{"gravity": [gx, gy, gz], "samples": 150, "calibrated_t": …}` (set by
"calibrate supine", cleared when the device changes). `id` is server-assigned (`p` + integer). `status` ∈ `"admitted" | "discharged"`. `bed` is a
string ("4", "4B").

**DeviceStatus** (runtime status of the patient's input)
```json
{
  "kind": "live",
  "device_id": "patch-01",
  "scenario": null,
  "link_ok": true,
  "source": "serial:/dev/cu.usbmodem1101",
  "source_status": "connected to /dev/cu.usbmodem1101",
  "packets": 1840,
  "gaps": 1,
  "missing_packets": 3,
  "last_packet_t": 1792877823.6
}
```
For simulated patients: `kind: "simulator"`, `device_id: null`, `scenario` set, `source:
"simulator:<scenario>"`, `link_ok: true` while running.

**Vitals** (one 5 s update; fields from §7, M3–M4)
```json
{
  "t": 1792877820.0,
  "rr_fused": 14.8, "rr_conf": 0.95, "rr_sources": ["belt", "accel", "airflow"],
  "flags": [],
  "rr_belt": 14.9, "rr_accel": 14.6, "rr_airflow": 14.8,
  "conf_belt": 0.97, "conf_accel": 0.93, "conf_airflow": 0.96,
  "rr_prelim": 15.1,
  "hr": 70.2, "hr_conf": 0.9,
  "quality": "clean", "quality_conf": 0.92,
  "posture": "supine",
  "motion": 0.004
}
```
`flags` may contain `"single_source"`, `"sources_disagree"`, `"outlier:<source>"`,
`"no_clean_source"`, `"warming_up"` (provisional rate in the first minute; display it as such). `quality` ∈ `"clean" | "motion" | "sensor_off" | "unknown"`.
`posture` ∈ `"supine" | "left" | "right" | "upright" | "unknown"`.

**Event** (M5)
```json
{
  "id": 17,
  "patient_id": "p4",
  "type": "obstructive",
  "start_t": 1792877700.0,
  "end_t": 1792877722.5,
  "duration_s": 22.5,
  "confidence": 0.85,
  "posture": "supine",
  "airflow_pct": 4.0,
  "effort_pct": 96.0
}
```
`type` ∈ `"obstructive" | "central" | "mixed" | "hypopnea"`. `airflow_pct`/`effort_pct`:
median amplitude during the event as % of baseline.

**Alert** (M6)
```json
{
  "id": 42,
  "patient_id": "p4",
  "kind": "apnea",
  "tier": 3,
  "reason": "Apnea 22 s (obstructive, supine), signal confidence high",
  "status": "active",
  "raised_t": 1792877722.5,
  "updated_t": 1792877722.5,
  "cleared_t": null,
  "escalated": false,
  "escalated_t": null,
  "acknowledged_by": null,
  "acknowledged_t": null,
  "ack_note": null
}
```
`kind` ∈ `"rr_low" | "rr_trend" | "apnea" | "apnea_cluster" | "sensor_check"`.
`tier` 0–3 (§8). `status` ∈ `"active" | "awaiting_ack" | "acknowledged" | "resolved"`:
- `active` — the condition is present (tier 3: buzzer on).
- `awaiting_ack` — **resolved, awaiting acknowledgment**: a latched tier 2–3 alert whose
  condition has been clear for 30 s. Still unacknowledged, so it still counts toward the
  patient's tier and still escalates, but the buzzer stops. Show it differently from an active
  alarm. If the condition returns it goes back to `active` (same alert id).
- `acknowledged` — a user acknowledged it (from `active` or `awaiting_ack`).
- `resolved` — a tier 0–1 alert whose condition cleared (tier 0 entries are created resolved).

`cleared_t` is when the condition cleared (`null` while present). `escalated` becomes true when
a tier-3 alert is not acknowledged within 60 s ("charge nurse"). `acknowledged_by` is a
**User** or `null`.

**MonitorCounts** (naive vs smart, §8)
```json
{"smart": {"alarms": 3, "per_hour": 1.5}, "naive": {"alarms": 41, "per_hour": 20.5}, "since_t": 1792870000.0}
```

**PatientSummary** (one row of the ward overview)
```json
{
  "id": "p4",
  "name": "Jordan Avery",
  "bed": "4",
  "device_kind": "live",
  "scenario": null,
  "link_ok": true,
  "tier": 2,
  "tier_reason": "Breathing rate 7/min for 75 s (baseline 15/min), signal confidence high",
  "rr": 7.2,
  "rr_conf": 0.91,
  "hr": 68.0,
  "posture": "supine",
  "quality": "clean",
  "last_event": {"type": "obstructive", "start_t": 1792877700.0, "duration_s": 22.5},
  "unacked_alerts": 1,
  "risk_factors": {"opioids": true, "supplemental_o2": true, "osa": "suspected", "age_over_65": false},
  "updated_t": 1792877820.0
}
```
`tier` is the highest tier among the patient's **unacknowledged** alerts (status `active` or
`awaiting_ack`), else 0. `unacked_alerts` counts both.
`tier_reason` is that alert's `reason` (or `null`). `last_event` is `null` if none.

**PatientState** (patient detail page)
```json
{
  "patient": { "...": "Patient" },
  "device": { "...": "DeviceStatus" },
  "vitals": { "...": "Vitals, or null before the first update" },
  "tier": 2,
  "tier_reason": "…",
  "baseline": {"rr": 15.1, "since_t": 1792870000.0, "n": 120},
  "active_alerts": [ { "...": "Alert (unacknowledged: active or awaiting_ack)" } ],
  "recent_events": [ { "...": "Event (newest first, max 10)" } ],
  "monitors": { "...": "MonitorCounts" }
}
```
`baseline` is `null` until enough clean data exists (§8).

**Summary** (LLM output, §10)
```json
{
  "kind": "handoff",
  "patient_id": "p4",
  "alert_id": null,
  "text": "Situation: …",
  "sbar": {"situation": "…", "background": "…", "assessment": "…", "recommendation": "…"},
  "model": "gemini-…",
  "cached": false,
  "created_t": 1792877900.0
}
```
`kind` ∈ `"explain" | "overnight" | "handoff"`. `sbar` is `null` unless `kind` is `handoff`.
`cached: true` means the API failed and this is the latest cached response. If the API fails
and nothing is cached yet, the summary endpoints return `503 {"detail": …}`.

### 19.4 Permissions

| Action | nurse | physician | admin |
|---|---|---|---|
| View ward, patients, state, vitals, events, alerts; WebSockets | ✓ | ✓ | ✓ |
| Acknowledge alerts | ✓ | ✓ | ✓ |
| Generate summaries | ✓ | ✓ | ✓ |
| Create / update / discharge patients | ✓ | — | ✓ |
| Source, serial ports, record, demo reset (`/api/source`, `/api/serial/ports`, `/api/record/*`, `/api/demo/reset`, `/api/admin/*`) | — | — | ✓ |

### 19.5 Endpoints

**Auth**

| Method & path | Body | Response |
|---|---|---|
| `POST /api/auth/login` | `{"username": "nurse", "password": "demo"}` | `200 {"user": User}` + `Set-Cookie`; `401` bad credentials |
| `POST /api/auth/demo` | `{"role": "nurse"}` | `200 {"user": User}` + cookie (signs in as the first account with that role) |
| `POST /api/auth/logout` | — | `204` (clears cookie; works when signed out too) |
| `GET /api/auth/me` | — | `200 {"user": User}`; `401` |
| `GET /api/auth/demo-accounts` | — | `200 [{"role": "nurse", "display_name": "Demo Nurse"}, …]` (no auth; for the login page buttons; never returns passwords) |

**Ward and patients**

| Method & path | Body / query | Response |
|---|---|---|
| `GET /api/patients` | `?status=admitted` (default) `\|discharged\|all` | `200 {"patients": [PatientSummary], "counts": {"0": 3, "1": 1, "2": 1, "3": 1}, "generated_t": …}` sorted by tier desc, then `unacked_alerts` desc, then bed (natural order) |
| `POST /api/patients` | PatientCreate (below) | `201 Patient`; `409` if the live device is already assigned |
| `GET /api/patients/{id}` | — | `200 Patient`; `404` |
| `PATCH /api/patients/{id}` | any subset of PatientCreate fields | `200 Patient`. Changing `device` restarts that patient's pipeline (state cleared, stored history kept). `409` on device conflict, `400` if discharged |
| `POST /api/patients/{id}/discharge` | `{}` | `200 Patient` (`status: "discharged"`); idempotent |

PatientCreate:
```json
{
  "name": "Jordan Avery",
  "bed": "4",
  "risk_factors": {"opioids": true, "supplemental_o2": true, "osa": "suspected", "age_over_65": false},
  "device": {"kind": "simulator", "scenario": "normal", "speed": 1.0, "loop": true},
  "notes": ""
}
```
`name`, `bed`, `risk_factors`, `device` required; `notes` optional (default `""`);
`speed` default 1.0, `loop` default true. Two admitted patients may not share a bed (409).

**Per patient** (`{id}` = patient id; all `404` for unknown ids)

| Method & path | Query / body | Response |
|---|---|---|
| `GET /api/patients/{id}/state` | — | `200 PatientState` |
| `GET /api/patients/{id}/vitals` | `?since_t=&limit=720` | `200 {"vitals": [Vitals]}` oldest first (trend chart; default last 60 min) |
| `GET /api/patients/{id}/events` | `?since_t=&limit=100` | `200 {"events": [Event]}` newest first |
| `GET /api/patients/{id}/alerts` | `?status=active\|awaiting_ack\|unacked\|all` (default all; `unacked` = active + awaiting_ack) `&since_t=&limit=100` | `200 {"alerts": [Alert]}` newest first |
| `POST /api/patients/{id}/alerts/{alert_id}/ack` | `{"note": "Patient roused, RR 12"}` (note optional) | `200 Alert` (`status: "acknowledged"`, `acknowledged_by` = current user); `404`; acknowledging twice returns the alert unchanged |
| `POST /api/patients/{id}/summary/explain/{alert_id}` | — | `200 Summary` |
| `POST /api/patients/{id}/summary/overnight` | — | `200 Summary` |
| `POST /api/patients/{id}/summary/handoff` | — | `200 Summary` (with `sbar`) |
| `GET /api/patients/{id}/summary/chart.png` | — | `200 image/png` (the trend chart sent to Gemini) |
| `POST /api/patients/{id}/calibrate/supine` | `{}` (patient lying flat and still) | `200 {"patient_id", "gravity": [gx, gy, gz], "samples", "calibrated_t", "posture_now"}`; `409` if < 3 s of accelerometer data, the patient is moving, or \|g\| is implausible. nurse/admin |
| `DELETE /api/patients/{id}/calibration` | — | `200 Patient` (`calibration: null`). nurse/admin |
| `POST /api/calibrate/supine` | `{}` | same as above for the patient wearing the live device (the M10 path) |

**Admin: device & data** (existing endpoints keep their paths and bodies; admin-only)

| Method & path | Body | Response |
|---|---|---|
| `GET /api/admin/device` | — | `200 {"source": "udp", "source_status": "…", "link_ok": true, "stats": {…ingest stats…}, "assigned_patient_id": "p4", "unassigned_packets": 0, "network": {"ips": ["172.20.10.10"], "http_port": 8000, "udp_port": 5005}, "recording": null \| {"label": "motion", "started_t": …, "samples": 1200}}` |
| `POST /api/source` | `{"source": "serial:auto", "speed": 1, "loop": false}` | `200` same body as `GET /api/admin/device` |
| `GET /api/serial/ports` | — | `200 [{"device": "/dev/cu.usbmodem1101", "description": "…", "vid": 11914, "pid": 5, "is_pico": true}]` |
| `POST /api/record/start` | `{"label": "motion"}` (§12 labels) | `200 {"recording": {…}}` |
| `POST /api/record/stop` | — | `200 {"recording": null, "file": "data/recordings/….csv"}` |
| `POST /api/demo/reset` | `{"reseed_ward": false}` | `200 {"patients": [PatientSummary], …}` (same as `GET /api/patients`). Clears runtime state, events, alerts and vitals of all patients and restarts simulators; `reseed_ward: true` also deletes all patients and re-creates the default ward |

**Pages** (static files served by the backend; all except `/login` redirect to `/login`
when not signed in — the JS does this on a `401` from `/api/auth/me`)

| Path | File | Content |
|---|---|---|
| `/login` | `web/login.html` | username/password form + one "Demo sign-in" button per role + the demo-credentials warning |
| `/` | `web/index.html` | **Ward overview**: tier-count banner (from `counts`), patient cards sorted as the API returns them, live vs simulated marker, unacked count; click → patient detail |
| `/patients/new` | `web/patient-new.html` | Add-patient form (PatientCreate); live device option disabled with a hint when already assigned (409 shown) |
| `/patients/{id}` | `web/patient.html` | **Patient detail** = today's dashboard for one patient (tile, waveforms, trend with baseline, events, alerts with Acknowledge, naive vs smart, AI panel), plus risk factors, edit and discharge |
| `/admin/device` | `web/admin.html` | **Device & data** (admin): Source selector, serial ports, record mode with label buttons, demo reset, network/connection info |

Shared code goes in `web/common.js` (auth check, fetch helper, nav bar with user and sign-out)
and `web/style.css`. Every page keeps the §12 footer.

### 19.6 WebSockets

**Ward socket `WS /ws/ward`** (overview page; light)

| Message | When | Shape |
|---|---|---|
| `ward` | on connect, then at most every 2 s when anything changed | `{"type": "ward", "patients": [PatientSummary], "counts": {"0": 3, "1": 1, "2": 1, "3": 1}, "generated_t": …}` |
| `alert` | immediately when any alert is raised, changes tier, escalates, is acknowledged or resolved | `{"type": "alert", "patient_id": "p4", "alert": Alert}` |

**Patient socket `WS /ws/patients/{id}`** (detail page; includes waveforms)

| Message | When | Shape |
|---|---|---|
| `hello` | on connect | `{"type": "hello", "patient": Patient, "state": PatientState, "trend": [[t, rr_fused_or_null], …], "window_s": 30, "trend_window_s": 3600}` |
| `wave` | up to 10 Hz (new samples only) | `{"type": "wave", "start": 15000, "step": 2, "fs": 50.0, "end": 15050, "t_end": …, "effort": [int\|null…], "airflow": [int\|null…], "pulse": [int\|null…]}` — raw ADC counts decimated to 25 Hz; `start`/`end` are sample indices; sample `start + k·step` is element `k`; `null` = gap |
| `vitals` | every 5 s of sample time | `{"type": "vitals", "vitals": Vitals, "trend": [[t, rr_fused_or_null], …]}` (`trend` = only the new points) |
| `event` | when an event ends | `{"type": "event", "event": Event}` |
| `alert` | as on the ward socket, this patient only | `{"type": "alert", "alert": Alert}` |
| `status` | every 1 s | `{"type": "status", "device": DeviceStatus, "tier": 2, "monitors": MonitorCounts}` |
| `reset` | pipeline restarted (device change, demo reset) | `{"type": "reset"}` — clear charts, then expect fresh data |
| `discharged` | patient discharged | `{"type": "discharged", "patient": Patient}`; server then closes the socket (code 1000) |

Unknown patient id: the socket is closed with code **4404**.

### 19.7 Config (added in M6.5)

```yaml
auth:
  enabled: true
  cookie_name: bw_session
  session_ttl_h: 12
  accounts:            # DEMO ONLY — never real credentials
    - {username: nurse, display_name: Demo Nurse, role: nurse, password: demo}
    - {username: doctor, display_name: Demo Physician, role: physician, password: demo}
    - {username: admin, display_name: Demo Admin, role: admin, password: demo}
ward:
  live_device_id: patch-01
  seed:                # created when the patients table is empty
    - {bed: "1", name: …, device: {kind: simulator, scenario: normal}, risk_factors: {…}}
    - …               # 6 beds; bed 4 = {kind: live, device_id: patch-01}
```
Default seed mix: bed 1 `normal`, bed 2 `motion`, bed 3 `obstructive` (OSA suspected),
bed 4 live, bed 5 `opioid_decline` (opioids), bed 6 `central`. Simulated patients start at a
random offset into their scenario so the ward does not move in lockstep, and on ward start /
demo reset each simulated bed is **backfilled** with `ward.backfill_s` (150 s) of history so rates,
baseline and trend show immediately (newly admitted patients get no backfill). Each simulated
patient's packets use device id `sim-<patient id>`; the live wearable keeps `patch-01`.

### 19.8 Transition from the single-patient API

Until M6.5, the single-patient endpoints (`/api/state`, `/api/events`, `/api/alerts`,
`/api/alerts/{id}/ack`, `/api/summary/*`, `/ws/live`) refer to the live patient (`p4`). From
M6.5 they remain as aliases for the patient assigned to `ward.live_device_id` (deprecated, for
older tests/tools); new frontend code uses only §19 endpoints.

### 19.9 Mock data for the frontend

Cursor's agent mocks these endpoints from the examples above. Mocks must cover: tier 0–3
patients, a disconnected live device (`link_ok: false`), a patient with `rr: null`
(`flags: ["no_clean_source"]`), a `single_source` patient, an escalated tier-3 alert, a
`cached: true` summary, and `401` → redirect to `/login`.

## 20. PUBLIC_DEMO mode (approved)

For putting the demo on the public internet (deployment notes: `deploy/PUBLIC_DEMO.md`).
Implemented in `server/public_demo.py` + `server/app.py` + `server/patients.py`; settings in
`config.yaml` → `public_demo`.

1. **On with env `PUBLIC_DEMO=1`** (or `public_demo.enabled: true`).
2. **Simulated beds only:** no UDP or serial listener and no bedside commands; the seeded live bed
   becomes a looping `public_demo.live_bed_scenario` (`demo`) simulator, also in a database made
   by a non-public run. Creating or switching a patient to a live device → `400`.
3. **Admin locked:** `POST /api/source`, `GET /api/serial/ports`, `POST /api/record/start` → `403`
   ("… is disabled in the public demo"). `POST /api/demo/reset` at most once per
   `reset_cooldown_s` (120 s) for the whole site → `429` with `Retry-After`.
4. **Login:** demo-role sign-in buttons only; `POST /api/auth/login` → `403`.
5. **Gemini cost control:** summaries limited per session (`summary_per_session_per_h`, 6) and
   globally (`summary_global_per_h`, 60). Over a limit → the latest cached summary for that patient
   and kind (`cached: true`), or `429` if none is cached.
6. **Banner** "Public demo — simulated data, research prototype" on every page: the server inserts
   `<div class="public-demo-banner" role="note">` right after `<body>` in each page it serves
   (so it is there whatever the frontend does; the frontend may restyle the class).
7. **Bound to `127.0.0.1`** (`public_demo.http_host`) behind an HTTPS reverse proxy (Caddy); uvicorn
   trusts `X-Forwarded-*` only from `public_demo.proxy_ips`.

Additions needed to make the seven points safe (decided while implementing):
- The session cookie is `Secure` in public mode (HTTPS only).
- The ward is capped at `max_patients` (12) admitted patients (each runs a simulator); more → `429`.
- `GET /api/public-config` (no sign-in): `{"public_demo": bool, "banner": str|null,
  "password_login": bool, "live_device": bool}` so the frontend can hide the password form and the
  live-device option.
