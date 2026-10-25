# UI pass — handoff spec

Goal: make the app feel like calm, trustworthy clinical software that a nurse or physician
can use without training. Less text, clearer hierarchy, consistent alignment, and the
visual identity of our pitch deck. **No backend/API changes** — the §19 contract stays as is.

## 1. Visual direction (from the pitch deck)

The current look (warm cream background + serif headings) must go. Match the deck:
white background, deep indigo, soft clinical blue.

| Token | Hex | Use |
|---|---|---|
| `--bg` | `#FFFFFF` | page background |
| `--surface` | `#F5F9FB` | panels, table header, subtle fills |
| `--line` | `#DCE7EE` | borders, dividers |
| `--ink` | `#1E2350` | primary text (deep indigo-navy) |
| `--ink-2` | `#5B6385` | secondary text |
| `--brand` | `#434C9C` | primary buttons, links, active nav, chart lines (deck indigo) |
| `--brand-soft` | `#D6E8EF` | selected states, chips, chart fills (deck blue) |

Tier colors (status only, never decoration; red is reserved for Urgent):

| Tier | Label shown | Color |
|---|---|---|
| 0 | **Stable** (rename from "Log") | `#5B6385` (quiet slate) |
| 1 | Watch | `#B7791F` (amber) |
| 2 | Notify | `#C2560C` (orange) |
| 3 | Urgent | `#C62828` (red) |

Every tier is shown as color **plus** its word (never color alone).

Type: **Poppins** for headings and the big vital-sign numbers (matches the deck), a clean
sans for body (e.g. Source Sans 3). Vendor both font files into `web/vendor/` so the local
demo works offline. Use tabular numerals for all vitals. Sentence case everywhere.

Remove these template habits currently on the pages:
- ALL-CAPS eyebrow labels above headings ("GENERAL WARD", "BED 4", "ADMISSION", "ADMIN").
- Meta strings joined with middle dots ("Simulated · demo · Obstructive · 14 s").
- Duplicate prototype notices: keep the top banner and the footer line; remove the
  "Research prototype" pill in the header.

The product name is Respiro. It lives in one constant (`PRODUCT_NAME` in
`web/common.js`) so renaming is a one-line change.

## 2. Content rules: clinical words, not engineering words

Audience: people who know hospitals. Keep clinical terms (supine, apnea, SBAR, baseline).
Hide engineering terms from nurse/physician views.

- Remove from default views: `conf 0.82`, `belt 15.2 · accel 15.0 · airflow 15.5`,
  `airflow 4.7% · effort 102.8% · conf 0.90`, raw ADC axis values (25,000–41,000),
  "running at 1x", device ids, speed, loop.
- Put them behind a small **"Signal details"** disclosure, shown expanded only for Admin.
- Signal quality becomes a word: Good / Low confidence / Sensor off / Starting up.
- Event times: "9:02 PM, 33 s" — not "9:02 PM–9:02 PM".
- Empty states say what is happening: "Starting up — first reading in about 15 s",
  "No reading — check sensor", never "— /min conf 0.00".

## 3. Role views

