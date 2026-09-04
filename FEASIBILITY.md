# AI Voice Panel — Feasibility Review & Implementation Plan

**Source:** [Boost Camp Oslo — AI Voice Panel Scoping](https://speechmatics.atlassian.net/wiki/spaces/~712020b3713ebb71814ed991afdd6d43fc1ac6/pages/6163628075/Boost+Camp+Oslo+-+AI+Voice+Panel+Scoping)
**Reviewed:** 1 September 2026 · **Revised:** 3 September 2026 · **Event:** 21 October 2026
**Status:** `panel_core` + `panel_sim` built. No audio layer yet.
**Decisions:** settled architecture decisions and their reasoning live in `docs/adr/`.

---

## 1. Verdict

**The concept is feasible in the window. The software is the least risky part of it.**

Nothing in the MVP scope requires a research breakthrough. Three LLM agents sharing a transcript, scoring their desire to speak, and being arbitrated by a deterministic floor controller is a well-understood pattern. Streaming STT → LLM → streaming TTS at conversational latency is a solved problem. The 2–4 week engineering estimate is realistic **for one dedicated engineer**, provided content authoring runs in parallel with a different owner.

The risk is concentrated in three places, none of which are code:

| Risk | Why it's the real risk | Mitigation |
|---|---|---|
| **Venue AV** | You cannot fix a bad mic feed in software on the day. Acoustic feedback between agent output and agent input is the single failure mode that ends the demo. | Resolve the AV spec in **week 1**, not week 5. Section 3.1. |
| **Rehearsal time** | The system's believability is a *tuning* property (thresholds, turn lengths, personas), not a code property. Tuning needs runtime. | Hard feature freeze **7 October**. Protect two weeks for rehearsal. |
| **Content risk** | 400 prospects and customers. An agent inventing a Speechmatics capability claim is a commercial problem, not a bug. | Structural fix in §4.4 — take the product claims out of the agents' mouths entirely. |

**Recommendation: proceed**, with the changes in §3 and §4 adopted before any code is written. Several of them are cheap now and expensive in October.

---

## 2. What the doc gets right

Worth stating explicitly so it doesn't get re-litigated:

- **Orchestrator-owns-the-floor is the correct architecture.** Agents proposing and a central authority disposing is the only design that stays controllable. Do not let agents drive audio directly.
- **Personas as structured data** is right, and is what makes the floor logic tractable.
- **Event-sourced conversation state** is right, and pays off far beyond what the doc claims — see §5.3.
- **Human absolute priority** is right and should be enforced structurally, not by prompting.
- **"Avoid unnecessary distributed infrastructure"** is right — and §3.4 shows how to honour it *while* using LiveKit, by taking its audio stack as a library rather than adopting its dialogue framework.
- **The core creative insight is correct**: the audience is buying *interaction*, not *answers*. Latency on a single answer matters far less than whether the turn-taking reads as human. Optimise for the latter.

---

## 3. Architectural changes I'd make before starting

### 3.1 Echo isolation is the #1 architectural constraint, not an acceptance criterion

The doc lists "AI input is isolated from AI output" under Live Event acceptance criteria. It belongs at the top of the architecture section, because if you get it wrong the system doesn't degrade — it detonates.

**The failure:** Agent TTS → PA → venue mic → Speechmatics → orchestrator → agents react to their own output → runaway loop. In a room with a PA and a live mic this is not a hypothetical.

**Three defences, all required:**

1. **Agent speech never enters the STT path.** The orchestrator already knows verbatim what each agent said — it generated it. Agent turns are injected into conversation state as text at the moment TTS starts. There is no reason to ever transcribe agent audio, and doing so is the loop.
2. **The mic feed to the AI system must be a pre-PA split.** A direct out, mic splitter, or matrix/aux send that excludes the PA mix (mix-minus). Not a room mic, not a board output that includes the agents. **This is the single most important thing to ask the venue AV team for.**
3. **Expect PA bleed into Ricky's mic anyway.** Use a close-mic (headset or lav), and calibrate a VAD energy gate during soundcheck. Do not attempt to solve this with content-level logic.

### 3.2 Barge-in must be driven by VAD, not by transcription

The doc's audio flow puts Speechmatics between the mic and the orchestrator, which implicitly puts a transcription round-trip in the interrupt path. That's ~300–600ms of an agent still talking after Ricky has started — which reads on stage as the agent ignoring him. That's the exact opposite of the intended effect.

**Split the two paths:**

```
Ricky's mic ─┬─► VAD  ──────────► orchestrator   (~30-50ms)  → STOP AGENT AUDIO
             └─► Speechmatics ──► orchestrator   (~300ms+)   → conversation content
```

VAD owns *stopping*. STT owns *understanding*. The agent must duck within ~100ms of Ricky's first syllable. Speechmatics is still doing the load-bearing work — it's just not in the reflex arc.

### 3.3 Use one STT channel per human mic — don't rely on diarization

The doc lists "Speaker identification / diarization" as a layer. For 1–2 humans on stage this is solving a problem you don't have to have.

Give each human mic its own channel and its own Speechmatics session. **Identity becomes a hardware fact rather than an inference** — dramatically more robust in a room with applause, laughter, and PA bleed. If you later add audience Q&A, a roving mic is simply one more channel.

Keep diarization in reserve only if the venue forces a shared/summed feed on you.

### 3.4 LiveKit as a library, not a framework

> **Revisions.** *1 Sept* — an earlier draft argued against LiveKit on latency
> grounds, conflating the agent *framework* with the cloud *SFU*; the objection
> only ever applied to the second. *3 Sept* — narrowed further: we take LiveKit's
> audio stack but not its dialogue abstraction. **Full reasoning: `docs/adr/0001-livekit-integration-tier.md`.**

**Decided.**

| Layer | Owner |
|---|---|
| Audio transport, device I/O, VAD (Silero) | LiveKit, used as a library |
| TTS + cancellation | LiveKit TTS plugin |
| STT — one client per **human** mic | **Agent STT** (Speechmatics preview API), raw WebSocket protocol |
| **Floor arbitration, interrupts, overlap, turn caps** | **`panel_core` (ours)** |

No `AgentSession` for the agent loop. No `speechmatics-voice`. No cloud SFU —
`AgentSession.start()` takes `room` as optional and `session.input.audio` /
`session.output.audio` are settable, so the plugin pipeline runs against a local
audio interface with nothing in the critical path.

**Why not `AgentSession`.** It is built for a **dialogue** — strict alternation,
one eligible speaker, the only question being *when* they start. A panel is a
**contended resource**: four parties, one channel, with priority, pre-emption and
deliberate overlap. That is closer to a real-time scheduler, and a scheduler must
own the resource it schedules. Three `AgentSession`s each own their own audio
output, so nobody owns the channel and every floor decision becomes distributed
coordination across three autonomous loops.

Two consequences decide it:

- **Simultaneous speech becomes structurally impossible** when one mixer owns the
  channel, rather than a coordination bug waiting for a slow moment.
- **Speculation (§3.6) collapses `AgentSession`'s value anyway.**
  `commit_user_turn()` always triggers a response, so speculating means
  generating text outside the session and using `session.say()` for the winner —
  at which point the agent loop is unused and we are carrying the abstraction for
  none of its benefit. Speculation is non-negotiable for the feel we want.

**What this costs us — and it is not free.** We forgo `AgentSession`'s hard-won
interruption tuning (`min_duration`, `min_words`, `backchannel_boundary`,
`resume_false_interruption`). **Backchannel suppression is the one that matters**:
Ricky saying "mm-hm" mid-point must not stop an agent, or the panel stutters
constantly. §3.2 below is naive about this — raw VAD cannot tell a backchannel
from a barge-in. Mitigation: these are parameters, not architecture. A duration
floor, a word-count floor and a boundary window are ~30 lines in the reducer, and
there they become deterministically unit-testable — which they can never be inside
an async framework wrapped around a live audio pipeline. **Take LiveKit's
heuristics, not its control flow.** Tracked as a Phase 0 deliverable (§8.1 S0.2).

**Package landscape.** `livekit-plugins-speechmatics` → `speechmatics-voice` →
`speechmatics-rt` were the layered alternatives considered; none of them is what
we run. STT now talks Speechmatics' **Agent STT** preview API directly over a raw
`websockets` connection — no Speechmatics SDK in the dependency tree at all, since
no released client speaks this protocol yet. `speechmatics-voice`'s value was its
`AgentSession` integration, which we are not using regardless. Keeps
`onnxruntime` + `transformers` out of the tree.

**Speechmatics Flow is deprecated** — confirmed internally, 3 Sept 2026.

Speechmatics and the TTS provider remain cloud WebSockets regardless, so wired
venue ethernet with a 5G failover is still required (§9).

### 3.5 The orchestrator must be deterministic — no LLM in the floor path

Floor arbitration needs to resolve in <50ms. An LLM call is 400ms+ and non-deterministic, which also makes rehearsal meaningless (you can't tune what you can't reproduce).

**LLM does:** generate the utterance, and produce the *signals* (relevance, disagreement, urgency).
**Plain code does:** arbitration, priority, interrupt thresholds, hysteresis, cooldowns.

The doc's instinct that the maths "does not need to be mathematically sophisticated" is correct. What it *does* need to be is pure, synchronous, and replayable.

### 3.6 Speculative generation is what makes it feel live

A naive pipeline is: Ricky stops → EOU detected → 3 agents think → arbitrate → TTS → audio. Realistic budget:

| Stage | Realistic |
|---|---|
| End-of-turn detection | 300–500ms |
| LLM time-to-first-token | 400–900ms |
| TTS time-to-first-byte | 100–300ms |
| Network/jitter | 50–100ms |
| **Total to first audio** | **~0.9–1.8s** |

**End-of-turn detection is native to Agent STT** — its `EndOfTurn` message
replaces the silence-threshold tuning we previously owned on `speechmatics-rt`.
There is no local knob for this figure any more; it is earned or lost by the
provider's turn detector, so it needs measuring against the venue mic rather than
tuned in code — see §8.1 S0.6.

That's survivable for a panel — real panellists pause too — but it's noticeably slower than human turn-taking, and it stacks up across a 20-minute run.

**The fix:** speculate during the human's turn. On each partial transcript update (debounced to ~800ms), each agent asynchronously produces a candidate response *and* its floor signals. Keep the latest. When EOU fires, the orchestrator already holds three scored candidates — it arbitrates instantly and starts TTS. **Gap collapses to TTS TTFB (~200–300ms), which is inside the range that reads as natural.**

Cost control: prompt-cache the stable prefix (system prompt + persona + approved knowledge + completed conversation history) so only the in-progress partial is uncached. Discard unused candidates. Budget for ~3–5× the token spend of a naive pipeline — trivial at this scale, transformative for the experience.

### 3.7 Allow ~300–500ms of genuine overlap on interruptions

True simultaneous speech is what sells "these are colleagues arguing" rather than "these are chatbots queuing." But sustained overlap over a PA in a large room is unintelligible.

Target: on an interrupt, let both voices overlap for 300–500ms, then hard-duck the interrupted agent to silence over ~150ms. Reads as natural; stays legible. Tune in the room.

---

## 4. Content and creative recommendations

### 4.1 Change "Amnesty International" to a fictional employer

Attributing invented opinions about AI to a named real NGO, in front of 400 customers, without their consent, is an avoidable legal and reputational exposure. It costs nothing to fix now.

**All three agents should have fictional employers.** "Irrational Industries" and "Servv.AI" are already the right pattern.

### 4.2 Take Speechmatics product claims out of the agents' mouths

The doc's guardrail approach — an approved knowledge layer plus prohibited-claims list — is the right instinct but is fighting the model rather than the architecture.

**Structural fix:** the agents are not Speechmatics employees and know nothing specific about Speechmatics. They discuss the *industry* — why voice interfaces historically failed, what's changed, what's still hard. **Ricky delivers any specific Speechmatics claim.** He's a human, he's on-script, he cannot hallucinate.

This inverts the risk: instead of "constrain a generative model from saying the wrong thing about us," it becomes "the model has no basis for saying anything about us." Far easier to enforce and far easier to prompt.

The agents still do all the dramatic work. Section 4 of the panel narrative ("What has actually been solved?") still lands — an agent says "the speech interface was always the bottleneck," another disagrees, and Ricky is the one who says what Speechmatics has actually shipped. That's a *better* segment anyway; it doesn't sound like an ad.

### 4.3 Extend the persona schema

The doc's schema is a good start. Add:

```yaml
agent:
  name: Dexter-K6
  job_title: Senior AI Infrastructure & Security Engineer
  employer: Irrational Industries        # fictional, always
  voice_id: <tts-voice>
  communication_style: measured
  interrupt_tendency: 0.3
  yield_tendency: 0.7                    # inverse isn't implied — model it
  topics_of_authority: [jailbreaking, security]
  # --- additions ---
  target_turn_seconds: 18                # rambling is the #1 LLM-panel failure
  max_turn_seconds: 30                   # hard cap, enforced by orchestrator
  verbosity: low
  speech_tics: ["Look —", "Right, but"]  # distinctiveness over a PA
  relationships:                         # what makes A2A feel real
    wayne: "finds him reckless but likeable"
    melia: "respects her, thinks she's slow"
  forbidden_topics: [...]                # per-persona, on top of global
```

Two of these are load-bearing:

- **`max_turn_seconds`** — the most common failure mode of LLM panels is an agent monologuing for 45 seconds and killing the room. Enforce it in the orchestrator, not in the prompt. Truncate at a sentence boundary and inject a "wrap up" directive.
- **`relationships`** — generic disagreement sounds generic. Wayne disagreeing with Dex *in the specific way Wayne finds Dex tiresome* is what reads as two colleagues rather than two models.

### 4.4 Voice distinctness must be tested on the actual PA

Three voices that are clearly distinct in headphones can collapse into indistinguishability over a large-room PA. Prioritise contrast in **pace and cadence** over timbre — timbre is what the room destroys first. Test on the real system, not on monitors.

### 4.5 Pre-render the opening

Each agent's self-introduction is fixed content. Pre-render it to audio files. Zero latency, zero failure risk, and it's the moment that sets the audience's expectation for the next 20 minutes. Also gives you a graceful cold-start while the live pipeline warms.

### 4.6 The video wall is MVP, not Phase 3

The doc has "basic speaker visualisation" in scope but schedules visuals in Phase 3. **Move it to Phase 1.**

Three disembodied voices over a PA in a 400-seat room is genuinely confusing — the audience can't reliably tell who is speaking, which destroys the "panel of colleagues" illusion the whole project depends on. A speaker indicator is *comprehension infrastructure*, not decoration.

It also doubles as the operator's live debug view, which you need from week one anyway. Building it early costs nothing extra.

---

## 5. Recommended repo structure

The 2–4 week estimate only holds if you can tune personas and thresholds **without booting audio**. That single constraint drives the whole layout.

```
voice_agent_panel/
├── pyproject.toml                 # uv workspace
├── packages/
│   ├── panel_core/                # PURE. no I/O, no async, no network.
│   │   ├── events.py              #   event types (the doc's list, typed)
│   │   ├── state.py               #   ConversationState
│   │   ├── floor.py               #   (state, event) -> [Command]   ← the heart
│   │   ├── scoring.py             #   desire-to-speak arithmetic
│   │   └── personas.py            #   pydantic schema for persona YAML
│   ├── panel_runtime/             # adapters — all I/O lives here
│   │   ├── stt/                   #   Speechmatics RT client (one per mic)
│   │   ├── vad/                   #   fast barge-in detector
│   │   ├── llm/                   #   agent brains, prompt cache management
│   │   ├── tts/                   #   streaming synth + immediate cancel
│   │   ├── audio/                 #   local device I/O, mixer, ducking
│   │   └── bus.py                 #   async event bus
│   ├── panel_sim/                 # TEXT-MODE HARNESS. no audio at all.
│   ├── panel_app/                 # wiring, config, entrypoint
│   └── panel_replay/              # replay a recorded session through core
├── apps/
│   ├── operator/                  # operator console (TS)
│   └── videowall/                 # stage visual (TS) — shares state feed
├── personas/                      # YAML, one per agent
├── content/
│   ├── beat_sheet.md              # the narrative spine
│   ├── approved_knowledge.md      # what agents may assert
│   └── prohibited.md              # global guardrails
├── recordings/                    # rehearsal event logs (JSONL)
└── docs/
```

Three principles do the real work:

### 5.1 `panel_core` is pure and synchronous

`floor.py` exposes something close to `apply(state, event) -> (state, [commands])`. No `await`, no network, no clock reads (time is passed in). This means the entire floor-control logic — the hardest and most-tuned part of the system — is unit-testable in milliseconds and behaves identically every run.

### 5.2 `panel_sim` is how you save two weeks

A text-mode harness: you type as Ricky, agents respond as text, you see floor decisions and scores printed live. No audio, no venue, no AV team.

This is where personas get written, interrupt thresholds get tuned, and the beat sheet gets rehearsed — all of which can happen **in parallel with, and before, the audio pipeline exists**. It also means non-engineers (content, marketing, Ricky himself) can iterate on the panel without an engineer in the loop. This is the highest-leverage 200 lines in the project.

### 5.3 Every event appends to a JSONL log — so every rehearsal is replayable

The doc already proposes event-sourcing for state. Take it one step further: persist the event stream. Combined with a pure core, that means any rehearsal can be replayed deterministically through modified floor logic. "Ricky interrupted and Wayne took 400ms too long to yield" becomes a regression test rather than an anecdote.

### 5.4 Language split

Python for `panel_core` / `panel_runtime` (Speechmatics SDK, audio, and async ecosystem are strongest there; uv for env management). TypeScript for the operator console and video wall. No shared runtime needed — they communicate over a WebSocket state feed.

---

## 6. Model and provider notes

| Layer | Recommendation |
|---|---|
| **Agent responses** | Start with **Claude Sonnet 5** at `effort: "low"`. Benchmark against **Haiku 4.5** (fastest TTFT, lower persona fidelity) and **Opus 5 fast mode** during Phase 0. Persona quality and TTFT trade directly against each other — measure, don't assume. |
| **Floor signals** | Don't make a separate call. Have the agent return **structured output** with the signals *and* the utterance in one request. Put the score fields first in the schema so they arrive early in the stream while `utterance` is still generating — worth validating in Phase 0. |
| **Prompt caching** | Cache the stable prefix: system prompt + persona + approved knowledge + completed turns. Only the in-progress partial varies. This is what makes speculative generation affordable. Verify with `cache_read_input_tokens` — if it's zero, something volatile has leaked into the prefix. |
| **Mid-turn directives** | Opus 5 and Opus 4.8 support mid-conversation `role: "system"` messages that don't invalidate the cache. Ideal for orchestrator directives — *"Ricky cut you off, yield"*, *"wrap up in one sentence"*, *"you have the floor, respond to Dex"*. Not available on Sonnet 5 — a genuine point in Opus 5's favour, weighed against its latency. |
| **TTS** | Non-negotiable requirements: streaming, sub-300ms TTFB, and **immediate cancellation mid-utterance**. Cancellation is the one most often missing and the one barge-in depends on. Test it before committing. |
| **Output sanitisation** | **Mandatory.** Never send raw model output to TTS. Strip XML/markdown/stage directions/emoji and enforce the length cap in code. A leaked `<thinking>` tag read aloud over a PA to 400 people is the worst-case failure and is trivially preventable. |

---

## 7. Failure handling (the doc leaves this TBD)

Every one of these needs to be *rehearsed*, not just implemented.

| Failure | Response |
|---|---|
| Agent LLM timeout (>2s) | Drop that agent's turn silently. Floor returns to moderator. **Never dead-air.** |
| Agent LLM error | Play a pre-rendered persona-appropriate stall line ("Sorry Ricky — say again?"). Buys 3 seconds and reads as human. |
| TTS failure | Fail over to secondary provider; if that fails, drop the turn. |
| STT disconnect | Auto-reconnect with backoff. Operator sees a red banner. Moderator falls back to **manual cue mode** — operator button-triggers agents by name. |
| Network loss | Wired venue ethernet primary, 5G failover pre-tested. |
| Agent says something unacceptable | Operator kill switch mutes all agent output within one audio buffer (<50ms). Physical button or dedicated key. |
| Total system failure | Ricky runs a rehearsed solo segment. **This must actually be rehearsed** — a written fallback nobody has performed is not a fallback. |

**Operator console requirements** (needed from Phase 1, because you need it to rehearse):
kill-all · mute individual agent · force agent to speak now · advance to next beat · inject "wrap up" · live state + scores · latency and health indicators.

---

## 8. Revised phase plan

Calendar is 1 Sept → 21 Oct = ~7 weeks. The doc's phases sum to ~4–5 weeks, which fits — but only if AV is unblocked immediately and content authoring is parallelised.

| Phase | Duration | Contents | Change from doc |
|---|---|---|---|
| **0 — Spike** | 3–4 days | Seven named deliverables — see §8.1. Resolves the integration-tier fork that determines Phases 1–2. | Expanded. Also: **send the AV requirements to the venue on day 1**, in parallel. |
| **1 — Core panel** | ~1 week | 3 agents, personas, state, floor control, human priority, invitations, TTS, event logging, `panel_sim`, **basic video wall**, **operator console v1**. | Visual + operator pulled forward from Phase 3. |
| **2 — Organic interaction** | ~1 week | Floor requests, disagreement, interrupt thresholds, A2A addressing and handoff, expertise weighting, **speculative generation**, overlap/ducking. | Speculation added. |
| **3 — Content & guardrails** | ~1 week | Beat sheet, approved knowledge, guardrails, output sanitisation, final personas, video wall polish. | Visuals mostly already done. |
| **4 — Hardening & rehearsal** | **2 weeks, protected** | Latency, failure recovery, AV integration, full-run rehearsals, **dress rehearsal in the actual room**. | Extended from 1 week. |

### 8.1 Phase 0 spike — deliverables

Revised 3 Sept. **The integration-tier fork is closed** (§3.4, ADR 0001): LiveKit
as a library, STT direct against the provider's own protocol, floor in
`panel_core`. The spike no longer compares options — it **validates the risky
parts of the chosen path**, which is a better use of the same four days.

Confidence in that choice is ~85%. The residual 15% is not "the other tier might
have been better" — it is "this tier has risks worth measuring". These are them.

| # | Deliverable | Gate |
|---|---|---|
| S0.1 | Dependency baseline: `livekit-agents` pinned, Agent STT reachable over raw `websockets`, imports clean, local audio device in and out | No `speechmatics-voice`, no `onnxruntime`. |
| S0.2 | **Backchannel discrimination** | The capability we gave up with `AgentSession` (§3.4). Prove "mm-hm" and "right" do **not** stop a speaking agent, while a real barge-in does. Implement as parameters in the reducer, with tests. |
| S0.3 | Barge-in latency: mic energy → agent audio ducked, owning our own mixer | **< 150ms.** The core claim of the whole architecture. |
| S0.4 | TTS cancellation mid-utterance | Hard gate — a provider that cannot cancel is disqualified before Phase 2. Decides §10 item 2. |
| S0.5 | Two Agent STT sessions, two mic channels | Identity-by-channel works; no diarization needed (§3.3). |
| S0.6 | Measure Agent STT's native end-of-turn latency/accuracy against the venue mic | No local tuning surface any more — this is now a measurement, not a calibration. Feeds the §3.6 latency budget. |
| S0.7 | Model TTFT bake-off + structured-output stream shape | Sonnet 5 / Haiku 4.5 / Opus 5 fast mode. Confirm signal fields arrive before `utterance`. Decides §10 item 3. |

All of S0.1–S0.4 run against a laptop mic and speakers — no server, no venue
hardware, nothing blocked on the AV team.

**Open for rehearsal, not for code:** `resume_false_interruption` resumes an agent
mid-sentence after a cough. On a stage, a voice stopping and restarting may read
*worse* than simply yielding. Decide in the room.

**Hard feature freeze: 7 October.** Everything after that date is tuning and rehearsal only.

Content authoring (personas, beat sheet, approved knowledge) should start in week 1 with a **non-engineering owner**, using `panel_sim` from Phase 1 onward. If content is on the engineer's critical path, the timeline fails.

---

## 9. Requirements for the venue AV team — send this week

1. **A pre-PA mic split.** Direct out / splitter / matrix send carrying Ricky's mic **only**, with no PA or agent audio in the mix. This is the single hardest requirement to retrofit and the one that must be locked first.
2. Ricky on a **close mic** (headset or lav), not a lectern or ambient mic.
3. A **line input** for agent audio, routed to the PA on its own fader with its own kill.
4. Confirmation of connector types, levels (mic vs line), and whether we can bring our own interface.
5. **Wired ethernet** at the operator position, with the ability to test throughput and stability in advance.
6. Access to the room for a **full dress rehearsal** with the real PA.
7. Answers on: audience reaction levels (applause/laughter into the mic), any existing DSP/AEC in the chain, and where the operator position is relative to the stage.

If item 1 or item 6 cannot be met, that's a material change to the risk profile and should trigger a scope conversation, not a workaround.

---

## 10. Open decisions

| # | Decision | Owner | Needed by |
|---|---|---|---|
| 1 | ~~Audio transport and integration tier~~ — **resolved 3 Sept, ADR 0001**: LiveKit as a library (transport/VAD/TTS), STT direct against the provider's own protocol, no `AgentSession`, no cloud SFU, floor in `panel_core` | Eng | ✅ done |
| 2 | TTS provider (gate on cancellation latency) | Eng | End of Phase 0 |
| 3 | Agent model + effort setting | Eng | End of Phase 0 |
| 4 | Final personas, names, fictional employers | Content | End of week 2 |
| 5 | Beat sheet and approved knowledge | Content + Ricky | End of week 3 |
| 6 | One moderator or two | Creative | End of week 2 |
| 7 | Video wall visual design | Design | End of week 3 |
| 8 | Who operates the console on the night | — | Before rehearsal |

---

## 11. Summary of recommended changes to the scoping doc

1. Promote echo isolation from acceptance criterion to top-level architectural constraint.
2. Split barge-in (VAD) from comprehension (STT) — don't put transcription in the interrupt path.
3. One STT channel per human mic; drop the diarization dependency.
4. Use LiveKit as a *library* (transport, VAD, TTS) rather than a framework — no `AgentSession` for the agent loop, no cloud SFU. STT is Agent STT, one client per human mic, no SDK in between. Floor arbitration stays in `panel_core`. (ADR 0001.)
5. Keep the LLM out of floor arbitration — deterministic core.
6. Add speculative generation during the human's turn.
7. Change Amnesty International to a fictional employer.
8. Move all Speechmatics product claims from agents to Ricky.
9. Extend the persona schema (turn caps, relationships, tics).
10. Move the video wall from Phase 3 to Phase 1.
11. Add mandatory TTS output sanitisation.
11b. Build backchannel discrimination ourselves — the one real capability given up in item 4.
12. Fill in failure handling (§7) and rehearse it.
13. Extend hardening/rehearsal to two protected weeks; freeze features 7 October.
