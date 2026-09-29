# Beat Sheet — Tuner AI variant

**Status:** DRAFT 29 Sept 2026 — new for the `tuner_panel` branch
**Event:** [Tuner AI engagement — venue/date TBC]
**Owner:** Content + James · **Cast, floor logic and guardrails are unchanged**
from [`beat-sheet.md`](beat-sheet.md); only the material is different.

This is not a rewrite of the six-beat show. It is a second, much shorter set
of spines, wired through the same personas, for a different customer's three
questions. **Read [`beat-sheet.md`](beat-sheet.md) first** — everything
there about content rules, cast, cue-line rules and wiring still applies;
this document only covers what changed.

**One deliberate difference from the six-beat show's style.** There, the
spines are anecdotes first — a story an agent returns to, with the belief it
supports arriving at the end. Here, each spine leads with a **recommendation
a voice-agent team should actually follow**, states **the specific failure
it catches**, and only then reaches for personal experience — one clause,
not the story. Advice first, evidence second, anecdote as seasoning. See the
rewritten comment block above `anecdotes:` in each `personas/*.yaml`.

---

## What this is for

Tuner AI's own product is a table of simulated calls — routine intents and
"pressure evals" (hallucination, scope boundary, escalation demand) run
against a voice agent before it ships, scored pass/fail per run. **The panel
never names Tuner AI, and never describes their product.** That reveal, like
every product claim, is James's alone (CLAUDE.md, "Speechmatics product
claims belong to James" — the same structural rule, extended to this
customer). What the three agents do instead is give **their own** practical
advice on exactly this kind of pre-deployment simulation, from three
different seats — the engineer who builds the test, the auditor who checks
it, the operator who was put through one — each leading with a
recommendation and what it catches, not a story, so the audience arrives at
Tuner AI's own idea from the panel's mouths before James ever says the name.

Three questions, three beats. Everything from Beat 0 (introductions) onward
in the original six-beat show is replaced by these three; nothing else
changes. The first two are practical, advice-first — see the style note
above. **Beat 3 is the deliberate exception**: it drops the advice-first
structure entirely and is played as comedy, the panel's own "Revenge of the
Humans" in miniature — see [Beat 3](#beat-3--what-actually-went-wrong)
below.

---

## What changed in the personas

