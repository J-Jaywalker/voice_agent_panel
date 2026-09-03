# AI Voice Panel — Boost Camp Oslo

Three AI agents on a live stage panel with a human moderator, 21 October 2026.

**Start with [FEASIBILITY.md](FEASIBILITY.md)** — architecture, risks, phase plan
and the AV requirements the venue needs. Settled decisions live in
[`docs/adr/`](docs/adr/); Phase 0 spike findings in
[`docs/spike-phase0.md`](docs/spike-phase0.md).

## Quick start

```bash
uv sync
uv run pytest              # floor, mixer and chunker criteria, ~0.3s
uv run panel-sim           # text-mode panel — no audio, no credentials
uv run panel               # the live pipeline — mic in, agents out
uv run barge-in            # interrupt reflex only — headphones required
```

`uv run panel` needs `SPEECHMATICS_API_KEY`, `ANTHROPIC_API_KEY` and
`ELEVENLABS_API_KEY`. Everything else runs with none.

## What exists today

| Package | Purpose |
|---|---|
| `packages/panel_core` | **Pure** floor logic. No I/O, no awaits, no clock reads. |
| `packages/panel_sim` | Text-mode harness — tune personas and floor behaviour without audio. |
| `packages/panel_runtime` | I/O adapters and the live runtime. STT, TTS, VAD, mixer. |
| `personas/` | The cast, as structured data. Source of truth for prompts. |

Not built yet: operator console, video wall, mid-turn steering.

---

# Running it

Three ways in, deliberately split: the panel has to be believable *and*
responsive, and those get tuned by different people against different feedback.
Plus benches for the measurements that have to be repeatable.

## 0. `panel` — the whole pipeline

```bash
uv run panel                     # mic -> STT -> floor -> brains -> TTS -> speakers
uv run panel --no-tts            # same floor behaviour, printed rather than spoken
uv run panel --log recordings/rehearsal.jsonl
uv run panel --list-devices
```

> ⚠️ **Headphones, or a pre-PA mic split.** On open speakers the agents' own
> audio reaches the mic. Agent audio never enters the STT path by construction,
> but the VAD will still hear it and duck.

**Ask a question and the panel answers. Make a statement and it stays quiet** —
that is the whole floor rule, and it is deliberately one a moderator can hold in
his head on stage. Naming an agent narrows the invitation to them. Agents that
want in but were not invited show as `✋ wants in`, for Ricky to call on.

The event log replays through modified floor logic afterwards, because
`panel_core` reads no clocks of its own.

## 2. `panel-sim` — conversation and personas, no audio

```bash
uv run panel-sim           # offline stub brains, no credentials needed
uv run panel-sim --live    # real model behind the personas
uv run panel-sim --live --model claude-sonnet-5 --effort low
uv run panel-sim --log recordings/run1.jsonl   # record for replay
```

Type as Ricky and watch the floor controller arbitrate:

```
Ricky › So Wayne, what do you think about human oversight?
Ricky › /scores      # why each agent did or didn't get the floor
Ricky › /force dex   # override — give an agent the floor now
Ricky › /mute wayne  # toggle
Ricky › /kill        # emergency silence  (/unkill to release)
Ricky › /state       # dump floor state
```

Edit `personas/*.yaml` and restart. **`/scores` is the feedback loop:** if an
agent talks too much, look at what it scores itself, then adjust
`interrupt_tendency` in the persona or the weights in `FloorConfig`.

This needs no engineer — content and creative can drive it directly.

## 3. `barge-in` — the interrupt reflex in isolation

```bash
uv run barge-in                  # 128-frame blocks @ 16kHz
uv run barge-in --list-devices
uv run barge-in --block 256      # compare buffer sizes
uv run barge-in --input-device 3 --output-device 4
```

> ⚠️ **Wear headphones.** On open speakers the agent's own audio reaches the mic
> and the VAD fires on it — the feedback loop [FEASIBILITY.md §3.1](FEASIBILITY.md)
> warns about, and a live demonstration of why the venue needs a pre-PA mic split.

macOS prompts for microphone permission on first run. If the VAD meter stays at
zero while you talk, permission was denied — grant it under
*System Settings → Privacy & Security → Microphone* and rerun.

An "agent" talks continuously. Speak, and watch it react:

| Say | Expect |
|---|---|
| "mm-hm" — a short burst | ducks to −15 dB, then **resumes**. Agent keeps the floor. |
| "sorry, hold on a second" | ducks, then **stops** once past 600ms. |
| laugh, or tap the desk | **no reaction.** Silero is speech-specific — this is the case against an energy gate. |

The display shows agent state, current gain, a live VAD probability meter, and
measured **ADC→DAC latency** (last and worst) against the 150ms hard limit.

This runs the real `panel_core.FloorController`, not a mock — so if the duck or
resume feels wrong by ear, that is a genuine finding about the thresholds, not a
demo artefact. The numbers most in need of a human verdict:

| Knob | Default | Question |
|---|---|---|
| `backchannel_duck_db` | −15 dB | Enough to feel responsive without being jarring? |
| `backchannel_max_duration_s` | 0.6s | Right boundary between "mm-hm" and a real interrupt? |
| `duck_ramp_ms` / `resume_ramp_ms` | 120 / 220 | Smooth, or audible as a pump? |

All live in `packages/panel_runtime/src/panel_runtime/config.py` and
`FloorConfig`.

**No STT in this harness**, so classification is duration-only — it cannot
distinguish "mm-hm" from "no, that's wrong" at equal length. Use `uv run panel`
for the content-based path.

## 4. `bench_vad_latency` — repeatable VAD measurement

```bash
uv run python packages/panel_runtime/tests/bench_vad_latency.py
```

Offline, deterministic, no mic. Five committed speech fixtures covering
different onset types (fricative, plosive, vowel, aspirate, nasal), swept across
probability thresholds. Worst case is what the design must survive.

**Run this on the venue machine** — the VAD term is the dominant cost in the
barge-in budget and it must be re-measured on the deployment rig.

## 5. Latency benches — TTS, models, and first audio

```bash
uv run python packages/panel_runtime/tests/bench_tts.py --voice <id>
uv run python packages/panel_runtime/tests/bench_brains.py
uv run python packages/panel_runtime/tests/bench_first_audio.py --voice <id>
```

`bench_first_audio` is the one to trust: it measures request to *first audible
word*, which is what the audience experiences. Everything else is an
intermediate. Findings live in [docs/spike-phase0.md](docs/spike-phase0.md).

---

## The two rules that keep this maintainable

**1. `panel_core` stays pure.** It is a reducer — `reduce(state, event) -> (state, commands)`.
No network, no `await`, no `time.time()`; timestamps arrive on events. This is
why the whole acceptance suite runs in under a second and cannot flake, and why
a recorded rehearsal replays identically through modified floor logic.

If you need I/O, it goes in `panel_runtime`, not here.

**2. Personas are data, not prose.** The floor controller reads
`interrupt_tendency`, `max_turn_seconds` and `topics_of_authority` at runtime.
Editing a persona should never mean editing a prompt string — `prompts.py`
renders the YAML.

## Floor hierarchy

Enforced in `floor.py`, in strict order:

1. **Human moderator** — absolute. Ricky speaking ducks any agent within one
   audio buffer, then stops it or resumes it once classification lands.
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
- Audio devices, sample rates and buffer sizes are **deployment config**. The
  panel runs on a different machine at the venue; nothing may be hardcoded and
  no latency measured on a dev box is a result, only a budget.
