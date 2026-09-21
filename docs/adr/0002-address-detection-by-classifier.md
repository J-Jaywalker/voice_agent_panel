# ADR 0002 — Address detection by classifier

**Status:** Accepted, behind a flag. Default off.
**Date:** 18 September 2026
**Decides:** who answers *"who did Ricky just invite to speak?"*
**Does not reopen:** ADR 0001. No `AgentSession`, we still own the mixer.

---

## Decision

1. **A model resolves the addressee.** `claude-haiku-4-5`, in
   `panel_runtime.address.AddressClassifier`.
2. **The decision half stays in `panel_core`.** The verdict is emitted as an
   `AddressDetected` event and reduced in `floor.py`. The reducer never opens a
   socket, so a recorded log still replays identically.
3. **It fails closed to the regex**, and closed means `verdict=None` — never an
   invented `NONE`.
4. **Behind `FloorConfig.llm_address_detection`** (`panel --llm-address`), off
   by default. With the flag off, `AddressDetected` is ignored outright and the
   classifier is never constructed.

---

## Context

The regex in `floor.py` is accurate on the phrasings it was written for —
153/153 of `packages/panel_core/tests/test_address.py`, by construction — and
**structurally incapable** of resolving a descriptive reference. "What does the
financial side make of that?" is Wayne, and no pattern over the transcript can
know that, because the fact that makes it true lives in `personas/wayne.yaml`.

That is the whole capability being bought. It is not an accuracy fix for the
phrasings the regex already handles.

### What this does and does not decide

Two decisions that were previously one, and keeping them apart is the point:

| Decision | Who makes it |
|---|---|
| Is the floor open, and to whom? | **This ADR.** Haiku, or the regex with the flag off |
| Who wins an *open* floor? | `scoring.py` — deterministic, no LLM, unit-tested |

On a *named* verdict the score floor is bypassed entirely (`_arbitrate`: "Ricky
named them. They answer"), so in practice the classifier's verdict is the
decision on most turns. CLAUDE.md's old "floor arbitration: no LLM" line was
written before this existed and read as false afterwards; it has been split.

---

## Latency is the design

The verdict sits exactly where the floor opens — the moment this project spent
weeks clearing. `~/git/FDE/amazon_alexa_demo/wake.py` solves the identical
problem and is the reference implementation; read it before changing any of
this.

- **Decoded from the first content delta**, not the finished message.
  `prompts.address_verdicts()` enforces a distinct initial per token, so one
  character normally settles it and time-to-verdict ≈ time-to-first-token. It
  **raises** if two tokens share an initial — renaming a persona is the moment
  to find that out, not the show.
- **The reason keeps streaming in the background** and is filed against the
  cache entry. Nothing ever waits on prose.
- **Partials are speculatively classified** while Ricky is still talking, so the
  common case at finalisation is a cache hit at zero measured cost.
- **An in-flight speculation for exactly the finalised text is joined, not
  cancelled.** Cancelling throws away the head start and pays a fresh round trip.
- **Cache keys are the whole normalised text, never a prefix.** A partial of
  "Wayne" looks vocative and resolves to WAYNE; the final "Wayne's point earlier
  was wrong, Melia, what do you think?" is MELIA, and Wayne is the one agent who
  must not get the floor. Never add a `startswith` lookup as an optimisation.

### The one event that waits

`TurnYielded`, and only `TurnYielded`, is held back for the verdict — bounded by
`ADDRESS_HOLD_TIMEOUT_S = 0.7` in `panel.py`. Speechmatics' `EndOfTurn` lands
within a few ms of the final that names an agent, so without the hold the floor
arbitrates before the invitation exists: floor closed, Ricky cued, dead air —
the exact bug removed the week before this was written.

`TranscriptUpdated` is **never** held. It drives the barge-in content check and
speculative generation, and delaying it by one round trip would undo both.

0.7s is deliberately *inside* the measured p95, not outside it. The hold is not
free in either direction: every millisecond is silence on stage, and the
fallback is a detector that is correct for all 153 corpus rows. The trade is
"the slowest few per cent of verdicts lose the new capability" against "every
turn pays the tail," and the first is much the cheaper.

---

## Measurements

| Metric | Value | Where |
|---|---|---|
| Accuracy | 153/153 | `tests/bench_address.py` against `panel_core/tests/test_address.py` |
| Time to verdict, p50 | 526ms | dev box |
| Time to verdict, p95 | 781ms | dev box |

Dev box, not the venue rig. Re-measure before trusting either (CLAUDE.md
§ Deployment); `ADDRESS_HOLD_TIMEOUT_S` is the first dial to move if the tail is
worse there.

---

## Model constraints, all verified rather than assumed

- `claude-haiku-4-5` is pre-4.6. `thinking`, `output_config` and `effort` are
  rejected outright — which is exactly why the verdict is decoded from
  characters rather than a JSON schema.
- **`temperature` is not passed, and cannot be.** The Anthropic SDK dropped it
  from `messages.stream()` at 1.x (checked against 1.2.0, which `panel_runtime`
  pins). `wake.py` sends `temperature=0.0` because it is on the 0.x line;
  copying that raises `TypeError`. A verdict is therefore not reproducible for
  free — `bench_address.py --repeat` is how you find out whether one is stable.
- The system prompt is cached (`cache_control: ephemeral`) and never changes
  mid-show, so the volatile utterance goes in the user turn.
- `panel_runtime.config.anthropic_base_url()`, never the ambient
  `ANTHROPIC_BASE_URL` — a token-saving proxy rewrites the prompt in flight and
  destroys the byte-exact prefix that prompt caching depends on.

---

## Consequences

- One input to the floor is now non-deterministic. The floor itself is not: the
  verdict enters as an event, so replay is preserved and `panel_core` keeps its
  no-I/O invariant.
- A flag-off replay must not double-apply detection, which is why
  `_address_detected` returns early rather than honouring the event regardless.
  The same log can be replayed through the recorded verdicts *or* the regex.
- The introduction latch moved to the classifier path (`INTRO` verdict). Two
  code paths able to start the one-shot round is one too many, and the regex one
  fired on "intro" anywhere in the sentence.
- `AMBIGUOUS` cannot name who tied — it is one word — so the runtime supplies
  the whole cast as the conflict set. Floor stays closed, operator decides.
- The regex stays. It is the fallback, and it cannot be deleted until the
  classifier's on-stage tail is known.

---

## Open question — the reason this is still a flag

**The on-stage cache-hit rate is unmeasured.** A speculative hit is free; a fresh
call is ~500ms in series with arbitration. Which of those is the common case
decides whether this capability costs nothing or costs half a second a turn.

`AddressVerdict.source` and the `⌖ address:` console line exist precisely to read
this off, and nothing aggregates them yet. Get that number in rehearsal before
making the flag default-on.

---

## References

- ADR 0001 — LiveKit integration tier (unaffected)
- FEASIBILITY.md §3.4 (determinism), §3.7 (this), §3.8 (agent-to-agent)
- `packages/panel_runtime/src/panel_runtime/address.py` — the network half
- `packages/panel_core/src/panel_core/floor.py` `_address_detected` — the decision half
- `packages/panel_core/tests/test_llm_address.py` · `packages/panel_runtime/tests/test_address_classifier.py`
- `~/git/FDE/amazon_alexa_demo/wake.py` — reference implementation
