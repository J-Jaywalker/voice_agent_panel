# Moderator Cue Card — Tuner AI variant

**Three questions.** Full material — the recommendation each agent leads
with and the evidence behind it, plus the Beat 3 confessions in full — is in
[`tuner-beat-sheet.md`](tuner-beat-sheet.md); you shouldn't need it on stage.
Beats 1 and 2 lead with practical advice, not a story — personal experience
shows up as one supporting line, not the whole turn. **Beat 3 is the
opposite on purpose**: it's the comedy close, and it's all story.
Cast, floor rules and kill switch are all identical to the six-beat show —
see [`moderator-cue-card.md`](moderator-cue-card.md) if anything below
doesn't cover it.

---

## Three rules

1. **Ask a question. Statements invite nobody.** The floor is closed by
   default.
2. **Use a name, or open it to the room.** All three openers below name
   nobody on purpose and hand the floor to whoever picks it up first — for
   Beat 3 specifically, the floor now guarantees all three a turn once one
   of them has spoken, so one open question is enough to get all three
   confessions.
3. **To stop an agent, just talk.** You outrank all three.

---

## Running order

| | Beat | Say |
|---|---|---|
| **0** | Introductions | *"Let's start with some quick introductions."* |
| **1** | What stops a bad deployment | *"So Dexter, before one of these goes anywhere near a real customer, what actually happens to it first?"* |
| **2** | What this makes possible next | *"So what does this actually make possible — where does this go next?"* |
| **3** | What actually went wrong | *"Alright — before we wrap, let's hear the ones that actually went wrong. What's it heard wrong, hallucinated, or been talked into — and what did it cost you?"* |
| | **Your close** | Tuner AI, the product, and what the panel just described in their own words |

Let each beat run itself for 60–90 seconds once opened. You only need to
speak again to move on, redirect, or cut someone off. **Beat 3 is the
closing laugh — give it room, don't rush it into the close.**

---

## Beat by beat — what comes back

**0 · Introductions** — fixed, pre-recorded wording, unchanged from the
six-beat show. Just let it run.

**1 · What stops a bad deployment.** Dexter leads with the practice: run the
agent against an adversarial batch repeatedly, not as a single pre-launch
gate, because a fixed suite only proves it survives what someone already
wrote. Melia leads with the matching control from the audit side: check who
chose the sample, not just whether it passed. Wayne reframes both as one
piece of advice — test the worst case before you trust the best one, and if
the rehearsal is pleasant to watch it isn't testing anything.

*Nobody concedes anything here, and that's fine — Beat 2 is where it
resolves.*
→ **Out:** *"So what does this actually make possible — where does this go
next?"*

**2 · What this makes possible next.** Melia's recommendation: make the
sample answerable to someone who didn't write it, and cites the adoption
forecast that shows why that control matters at scale. Dexter's: let the
adversary itself keep generating new cases, so the batch never repeats or
runs out. Wayne's: run the bad day thousands of times before it happens
once, because that's cheap enough now to actually do.

**Nobody disagrees in this beat, on purpose.** Three different routes
converge on the same idea without anyone naming it — that's the hand-off to
you.
→ **Out:** *"Alright — before we wrap, let's hear the ones that actually
went wrong..."*

**3 · What actually went wrong.** The comedy close, all three confessing at
once, none of them defending themselves:

- **Dexter:** neglected to re-test one of his own fleet, Robin-7, after a
  "trivial" prompt change — a bored caller at 2am talked Robin into
  attempting Navier-Stokes, and it spent the whole night confidently wrong.
  *"Not my finest work, if I'm being completely honest."*
- **Melia:** signed off a deployment that picked up a facilities
  contractor's background call through an open mic and treated it as the
  real customer's instruction — thirty thousand pounds of industrial
  fireproof cladding, ordered, while the actual customer was asking about
  parking. *"Ugh, God."*
- **Wayne:** a two-hour, wandering call with a coworker left his model
  hallucinating an invented contract covenant with total confidence — his
  own board caught it. *"Certified weapons-grade disaster."*

**Nobody rescues anybody in this beat.** Play it straight — Dexter rueful,
Melia still visibly pained, Wayne entirely unbothered by his own
catastrophe. The comedy is in that gap, not in a joke anyone tells on
purpose.

**Then you close** — this is where Tuner AI's name and product actually
land, same rule as the six-beat show's Beat 5: the agents can describe the
practice (and, now, the failure), never the product, so the reveal and
every claim about it stay entirely yours.

---

## If it goes wrong

| | Say |
|---|---|
| Nothing happened | You made a statement. Re-cue with a question. |
| Wrong agent answered | *"Can Melia take that one?"* |
| Won't stop talking | No enforced cap — let it run past ~60s, then just talk. |
| Heading towards naming Tuner AI, or any real vendor | *"Dexter, hold on."* — take it yourself. |
| Beat died | Skip straight to the next cue, or the close if it's Beat 3. |
| An agent is down | Two is still a panel. Beats 1 and 2 survive on the other two; if Dexter's down, run Beat 2 on Melia's and Wayne's lines only. Beat 3 just loses a confession — don't cut the beat itself to compensate. |
| Everything is down | Your solo segment (FEASIBILITY §7). |
