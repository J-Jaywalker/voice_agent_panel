# AI Voice Panel — Boost Camp Oslo

Three AI agents on a live stage panel with a human moderator, 21 October 2026.

**Start with [FEASIBILITY.md](FEASIBILITY.md)** — architecture, risks, phase plan,
spike findings (§8.1), and the AV requirements the venue needs. Settled
decisions live in [`docs/adr/`](docs/adr/).

## Quick start

```bash
uv sync
make help          # lists every target below
make test-core     # floor logic only — the tight loop
make test          # everything
make sim           # text-mode panel — no audio, no credentials
make display       # video wall alone, synthetic panel — no mic, no keys
make panel         # the live pipeline: mic, agents, video wall, logged
```

`make panel` needs `SPEECHMATICS_API_KEY`, `ANTHROPIC_API_KEY` and
`ELEVENLABS_API_KEY`, plus `TYPESAFE_API_KEY` for the default addressing
classifier (see [ADR 0002](docs/adr/0002-address-detection-by-classifier.md)).
`make sim`, `make display`, `make test` and `make lint` need none.

Every target is a thin wrapper over a `uv run ...` command — read the
[`Makefile`](Makefile) for the exact line. Anything without a target (`barge-in`,
the benches, unusual flag combinations) is typed out in full below.

| Target | Runs |
|---|---|
| `make panel` | `uv run panel --display --log recordings/<timestamp>.jsonl` |
| `make panel-unlocked` | the same plus `--no-speaker-lock` (no enrolment) |
| `make enrol` | `uv run panel --re-enrol` |
| `make display` | `uv run panel-display --demo` |
| `make sim` | `uv run panel-sim` |
| `make aec-test` | `uv run aec-test` |
| `make feedback-test` / `make feedback-test-unlocked` | `uv run feedback-test` (`--no-speaker-lock`) |
| `make test` / `make test-core` | `uv run pytest` (`packages/panel_core`) |
| `make lint` | `uv run ruff check .` |

## What exists today

| Package | Purpose |
|---|---|
| `packages/panel_core` | **Pure** floor logic. No I/O, no awaits, no clock reads. |
| `packages/panel_sim` | Text-mode harness — tune personas and floor behaviour without audio. |
| `packages/panel_runtime` | I/O adapters and the live runtime. STT, TTS, mixer, speaker enrolment, address classifiers. |
| `packages/panel_display` | The 12m video wall, served on `http://localhost:8765`. No build step. |
| `personas/` | The cast, as structured data. Source of truth for prompts. |

Not built: operator console. Mid-turn steering was cut.

---

# Running it

Deliberately split: the panel has to be believable *and* responsive, and those
get tuned by different people against different feedback. Plus benches for the
measurements that have to be repeatable.

## 1. `panel` — the whole pipeline

```bash
make panel                       # the full show: wall + logging + speaker lock
make panel-unlocked              # same, mic ungated — skips Ricky's enrolment
make enrol                       # re-capture Ricky's voice, ignoring the stored one
```

Then open http://localhost:8765/ and fullscreen it (⌃⌘F, or Chrome with `--kiosk`).

Without a target, the raw command and its flags:

```bash
uv run panel                     # mic -> STT -> floor -> brains -> TTS -> speakers
uv run panel --no-tts            # same floor behaviour, printed rather than spoken
uv run panel --display           # + the video wall
uv run panel --address-backend regex   # regex | haiku | typesafe (default)
uv run panel --aec               # acoustic echo cancellation (see CLAUDE.md for setup)
uv run panel --log recordings/rehearsal.jsonl
uv run panel --list-devices      # then --input-device / --output-device
```

`uv run panel --help` is the full list.

Before the first run, Ricky's voice is enrolled (up to 30s of speech); the
result is stored in `.panel/speakers.json`, which is gitignored, machine-bound
and must be re-captured at the venue. Only enrolled-Ricky speech can duck or
stop an agent.

While it runs, two console keys: **`m`** toggles an emergency mic mute and
**`j`** is an emergency interrupt that stops whoever is speaking and hands the
floor to Ricky.

> ⚠️ **Headphones, or a pre-PA mic split, or `--aec`.** On open speakers the
> agents' own audio reaches the mic. Agent audio never enters the floor's STT
> path by construction, and speaker enrolment stops the bleed becoming words,
> but the endpointer on Ricky's socket still hears it. Run `make aec-test` and
> `make feedback-test` on the real rig to check.

