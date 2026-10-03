# ADR 0002 — Address detection by classifier

**Status:** Accepted, behind a flag. Default off.
**Date:** 18 September 2026. **Amended 2 October 2026** — a verdict may name
several panellists, and the classifier is given one line of conversation
context. See *Amendment* below.
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

The regex in `floor.py` is accurate on the phrasings it was written for — every
row of `packages/panel_core/tests/test_address.py`, by construction — and
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

- **Decoded from the stream as soon as the verdict provably closes**, not from
  the finished message. `prompts.address_verdicts()` enforces a distinct initial
  per token, so one character settles *which* panellist, and it **raises** if
  two tokens share an initial — renaming a persona is the moment to find that
  out, not the show. Since the 2 Oct amendment a complete name is also a legal
  prefix of a pair, so the decoder needs one character past the last name; see
  *Amendment* for what that measured.
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
  The key also carries the conversation context, for the same reason on the
  other axis — see *Amendment*.

### The one event that waits

`TurnYielded`, and only `TurnYielded`, is held back for the verdict — bounded by
`ADDRESS_HOLD_TIMEOUT_S = 0.9` in `panel.py` (0.7 until 2 Oct). Speechmatics' `EndOfTurn` lands
within a few ms of the final that names an agent, so without the hold the floor
arbitrates before the invitation exists: floor closed, Ricky cued, dead air —
the exact bug removed the week before this was written.

`TranscriptUpdated` is **never** held. It drives the barge-in content check and
speculative generation, and delaying it by one round trip would undo both.

The hold is not free in either direction: every millisecond of it is silence on
stage, and the fallback is a detector that is correct for every corpus row. The
trade is "the slowest few per cent of verdicts lose the new capability" against
"every turn pays the tail," and the first is much the cheaper. The hold is not a
fixed delay — it ends when the verdict lands — so raising the ceiling buys the
tail and costs nothing on a normal turn; what bounds it is that past about a
second the audience hears a gap, and a cued moderator beats a late invitation.

---

## Measurements

| Metric | Value | Where |
|---|---|---|
| Accuracy, regression corpus | 156/156 | `tests/bench_address.py --repeat 3` against `panel_core/tests/test_address.py` |
| Accuracy, new capability | 75/75 | same run; scored separately, see the bench docstring |
| Time to verdict, p50 | 611ms | dev box |
| Time to verdict, p95 | 728ms | dev box |
| Time to verdict, max | 1408ms | dev box |
| Cost of the set terminator | +115ms p50 (0-190) | paired, both decoders over the same 77 streams |

Dev box, not the venue rig. Re-measure before trusting any of it (CLAUDE.md
§ Deployment); `ADDRESS_HOLD_TIMEOUT_S` is the first dial to move if the tail is
worse there.

The pre-amendment figures were 153/153, p50 526ms, p95 781ms.

---

## Amendment — 2 October 2026

Two changes, both in service of the same thing: an invitation is a *set* of
panellists, and some of Ricky's invitations only make sense against the
conversation.

### A verdict may name several panellists

The verdict vocabulary gains **any two or more of the agent tokens joined by
`+`** — `MELIA+WAYNE`. `Invitation.agent` becomes `Invitation.agents`, a tuple
in cast order, empty for an open floor.

A joined verdict is a real invitation to exactly those panellists:
`Invitation.admits()` bars everyone outside the set, and drops the once-each
rule *inside* it so the two of them can actually go back and forth —
`turns_remaining` and `max_consecutive_agent_turns` bound the exchange, and
`recency_penalty` is what makes it alternate. Naming the whole panel normalises
to `OPEN`; there is nobody left to bar, and an open floor owes every agent a
turn, which a group deliberately does not.

**This replaces `AMBIGUOUS` for the two-names case**, on both detection paths.
"Melia and Wayne, can you take that between you?" used to close the floor and
refer the decision to an operator — on a console that was never built — which
refused a question Ricky had asked perfectly clearly. The regex path groups
coordinated names instead of reporting a tie, so `_Detection.conflict` is gone.
`AMBIGUOUS` survives, narrowed to "we cannot tell *who* was invited", and is now
reachable only from the classifier: no pattern over a transcript concludes that.

The cost is in the decoder. A complete name is a legal prefix of a pair, so
`decode_address_verdict` resolves one character past the last name rather than
at the first content delta. Measured paired — both decoders over the same deltas
of the same 77 responses — that is **+115ms p50**, range 0-190ms, and zero on
the speculative path. `ADDRESS_HOLD_TIMEOUT_S` went 0.7 → 0.9 to cover it, and
because an expired hold now loses capability the regex has no answer for rather
than a descriptive reference it might have got right by luck.

A fixed-width slot encoding (`D.W`) closes the set without a terminator and was
rejected anyway: unreadable in a rehearsal log, and asking a model for a
positional code rather than for names trades accuracy — the thing this
classifier exists to buy — for about one delta.

### The classifier gets one line of conversation context

"I'd like to hear from the other two" is Melia and Wayne after Dexter's turn and
a different pair after Melia's. The sentence names nobody and contains nothing to
count against, so `prompts.build_address_context` renders who the panel has heard
from recently — most recent first, bounded to the last six transcript entries
plus whoever is speaking — and `PanelRuntime` passes it with every `speculate()`
and `classify()` call.

Two constraints on where it goes, and both are load-bearing:

- **The user turn, never the system prompt.** The system block is cached
  (`cache_control: ephemeral`) and must stay byte-identical for the whole show;
  putting moving text in it re-prefills the prompt every turn.
- **Part of the cache key.** Same words, different exchange, different answer.
  Keyed on the utterance alone, the second "the other two" of a show would be
  served the first one's verdict — putting the one panellist Ricky had just
  heard from straight back on the PA.

### The prompt is a fixed budget

Discovered the hard way, and worth stating as a rule. The first draft of the two
new rule blocks scored 52/52 on the regression corpus and flipped "Who have we
got with us tonight?" from `INTRO` to `NONE`, 6/6 deterministically — with that
exact phrasing listed in the `INTRO` rules. Reverting any single new sentence
left it failing; reverting all of them fixed it; moving the blocks above the
`INTRO` rules did not help; halving their length did not help. What fixed it was
making the `INTRO` rule explicit about that phrasing, after which the verbose and
terse drafts both scored 8/8 on it.

So a new rule in this prompt competes with the existing ones rather than simply
adding to them, and `bench_address.py` is the only way to find out what one
cost. Both new blocks are kept terse because the terse draft measured identically
and costs fewer tokens.

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
- `AMBIGUOUS` cannot name who it could not separate — it is one word — so the
  runtime supplies the whole cast as the conflict set. Floor stays closed,
  operator decides. Since the amendment it no longer covers "two panellists were
  named", which is a real verdict.
- The regex stays. It is the fallback, and it cannot be deleted until the
  classifier's on-stage tail is known.

---

## Open question — the reason this is still a flag

**The on-stage cache-hit rate is unmeasured.** A speculative hit is free; a fresh
call is ~600ms in series with arbitration. Which of those is the common case
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
