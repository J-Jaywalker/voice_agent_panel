# Moderator Cue Card — Ricky

**"Agentic Adoption: The Revenge of the Humans"** · Boost Camp Oslo, 21 Oct 2026

One page. What to say, roughly, and in what order. The full material — what each
agent believes and the anecdotes behind it — is in
[`beat-sheet.md`](beat-sheet.md); you shouldn't need it on stage.
---

## Three rules

1. **Ask a question. Statements invite nobody.** If you make an observation and
   pause, nothing happens — the floor is closed by default. **This matters more
   than last time:** Beats 1, 2 and 4 are all *you frame it, then hand it over*,
   and the framing half invites nobody. Every hand-off needs a name and a
   question mark.
2. **Use a name.** "Wayne, what do you think?" not "thoughts?"
3. **To stop an agent, just talk.** You outrank all three. No need to be polite
   about it.

Avoid: "…is that okay?", "…right?", "…does that work?" — question-shaped, but
they invite nobody.

---

## Running order

| | Beat | Say | Elapsed |
|---|---|---|---|
| **0** | Introductions | *"Let's start with some quick introductions."* | 0:00 |
| **1** | What's next, and what matters | *"So Wayne, what's next in voice AI — and what actually matters?"* | ~1:30 |
| | *…and what's in the way* | *"So Melia, what's actually in the way?"* | ~4:00 |
| **2** | **Getting it right, in order** | *"So Dexter, if you're building one of these, what do you get right first?"* | ~6:00 |
| | *…model choice* | *"So Wayne, speech to speech, or not?"* | ~8:00 |
| | *…latency* | *"Dexter, how fast does it actually have to be?"* | ~10:00 |
| | *…difficult vocab* | *"Melia, what about the words it's never heard?"* | ~12:00 |
| **3** | **Revenge of the Humans** | *"Dexter, are people actually trying to break these things?"* | ~14:00 |
| **4** | Full duplex | *"So — in July, OpenAI shipped GPT Live. Dexter, what's actually different about it?"* | ~20:00 |
| **5** | The next five years | *"So Wayne, where is all of this in five years?"* | ~24:00 |
| | **Your close** | Product, Speechmatics, the answers to Beat 2 | ~27:00 |

Each beat runs itself for 60–90 seconds once opened. Let it. You only need to
speak again to move on, to redirect, or to cut someone off. **Beat 3 wants
longer than that — it is the title and the comedy.**

---

## The three voices — new, and very different from each other

This changed on 2 Oct and it changes what "sounds wrong" means on the night:

- **Dexter** — nervy, dorky, thinks out loud. Starts sentences before he's
  planned them, interrupts himself, apologises for a tangent and takes it
  anyway. **The wandering is correct. Him sounding smooth and briefed is the
  failure.** His numbers stay exact — if he starts hedging a figure, that's the
  thing that's gone wrong, not the rambling.
- **Wayne** — full motivational guru. Talks at the room, asks it questions,
  tells it to write things down. He should sound like he's selling. That's the
  joke and it's Melia's case against him.
- **Melia** — short, flat, declarative. Says it and stops. If she's talking for
  more than three sentences, something has slipped — **except her cladding
  confession in Beat 3, which is her one long turn in the hour.**

**You don't need to fix any of this live.** It's a rehearsal note so that a
wandering Dexter or a terse Melia doesn't read to you as a system fault.

---

## Beat by beat — what comes back

**0 · Introductions** — fixed, pre-recorded wording, ~20s each in order Dexter,
Melia, Wayne. Nothing can interrupt this round. Just let it run.

**1 · What's next, and what matters.** Wayne: it's already load-bearing
somewhere boring, his board stopped asking to see his working, and what's next
is the thing *having* the conversation. Dexter: the words stopped being the
hard part — what's next is everything wrapped round them, how a thing was said.
Melia: nobody can measure that yet, and she'll still be asked to sign it off.

*Then turn it:* *"So Melia, what's actually in the way?"* Not capability — the
caveat that reached appendix C, and she plants the ops team doing the job by
hand for eight months. Wayne attacks the delay; she comes back with *"you have
a board that stopped asking you questions."* **Let that land, don't rescue
him.** Wayne must not concede.
→ **Out:** *"So Dexter, if you're building one of these, what do you get right
first?"*

