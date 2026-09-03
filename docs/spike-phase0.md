# Phase 0 spike — running findings

Started 3 Sept 2026. Deliverables defined in FEASIBILITY.md §8.1.

| # | Deliverable | Status |
|---|---|---|
| S0.1 | Dependency baseline | ✅ done |
| S0.2 | Backchannel discrimination | ✅ logic + tests; needs live audio to tune |
| S0.3 | Barge-in latency < 150ms | 🟡 VAD term measured; hardware term needs venue rig |
| S0.4 | TTS cancellation | ✅ passed, and TTFB cut 55% on the way |
| S0.5 | Two `speechmatics-rt` channels | 🟡 adapter built; needs live mics to verify |
| S0.6 | End-of-turn tuning | 🟡 wired and configurable; needs venue mics |
| S0.7 | Model TTFT bake-off | ✅ done — and it changed the architecture |

---

## S0.1 — Dependency baseline ✅

`packages/panel_runtime` created. `speechmatics-rt==1.1.1` resolves cleanly and
pulls only `websockets`. No `speechmatics-voice`, no `onnxruntime`. LiveKit is an
optional extra (`panel-runtime[livekit]`) so `panel_core` work never pays for it.

**Import is `from speechmatics.rt import AsyncClient, ...`** — the distribution
is `speechmatics-rt` but `speechmatics.rt` is the package (`speechmatics/rt/__init__.py`).

### Three findings that change assumptions

**1. `AsyncMultiChannelClient` exists — §3.3 is better supported than assumed.**
Multiple audio streams over a *single* WebSocket, each tagged with a channel id,
server-side. So channel-per-mic is not N connections; it is one connection with
N tagged streams, and `channel_diarization_labels` lets us name them ("ricky").
Identity stays a hardware fact with no diarization inference.

**2. Server-side end-of-turn already exists.** `ServerMessageType.EndOfUtterance`
fires from `ConversationConfig.end_of_utterance_silence_trigger`. We do not
hand-roll silence detection — this maps directly onto our `TurnYielded` event.
S0.6 becomes *tuning a parameter*, not building a detector.

**3. `AudioEventStarted` / `AudioEventEnded`** — audio event detection. Possibly
relevant to §9's open question about applause and laughter hitting the mic.

Relevant latency knobs on `TranscriptionConfig`: `max_delay`, `max_delay_mode`,
`enable_partials`, `operating_point`.

**Local audio:** PortAudio v19.7 via `sounddevice`. Device enumeration works.

> ⚠️ **Do not build anything around this machine's audio setup.** The panel
> deploys to a different machine at the venue. Device names, sample rates,
> channel counts and buffer sizes are all deployment configuration, never
> hardcoded, and every latency figure has to be re-measured on the target rig.

---

## S0.2 — Backchannel discrimination ✅ (logic)

### The tension this surfaced

LiveKit's defaults are `min_duration=0.5` plus `resume_false_interruption`:
wait 500ms before treating speech as an interruption, and resume if it turns out
not to be one. **That directly contradicts our <150ms barge-in target** (§8.1
S0.3). Waiting to classify costs responsiveness; assuming "interrupt" means an
agent stops dead every time Ricky says "mm-hm" and the panel stutters.

### The resolution: duck first, classify after

Not stop-and-resume — **attenuate and then decide**. On VAD onset the agent is
ducked ~15 dB over a 120ms ramp but keeps talking. Classification runs on the
evidence as it arrives:

| Signal | Arrives | Outcome |
|---|---|---|
| VAD onset | ~30ms | `DuckSpeech` — always, immediately |
| Duration > 600ms | on `Tick` | `StopSpeech` — a bid for the floor whatever the words |
| ≥3 substantive words | ~300ms, from STT | `StopSpeech` — content overrides duration |
| Speech ends short + not substantive | on `HumanSpeechEnded` | `ResumeSpeech` — restore gain over 220ms |

This is what a human panellist actually does: drop your volume when someone
says "mm-hm", stop only if they keep going. Acoustically natural, no jarring
restart, and **responsiveness never trades against correctness** — we get the
150ms reflex *and* correct classification.

