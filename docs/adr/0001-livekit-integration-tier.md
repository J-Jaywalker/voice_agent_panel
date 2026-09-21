# ADR 0001 — LiveKit integration tier, and the STT client

**Status:** Accepted
**Date:** 3 September 2026
**Decides:** FEASIBILITY.md §10 #1 (audio transport), S0.2 fork in §8.1

---

## Decision

1. **LiveKit as a library, not a framework.** Take audio transport, VAD (Silero), TTS plugins. No `AgentSession` for the agent loop.
2. **STT direct against the provider's protocol** — one client per human mic, no framework wrapper. `speechmatics-voice` ruled out. (Originally `speechmatics-rt`, superseded 4 Sept by Agent STT — see addendum. Reasoning below unaffected: neither is `speechmatics-voice`, neither brings `AgentSession`.)
3. **Floor arbitration stays in `panel_core`**, which owns the output mixer.

Referred to as **Tier B**.

---

## Context

`livekit-plugins-speechmatics` → `speechmatics-voice` → `speechmatics-rt` are layers, not alternatives — the wrapper's value is `AgentSession` integration. Fork:

- **Tier A** — one `AgentSession` per agent, `turn_detection="manual"`, floor driven externally.
- **Tier B** — LiveKit for transport/VAD/TTS as libraries, direct STT, our own orchestrator owning the audio channel.