- **Nurse**: Ward, patient page, acknowledge, add patient.
- **Physician**: same views; clinical note section shown near the top of the patient page.
  (Physicians can't admit/discharge — hide those controls rather than showing errors.)
- **Admin**: everything plus Device & data and expanded Signal details.
Show the signed-in role clearly in the header (name + role), nothing else.

## 4. Ward overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│ [Name]   Ward  Add patient                          Demo Nurse (Nurse) ⏏ │
├──────────────────────────────────────────────────────────────────────────┤
│ Ward 3B  ·  6 patients                     This shift: 7 smart alarms     │
│ [● Urgent 3] [● Notify 1] [● Watch 0] [● Stable 2]   vs 23 standard       │
├──────────────────────────────────────────────────────────────────────────┤
│ Bed  Patient                      Breathing      Heart   Position Status  │
│ ▌4   Jordan Avery   Live sensor   15 /min  ⌄     71      Supine  Urgent   │
│      Breathing paused 33 s, 2 min ago                     3 to ack.       │
│ ▌3   Sam Okafor     Simulated     14 /min        70      Supine  Urgent   │
│      ...                                                                  │
└──────────────────────────────────────────────────────────────────────────┘
```

- Tier counts become a compact row of chips that also **filter** the table (click Urgent →
  only urgent beds). Urgent chip is visually strongest; Stable is quiet.
- **Smart vs standard alarm count moves here** (ward header, right side). It is the
  headline demo stat. Sum per-patient counts client-side; if the API doesn't expose them,
  note it in NOTES.md for Claude Code instead of changing server/.
- Each row: one name line + **one plain-language status line** (current situation only).
  A small "Live sensor" / "Simulated" tag; Live sensor uses `--brand`.
- Breathing column: rate + small trend arrow (or a tiny sparkline if easy).
- Remove the Signal column; show a small warning icon next to the rate only when signal
  is not Good.
- **Fix alignment**: fixed CSS grid columns so every row lines up (Beds 1 and 2 are
  currently shifted right). Numbers right-aligned, text left-aligned.
- Left tier edge on each row stays — it works.

## 5. Patient page

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ← Ward                                                                    │
│ Bed 4  Jordan Avery                         [ Urgent ]                    │
│ Breathing paused 33 s (obstructive), 2 min ago                            │
│ Opioids  Supplemental O₂  OSA suspected   ·  Live sensor                  │
├──────────────────────────────────────────────────────────────────────────┤
│ Breathing 15 /min ⌄ │ Heart 71 bpm │ Position Supine │ Signal Good        │  ← one strip
├──────────────────────────────────────────────────────────────────────────┤
│ Needs attention (2)                                                       │
│ ▌Urgent  Breathing paused 33 s, obstructive      9:02 PM   [Acknowledge]  │
│ ▌Notify  Rate fell 36% below baseline in 4 min   8:57 PM   [Acknowledge]  │
│ Recovered, waiting for acknowledgment (2)            [Acknowledge all]    │
│ Show acknowledged (5) ▸                                                   │
├──────────────────────────────────────────────────────────────────────────┤
│ Live breathing — chest movement / airflow / pulse (no raw axis numbers)   │
│ Breathing rate, last 60 min — events shaded on the chart, baseline dashed │
├────────────────────────────────────┬─────────────────────────────────────┤
│ Clinical note (Gemini)             │ Events this shift                   │
│ [Explain alert][Summary][Handoff]  │ Obstructive  33 s  Supine  9:02 PM  │
│ ...                                │ Central      27 s  Supine  9:04 PM  │
│                                    │ Show all (12) ▸                     │
├────────────────────────────────────┴─────────────────────────────────────┤
│ Admission ▸ (collapsed)                                                   │
└──────────────────────────────────────────────────────────────────────────┘
```

- **Vitals become one horizontal strip** across the top. This removes the tall vitals card
  with the huge empty area beside the alert list.
- **Alerts**: one row per alert, with the Acknowledge button on the right. The note field
  is hidden behind a small "Add note" link that expands inline. Three groups: Needs
  attention (active), Recovered — waiting for acknowledgment (muted, with an
  "Acknowledge all" button), Acknowledged (collapsed history).
- "Escalated to charge nurse" appears once, as a small red tag on the alert, not repeated
  in the sentence and again as a heading.
- **Fix the clipped text**: alert type labels and sentences currently touch/overlap the
  left tier border ("URGENT", "Breathing rate…"). Add consistent inner padding.
- **Events next to empty space**: Smart vs naive leaves the patient page (it's on the ward
  now). Events become a compact table (type tag, duration, position, time) paired with the
  clinical note in a two-column row; show the latest 5 with "Show all". Mark the same
  events as shaded bands on the breathing-rate chart.
- Clinical note: for handoff notes show only the four SBAR cards plus the chart
  description and discrepancy line — not the raw text above them.
- Admission collapsed by default; "Discharge" asks for confirmation. Remove the heavy
  green focus outline on the disclosure (keep a visible but subtle focus ring).

## 6. Add patient

- Short form: Name, Bed (dropdown of open beds), Notes.
- Risk factors as toggle chips (Opioids, Supplemental O₂, Age over 65, OSA: none /
  suspected / known), with the one-line note "Context only — never changes alarm
  thresholds".
- Monitoring: two clear options — **Live wearable** or **Demo simulation** (scenario
  dropdown). Device id, speed and loop go under "Advanced" (Admin only).
- Primary button "Admit patient", secondary "Cancel". Style checkboxes/radios to the
  palette (no default browser blue).

## 7. Device & data (Admin)

- Connection status in words with a status dot: "Wearable connected over USB — receiving",
  "Not connected", etc. (not "–").
- In public demo mode, show one clear line, "Device controls are turned off in the public
  demo", instead of empty, unusable inputs.

## 8. Everywhere

- One spacing scale (4/8/12/16/24/32/48) and one radius scale (small for chips, medium for
  panels). Not every block needs to be a rounded card — use dividers inside panels.
- Max content width ~1200 px, left-aligned.
- Works at 390 px wide (phone): ward table becomes stacked rows; vitals strip wraps 2×2.
- Visible keyboard focus; color contrast AA.
- Motion: only a subtle pulse on an *active Urgent* status. Nothing else animates.

## 9. How to work

1. Screenshot every page before starting (desktop + 390 px).
2. Implement in this order, committing after each: tokens + type + header → ward →
   patient page → add patient → admin → mobile pass.
3. After each step, screenshot again and compare against this spec; fix alignment issues
   you see.
4. Stay in `web/`. If something needs data the API doesn't provide, write it in NOTES.md
   for Claude Code.
5. Log the work in BUILD_LOG.md as built by Cursor.