It is also strictly better than the behaviour we would have inherited from
`AgentSession`, which stops dead and then restarts mid-sentence.

### Implementation

- New commands `DuckSpeech(agent, gain_db, ramp_ms)` / `ResumeSpeech(agent, ramp_ms)`,
  distinct from `StopSpeech`.
- New `FloorConfig` knobs: `backchannel_duck_db`, `duck_ramp_ms`,
  `resume_ramp_ms`, `backchannel_max_duration_s`, `interrupt_min_words`.
- `is_backchannel()` in `scoring.py`. **Conservative by construction**: unknown
  words count *against* backchannel. Being wrong towards "interrupt" is safe
  (the human wanted the floor anyway); being wrong towards "backchannel" means
  talking over Ricky, which is not.
- Six new tests, including the race where a short burst resumes and the
  substantive transcript lands afterwards — content still wins.

**Still to do on S0.2:** thresholds are educated guesses. `backchannel_max_duration_s`
and `interrupt_min_words` need tuning against real speech, and the lexicon needs
Ricky's actual verbal habits added. That is a rehearsal task, not a code task.

---

## S0.3 — Barge-in latency: the measurement plan

Revised after ruling out any dependence on this machine's audio devices.

### The budget decomposes into three terms

```
total barge-in latency
  = input hardware      (mic -> ADC -> first sample in our buffer)
  + our pipeline        (VAD onset -> duck gain applied to an output buffer)
  + output hardware     (gain applied -> DAC -> speaker)
```

**Only the middle term is ours.** The outer two are set by the deployment
machine, its interface and its buffer size — which is exactly why they cannot be
measured here and must be measured on the venue rig.

### Two measurements, two purposes

**1. Internal latency — runs anywhere, no special hardware, goes in CI.**
Instrument the pipeline: timestamp VAD onset, timestamp the first output buffer
written with the ducked gain, take the delta. Deterministic and machine-independent
enough to act as a regression guard. **Budget it at ≤ 30ms** so the hardware terms
have room inside 150ms.

**2. Round-trip latency — on the deployment machine only.**
- *Electrical loopback* (preferred): interface output patched to interface input
  with a cable. Exact, trivial to arrange at the venue, and it measures the real
  signal path.
- *Acoustic fallback*: play a click through the PA, capture on Ricky's mic.
  Noisier, but it is the honest end-to-end number and needs no extra kit.

This is the figure that has to come in under 150ms, and it is a **soundcheck
task**, not a desk task.

### The dominant tunable is buffer size

At 48kHz a buffer costs, on input *and* output:

| Frames | Latency each way |
|---|---|
| 128 | ~2.7ms |
| 256 | ~5.3ms |
| 512 | ~10.7ms |
| 1024 | ~21.3ms |

Silero VAD operates on ~32ms windows, so it will likely dominate our own term.

**Worth testing:** a cheap energy gate running at buffer rate to trigger the
*duck*, with Silero confirming a frame or two later — the same duck-first,
classify-after pattern one level further down. If it works, the reflex is
sub-buffer and Silero never sits in the critical path.

### Deliverables

- `panel_runtime` audio harness with device/samplerate/buffer as **config**
- Internal-latency measurement + a regression test asserting the ≤30ms budget
- A written soundcheck procedure for the round-trip measurement at the venue
- A recommended buffer size, with the measured cost of each option

---

## S0.3 — measured: the VAD term ✅

Bench: `packages/panel_runtime/tests/bench_vad_latency.py`. Five onset types
(fricative, plosive, vowel, aspirate, nasal), committed `say` fixtures, offline.

```
onset type       p>=0.3   p>=0.5   p>=0.7   confirm
h_aspirate         66.8     66.8     98.8      98.8
m_nasal            44.8     44.8     44.8      76.8
p_plosive          48.0     48.0     48.0      80.0
s_fricative        34.9     66.9     98.9      98.9
v_vowel            51.3     51.3     83.3      83.3

  p>=0.3   mean 49.1ms   WORST 66.8ms
  p>=0.5   mean 55.5ms   WORST 66.9ms      <- chosen
  p>=0.7   mean 74.8ms   WORST 98.9ms
  confirm  mean 87.5ms   WORST 98.9ms
```