Cost was raised (3 `AgentSession`s each standing up an STT pipeline we don't want) but is not the reason — Speechmatics, STT spend isn't a constraint. Reason is architectural: `AgentSession` is built for strict user↔agent alternation. A panel is 4 parties contending for one channel with priority, pre-emption, deliberate overlap — a scheduler, not a dialogue loop. Under Tier A, 3 `AgentSession`s each own their own output, so nobody owns the channel. Under Tier B, one mixer, three faders.

---

## The four mechanics

| # | Mechanic | Tier A | Tier B |
|---|---|---|---|
| 1 | Barge-in latency | Fan `session.interrupt()` to 3 sessions, each disabling its own interruption logic first. 3 racing async calls. | One VAD → reducer (µs) → duck all streams. Latency floor is the audio buffer. |
| 2 | Simultaneous speech | Coordination bug waiting to happen — 3 loops can each decide to speak, suppressed after the fact. | Structurally impossible. Mixer opens one stream. |
| 3 | Speculation (§3.5) | `commit_user_turn()` always triggers a response ([livekit/agents#5026](https://github.com/livekit/agents/issues/5026)); speculating means generating outside the session via `session.say()` — agent loop goes unused. | Native. Candidates live in `PanelState.proposals`; only the winner reaches TTS. |
| 4 | Controlled overlap (§3.6) | `AgentSession` owns its output; no seam for sample-level gain automation across two streams. | We own the mixer. 380ms crossfade is gain automation. |

Mechanic 3 is where Tier A collapses: speculation is what takes the post-turn gap from ~1.5s to ~250ms, non-negotiable for the feel we want — the moment we speculate, `AgentSession` degrades to a TTS pipe, so we'd carry the abstraction for none of its benefit. Mechanic 2 is the strongest single argument: structural impossibility beats correct coordination, especially in front of 400 people.

---

## What this costs us

`AgentSession` ships tuning we walk away from: `min_duration=0.5`, `min_words`, `backchannel_boundary=(1.0, 1.0)`, `resume_false_interruption=True`, `false_interruption_timeout=2.0`.

**Backchannel suppression matters most** — "mm-hm" must not stop an agent, or the panel stutters. Raw VAD can't distinguish backchannel from barge-in.

**Mitigation:** these are parameters/heuristics, not architecture — a duration floor, word-count floor, boundary window are ~30 lines in the reducer, and unlike inside an async framework, they're then deterministically unit-testable in `panel_core`. Take LiveKit's heuristics, not its control flow.

---

## Consequences

- Undocumented-by-LiveKit path. Every piece is public API (standalone `STT` streaming, optional `room=` on `AgentSession.start()`, settable `session.input.audio`/`session.output.audio`) but we own the seams — accepted deliberately.
- `panel_core` stays transport-agnostic, unaffected (18 tests green throughout).
- We implement backchannel discrimination ourselves — Phase 0 deliverable, not a Phase 2 discovery.
- No `onnxruntime`/`transformers` dependency (comes with `speechmatics-voice`, unused regardless of STT client).
- LiveKit transport stays available if remote/browser participants (video wall, remote operator) are ever needed.

---

## Confidence

**~85%.** Residual 15% isn't "Tier A might be better," it's "Tier B has risks worth measuring": <150ms barge-in owning our own mixer, TTS cancellation, backchannel discrimination proving "mm-hm" doesn't stop an agent, two STT sessions identity-by-channel, end-of-turn latency/accuracy on the venue mic.

## Open question for rehearsal, not code

`resume_false_interruption` resumes an agent mid-sentence after a cough. On stage, stop-and-restart may read worse than simply yielding the floor. Decide in the room.

---

## Addendum — 4 Sept 2026: STT client swapped to Agent STT

STT client changed from `speechmatics-rt` (released SDK) to **Agent STT**, a preview WebSocket API, spoken directly via `websockets` (no released client yet). Doesn't reopen this ADR — Tier B, "own the mixer," "no `AgentSession`" unaffected. What changes: the end-of-turn silence-threshold tuning this ADR discusses no longer exists — Agent STT emits its own `EndOfTurn` natively, no local knob. Trades a tuning surface we could regression-test in `panel_core` for one we can't — watch in rehearsal, no code-side fix if it's too eager/slow on the venue rig. `speechmatics-rt` is no longer a dependency.

## Addendum — 21 Sept 2026: what "LiveKit as a library" actually came to mean

The decision above says "take audio transport, VAD (Silero), TTS plugins." Only
the VAD was ever taken. Current usage is exhaustively:

- `silero.VAD.load(...)` — `panel.py`
- `rtc.AudioFrame(...)` — a container to push PCM into Silero
- `lkvad.VADEventType` — reading the stream

Audio I/O is `sounddevice`/PortAudio, the mixer is ours, TTS is ElevenLabs over
raw `websockets`, STT is Agent STT over raw `websockets`. Deps are
`livekit-agents` and `livekit-plugins-silero`. **LiveKit is a Silero wrapper in
this codebase.** FEASIBILITY §3.3 already described it correctly; this ADR's
decision line did not. Recorded so nobody reads "transport" here and assumes a
room, a track or an SFU exists to build on.

Practical consequence: "should we adopt the framework after all?" is not a
config change, it is a rewrite of the audio path.

## Addendum — 21 Sept 2026: scope change, and a re-run of the four mechanics

Two changes: agent-to-agent barge-in is no longer wanted (agents pass turns,
they never cut each other off), and floor *opening* is now decided by a Haiku
classifier (ADR 0002). Re-running the table:

| # | Mechanic | Still load-bearing? |
|---|---|---|
| 1 | Barge-in latency | **Yes.** Human barge-in is unchanged; duck-first-classify-after lives in the reducer. |
| 2 | Simultaneous speech | **Yes, more so.** More agent-to-agent turn passing (FEASIBILITY §3.8) means one mixer owning one channel matters more, not less. |
| 3 | Speculation | **Yes, still decisive.** Racing generations, per-round epochs and `written_against_t` staleness (§3.5) are not expressible through `AgentSession`; [livekit/agents#5026](https://github.com/livekit/agents/issues/5026) is unchanged. |
| 4 | Controlled overlap | **No.** It existed only for agent-to-agent interrupts, and `panel_runtime` never honoured `interrupt_overlap_ms` anyway — `_execute` stops the mixer immediately. Both were deleted on 21 Sept 2026; mechanic 4 no longer argues for anything. |

One of four falls away, and it is the one that was never implemented. **Tier B
stands.** There is also no latency argument for reversing it: the gains in
`96cb1da` came from not cancelling generations, per-round epochs, and holding
`TurnYielded` for the verdict — all three require owning the loop.

ADR 0002 does not reopen this one either. The classifier is a model call in
`panel_runtime` whose answer enters the reducer as an event; "own the mixer" and
"no `AgentSession`" are untouched.

## References

- FEASIBILITY.md §3.1 (echo isolation), §3.2 (VAD vs STT), §3.3 (LiveKit), §3.5 (speculation), §3.6 (overlap), §3.7 (classifier), §3.8 (agent-to-agent), §8.1 (spike)
- ADR 0002 — address detection by classifier
- `livekit-plugins-speechmatics` 1.7.1 · `speechmatics-voice` 0.2.8
- Speechmatics Agent STT: preview API, `wss://preview.rt.speechmatics.com/v2/agent`
- Speechmatics Flow: deprecated, confirmed internally 3 Sept 2026
