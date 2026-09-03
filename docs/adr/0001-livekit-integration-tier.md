# ADR 0001 — LiveKit integration tier, and the STT client

**Status:** Accepted
**Date:** 3 September 2026
**Decides:** FEASIBILITY.md §10 decision 1 (audio transport), and the S0.2 fork in §8.1
**Does not supersede:** FEASIBILITY.md §3.4 / §8.1 — this ADR records the reasoning behind them

---

## Decision

1. **Use LiveKit as a library, not as a framework.** Take its audio transport, VAD
   (Silero) and TTS plugins. Do **not** use `AgentSession` for the agent loop.
2. **STT is `speechmatics-rt` directly** — one client per human mic.
   `speechmatics-voice` is ruled out.
3. **Floor arbitration stays in `panel_core`**, which owns the output mixer.

Referred to elsewhere as **Tier B**.

---

## Context

`livekit-plugins-speechmatics` → `speechmatics-voice` → `speechmatics-rt` are
layers, not alternatives. The wrapper's value is its `AgentSession` integration.
That put a fork in front of us:

- **Tier A** — one `AgentSession` per agent, `turn_detection="manual"`, floor
  driven externally. Documented and blessed by LiveKit.
- **Tier B** — LiveKit for transport/VAD/TTS as libraries, `speechmatics-rt`
  direct for STT, our own orchestrator owning the audio channel.

Cost was raised as a factor (three `AgentSession`s each standing up an STT
pipeline we don't want). **Cost is not the reason for this decision** — we are
Speechmatics and STT spend is not a constraint. The reason is architectural.

---

## The reframe that decides it

> `AgentSession` is built for a **dialogue**. We are not building a dialogue.
> We are building a **contended resource**.

An assistant is strict alternation: user → agent → user → agent. Exactly one
party is *eligible* to speak at any moment; the only question is *when* they
start. `AgentSession` is superbly tuned for that.

A panel is different in kind. Four parties want one audio channel, with
priority, pre-emption and deliberate overlap. That is not a dialogue loop — it
is closer to a **real-time scheduler**. The first rule of a scheduler is that it
must own the resource it schedules.

Under Tier A, three `AgentSession`s each own their own audio output, so nobody
owns the channel. Every floor decision becomes distributed coordination across
three autonomous loops that each hold their own opinion about when to speak and
when to stop. Under Tier B there is one mixer with three faders.

---

## The four mechanics of conversational flow

| # | Mechanic | Tier A | Tier B |
|---|---|---|---|
| 1 | Barge-in latency | Fan `session.interrupt()` out to 3 sessions, each with its own interruption logic that must first be disabled. Racing 3 async calls. | One VAD → reducer (µs) → duck all streams. One code path. Latency floor is the audio buffer. |
| 2 | Simultaneous speech | A coordination bug waiting for a slow moment — 3 loops that can each decide to speak, suppressed after the fact. | **Structurally impossible.** The mixer opens one stream. |
| 3 | Speculation (§3.6) | `commit_user_turn()` always triggers a response ([livekit/agents#5026](https://github.com/livekit/agents/issues/5026)). Speculating means generating text outside the session and using `session.say()` — at which point the agent loop is unused. | Native. Candidates live in `PanelState.proposals`; only the winner reaches TTS. |
| 4 | Controlled overlap (§3.7) | `AgentSession` owns its output; no seam for sample-level gain automation across two streams. | We own the mixer. 380 ms crossfade is gain automation. |

**Mechanic 3 is where Tier A quietly collapses.** Speculation is what takes the
post-turn gap from ~1.5 s to ~250 ms, and it is non-negotiable for the feel we
want. The moment we speculate, `AgentSession`'s central value evaporates and it
degrades to a TTS pipe — so we would be carrying the whole abstraction for none
of its benefit.

**Mechanic 2 is the strongest single argument.** Structural impossibility beats
correct coordination, always, and especially in front of 400 people.

---

## What this costs us

Not free. `AgentSession` ships real, hard-won tuning we are walking away from:

```
min_duration = 0.5
min_words
backchannel_boundary = (1.0, 1.0)
resume_false_interruption = True
false_interruption_timeout = 2.0
```

**Backchannel suppression is the one that matters.** Ricky saying "mm-hm" or
"right" mid-point must *not* stop an agent, or the panel stutters constantly and
reads as broken. FEASIBILITY.md §3.2 ("VAD owns stopping") is naive about this:
raw VAD cannot distinguish a backchannel from a barge-in.

**Mitigation:** these are parameters and heuristics, not architecture. A
duration floor, a word-count floor and a boundary window are ~30 lines in the
reducer — and once there they are **deterministically unit-testable in
`panel_core`**, which they can never be inside an async framework wrapped around
a live audio pipeline. Given that believability here is a *tuning* property
(§1), having the tuning surface under test is worth more than inheriting good
defaults we cannot regression-test.

> Take LiveKit's heuristics, not its control flow.

---

## Consequences

- We are on a path LiveKit does not document as recommended. Every piece is
  public API — standalone `STT` streaming, optional `room=` on
  `AgentSession.start()`, settable `session.input.audio` / `session.output.audio`
  — but **we own the seams**. Accepted deliberately: we were already writing a
  floor controller LiveKit has no primitive for.
- `panel_core` remains transport-agnostic and was unaffected by this decision
  (18 tests green throughout). Keep it that way.
- We must implement backchannel discrimination ourselves. Track as a Phase 0
  deliverable, not a Phase 2 discovery.
- We avoid the `speechmatics-voice` 0.2.8 → `speechmatics-rt` >=0.5.3 version
  skew (an open lower bound now resolving to 1.1.1, across a major version).
- No `onnxruntime` / `transformers` dependency, since SMART_TURN goes with the
  wrapper. End-of-turn is silence-threshold tuning on `speechmatics-rt`, so that
  tuning matters more than it would have.
- LiveKit transport remains available if remote/browser participants (video
  wall, remote operator) are ever needed.

---

## Confidence

**~85%.** The residual 15% is not "Tier A might be better" — it is "Tier B has
risks worth measuring". That is why the spike shifts from *comparing tiers* to
*validating Tier B*:

- < 150 ms barge-in while owning our own mixer — the core claim
- TTS cancellation — unchanged hard gate on provider choice
- **Backchannel discrimination** — prove "mm-hm" does not stop an agent
- Two `speechmatics-rt` sessions, identity-by-channel (§3.3)
- End-of-turn tuning without SMART_TURN

---

## Open question for rehearsal, not for code

`resume_false_interruption` resumes an agent mid-sentence after a cough. On a
stage, a voice stopping and restarting may read *worse* than simply yielding the
floor. Decide in the room, not in the spec.

---

## References

- FEASIBILITY.md §3.1 (echo isolation), §3.2 (VAD vs STT), §3.3 (channel-per-mic),
  §3.4 (LiveKit), §3.6 (speculation), §3.7 (overlap), §8.1 (spike)
- `livekit-plugins-speechmatics` 1.7.1 · `speechmatics-voice` 0.2.8 ·
  `speechmatics-rt` 1.1.1
- Speechmatics Flow: deprecated, confirmed internally 3 Sept 2026