### Findings

**My ≤30ms internal budget was wrong.** Silero alone costs ~67ms worst case. The
budget had to be rebuilt around the measurement rather than the other way round.

**Duck on `inference_done`, not `start_of_speech`.** Silero's debounced
`start_of_speech` costs 87.5ms mean / 98.9ms worst. The per-inference
`probability` on `INFERENCE_DONE` is available ~20-30ms earlier and is what the
duck fires on. `start_of_speech` remains useful as the confirmation signal.

**`duck_probability = 0.5`, measured not guessed.** p≥0.3 buys nothing in worst
case (66.8 vs 66.9ms) and would cost false ducks. p≥0.7 pushes worst case to
98.9ms, which does not fit.

**Block size is irrelevant to the VAD term.** 128/256/512 all produce identical
detection latency — LiveKit buffers into Silero's fixed 32ms windows regardless.
Block size still costs on the output side, so it stays a tunable, just not this one.

**Silero inference is free.** ~0.25ms mean, 1.5ms worst. Model load 30ms, once,
at prewarm. Inference is nowhere near the critical path.

### Revised budget

```
  Silero detection    ~67ms worst case   <- dominates everything
  resample 48k->16k   ~1-2ms
  floor reducer        <1ms
  output block          5.3ms  (256 @ 48kHz)
  ------------------------------
  internal total       ~75ms
  remaining for ADC/DAC + input buffering, measured at the venue:  ~75ms
```

Fits the 150ms hard limit, but **the VAD dominates and the margin is real rather
than comfortable.** Two consequences:

- Input buffer size matters more than expected. Budget ≤ 256 frames @ 48kHz.
- If the venue rig's hardware round trip exceeds ~75ms, the options are a smaller
  buffer or a lower-latency interface — not a VAD change.

### Caveat

These are clean synthetic fixtures at 16kHz. Real speech through a close mic in
a room with PA bleed will differ, and probably not favourably. **Re-run the
equivalent measurement on the venue rig with Ricky's actual mic** before treating
75ms as settled.

---

## S0.3 — live harness

`uv run barge-in` — speak into the mic, hear the agent duck, see measured latency.

Runs the **real** `panel_core.FloorController`, not a mock: Silero VAD on the
mic, an "agent" talking continuously, and the duck / resume / stop behaviour
from ADR 0001 driving a sample-accurate gain envelope.

### Latency measurement without a loopback device

PortAudio exposes `inputBufferAdcTime` and `outputBufferDacTime` on every
callback. Mapping a VAD event's `samples_index` back to the ADC time of the
block that carried it, and comparing against the DAC time of the first output
block with a changed gain, gives **true ADC-to-DAC barge-in latency** — no
virtual audio device, no patch cable, works on any machine including the venue rig.

### Measured on the dev MacBook

| Term | Value |
|---|---|
| ADC→DAC buffer delta @ 128 frames / 16kHz | **19.3ms** |
| Silero detection (worst case, from bench) | ~67ms |
| **Projected total** | **~86ms** — inside the 150ms limit |

⚠️ PortAudio *reports* `in_latency=290ms` on this machine, which contradicts the
19.3ms it measures from its own timestamps. The measured delta is the number the
harness uses and the one to trust; treat the reported hint as unreliable.

### Running it

```bash
uv run barge-in                    # defaults: 128-frame blocks @ 16kHz
uv run barge-in --list-devices
uv run barge-in --block 256        # compare buffer sizes
uv run barge-in --input-device 3
```

**Headphones are required.** On open speakers the agent's audio reaches the mic
and the VAD fires on it — the exact feedback loop §3.1 warns about, and a live
demonstration of why the venue needs a pre-PA mic split.

macOS will prompt for microphone permission on first run.

**What to try:**

