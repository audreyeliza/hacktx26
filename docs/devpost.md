# Respiro — Devpost draft

Paste each section into the matching Devpost field. Replace the placeholders before submitting.

**Cloud URL:** `https://tryrespiro.tech`

**Demo video:** `[DEMO_VIDEO_URL]`

**Elevator pitch** (200 characters):

A wearable that watches breathing effort and airflow between ward checks, ignores junk signals, and flags slowed breathing and apnea. Gemini writes the handoff.

---

## Inspiration

On a general ward, someone checks vitals every four to six hours. Opioids can slow or stop breathing in that gap, and a pulse oximeter is a weak safety net once the patient is on oxygen. The continuous monitors hospitals already use often get ignored, because motion and a loose sensor sound the same as a real event. A lot of these patients also have sleep apnea nobody has diagnosed: the chest keeps trying, and the air does not move.

We wanted a wearable a nurse could glance at without learning to tune it out, and a handoff written from what the shift actually looked like.

## What it does

Respiro is a research prototype, not a medical device, and the thresholds are for demonstration.

A chest patch measures breathing effort, airflow at the nose, posture, motion, and heart rate. Before it will alarm, it checks that the signal is clean, compares the breathing rate with the patient's own baseline, and classifies what it sees: obstructive apnea, central apnea, mixed, or hypopnea. Every alert includes a reason. A noisy window is logged and cannot raise one. Opioids, oxygen, and suspected apnea are on the screen for the nurse, and they never change those rules.

The ward view is six beds, with the urgent ones first. A simpler monitor with fixed thresholds runs on the same data so you can see how many extra alarms it would have fired. Gemini writes an explanation, an overnight summary, and an SBAR handoff from a short digest and a chart of the trend. If that call fails, the page shows the last saved copy. The same ward runs with no hardware attached.

## How we built it

We wrote the contract first, then split the work. Claude Code built the hub: the simulator, the signal processing, the alerts, the multi-patient API, and the Gemini client. Cursor built the dashboard against that contract before the backend was finished, and then the Pico firmware. We built the belt and wired the sensors.

The wearable is a Pico 2W running MicroPython. It samples at 50 Hz and sends a small JSON packet over USB to a laptop. The server is FastAPI, and the dashboard is plain HTML, so it still loads when the network is down. We scored breathing rates, event types, and alert tiers against the simulator before we had a working board.

## Challenges we ran into

The first filter would have hidden the breaths we cared about most. Six breaths a minute sits at 0.1 Hz, so a cutoff right there shrinks the waveform exactly where the apnea thresholds are judged. We moved the corner down and wrote that into the spec before the code existed.

A real chest belt often loses a sensor. If two clean sources agree, we trust them. If only one is clean, it can still alarm, with the confidence capped, so a weak accelerometer does not turn the bed off.

The Pico clock wraps, and a timer interrupt in MicroPython cannot allocate the memory a packet needs. Sampling is a plain loop, and the server builds its timeline from when packets arrive and whether any sequence numbers are missing. A gap stays a gap.

An unplugged analog pin does not read zero. It floats, and it had to be called a missing sensor, while a quiet breath-hold still had to look like apnea. We also timed the urgent alarm instead of trusting the design note. It now fires about 21 seconds into a long apnea, while the person is still holding their breath.

## Accomplishments that we're proud of

The alarm path is deliberately boring: rules, a cooldown, and a sentence that says why. A patient's opioids or suspected apnea cannot quietly retune it. During motion, ours stays quiet while the naive monitor keeps counting. When breathing slows, or during a long apnea, ours still reaches the urgent tier.

The ward is something a nurse can actually use on a phone: six beds, live waveforms, and a handoff at the end of the shift. Unplug the board and the simulated patients still tell the story. The public demo does that on purpose, and the page says the data is simulated.

## What we learned

If two people are building at once, the awkward states have to be written down. "Resolved, awaiting acknowledgment" is a small phrase, and it changes the whole alarm panel.

We also had to time the moment we planned to show. The apnea alarm was late because we weren't checking often enough, not because the detector was slow.

Gemini is good at explaining a night and bad at deciding who is in danger. Giving it a chart and a digest, and asking it to say when the two disagree, is more honest than asking it to make the call.

## What's next

The public demo is at https://tryrespiro.tech, with simulated beds only. Next we want a real recording set so the signal-quality model can train on the wearable, a second calibration pose so a patch rotated on the chest does not swap left and right, and an oxygen measurement so hypopnea is not a simplified guess. None of that makes this a medical device.