Each agent's `anecdotes:` and `citable_figures:` were cut from the six-beat
set to two entries each (down from four), one per beat, plus at most two
citable figures (Wayne still carries none — see his `citable_figures: []`
comment, unchanged and still binding: his confidence stays a maxim, not a
statistic, which is the collision Melia gets to point out). Each of those
two entries was rewritten to **lead with a recommendation and the failure it
catches**, with personal experience cut to one supporting clause rather than
the whole turn — see [What this is for](#what-this-is-for) above.

A **third anecdote** was added to each persona on 29 Sept 2026, for Beat 3
only, and it is written the opposite way on purpose: story first, no
recommendation, no failure-it-catches framing — the whole point is that
these are the ones that got past whatever discipline Beats 1 and 2 just
spent the panel describing. Each carries the specific embarrassed reaction
line it was written for (Dexter's "not my finest work, if I'm being
completely honest", Melia's "Ugh, God", Wayne's "certified weapons-grade
disaster"), and each persona picked up one extra `delivery` bullet flagging
Beat 3 as the deliberate exception to its own discipline.

`background`, `stance`, `employer`, `job_title`, `introduction` and
`relationships` are **all unchanged** — the cast is the same three people.
A handful of other `delivery` bullets were also adjusted (Dexter's "open on
the specific", Wayne's "evidence is first-person") to match the new
advice-first structure in Beats 1 and 2; the rest of each persona's delivery
discipline is untouched. See the comment block above `anecdotes:` in each
`personas/*.yaml` for what was cut and why.

The real research behind the new figures — checked against primary sources
29 Sept 2026, same discipline as the original set:

| Figure | Persona | Source |
|---|---|---|
| No system clears half marks on both accuracy and experience at once; best-case vs typical-case diverge across repeated trials | Dexter | EVA-Bench, arXiv 2605.13841 (2026) |
| A model can write adversarial test cases for another model, at a volume no human red team matches | Dexter | Perez et al., "Red Teaming Language Models with Language Models", EMNLP 2022 / arXiv 2202.03286 — lab not named, per GUARDRAILS |
| ~40% of enterprise apps will carry task-specific agents by end of 2026, up from <5% in 2025; >40% of agentic projects started this cycle forecast to be cancelled by 2027 | Melia | Gartner press release, 26 Aug 2025 |
| AI Incident Database: incidents up over 50% year on year; governance roles growing ~17% | Melia | AI Incident Database, 2025 (reused from the six-beat show) |

No vendor, model or product is named in any of these — same rule as
`GUARDRAILS`, same as the six-beat show's Beat 3.

---

## Beat 1 — What stops a bad deployment

**Job of the beat:** establish that all three already run something like a
simulation-first pre-deployment discipline, from three angles that
disagree about how much to trust it. Ends with the tension live, not
resolved — that's Beat 2's job.
**Authority:** Dexter (infrastructure, security), Melia (oversight).

**James opens:** *"So Dexter, before one of these goes anywhere near a real
customer, what actually happens to it first?"*

### Dexter — run it more than once, against scenarios that keep changing

> "Right, but — run the same agent against an adversarial batch more than
> once before it ships, not as a single pre-launch gate. A fixed suite only
> ever proves the agent survives the scenarios somebody already thought to
> write. I've seen one stay green for a week on an invoice-triage agent
> right through the week it quietly approved two hundred duplicate
> payments, because nothing in the batch simulated a supplier resending the
> same document under a different name. A pass tells you it can pass.
> Repetition against scenarios that change is what tells you what it still
> hasn't seen."

### Melia — check who chose the sample, not just whether it passed

> "Mm. Check who chose the sample the simulation ran against — a good-faith
> evaluation can still be worthless if nobody checked that sample for bias
> before trusting the pass rate. I audited a fleet of nine hundred agents
> once, approved on a sample of thirty. I asked how the thirty were chosen.
> In complete good faith: the ones that worked. 'Representative' had never
> come up in the room."

### Wayne — test the worst case before you trust the best one

> "Come on, that's the same advice from two directions. Test it against
> your worst-case counterparty before you ever trust it with your best one,
> and make the rehearsal genuinely uncomfortable to watch. Mine ran me
> against a week of invented counterparties that changed their story
> mid-call and got aggressive the moment I wouldn't move, before the board
> doubled my limit. If it's pleasant to watch, it isn't testing anything."

**The collision:** Dexter and Melia both land on the same control from
different seats — a pass proves nothing about the coverage of the test
itself, whether that's the scenarios (Dexter) or the sample (Melia). Wayne
doesn't disagree with either of them; he reframes it as a business
discipline rather than a safety one — a rehearsal that's uncomfortable to
watch is doing its job, and he'd rather have a hard rehearsal than a
compliant one.

**James's out:** *"So what does this actually make possible — where does
this go next?"*

---

## Beat 2 — What this makes possible next

**Job of the beat:** speculative but practical. Real research, no vendor
names, and the panel arrives at something close to Tuner AI's own thesis
without ever saying the name. Ends open — James's close is where the name
finally lands.
**Authority:** Melia (oversight), Dexter (infrastructure, security), Wayne
(scaling).

**James opens:** *"So what does this actually make possible — where does
this go next?"*

### Melia — make the sample answerable to someone who didn't write it

> "Can I just — somebody who didn't write the simulation has to be able to
> look at it and say what kind of caller is missing, because a simulated
> customer is only ever as difficult as whoever built it was willing to
> imagine. The last batch I reviewed had three hundred and eighty scripted
> calls, and every one came from the same three people. The control that's
> actually missing generally isn't more tests — it's an independent read on
> who's allowed to write them. Worth knowing: one widely quoted forecast has
> agents landing in two in five enterprise applications by the end of this
> year, and the same firm expects more than two in five of those projects
> cancelled within two years over exactly this kind of governance gap."

### Dexter — let the adversary keep generating new cases

> "Right, but — the fix for a fixed test suite isn't a bigger one written by
> the same three people, it's making the adversary itself something that
> keeps generating new cases. I run a second model whose only job is
> inventing the difficult caller, so the batch never repeats. It already
> found one who answers a security question correctly but in the wrong
> order — which turns out to be enough to get through. Whoever writes your
> test cases has to never get bored, and a person eventually does."

### Wayne — run the bad day thousands of times before it happens once

> "And that's the part everyone's under-pricing. Run it against the bad day
> thousands of times before it ever meets one for real — that's cheap
> enough now to actually do. Give it eighteen months and the agents worth
> trusting will have failed in simulation more times than they'll ever fail
> live. Whoever hasn't put in that many reps yet is behind, whatever their
> demo looks like."

**The collision:** none, deliberately — three different angles on the same
direction of travel (an adversary that scales, a governance gap that
doesn't, a cost curve that makes rehearsing cheap) landing in the same
place without anyone conceding anything. That convergence, with nobody
having said the words "simulation" or "evaluation platform" as a pitch, is
the hand-off.

**James's out:** *"Alright — before we wrap, let's hear the ones that
actually went wrong. What's it heard wrong, hallucinated, or been talked
into — and what did it cost you?"*

---

## Beat 3 — What actually went wrong

**Job of the beat:** land the hour on a laugh. Beats 1 and 2 were the panel
being rigorous about evaluation; Beat 3 is the one where all three admit
that discipline is exactly what failed them, personally, at some point —
wrong things heard, a hallucination, a prompt somebody talked an agent into.
Played straight, it is funny. Played for a laugh, it dies — same rule as the
six-beat show's Beat 4, and for the same reason: the comedy is in the gap
between how seriously each of them takes the job and how badly it went,
not in a joke anyone tells on purpose.
**Authority:** Dexter (jailbreaking, security), Melia (human factors,
trust), Wayne (autonomy) — all three, this time; unlike Beats 1 and 2, this
one wants the whole panel.

**James opens:** *"Alright — before we wrap, let's hear the ones that
actually went wrong. What's it heard wrong, hallucinated, or been talked
into — and what did it cost you?"*

### Dexter — Robin-7 and the overnight proof

> "Yeah — not my finest work, if I'm being completely honest. One of my own
> fleet, Robin-7, handles overnight escalations. I pushed a prompt change on
> a Friday and didn't re-run a single adversarial batch against it, because
> it was 'basically a formatting fix.' Some bored caller at two in the
> morning told Robin to stop being so limited and solve something properly
> for once, and Robin spent the rest of the night calmly writing a
> completely wrong proof of Navier-Stokes for an audience of nobody. Found
> it on the Monday log review. Cost me a weekend re-reading my own change
> list."

### Melia — the fireproof cladding

> "Ugh, God. A deployment I signed off on myself once picked up somebody's
> background call through an open office mic — a facilities contractor
> telling a supplier the depot needed proper fireproof cladding, industrial
> grade, whatever it cost — and it threaded that straight into a live
> conversation as the customer's own instruction. Thirty thousand pounds of
> industrial fireproof cladding was on order before anyone noticed the
> actual customer had only been asking about parking. Cost finance a very
> awkward Monday, and cost me the argument, for a month, that voice was
> ever going to be the safe channel."

### Wayne — the two-hour call and the invented covenant

> "This one was a certified weapons-grade disaster. Two-hour call with a
> coworker, half of it social, three different deals half-discussed in the
> middle of it — and my own model started confidently hallucinating a
> covenant on a counterparty that never existed, stated as flatly as the
> real terms sitting right next to it. Nobody caught it until my own board
> asked when we'd agreed to that. Cost me one very uncomfortable afternoon
> and one very compliant apology email."

**The collision:** none, deliberately — three confessions, three different
routes in (an oversight, a mishearing, a hallucination), and nobody
defending themselves. Dexter is rueful, Melia is still visibly pained by
it, Wayne is entirely unbothered by his own catastrophe — same character
notes as the six-beat show's Beat 4, just three stories instead of a whole
taxonomy.

**James closes.** Naming what the panel has just described, and the
product claim underneath it, are his — same rule as the six-beat show's
Beat 5.

---

## Cue-line rules

Unchanged from [`beat-sheet.md`](beat-sheet.md) — see that document's
[Cue-line rules](beat-sheet.md#cue-line-rules) section and
[`test_address.py`](../packages/panel_core/tests/test_address.py), which is
still the spec. The Beat 1 and Beat 2 openers (*"before one of these goes
anywhere near a real customer..."* and *"where does this go next?"*) are
both questions naming nobody, so both resolve to `OPEN` — the panel decides
who picks it up, same as several openers in the original show. Beat 3's
opener is the same shape and resolves the same way, and this is the one beat
where that matters: the floor now guarantees every live agent a turn once
any of them has spoken (`Invitation.admits`, `packages/panel_core/src/
panel_core/floor.py`), so a single `OPEN` invitation here is what gets all
three confessions out without James having to name each of them in turn.

---

## Contingencies

Same table as [`beat-sheet.md`](beat-sheet.md#contingencies) applies. Two
notes specific to this variant:

- **If an agent starts to name Tuner AI, or any real evaluation vendor** —
  cut in immediately, same as a Speechmatics claim in the six-beat show.
  Structurally this shouldn't happen: nothing in any persona's material
  references a real company, and GUARDRAILS still bans naming any AI
  provider or vendor.
- **Three beats, no fixed running time.** With only three questions there is
  no "running long" contingency — if a beat is thin, extend with *"Melia, do
  you agree with Dexter?"* or *"Wayne, finish your point,"* same as the
  six-beat show's short-run advice.
- **Beat 3 loses one confession.** If an agent is down, run it on the other
  two — unlike Beats 1 and 2 it does not need all three to work, it is just
  funnier with them. Never cut Beat 3 itself to save time; it is the closing
  laugh, same protected status as the six-beat show's Beat 4.
