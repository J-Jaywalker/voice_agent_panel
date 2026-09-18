# AI Voice Panel — Feasibility & Status

**Source:** [Boost Camp Oslo — AI Voice Panel Scoping](https://speechmatics.atlassian.net/wiki/spaces/~712020b3713ebb71814ed991afdd6d43fc1ac6/pages/6163628075/Boost+Camp+Oslo+-+AI+Voice+Panel+Scoping)
**Revised:** 7 Sept 2026 · **Event:** 21 Oct 2026
**Status:** `panel_core`, `panel_sim`, `panel_runtime` (STT, TTS, VAD, mixer, chunking, live `panel` pipeline) built and tested. Operator console and video wall not started.
**Decisions:** `docs/adr/`.

---

## 1. Verdict

Feasible in the window. Engineering ahead of risk. Risk is now non-code:

| Risk | Status | Note |
|---|---|---|
| Venue AV | Confirmed by the venue 16 Sept. | §9. Closed — sanity-check on load-in, not a tracked risk. |
| Rehearsal infra | Not started. Operator console + video wall = Phase 1, no code. | §7 failure handling has nothing to rehearse with. |
| Content | Structural fix built (§4.1–4.2). Beat sheet and approved knowledge signed off 16 Sept (`docs/beat-sheet.md`), late against the 14 Sept due date. **Restructured 17 Sept** — barriers beat folded into Beat 2, STT became a capability beat, jailbreaking became its own beat ("Revenge of the Humans"). Three new spines (D4, W4, M4) are live in `personas/*.yaml` and **pending re-sign-off** — §10 #5 reopened. Two `GUARDRAILS` rules added with it: no naming providers or models, and no repeatable jailbreak method. | Legal/commercial exposure closed for signed-off material; the new spines are the open bit. Script not written. |

No owner actively moving on operator console or video wall. §8 can't reach Phase 4 without the first, or mean anything on stage without the second.

---

## 3. Architecture

### 3.1 Echo isolation — #1 constraint

Failure chain: agent TTS → PA → venue mic → Speechmatics → orchestrator → agents react to own output → runaway loop.

- **Agent speech never enters STT** — enforced structurally (`panel_runtime.stt`, `panel_runtime.panel`, CLAUDE.md invariant). Done.
- **Pre-PA mic split** — venue fact, not code. Open, §9.
- **PA bleed into Ricky's mic** — expect it. Close mic + calibrated VAD gate is the mitigation. `uv run barge-in` on open speakers demonstrates the failure live.

### 3.2 Barge-in — VAD, not transcription

Silero VAD for barge-in detection, Agent STT `EndOfTurn` for finalization to LLM.

### 3.3 LiveKit as library — ADR 0001

| Layer | Owner |
|---|---|
| VAD (Silero) | LiveKit, plugin only |
| Audio I/O, mixer | `sounddevice`/PortAudio, direct |
| TTS + cancellation | ElevenLabs `multi-stream-input`, hand-rolled over raw `websockets` |
| STT, one connection per mic | Agent STT (Speechmatics preview API), raw `websockets` |
| Floor, interrupts, overlap, turn caps | `panel_core` |

No `AgentSession`, no cloud SFU, no `speechmatics-voice`. TTS ended up hand-rolled for the same reason STT did: cancellation latency must be our code's property, not a plugin's.

STT client has moved again since the ADR: Agent STT has no multi-channel mode — each mic is its own connection.

### 3.4 Floor stays deterministic

`floor.py` is pure `reduce(state, event) -> (state, commands)`. No network, no clock reads. Whole acceptance suite (floor, mixer, chunker) runs <1s, can't flake.

### 3.5 Speculative generation

Budgeted pre-spike at 0.9–1.8s to first audio. Measured (S0.7) worse — hence streaming:

| Path | Signals ready | 1st sentence | First audio |
|---|---|---|---|
| One-shot | — | — | 4699ms |
| Streamed | 2475ms | 3035ms | 3275ms |

Cost is generation (~200 tokens @ ~40 tok/s), not reasoning. Streaming removes 1424ms/turn. Built: `panel_runtime.brains.StreamingClaudeBrain`, `panel_runtime.chunking.SentenceChunker`.

Each agent proposes during the human's turn, not just at EOU — generation is usually underway or done by the time the floor needs an answer. Three qualifications, all from the same live failure (an agent answering "So, Wayne, uh" with "Take your time, Ricky — we'll be here", aired because a named invitation bypasses the score floor):

- A partial under `speculation_min_words` is not worth asking about. Speechmatics segments on pauses, so every turn opens with a fragment.
- One stream per agent at a time, torn down and restarted when a *final* replaces the text it was generated against (`PanelState.speculation_epoch`). Per-final, not per-partial: cancelling every 0.8s is what used to leave `state.proposals` empty at the first arbitration.
- A line written more than `named_proposal_lookback_s` before the question cannot answer it. A silent arbitration re-requests against the completed turn, so this costs a beat rather than the answer.

Not re-measured: Sonnet 5 at `effort: "low"` (§10 #3) wasn't a direct S0.7 row — Sonnet alone at `low` was 4110-4129ms one-shot. Streaming should cut it proportionally.

### 3.6 Controlled overlap on interrupts

Duck-first-classify-after (S0.2), not the originally proposed flat 300–500ms overlap + hard duck.

- Ricky interrupts agent: instant duck, zero overlap (`human_duck_ms = 90`).
- Agent interrupts agent: **off by default** (`FloorConfig.allow_agent_interrupts = False`,
  18 Sept). Re-enable per run with `panel --agent-interrupts` /
  `panel-sim --agent-interrupts`. The designed 380ms overlap
  (`interrupt_overlap_ms`) is asserted on the command and rendered by
  `panel_sim`, but `panel_runtime` has never honoured it — its `StopSpeech`
  handler stops the mixer immediately, so on stage this was a hard cut, not an
  overlap. Fix that before switching it back on for the show.

---

## 4. Content

§4.1–4.3 done/structural. §4.4–4.5 open, more urgent as 21 Oct nears.

**4.1 Fictional employers** — done. "Irrational Industries", "Servv.AI", "The Kestrel Foundation".

**4.2 Extended persona schema** — built, grew. `verbosity` — target length by prompt (`GUARDRAILS`), no orchestrator-enforced ceiling; agents may hold the floor for a minute or more, moderator handles the rest live. `relationships` is what makes disagreement read as colleagues, not models. See `personas/*.yaml`.

**4.3 Pre-render the opening** — not done. Self-intro is fixed content; pre-rendering removes it from failure surface, gives graceful cold-start. Currently built as floor mechanic `InvitationSource.INTRODUCTION` in `floor.py`.

---

## 5. Repo structure

Matches what's built: `packages/panel_core`, `panel_runtime`, `panel_sim` (paths in CLAUDE.md). Python for core/runtime, TypeScript for unbuilt operator console/video wall.

**`panel_sim`** — text harness: type as Ricky, agents respond as text, floor decisions/scores print live. Where personas get written, thresholds tuned, without an engineer. Can't rehearse the introduction round.

**Event log** — built. `panel` and `panel-sim` append every event to JSONL (`--log`), replayable.

---

## 6. Model and provider notes

| Layer | Decision |
|---|---|
| Agent responses | Claude Sonnet 5, `effort: "low"` — reverted 11 Sept from the 7 Sept Opus 5 decision once mid-turn directives were cut (see below): Opus's only justification was mid-conversation `role: "system"` messages, which Sonnet 5 doesn't support and nothing now needs. S0.7 measured sonnet-5 at 4110-4129ms total vs. opus-5 at 6207-6218ms. Default in `BrainConfig`/`panel-sim`. |
| Floor signals | `PROPOSAL_SCHEMA` puts six signal fields before `utterance`; S0.7 confirmed signals complete while utterance still streaming. |
| Prompt caching | System prompt (persona + guardrails) cached `ephemeral`; only per-turn partial uncached. Makes asking full cast on every partial affordable. |
| Mid-turn directives | **Cut, 11 Sept.** Was going to need Opus for mid-conversation `role: "system"` messages, but `InjectDirective`'s regenerate-and-splice mechanics were never built and added little over the alternative: turn length is a prompt instruction (`GUARDRAILS`) with no orchestrator-enforced ceiling, and the moderator handles the rest live. |
| TTS | Streaming, cancellation, sub-300ms TTFB required. **ElevenLabs**, over `multi-stream-input`. Both gates passed (cancellation: 0.089ms to flag, 0 chunks after; TTFB: 198ms median/284ms worst, pooled) against a placeholder stock voice. Re-measure after casting. |
| Output sanitisation | Built, mandatory (`sanitise()`). `stable_prefix()` withholds anything still-open in a streaming utterance — sanitising a half-arrived construct can produce non-prefix text. `panel_core.prompts`. |

---

## 7. Failure handling — mostly unimplemented, unrehearsed

| Failure | Response |
|---|---|
| Agent LLM timeout (>2s) | Drop turn silently. Floor returns to moderator. Never dead-air. |
| Agent LLM error | Pre-rendered stall line ("Sorry Ricky — say again?"). Buys 3s. |
| TTS failure | Fail to secondary provider; else drop turn. |
| STT disconnect | Auto-reconnect w/ backoff (built). Red banner (not built, no console). Manual cue mode fallback. |
| Network loss | Wired venue ethernet primary, 5G failover pre-tested. |
| Unacceptable agent output | Operator kill switch mutes all agent output <50ms. Physical button/key. |
| Total system failure | Ricky runs rehearsed solo segment — must actually be rehearsed. |

**Operator console requirements** (needed from Phase 1 to rehearse — none built): kill-all, mute individual agent, force agent to speak now, advance beat, live state + scores, latency/health indicators.

---

## 8. Phase plan

Calendar: 1 Sept → 21 Oct (~7 weeks). One week in, 4 weeks to freeze.

| Phase | Status |
|---|---|
| 0 — Spike | Done. 7 deliverables (below). Closed ADR 0001 fork, forced sentence-streaming (§3.5) and duck-first backchannel (§3.6). |
| 1 — Core panel | Built: 3 agents, personas, floor control, human priority, invitations, address/role parsing, TTS, event logging, `panel_sim`. Not built: video wall, operator console. |
| 2 — Organic interaction | Built: interrupt thresholds, A2A addressing/handoff, expertise weighting, speculative generation, overlap/ducking, backchannel discrimination. |
| 3 — Content & guardrails | Beat sheet and approved knowledge signed off 16 Sept (`docs/beat-sheet.md`), late against the 14 Sept target. Spines wired into `personas/*.yaml`/`prompts.py`. Reopened 17 Sept by the restructure: twelve spines now, three of them unsigned, and Beat 4 is new material that has never been rehearsed. |
| 4 — Hardening & rehearsal | Not started. 2 protected weeks after 7 Oct freeze. Depends on operator console (§7) and AV split (§9). |

### 8.1 Spike results

| # | Deliverable | Result |
|---|---|---|
| S0.1 | Dependency baseline | Agent STT reachable over raw `websockets`; `speechmatics-rt` dropped. |
| S0.2 | Backchannel discrimination | Built: duck-first-classify-after, 6 tests. Thresholds are guesses pending real speech. |
| S0.3 | Barge-in <150ms | ~86ms projected, dev machine. Not measured on venue rig. |
| S0.4 | TTS cancellation | Passed, placeholder voice. |
| S0.5 | Two STT channels | Built as 2 independent connections, not multi-channel. Two simultaneous live mics unverified. |
| S0.6 | Native end-of-turn | No local tuning knob — Agent STT's own `EndOfTurn`. Venue task, not code task. |
| S0.7 | Model bake-off | Forced sentence-streaming design. Model landed on Opus 5 against this bake-off's own numbers 7 Sept, then reverted to Sonnet 5 11 Sept once the reason for that override (mid-turn directives) was cut (§10 #3). |

**Feature freeze: 7 Oct.** After that: tuning and rehearsal only.

---

## 9. Venue AV requirements — confirmed

Sent to Boost week 1; confirmed by the venue 16 Sept. All four met:

1. Pre-PA mic split — Ricky's mic only, no PA/agent audio in the mix. Hardest to retrofit, was the one that had to lock first.
2. Ricky on close mic (headset/lav), not lectern/ambient.
3. Line input for agent audio, own fader, own kill.
4. Connector types, levels (mic vs line), can we bring our own interface.

Closed. Still worth a sanity check on load-in — §3.1 (echo isolation) and §7
(operator kill switch routing) both depend on this — but no longer tracked as
an open risk.

---

## 10. Open decisions

| # | Decision | Owner | Needed by |
|---|---|---|---|
| 1 | ~~Audio transport/integration tier — resolved 3 Sept, ADR 0001~~ | Eng | done |
| 2 | ~~TTS — resolved 7 Sept: ElevenLabs~~ (S0.4 passed vs placeholder voice — redo once cast) | Eng | re-rehearse |
| 3 | ~~Agent model/effort — resolved 11 Sept: Sonnet 5, `effort: "low"`~~. Opus 5 was chosen 7 Sept against S0.7's own numbers (Opus slowest: 6207-6218ms vs Haiku 2582ms) purely because it supports mid-conversation `role: "system"` messages, the only way `InjectDirective`/operator "wrap up" could work. Mid-turn directives were cut 11 Sept (prompt instruction + hard turn-limit stop instead), removing that justification; reverted to Sonnet 5 (4110-4129ms) for the latency win. `low` mitigates latency; untested combination. | Eng | re-measure before hardening |
| 4 | ~~Personas/names/employers — done~~: Dexter, Wayne, Melia, fictional employers, schema built (§4.2) | Content | done |
| 5 | **Reopened 17 Sept.** Beat sheet + approved knowledge were signed off 16 Sept (late against 14 Sept) and wired per option 1 of `docs/beat-sheet.md` "Wiring". The 17 Sept restructure adds three spines — D4 (Dexter was jailbroken by thirty-one polite turns), W4 (Wayne followed instructions hidden in a document), M4 (Melia accidentally produced a proof of a Millennium Prize problem and filed it unread). All three are first-person accounts of an agent being compromised, which is new territory for this panel; D4 and M4 need a second pair of eyes before the show. | Content + Ricky | before 7 Oct freeze |
| 6 | ~~One moderator or two — resolved 7 Sept: one~~ | Creative | done |
| 7 | Video wall visual design | Design | end of week 3 — no code |
| 8 | Who operates console on the night | — | before rehearsal — console doesn't exist |
