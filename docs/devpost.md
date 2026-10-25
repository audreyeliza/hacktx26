# Respiro — Devpost draft

Paste each section into the matching Devpost field. Replace the placeholders before submitting.

**Cloud URL:** `[CLOUD_URL]`

**Demo video:** `[DEMO_VIDEO_URL]`

---

## Inspiration

On a general ward, someone checks vital signs every four to six hours. Opioids can slow or stop breathing in the gap. A pulse oximeter is a weak safety net once the patient is on supplemental oxygen: saturation can look fine while ventilation is not. The continuous monitors that hospitals do use often train the staff to ignore them, because motion and a loose sensor sound the same as a real event. A lot of these patients also have obstructive sleep apnea that nobody has diagnosed, and that pattern — chest still working, air not moving — is different from a central apnea, where the effort stops too.

We wanted a wearable a ward nurse could glance at, that refuses to alarm on garbage, and that can explain itself in a handoff note. We also wanted the explanation kept on a short leash: a language model can summarize, and it cannot decide that an alarm is real.

## What it does

Respiro is a research prototype. It is not a medical device, and the thresholds are for demonstration.

A chest patch measures breathing effort, nasal airflow, posture, motion, and heart rate, and streams samples to a hub. The hub runs a pipeline per patient: signal quality first, then breathing rate against that patient's own baseline, then a classification of obstructive apnea, central apnea, mixed apnea, or hypopnea. Alerts are tiered. Each one stores a reason a person can read. Low-confidence or motion-corrupted windows are logged and cannot raise an alarm. Risk factors such as opioids or suspected sleep apnea are shown to the nurse and included in the summary. They do not change the thresholds.

The ward dashboard is the nurses' station: six beds, the urgent ones first, live waveforms and a trend on the patient page, and Acknowledge on every open alert. A naive monitor runs beside the real rules and counts how many extra alarms a fixed threshold would have fired. Gemini writes an alert explanation, an overnight summary, and an SBAR handoff from a structured digest and a chart of the breathing-rate trend. If the API fails, the last saved summary is what the page shows.

The same story runs with no hardware. A simulator feeds the real ingest path, and the default ward is already full of simulated patients so one wearable has a station around it.

## How we built it

Two agents, one spec. `AGENTS.md` was the contract: data format, alert rules, API shapes, and the rule that the model never owns the alarm. Claude Code (Claude Opus 5.5, in Claude Code) built the hub — simulator, ingest, filtering and rate estimation, quality rules, event detection, deterministic alerts, the multi-patient API, and the Gemini client. Cursor (Grok 4.7, in the Cursor IDE) built the ward dashboard against that API before the backend had landed, using a mock, then connected the pages to the live server and wrote the Pico firmware. The human team reviewed each milestone, made the design calls, and built the belt, the sensors, and the wiring. `BUILD_LOG.md` records which agent built which part, and which prompts were worth keeping.

The wearable is a Raspberry Pi Pico 2W in MicroPython, sampling at 50 Hz and sending a small JSON packet over UDP, with USB serial as the fallback. The hub is Python, FastAPI, numpy, and scipy, with SQLite and no build step on the frontend. Chart.js is vendored so the dashboard still loads with no network. Summaries use the Gemini API, including the trend chart as an image, and the model that actually answered is shown on the page.

We checked the packet schema, the rate estimates, the event types, and the alert tiers against the simulator, without a board and without the network. The demo script is a volunteer breathing normally, moving, breathing very slowly, then holding a breath with effort and without it — or the same sequence from the `demo` scenario.

## Challenges we ran into

The first plan would have filtered out the breaths we most needed to see. Six breaths a minute sits at 0.1 Hz, and a band-pass corner at that frequency would have shrunk the waveform exactly where apnea thresholds are judged. We moved the corner and wrote the decision into the spec before the filter existed.

Fusing three breathing-rate estimates sounds clean until one sensor is weak, which is likely on a real chest belt. The rule we shipped uses the agreeing sources when two or more are clean, and still allows a single clean source to alarm, with the confidence capped, so losing the accelerometer does not silence the bed.

The Pico clock wraps, and a timer interrupt on MicroPython cannot allocate the memory a JSON packet needs. Sampling is a polling loop scheduled with `ticks_diff`, and the hub builds its timeline from arrival time and sequence number. A gap in sequence numbers is a gap in the timeline, not a squashed waveform.

We measured the apnea alarm instead of trusting the design note. It was firing late because of how often the detector ran, not because the detector was slow. A one-second watch fires the urgent alarm about 21–22 seconds into a long apnea, while the breath-hold is still going, which is what a demo has to show.

An unplugged analog pin does not read zero. It floats in a band and jitters in step with the other open pins. That had to be classified as a sensor off, or a disconnected thermistor would look like a clean signal. Apnea itself must not look like a sensor off: a quiet, attached sensor is the event we are trying to catch.

The frontend and the backend were written at the same time. The API was frozen in the spec, including a status for an alarm that has resolved but still needs a person to acknowledge it. The button for "calibrate supine" and the endpoint that records the gravity vector were written by different agents and met on that contract.

Gemini's model ids and quota errors moved under us during the weekend. The client tries a fast model first and falls back, and a failed call returns the cached note rather than a blank panel.

## Accomplishments that we're proud of

The alarm path is boring on purpose. It is rules, persistence, a cooldown, and a sentence that says why. The language model is downstream of that, and a test plus the product rule keep risk factors from secretly retuning it.

On the demo's motion phase the smart monitor stays quiet while the naive one counts alarms. On the slowing and on a long apnea, the smart monitor still reaches the urgent tier, with a bedside color and buzzer and an escalation if nobody acknowledges it.

The ward is a product, not a single chart. Six beds, roles, a phone-usable page, live waveforms, and a handoff note a nurse can read at shift change. The public-demo mode locks the admin tools, serves simulated beds only, and says so in a banner.

The firmware, the schema tests, and the dashboard agree on one packet. We can unplug the board and the station still tells the story.

## What we learned

Write the contract down, including the awkward states, or two people will invent two of them. "Resolved, awaiting acknowledgment" is a small phrase and it changes the whole alarm panel.

Measure the latency you plan to demo. Our apnea alarm was late for a mundane reason, and the fix was a faster watch, not a smarter detector.

A language model is a good explainer and a bad safety interlock. Giving it a chart and a digest, and asking it to say when the two disagree, is more honest than asking it who is in danger.

Floating pins, serial banners, and a wrapping clock are the real hardware. The simulator had to grow those faults or the first hour with the Pico would have been the first time we saw them.

## What's next

`[CLOUD_URL]` is the public demo, simulated beds only, with the banner that says so. The video is `[DEMO_VIDEO_URL]`.

The Raspberry Pi hub setup is written and not yet the thing we demo from. Next is a longer recording set so the quality model can be trained on real windows instead of rules alone, a second calibration pose so a patch rotated on the chest does not swap left and right, and a real oxygen measurement so hypopnea is not the simplified version we have to label as such. None of that makes this a medical device. A clinical study, regulatory work, and thresholds that are not demo values are future work, with clinicians, and not a weekend claim.