| Say | Expect |
|---|---|
| "mm-hm" — a short burst | duck to −15dB, then **resume**. Agent keeps the floor. |
| "sorry, hold on a second" | duck, then **stop** once past 600ms. |
| nothing, but laugh or tap the desk | should *not* trigger — Silero is speech-specific, which is why an energy gate was rejected. |

**Current limitation:** no STT in this harness, so classification is duration-only.
Content-based classification ("mm-hm" vs "no, that's wrong" at equal length)
needs `speechmatics-rt` wired in — S0.5.


---

## S0.4 — TTS cancellation ✅

Two gates. ElevenLabs clears both, but only after a design change that the
measurement forced.

### Gate 1: cancellation — passed

| Measure | Result |
|---|---|
| `cancel()` returns in | **0.089ms** |
| Audio chunks delivered after cancel | **0** |

The important part is *why* this passes, because it is not a property of the
provider. We never ask ElevenLabs to stop and wait for it to comply:

    StopSpeech -> envelope.ramp_to(-inf, ~20ms)   <- the audible stop
               -> stream.cancel()                 <- local flag, returns in µs
               -> {"close_context": true}         <- teardown, fire and forget

The audible stop is the mixer's gain envelope, inside one output buffer. The
provider is told afterwards, off the latency path. **This keeps the 150ms hard
limit a property of our own code rather than of somebody else's network** — and
it means a provider swap cannot regress barge-in.

A related finding reinforces it: with `auto_mode`, Flash returns a whole
20-second utterance in ~3 very large chunks. Chunk-level cancellation
granularity is therefore meaningless, and any design that depended on "stop
reading the stream in time" would have failed. Local cancellation is not an
optimisation here; it is the only thing that works.

### Gate 2: TTFB — passed after a fix

Speculative generation collapses the post-turn gap to TTS TTFB and nothing
else (§3.6), so this number *is* the panel's responsiveness.

First measurement, one WebSocket per utterance:

| | median | worst |
|---|---|---|
| Fresh socket per turn | 444ms | 963ms |

Roughly **200ms of every turn was WebSocket handshake**, paid again on each
answer. The single-stream endpoint invites this mistake — its protocol is
framed as one input stream per connection, ending with a `{"text": ""}`
sentinel that closes the socket.

The fix is the `multi-stream-input` endpoint: one connection per voice, held
open for the whole show, with each turn scoped to its own `context_id`.
Interrupting a turn closes that *context*, not the connection.

| | median | worst |
|---|---|---|
| Pooled connection per voice | **198ms** | **284ms** |

Handshake becomes a 202ms cost paid once during the pre-show, when there is no
audience waiting on it. Worst case now sits inside the ~300ms target.

**Not yet decided:** voice casting. The measurement used a stock voice; the
personas still carry `voice_id: TBD-*`. Re-measure after casting — TTFB is
model-bound, not voice-bound, so this should hold, but "should" is not a
measurement.

---

## S0.5 — Speechmatics channels 🟡

Adapter built (`panel_runtime/stt.py`). One `AsyncMultiChannelClient` carries
every human mic on a single persistent WebSocket, tagged by channel id, so
speaker attribution is a property of the wiring rather than of diarisation.

### The one non-obvious integration point

`speechmatics-rt` streams from any object with a `read()`, and **awaits it if it
is a coroutine** (`_audio_sources._make_iter`, line 111). That is what lets a
live mic work without a bridging thread or a temp file: `PushAudioSource`
exposes an `async read()` fed by an `asyncio.Queue`, and `feed()` is called from
the PortAudio callback via `call_soon_threadsafe` so the audio thread never
blocks. Full queues drop the oldest block and count it — a rising count means
the uplink is not keeping up, which the operator should see.

### Still to verify on real hardware

- Channel labelling actually round-trips (`channel_diarization_labels` in,
  `channel` out on results) — needs two live mics.
- `EndOfUtterance` timing under room noise. `end_of_utterance_silence_trigger`
  is at 0.6s on a guess; that is S0.6 and it must be tuned in the venue, since
  room tone moves it.

