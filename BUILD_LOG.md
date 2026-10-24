# BUILD_LOG — which agent built what

Honest record of which tool or agent produced each part of the project.

## Workflow

- **Claude Code** (Anthropic, model Claude Opus 5.5) builds the server, simulator and processing
  (milestones 1–7).
- **Cursor's agent** builds the firmware (milestone 8) and polishes the dashboard later.
- Both agents read `AGENTS.md` as the single source of truth. Agreed decisions are written into
  `AGENTS.md` itself (not only `NOTES.md`) so both agents follow the same rules.
- The human team reviews each milestone's plan, makes the design calls and builds the hardware.

## Log

| When (2026-10-24) | Agent | Milestone | What |
|---|---|---|---|
| Sat afternoon | Claude Code | planning | Read AGENTS.md, proposed the M1–3 plan and flagged spec issues (filter corner at 6/min, window length, `rr_fused` undefined, `t0_ms` wrap, firmware timer ISR). Team accepted with changes (single-source fallback for `rr_fused`). |
| Sat afternoon | Claude Code | docs | Wrote the agreed decisions into AGENTS.md §4, §7, §13. |
| Sat afternoon | Claude Code | M1 | `config.yaml`, `server/config.py`, `server/schema.py`, `server/simulator.py` (8 scenarios + ground-truth labels), tests. |

## Prompts that worked

- "Read AGENTS.md fully. Before writing any code, give me a short plan for milestones 1–3: which
  files you'll create, how you'll test the packet schema, and anything in the spec that's
  ambiguous or that you'd change." — getting the plan reviewed first caught four spec issues
  before any code existed.
