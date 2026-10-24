# BreathWatch — demo runbook

Research prototype — not a medical device. Thresholds are for demonstration.

Two ways to run the demo, with the same story:

- **Live:** a volunteer wears the belt, thermistor and finger sensor (Pico on USB to the Mac).
- **Fallback:** the built-in `demo` scenario plays the same story from the simulator (no hardware).

Story: **normal → motion → slowing → obstructive breath-hold (≥ 30 s) → central breath-hold →
handoff note (SBAR)**. What to point at: the smart monitor stays quiet through motion while the
naive monitor fires; it escalates on the slowing and fires tier 3 mid-apnea; Gemini writes the
handoff from the data.

---

## 1. Before judging (once, 15 minutes ahead)

- [ ] Mac on power, Wi-Fi or phone hotspot on, volume up (alarm sound is on the Pico buzzer).
- [ ] `.env` in the repo root has `GEMINI_API_KEY=…` (never show it on screen).
- [ ] Pico flashed and plugged into the Mac by USB; belt, thermistor and finger clip attached.
- [ ] Nothing else is using port 8000 or the Pico's serial port (close Thonny / mpremote):
      `lsof -iTCP:8000 -sTCP:LISTEN` should print nothing.
- [ ] **Rehearse once and press all three AI buttons on Bed 4.** Every Gemini answer is cached, so
      if the network or Gemini fails during judging the panel shows the saved copy
      ("saved copy" under the text) instead of an error. Demo reset keeps these copies.
- [ ] Volunteer: healthy adult, no heart or lung condition, comfortable with ~30 s breath-holds,
      sitting or lying down. Agree a stop signal; stop any breath-hold early if uncomfortable.

## 2. Startup order (exact commands)

1. **Server** (Terminal, repo root):

   ```bash
   cd ~/Documents/dev/repositories/hacktx26
   . .venv/bin/activate
   python -m server.app
   ```

   Wait for `Application startup complete` and note the `Dashboard: http://<ip>:8000` line
   (the phone uses that address). If macOS asks whether Python may accept connections: **Allow**.

2. **Laptop browser:** `http://localhost:8000/login` → **Demo sign-in · Admin**.
3. **Connect the wearable:** **Device & data** → Source `serial:auto` → **Switch source**.
   The connection panel should say *Receiving*, and Bed 4's waveforms should move.
   (Wi-Fi instead of USB: Source `udp`; the Pico's `SERVER_IP` must be the Mac's address above.)
4. **Phone (optional, for the "nurse's phone" moment):** same network, open
   `http://<ip>:8000/login` → **Demo sign-in · Nurse** → Ward.
5. **Calibrate supine** (only if the volunteer will be **lying** flat): volunteer lies still for
   5 s → Bed 4 → **Calibrate supine**. Skip it for a seated demo (it would call sitting "supine").
6. **Fresh start:** **Device & data** → **Demo reset** → leave *Restore the default six-bed ward*
   unticked → **Reset streams** → confirm. Bed 4's first breathing rate appears after **~15 s**
   (marked as warming up), the full rate after ~50 s. Other beds keep running (simulated patients).

## 3. The scripted sequence (live, ~9 minutes)

Keep Bed 4's page open on the laptop (waveforms, tile, alerts, *Smart vs naive*). Times are from
the reset in step 6.

| # | Volunteer does | Duration | What should happen | Say |
|---|---|---|---|---|
| 1 | **Normal** quiet breathing, still | 3 min | Rate ~12–18/min, quality *Clean*, no alerts. Builds the patient's own baseline (needs ~2 min of full-confidence rates, which start ~50 s after the reset). | "It learns this patient's baseline first." |
| 2 | **Motion:** shift, reach, wiggle the belt | 20–30 s | Quality *Motion*; the rate is withheld (motion is masked). *Smart vs naive*: naive count goes up, smart stays 0. | "A normal monitor would be alarming now. We don't alarm on garbage." |
| 3 | **Slowing:** very slow, deep breaths ~5/min (≈6 s in, 6 s out) | 2–3 min | Rate falls over ~30 s (60 s window). Tier 1 then tier 2 *Breathing rate* (< 10, then < 8, each for 60 s; plus tier 2 *Declining trend* if the baseline is ready), then **tier 3** once < 6/min for 30 s; bedside buzzer; escalates to charge nurse after 60 s unacknowledged. | "This is the opioid pattern: slow, then slower." |
| 4 | **Acknowledge** on the tier-3 alert (note optional); resume normal breathing | 1 min | Buzzer stops; alert shows who acknowledged. Once breathing recovers the remaining latched alerts read *Resolved, awaiting acknowledgment* — acknowledge them too. | "Every alarm states its reason." |
| 5 | **Obstructive breath-hold:** breathe out normally, close mouth, pinch nose, keep making gentle breathing movements with chest/belly — **at least 30 s** | 30–35 s | Airflow goes flat while effort continues. **Tier 3 "Apnea in progress … (obstructive)" at ~21–22 s**, while still holding. Event logged as *Obstructive* ~12–15 s after it ends. | "Effort without airflow: obstructive." |
| 6 | Normal breathing; acknowledge | 1 min | Baseline returns. | |
| 7 | **Central breath-hold:** breathe out, then hold with chest and belly **relaxed and still** | 25–30 s | Effort and airflow both flat. Tier 3 at ~21–22 s; event *Central*. | "No effort at all: central — a different problem." |
| 8 | Normal breathing | — | — | |
| 9 | **AI panel → Handoff note (SBAR)** (and **Overnight summary** if time) | ~5–30 s | Gemini writes Situation / Background / Assessment / Recommendation from the stored data and the trend chart; the model name is shown. | "The language model explains; it never decides an alarm." |