### Known deprecation, deliberately not chased

The server returns a `Warning` that `operating_point` is deprecated in favour of
a `model` property. `speechmatics-rt` 1.1.1 exposes no such field — the SDK is
behind the API. Left on `operating_point` rather than guessing at an
undocumented parameter; revisit on the next SDK release.


---

## S0.6 — End-of-turn 🟡

Wired, not tuned. `ConversationConfig.end_of_utterance_silence_trigger` sits at
**0.6s** in `STTConfig` and produces `EndOfUtterance`, which the adapter turns
into `TurnYielded`.

0.6s is a guess and is explicitly not a result. It is the single most
personality-defining number in the system — too short and Ricky gets cut off
mid-thought, too long and the panel feels sluggish — and it moves with room
tone, mic choice and how the moderator actually speaks. **Tune it in the venue,
with Ricky, on the real rig.** Nothing measured at a desk transfers.

---

## S0.7 — Model bake-off ✅

This one changed the design. Measured on a realistic mid-panel scenario (three
turns of history, one agent directly addressed):

| model | effort | ttft | signals | total | output |
|---|---|---|---|---|---|
| opus-5 | low | 3247ms | 4344ms | 6207ms | 170 tok |
| opus-5 | medium | 1482ms | 2921ms | 6218ms | 197 tok |
| sonnet-5 | low | 1393ms | 2467ms | 4129ms | 193 tok |
| sonnet-5 | medium | 1437ms | 1973ms | 4110ms | 213 tok |
| haiku-4.5 | — | — | — | 2582ms | 150 tok |

Haiku rejects the `effort` parameter outright (400), so it is unconfigurable
here rather than merely faster.

### The finding that mattered

**The time is generation, not reasoning.** ~200 output tokens at roughly 40
tokens/sec. Setting `thinking: {"type": "disabled"}` changed nothing:

| variant | median |
|---|---|
| sonnet-5, no effort | 4865ms |
| sonnet-5, thinking disabled | 5088ms |
| opus-5, thinking disabled | 5022ms |
| haiku-4.5, no effort | 2582ms |

So no model choice and no effort setting rescues a design that waits for the
whole proposal before speaking. **4-6 seconds of dead air is a broken panel**,
and this was going to be discovered on stage rather than at a desk.

### The fix: stream sentences into TTS

An agent does not need its last sentence written to say its first. The proposal
streams; each sentence goes to TTS the moment the chunker finds a boundary.
`multi-stream-input` already supports incremental text into a live context, so
this cost no extra connection.

| path | signals | 1st sentence | **first audio** |
|---|---|---|---|
| one-shot | — | — | **4699ms** |
| streamed | 2475ms | 3035ms | **3275ms** |

**1424ms of dead air removed from every single turn**, and TTS adds only ~240ms
on top of the first sentence. The two clocks work in our favour from there:
generation runs at ~40 tokens/sec against speech at ~2.8 words/sec, so writing
stays comfortably ahead and the agent never catches up with itself.

This also vindicates the `PROPOSAL_SCHEMA` field ordering. Signals complete at
2475ms against a 4100ms total, so the floor is arbitrated while the utterance is
still being written — the ordering was speculative when it was written and is
now measured.

### Bug this surfaced

`BrainConfig.timeout_s` was **4.0s** against measured 4-6s generation. The
non-streaming brain timed out more often than it succeeded, and the failure mode
was silent: no proposal, no candidate, an agent that simply never spoke. Now
12.0s, and the streaming path makes the bound close to irrelevant.

### Still open

- **Model choice is not settled.** Sonnet 5 is the current default on latency.
  Whether Opus 5's answers are enough better to be worth ~2s more is a content
  judgement for a rehearsal, not a benchmark.
- **Voice casting.** Personas now carry real stock ElevenLabs voice ids so the
  pipeline runs end to end, marked `PLACEHOLDER` in the YAML. Recast before
  rehearsal; TTFB is model-bound rather than voice-bound so the numbers should
  hold, but that is an expectation, not a measurement.
