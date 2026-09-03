# AI Voice Panel — Boost Camp Oslo

Three AI agents on a live stage panel with a human moderator, 21 October 2026.

**Start with [FEASIBILITY.md](FEASIBILITY.md)** — architecture, risks, phase plan
and the AV requirements the venue needs.

## Quick start

```bash
uv sync
uv run pytest              # floor-control acceptance criteria, ~0.3s
uv run panel-sim           # text-mode panel, offline stub brains
uv run panel-sim --live    # real model behind the personas
```

## What exists today

| Package | Purpose |
|---|---|
| `packages/panel_core` | **Pure** floor logic. No I/O, no awaits, no clock reads. |
| `packages/panel_sim` | Text-mode harness — tune personas and thresholds without audio. |
| `personas/` | The cast, as structured data. Source of truth for prompts. |

Not built yet: STT, VAD, TTS, audio I/O, operator console, video wall.

## The two rules that keep this maintainable

**1. `panel_core` stays pure.** It is a reducer — `reduce(state, event) -> (state, commands)`.
No network, no `await`, no `time.time()`; timestamps arrive on events. This is
why the whole acceptance suite runs in under a second and cannot flake, and why
a recorded rehearsal replays identically through modified floor logic.

If you need I/O, it goes in a runtime adapter, not here.

**2. Personas are data, not prose.** The floor controller reads
`interrupt_tendency`, `max_turn_seconds` and `topics_of_authority` at runtime.
Editing a persona should never mean editing a prompt string — `prompts.py`
renders the YAML.

## Tuning the panel (no engineer required)

`uv run panel-sim --live`, then talk to it as Ricky.

```
Ricky › So Wayne, what do you think about human oversight?
Ricky › /scores      # why each agent did or didn't get the floor
Ricky › /force dex   # override
Ricky › /kill        # emergency silence
```

Edit `personas/*.yaml` and restart. `/scores` is the feedback loop: if an agent
is talking too much, look at what it is scoring itself, then adjust
`interrupt_tendency` or the weights in `FloorConfig`.

Record a session with `--log recordings/name.jsonl` for replay.

## Floor hierarchy

Enforced in `floor.py`, in strict order:

1. **Human moderator** — absolute. Ricky speaking stops any agent within one
   audio buffer, with zero overlap.
2. **Explicitly invited agent** — "So Wayne, …" outranks any score.
3. **Strongest contextual case** — weighted signals, deterministic.
4. **Everyone else** — including saying nothing, which is a legitimate outcome.

Safety valves: per-persona turn-length caps, a consecutive-agent-turn limit that
hands back to the moderator, and an operator kill switch.

## Conventions

- Python 3.12+, `uv` workspace. `ruff` for lint, line length 100.
- Agent output is **always** passed through `sanitise()` before it would reach
  TTS. Never send raw model output to a PA.
- New floor behaviour lands with a test in `packages/panel_core/tests/`. Those
  tests are the scoping doc's acceptance criteria — keep them readable as such.