Notes:

- Apnea alarms fire ~1–2 s after the 20 s mark, so holds shorter than ~22 s alarm right after
  they end. Hold ≥ 30 s for the obstructive one so the alarm clearly fires mid-hold.
- An apnea only counts once the airflow channel has a baseline (≥ 30 s of breathing after a reset).
- The finger pulse sensor only drives HR; if it slips off, breathing alarms still work.
- Hypopnea (shallow breathing) is shown as an event but never alarms (no SpO₂).

## 4. Fallback: the simulated demo (no hardware)

**Device & data** → Source `simulator:demo` → Speed `1` (or `2` for a 10-minute version) → leave
*Loop simulator* off → **Switch source**. Bed 4 then plays this script (times at speed 1; halve
them at speed 2):

| Time | Phase | What fires |
|---|---|---|
| 0:00–3:00 | Normal, 15/min | first rate at 0:15 (warming up) |
| 3:00–6:03 | Four motion bursts | naive alarms, smart none |
| 6:03–11:03 | Slowing 16 → 5/min | tier 2 trend ~9:55; tier 1 ~10:25; tier 2 ~11:15; **tier 3 ~11:40** |
| 11:03–13:03 | 5/min, then recovery | escalation to charge nurse if not acknowledged |
| 14:03, **15:10 (32 s)**, 16:35 | Obstructive apneas | tier 1 at 14:32; **tier 3 at 15:32 (mid-apnea)** |
| **18:00 (26 s)**, 19:33 | Central apneas | tier 3 at ~18:22; tier 2 cluster (5 apneas) ~20:02 |

Over the 20½ minutes the naive monitor raises ~22 alarms and the smart monitor 7.

Terminal alternative (exercises the UDP path): Source `udp`, then

```bash
python -m server.simulator --scenario demo --udp --speed 2
```

## 5. If something fails mid-demo

| Symptom | Do this |
|---|---|
| Bed 4 shows *No packets* / waveforms frozen | Check the USB cable. **Device & data** → Source `serial:auto` → **Switch source**. Still nothing → close Thonny/mpremote (they hold the port) → or switch to `simulator:demo` (section 4) and keep talking. |
| Bed 4 rate "—" right after a reset | Normal: the first rate appears after ~15 s. |
| Quality stuck on *Sensor off* | A sensor is loose, clipped at a rail, or a wire is unplugged (an unconnected pin "floats" and is detected). Reseat the thermistor under the nose and the belt; check the wiring. After 60 s a tier-1 *Check sensor* appears — that is correct behaviour, say so. |
| Quality stuck on *Motion* | Ask the volunteer to keep still; it clears within ~10 s. |
| An expected alarm did not fire | Check the tile: signal confidence must be high and quality *Clean* (by design, noisy data never alarms). Make the slow breathing slower / the hold longer. |
| Buzzer won't stop | Acknowledge every tier-3 alert on Bed 4 (including ones *awaiting acknowledgment*). |
| Pico LED blinking blue | It hasn't heard from the server for 10 s: the server stopped or the link dropped — check the terminal, then the cable. |
| AI panel slow | Gemini can take 5–30 s under load (it tries `gemini-3.5-flash`, then `-lite`, then `3.8-flash`). Keep talking. |
| AI panel says unreachable / shows "saved copy" | Network or Gemini is down; the saved copy from the rehearsal is shown. That *is* the offline design — mention it. |
| Page stops updating | Reload the page (it reconnects). |
| Server crashed / terminal shows an error | `Ctrl+C`, then `python -m server.app` again (step 2.1), sign in, **Switch source**, **Demo reset**. History and saved AI answers are kept in `data/breathwatch.db`. |
| `address already in use` on start | An old server is still running: `lsof -iTCP:8000 -sTCP:LISTEN`, stop that process, start again. |
| Phone can't open the page | Same Wi-Fi/hotspot as the Mac? Use the IP from the startup log, not `localhost`. macOS firewall must allow Python. Otherwise show the laptop. |
| Ward looks wrong after experiments | **Demo reset** with *Restore the default six-bed ward* ticked. |
| Everything is on fire | Fallback: `simulator:demo` at speed 2 tells the whole story in 10 minutes without hardware. |

## 6. After the demo

- **Ctrl+C** in the terminal stops the server.
- Recordings (if any) are in `data/recordings/`; the database in `data/breathwatch.db` (gitignored).