**Ask the panel a question and it answers. Make a statement and it stays quiet**
— that is the whole floor rule, and it is deliberately one a moderator can hold
in his head on stage. Naming an agent narrows the invitation to them; a courtesy
tag like "is that okay?" invites nobody. See
[Moderator phrasings](#moderator-phrasings) for what the floor does and does not
recognise. Agents that want in but were not invited show as `✋ wants in`, for
Ricky to call on.

The event log replays through modified floor logic afterwards, because
`panel_core` reads no clocks of its own.

## 2. `panel-sim` — conversation and personas, no audio

```bash
make sim                   # offline stub brains, no credentials needed
uv run panel-sim --live    # real model behind the personas
uv run panel-sim --live --model claude-sonnet-5 --effort low
uv run panel-sim --log recordings/run1.jsonl   # record for replay
```

| Flag | Default | What it changes |
|---|---|---|
| `--live` | off | Real model behind the personas instead of `StubBrain`. Needs `ANTHROPIC_API_KEY`. |
| `--model` | `claude-sonnet-5` | Matches `BrainConfig`, so the sim rehearses the model that will be on stage. |
| `--effort` | `low` | `low` / `medium` / `high`. Ignored without `--live`. |
| `--personas` | `personas` | Point at a directory of alternative `*.yaml` — try a re-written cast without touching the committed one. |
| `--log` | none | Append a JSONL event log for replay through modified floor logic. |
| `--seed` | `0` | Stub-brain reply selection. Without `--live`, the same seed and the same typing gives the same panel every time. |

Flags compose, and the combinations worth knowing:

```bash
# Deterministic floor behaviour — no credentials, no model variance.
# The seed makes a misfire reproducible, so it can go in a bug report.
uv run panel-sim --seed 7 --log recordings/repro.jsonl

# Cheaper, faster rehearsal loop for persona-writing.
uv run panel-sim --live --model claude-sonnet-5

# Rehearse an alternative cast against the real model.
uv run panel-sim --live --personas personas-draft
```

`--model` and `--effort` are what keep the S0.7 bake-off re-runnable — the
default is a decision (see `BrainConfig` in
[`brains.py`](packages/panel_runtime/src/panel_runtime/brains.py), FEASIBILITY
§10 #3), not a constant. Note that Haiku rejects `effort` outright with a 400,
so `--model claude-haiku-4-5-20251001` ignores whatever `--effort` you pass.

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
agent talks too much, look at what it scores itself, then adjust the weights in
`FloorConfig` (`w_disagreement`, `w_urgency`, `recency_penalty`) or the
persona's `topics_of_authority`.

This needs no engineer — content and creative can drive it directly.

## 3. `barge-in` — the interrupt reflex in isolation

```bash
uv run barge-in                  # 128-frame blocks @ 16kHz
uv run barge-in --list-devices
uv run barge-in --block 256      # compare buffer sizes
uv run barge-in --input-device 3 --output-device 4
```

Needs `SPEECHMATICS_API_KEY` — the reflex is a real Agent STT session, not a
local detector.

> ⚠️ **Wear headphones.** On open speakers the agent's own audio reaches the mic
> and the endpointer fires on it — the feedback loop [FEASIBILITY.md §3.1](FEASIBILITY.md)
> warns about, and a live demonstration of why the venue needs a pre-PA mic split.

macOS prompts for microphone permission on first run. If the mic meter stays at
zero while you talk, permission was denied — grant it under
*System Settings → Privacy & Security → Microphone* and rerun.

An "agent" talks continuously. Speak, and watch it react:

| Say | Expect |
|---|---|
| "mm-hm" — a short burst | ducks to −15 dB, then **resumes**. Agent keeps the floor. |
| "sorry, hold on a second" | ducks, then **stops** once past 600ms. |
| laugh, or tap the desk | **no reaction.** The endpointer is speech-specific — this is the case against an energy gate. |

The display shows agent state, current gain, a live mic level, and two measured
latencies (last and worst):

| Number | What it is |
|---|---|
| onset → SpeechStarted | voice onset at the ADC to `HumanSpeechStarted` arriving off the socket. **The number the 6 Oct 2026 change left unverified** — Silero's old 66.8ms does not transfer. |
| onset → DAC | the whole budget, onset to ducked gain reaching an output buffer, against `BargeInConfig.hard_limit_ms` (a target, not a result). |

Onset is marked by an RMS threshold (`--onset-rms`), not a detector — it is the
stopwatch's reference mark, and timing the socket against another detector would
measure the gap between two detectors. **Run this on the venue machine**: the
network term is now the dominant unknown in the barge-in budget.

This runs the real `panel_core.FloorController`, not a mock — so if the stop
feels wrong by ear, that is a genuine finding about the thresholds, not a demo
artefact. The one knob left on this path is `FloorConfig.human_duck_ms` (90ms),
the ramp the full stop fades out over: short enough to read as immediate,
long enough not to click. The rest live in
`packages/panel_runtime/src/panel_runtime/config.py`.

This harness has a real STT session, so what it exercises is the live rule:
any segment attributed to Ricky while an agent is speaking stops that agent,
"mm-hm" and "no, that's wrong" alike. The `DuckSpeech` branch in its `apply()`
is vestigial — nothing on the live floor path emits one any more.

## 4. Latency benches — TTS, models, first audio, addressing

```bash
uv run python packages/panel_runtime/tests/bench_tts.py --voice <id>
uv run python packages/panel_runtime/tests/bench_brains.py
uv run python packages/panel_runtime/tests/bench_first_audio.py --voice <id>
uv run python packages/panel_runtime/tests/bench_address.py   # rerun after touching the address prompt
```

`bench_first_audio` is the one to trust: it measures request to *first audible
word*, which is what the audience experiences. Everything else is an
intermediate. Findings live in [FEASIBILITY.md §6/§8.1](FEASIBILITY.md).

---

## The two rules that keep this maintainable

**1. `panel_core` stays pure.** It is a reducer — `reduce(state, event) -> (state, commands)`.
No network, no `await`, no `time.time()`; timestamps arrive on events. This is
why the whole acceptance suite runs in under a second and cannot flake, and why
a recorded rehearsal replays identically through modified floor logic.

If you need I/O, it goes in `panel_runtime`, not here.

**2. Personas are data, not prose.** `topics_of_authority` is rendered into
every prompt by `prompts.py` and read by the sim's stub brain.
Editing a persona should never mean editing a prompt string — `prompts.py`
renders the YAML.

## Floor hierarchy

Enforced in `floor.py`, in strict order:

1. **Human moderator** — absolute. Ricky speaking ducks any agent within one
   audio buffer, then stops it or resumes it once classification lands.
2. **Explicitly invited agent** — "So Wayne, …" outranks any score, though Wayne
   may still hand off to a better-placed colleague.
3. **Strongest contextual case** — weighted signals, deterministic.
4. **Everyone else** — including saying nothing, which is a legitimate outcome.

Safety valves: a consecutive-agent-turn limit that hands back to the moderator,
and the `m` / `j` console keys on the live panel (`/kill` in `panel-sim`).
Turn length is a prompt instruction, not an enforced ceiling.

## Moderator phrasings

Who Ricky addressed is decided by the TypeSafe classifier by default
(`--address-backend`, [ADR 0002](docs/adr/0002-address-detection-by-classifier.md)),
falling back to a regex that follows the same rules — **grammatical role, not
position in the sentence**. Three roles, in strict precedence:

| Role | Example | Addressee |
|---|---|---|
| Subject of a request | "can **Melia** speak?", "over to **Melia**", "what about **Melia**" | Melia |
| Vocative | "**Melia**, can you continue?", "what do you think, **Wayne**?" | Melia / Wayne |
| Oblique | "sorry for interrupting **Dexter**" | **nobody, ever** |

So "Sorry Dexter, can Melia speak?" invites Melia — Dexter is a vocative on an
apology, Melia is the subject of the request, and the subject wins. Position-based
matching got this backwards and put the agent being *stood down* on the PA.

Two non-outcomes matter as much as the outcomes:

- **Courtesy tags invite nobody.** "Is that okay?", "does that work?", "right?"
  are question-shaped but aimed at the person being interrupted. A question mark
  alone no longer opens the floor.
- **Two agents in the same role are both invited.** "Melia and Wayne, what do
  you think?" opens the floor to those two and bars the third. Only the
  classifier can still report *ambiguous* — it cannot tell who — and that keeps
  the floor closed. A missed invitation costs one beat; a wrong one puts an
  agent on the PA over Ricky in front of 400 people.

The corpus is the spec.
[`packages/panel_core/tests/test_address.py`](packages/panel_core/tests/test_address.py)
holds every phrasing the floor is known to handle and what it is allowed to do
about each — vocatives, requests, apologies, courtesy tags, statements, ties.
**When a phrasing misfires in rehearsal, add a row.** That is the intended
maintenance loop: the spec grows by observation, not by guessing at grammar.

Two consequences for rehearsal:

- **Brief Ricky on the reliable form** — a short vocative and a request, "Melia,
  carry on." Compound apologies ("sorry Dexter, can Melia speak? sorry for
  interrupting, is that okay?") are the hardest input the floor sees and the
  phrasing he is least attached to. Stagecraft is a legitimate mitigation.
- **A misheard name is a floor failure, not a transcript blemish.** "Melia"
  transcribed as "Amelia" matched no agent and cost a turn on stage.
  `personas/*.yaml` carries `aliases` and `sounds_like` for exactly this — the
  alias is what the floor accepts, `sounds_like` is what STT is told to expect.

## Conventions

- Python 3.12+, `uv` workspace. `ruff` for lint, line length 100.
- Agent output is **always** passed through `sanitise()` before it would reach
  TTS. Never send raw model output to a PA.
- New floor behaviour lands with a test in `packages/panel_core/tests/`. Those
  tests are the scoping doc's acceptance criteria — keep them readable as such.
- A new moderator phrasing lands as a row in `tests/test_address.py`, not as a
  tweak to a regex. See [Moderator phrasings](#moderator-phrasings).
- Audio devices, sample rates and buffer sizes are **deployment config**. The
  panel runs on a different machine at the venue; nothing may be hardcoded and
  no latency measured on a dev box is a result, only a budget.
