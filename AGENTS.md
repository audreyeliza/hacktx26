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

## 3. Hardware (for context; the human builds this)

- **Wearable:** Raspberry Pi Pico 2W (MicroPython), clipped to a chest belt, USB power bank.
- **Server:** Raspberry Pi 4 Model B 1 GB, Raspberry Pi OS Lite (64-bit), acts as WiFi hotspot.
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
  not from `t0_ms` (`ticks_ms` wraps and the Pico clock drifts). `t0_ms` is kept for
  diagnostics only. A `seq` gap must appear as a **gap in the timeline** (missing samples are
  marked NaN for the gap duration), never as compressed data. Processing treats NaN spans as
  missing data, not as a flat signal.

### Pi → Pico (bedside commands)

- UDP to the Pico at port `5006`, JSON: `{"cmd": "status", "color": "green|yellow|orange|red", "buzzer": false}`.
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
│   ├── alerts.py           # baseline, trends, tiers, escalation, naive monitor
│   ├── store.py            # SQLite
│   ├── llm.py              # Gemini API summaries with caching
│   ├── record.py           # record mode: raw data + labels to CSV
│   └── train_quality.py    # trains the signal-quality model from recordings
├── web/
│   ├── index.html          # nurses' station dashboard
│   ├── app.js
│   └── style.css
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

**Signal quality** (`quality.py`), per 10 s window, output `clean | motion | sensor_off` plus a
confidence 0–1:

- **v0 (rules, build first):** sensor_off if a channel is flat (std below threshold) or clipped
  near 0/65535; motion if motion energy above threshold; confidence lowered when `rr_belt` and
  `rr_accel` disagree by more than 3 breaths/min.
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

- Use the official `google-genai` Python SDK. Key from env `GEMINI_API_KEY`; model name from
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

## 11. Server API (`app.py`)

- `GET /` serves `web/index.html`.
- `WS /ws/live`: pushes at 10 Hz the latest downsampled waveforms (effort, airflow, pulse),
  and every 5 s the vitals, quality, posture, tier, and new events/alerts.
- `GET /api/state`, `GET /api/events`, `GET /api/alerts`, `POST /api/alerts/{id}/ack`.
- `POST /api/source` switch input: `simulator:<scenario>`, `udp`, `serial:<port>`.
- `POST /api/record/start` with label, `POST /api/record/stop`.
- `POST /api/demo/reset` clears state for a fresh demo run.
- Run with `uvicorn server.app:app --host 0.0.0.0 --port 8000`.

## 12. Dashboard (`web/`)

Vanilla HTML/JS/CSS, Chart.js from a CDN, no build step. Single page, works on a laptop and a
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
7. **LLM (Gemini).** Summary endpoints work, including the chart image; cache fallback works
   with the network unplugged (simulate with a bad key).
8. **Firmware.** Pico packets pass the same schema test; switching source to `udp` shows real
   waveforms.
9. **Record mode + quality v1.** Recordings saved as CSV with labels; `train_quality.py`
   trains, saves `data/quality_model.joblib`, prints a confusion matrix; server uses it.
10. **Polish:** demo script works end to end from `POST /api/demo/reset`.

## 15. Conventions

- Python 3.11, type hints, small modules, no global mutable state outside an app `State` class.
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

## 17. Raspberry Pi 4 setup notes (for the human)

- Flash Raspberry Pi OS Lite 64-bit; enable SSH.
- Hotspot (Bookworm, NetworkManager):
  `sudo nmcli device wifi hotspot ifname wlan0 ssid BreathWatch password <pass>` — the Pi is
  usually `10.42.0.1` on its own hotspot; confirm with `ip addr` and set `SERVER_IP` to match.
- Internet for the Gemini API via Ethernet.
- `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`

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