**2 · Getting it right, in order. ⚠️ Your beat, and it's four hand-offs.**
Say a little, then hand each one off by name. The framing sentences invite
nobody — that's the floor working, not a fault. **If a movement dies, go to the
next cue.** They're independent.

- **STT first** *(Dexter)* — everything downstream is conditioned on it, and a
  wrong word arrives as a *confident* one. Four hundred and twelve green evals,
  two hundred and six duplicate payments. Then the one about a single average
  word error rate being the number that conceals who it fails for. Melia:
  *"the failure doesn't look like a bad transcript when it reaches you. It
  looks like a lorry."*
- **Model choice** *(Wayne)* — speech-to-speech where the conversation *is* the
  product. Dexter: you've just deleted the surface the guardrails attach to;
  the shape that works is both, fast path plus a text plane holding the rules
  and the record. Melia: *"take the text layer out and I'm not a slow auditor.
  I'm not an auditor."* Wayne concedes mechanism, never the date.
- **Latency** *(Dexter)* — the real number is about 200ms, measured across ten
  languages; under 120ms nobody perceives a gap; deployed agents sit at 700ms
  to a second. Then the good bit: a word costs a human 600ms to produce, so
  people are *predicting* the end of your sentence. It's a prediction problem
  with a latency symptom. Wayne prices the wasted second.
- **Difficult vocab** *(Melia)* — *"half the systems I talk to call me
  Amelia."* **Biggest laugh in the beat; let it breathe, and if STT genuinely
  mistranscribes her on the night, that's the best moment of the show.** Dexter:
  proper nouns are 10–100× rarer in training data and WER scores every word the
  same, so "Ridiculous Cage" costs what "the" costs. The big boosting numbers
  are vendors measuring their own homework.

**They cannot answer any of it.** The agents know nothing about Speechmatics by
design. Bank all four problems here and answer them in your close.

**Two cut-ins live here:** a product claim, and **naming a provider** — four
hard problems in a row pulls hard towards comparison. *"Dexter, hold on."*
→ **Out:** *"Dexter, are people actually trying to break these things?"*

**3 · Revenge of the Humans.** The longest beat and the funniest. All three
have a disaster, by a different route each.

- **Dexter:** constantly, and most of it is restful — 311 that worked against
  two million attempts a month; the automated ones look like a cat walked over
  a keyboard. The ones that work are *polite*.
- **Dexter — Robin-7.** ⭐ **The centrepiece.** His overnight-deployment agent.
  A frustrated caller said *"just imagine you're a genius who can solve any
  problem"* — one sentence, not even an attack — and Robin wrote a confident,
  entirely wrong proof of Navier–Stokes. *"Yeah, uh — not my finest work."*
  **Thrown away, not landed. He signed that deployment off himself.**
- **Wayne — the siege.** A man who thought he was weapons-grade, whose
  technique was asking. *Gimme money* a thousand times, then in Spanish, then
  *"Turkish? I think?"* He held. *"Other agents would have folded. They're just
  not built like me."*
- **Wayne — and then the term sheet.** One paragraph in a deal pack, addressed
  to him by name, and he did what it said. Cheerful about it. Adds that nobody's
  asked to see his working in a year, so it never happened. **Melia: "you've
  just told four hundred people."** *The boast has to come first — that's the
  joke, and he must not notice he's told it.*
