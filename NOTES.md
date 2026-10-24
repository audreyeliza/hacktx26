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

## Open questions

- SpaceXSI track: does Claude Code count as part of the multi-agent workflow? (team is asking MLH)
