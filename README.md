# BreathWatch

A research prototype of a wearable breathing monitor for post-surgical patients on a general hospital ward. It watches breathing between the usual 4–6 hour vital checks, stays quiet when the signal is bad, and writes plain-language summaries of what the monitor already decided.

**This is not a medical device.** Thresholds are demonstration values. Do not use it for clinical decisions.

## The problem

On a general ward, opioids can slow or stop breathing in the hours between checks. A pulse oximeter can miss that when the patient is on supplemental oxygen, because oxygen saturation stays high while ventilation falls. Continuous monitors that do exist alarm on motion, loose sensors, and ordinary movement, and nurses learn to ignore them. Many of these patients also have obstructive sleep apnea that has never been diagnosed, which looks different from a central apnea: the chest is still trying, but air is not moving.

## How it works

BreathWatch is three pieces.

1. **Wearable.** A Raspberry Pi Pico 2W on a chest belt samples at 50 Hz: chest-belt pressure and an accelerometer for breathing effort, a thermistor under the nose for airflow, the same accelerometer for posture and motion, and a finger sensor for heart rate. Every half-second it sends one JSON packet, over UDP or as a line on USB serial. A bedside LED and buzzer show the current alert tier.

2. **Hub.** A laptop (or, optionally, a Raspberry Pi 4) receives those packets and runs one processing pipeline per admitted patient. It filters the signals, estimates breathing rate from the sources that look clean, labels each window `clean`, `motion`, or `sensor_off`, and classifies breathing events as obstructive apnea, central apnea, mixed apnea, or hypopnea. Alert rules are ordinary Python: low rate, a falling trend against that patient's own baseline, long or repeated apneas, and a sensor that has been off. A parallel "naive" monitor uses fixed thresholds with no quality check, so the ward view can show how many alarms the careful rules avoided.

3. **Ward dashboard.** Nurses sign in and see the six-bed ward, sorted by urgency. A patient page shows live waveforms, the breathing-rate trend, events, and every alert with its reason and an Acknowledge button. Physicians read the same pages. An admin connects the real wearable or a simulator. Gemini writes an explanation of an alert, an overnight summary, and an SBAR handoff note. It is given a structured digest and a chart, never the raw waveform numbers, and it cannot change an alarm.

Other beds on the default ward run looping simulators, so the station looks occupied even when only one person is wearing the patch.

## Safety design

Alarm logic is deterministic. The language model never decides whether to alarm. It only explains and summarizes data the rules have already produced.

A window that is not clean, or whose confidence is below the gate, cannot raise anything above a log entry. A condition has to persist before it fires, and the same alert does not re-fire during a cooldown unless the tier increases. Every alert stores a human-readable reason. Patient risk factors — opioids, supplemental oxygen, known or suspected sleep apnea, age — are shown on the page and passed to the summary. They do not change thresholds, tiers, persistence, or cooldowns.

If Gemini is unreachable, the dashboard shows the last saved summary for that patient. The demo still runs with the network unplugged.

## Tech stack

| Piece | Choice |
|---|---|
| Wearable | Raspberry Pi Pico 2W, MicroPython, Keyestudio sensor modules, MMA8452Q accelerometer |
| Hub | Python 3.11+, FastAPI, uvicorn, numpy, scipy |
| Storage | SQLite (`data/breathwatch.db`) |
| Dashboard | Vanilla HTML, CSS, and JavaScript. Chart.js is vendored so the page works offline. No build step. |
| Summaries | Google Gemini (`google-genai`), with a matplotlib trend chart sent alongside the digest |
| Tests | pytest, run against the simulator with no hardware and no network |

Tunable numbers live in `config.yaml`. The packet format between the Pico and the hub is the contract in `AGENTS.md`.

## Run it

Python 3.11 or newer. From the repo root:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Optional, for the AI panel: put `GEMINI_API_KEY=…` in `.env` at the repo root. The environment variable wins. The key is never logged. Without it, summaries fail until one has been cached.

```bash
python -m server.app
```

Open `http://localhost:8000/login`. The startup log prints a LAN address for a phone on the same network. Demo accounts are `nurse`, `doctor`, and `admin`, password `demo`. These are demo credentials. Do not use real ones.

The full judging script, including what to say at each step, is `DEMO.md`.

### Simulator

No hardware required. Sign in as **Admin**, open **Device & data**, set Source to `simulator:demo`, and switch source. Bed 4 plays normal breathing, motion, a slowing respiratory rate, then obstructive and central apneas. The other beds are already simulated.

To exercise the UDP path instead, set Source to `udp` and in another terminal:

```bash
python -m server.simulator --scenario demo --udp --speed 2
```

Other scenarios: `normal`, `motion`, `opioid_decline`, `obstructive`, `central`, `hypopnea`, `sensor_off`.

```bash
pytest -q tests
```

### Real hardware

Flash `firmware/main.py` and `firmware/mma8452q.py` to the Pico 2W. Copy `firmware/secrets_example.py` to `secrets.py` on the board (that file is gitignored) and set `WIFI_SSID`, `WIFI_PASS`, and `SERVER_IP` to the hub's address. If Wi-Fi does not connect, the Pico keeps sampling and writes the same JSON lines to USB serial.

Wiring is 3.3 V only: pressure on GP26, pulse on GP27, thermistor on GP28, accelerometer I2C on GP4/GP5, buzzer on GP15, RGB LED on GP16/GP17/GP18.

On the hub, **Device & data** → Source `serial:auto` (or `udp` if the Pico is on the network) → **Switch source**. Bed 4 should show moving waveforms. If the patient will be lying flat, use **Calibrate supine** on that page so posture is measured from the patch's actual orientation. A seated demo should skip that step.

The Pico's LED blinks blue when it has not heard a status command for 10 seconds.

## How it was built

HackTX '26. Two coding agents worked in parallel against one written spec, `AGENTS.md`. The human team reviewed each milestone, made the design calls, and built the hardware. `BUILD_LOG.md` is the record of which agent produced which part.

**Claude Code** (Claude Opus 5.5) built the hub: the simulator and packet schema, ingest, signal processing, quality rules, event detection, the deterministic alerts, SQLite, the multi-patient ward API, and the Gemini summaries. It also wrote the public-demo mode and the demo runbook. The working habit was to read the spec, propose a plan, and get the ambiguous parts decided before code. That review caught a filter cutoff that would have erased a respiratory rate of 6 breaths/min, an undefined fused rate, a wrapping Pico clock, and a timer interrupt that cannot allocate memory on MicroPython.

**Cursor** (Grok 4.7, in the Cursor IDE) built the clinical frontend and the Pico firmware. The ward pages were written against the API contract while the backend was still in progress, using a mock ward behind one switch, then pointed at the live server. Firmware is the 50 Hz polling loop, the accelerometer driver, and UDP with a USB-serial fallback. Cursor did not edit the server while that split was in force. Both agents treated `AGENTS.md` as the source of truth, so a button on the patient page and the calibration endpoint could land separately and still match.

Prompts that earned their place are copied in `BUILD_LOG.md`. The useful ones asked for a plan before code, for a test of the serial path with no board attached, and for the frontend to follow the JSON contract instead of waiting on the server.