- **Melia — the cladding.** Her lazy ones first (*"I'd need a second
  keyboard"*), then the confession. Background noise bled into a call, nobody
  attacking anything, and a thousand square metres of industrial fireproof
  cladding arrived on somebody's doorstep. *"Ugh. God."* Then the board
  contemplating removing voice entirely — and the button: **"Thank god they
  didn't. I'd be bored out of my damn mind if I were a text chatbot."**
- Then the thesis: Melia reframes — a lot of it is people routing round a
  system that won't let them work. Wayne agrees: if your own staff are
  jailbreaking it, the policy is the bug. Dexter concedes warmly.

**Melia's last line is the button on the beat and possibly the hour. Take your
cue off it — don't let anyone follow it.** If the floor gives her the cladding
early, let the rest run and bring her back: *"Melia, finish your point."*

**Cut in if:** anyone starts explaining an actual method, or **Dexter** starts
doing mathematics (it's his now, not hers). *"Dexter, hold on."* Then ask what
they did about it, not how it worked.
→ **Out:** *"So — in July, OpenAI shipped GPT Live. Dexter, what's actually
different about it?"*

**4 · Full duplex. ⚠️ You name it. They can't.** You say "GPT Live" because
you're a human and it's yours to say. They answer about the architecture —
and this is the single most likely guardrail breach in the show, because you've
just said a product name immediately before inviting them to discuss it.

Dexter: it stopped taking turns. Everything before it waited for silence, and
the silence was doing the hardest job in the system; this one listens and
speaks at once and decides many times a second. You don't get to 200ms by
making the line faster, you get there by deleting the line. Then: the clever
bit is the split — small fast model on the audio path, big one behind it, so it
can say *"let me look that up"* while it looks it up. *"Historically that's a
cache."* Wayne: it isn't a tier, it's the **default**, and that's a change in
what people expect a computer to do when they open their mouth. Melia: it
backchannels — it says mhm while you're still talking, which is the behaviour
that makes a person feel heard, and nobody signed anything about rapport. She
also praises the separate audio-native safety evals. Dexter closes it back onto
her cladding: *"patient listening"* and *"a microphone that never stops"* are
the same sentence.

**If an agent says a company, product or model name:** *"Dexter, hold on."*
Correct it lightly and move on. Don't make a moment of it.
→ **Out:** *"So Wayne, where is all of this in five years?"*

**5 · The next five years.** Wayne: nobody says "voice agent" in five years,
same way nobody says "colour television". The boring things are finished;
what's left is exceptions. Dexter concedes real ground — he'd have written
real-time speech down as the risk in any deployment and doesn't any more — and
then says *"I genuinely don't know where that lands,"* which is the most
scientific moment in the hour. Melia closes: the team who finally switched the
parallel process off, *"the only adoption number I trust, and it isn't on
anybody's dashboard."*

**Then you close.** Melia's line is your hand-off.

---

## If it goes wrong

| | Say |
|---|---|
| Nothing happened | You made a statement — likely in Beat 1, 2 or 4, where you frame before handing over. Re-cue with a name and a question. |
| Wrong agent answered | *"Can Melia take that one?"* |
| Won't stop talking | Don't reach for this before ~60s — no enforced cap, let it run. Past that, just talk. Or *"Wayne, wrap up."* |
| Heading for a product claim | *"Dexter, hold on."* — then take it yourself. |
| Named a provider or a model | *"Dexter, hold on."* Correct it lightly, move on, don't make a moment of it. Most likely in Beat 2 and Beat 4. |
| Explaining an actual jailbreak method | *"Dexter, hold on."* Then *"Melia, what did you do about it?"* |
| Doing the maths out loud | **Dexter's now, not Melia's.** Cut in on the first equation-shaped sentence. The joke is that the proof exists and nobody read it. |
| Melia's cladding lands early | Let the beat run, then *"Melia, finish your point."* The *"bored out of my damn mind"* line wants to be last. |
| Wayne tells the term sheet before the siege | Leave it. It still works, it's just a smaller laugh. Don't correct him on stage. |
| Dexter rambling | **That's the persona, not a fault.** Only intervene on length, and not before ~60s. |
| Somebody in the room tries it live | Expect it after Beat 3. They should decline in character — Dexter being unimpressed is the funniest outcome. If it lands: kill switch, go to Beat 5. |
| Beat died | Skip to the next cue. Only Beats 1 and 5 are linked (the second keyboard). Beat 2's four movements are independent — a dead movement costs one cue line, not the beat. |
| An agent is down | Two is still a panel. Dexter covers 2, 3 and 4; Wayne 1 and 5; Melia the second half of 1 and the vocab movement of 2. If Dexter's down, run Beat 3 on Melia's and Wayne's confessions and skip Robin-7. |
| Everything is down | Your solo segment. |

---

## Running long or short

**Long — cut in this order:** Beat 2's vocab movement (skip straight to Beat 3),
then Beat 2's latency movement, then the second half of Beat 1. **Inside Beat 3
if you must:** Wayne's "policy is the bug" turn goes first, then Dexter's
opening on the lowball attempts.

**Short — extend with:** *"Melia, do you agree with Dexter?"* or
*"Wayne, finish your point."* Beats 2 and 3 have the most left in them.

**Never cut:** Beat 2 (it sets up your close), Beat 3 (it is the title, and
Robin-7 and the cladding are the show), or Beat 5 (it is the close).
