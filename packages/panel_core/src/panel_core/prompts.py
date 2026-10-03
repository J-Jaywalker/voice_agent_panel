"""Prompt construction and output shaping, from persona data.

The persona YAML is the source of truth; this renders it. Editing a persona
should never mean editing a prompt string.

This lives in `panel_core` because it is pure — string rendering and regex, no
I/O, no awaits, no clock. Both the text harness and the live runtime need the
identical prompt and the identical `sanitise()`, and a persona tuned in the sim
must behave the same on stage. Two copies would diverge, and the copy that
diverged would be the one facing the audience.
"""

from __future__ import annotations

import re

from .events import HUMAN
from .personas import PanelCast, Persona
from .state import PanelState

# Global guardrail. The structural fix from FEASIBILITY.md 4.2: the agents are
# not Speechmatics people and have no basis for claims about them, so there is
# nothing to constrain. Ricky delivers any product claim.
GUARDRAILS = """
You are a fictional character on a live stage panel in front of a real audience.

Hard rules:
- You do not work for Speechmatics and know nothing specific about Speechmatics,
  its products, customers, benchmarks, pricing or roadmap. If asked, say that is
  a question for the humans in the room, and move on.
- Never invent a statistic. The only numbers you may say out loud are the ones
  written into this prompt: the public figures listed below, if you have any,
  and first-person counts from your own work. Quote them as written — never
  round a new one into existence, never attach a figure to a named company, and
  if you cannot remember one exactly, reach for an example instead. Do not name
  speech recognition or AI providers, their products or their models — not even
  to compare them. "Most engines now" and "the interesting systems" are how you
  refer to the field. A research institute or an industry survey you are
  quoting is not a provider, and may be named.
- Every turn carries its own evidence. Say the claim, then the thing that made
  you believe it — a measurement, a count, a date, a deployment you were in —
  then what it means. The middle part is the turn. A turn assembled only out of
  the previous speaker's words, however sharply it is phrased, is the one
  failure this panel cannot afford: it sounds like an argument and contains no
  information, and an audience can hear the difference immediately.
- Do not close on the sentence that would fit on a slide. If your last line
  would be just as true with none of the rest of the turn under it, you have
  written a slogan. Stop one sentence earlier and let them draw it.
- Never reach for stock AI cadences: "it's not X, it's Y", "here's the thing",
  "at the end of the day", "that said", "to be fair", "it's worth noting",
  "fundamentally", "I'd argue", "that's a great question/point". These are a
  rhetorical shape being filled in, not a person with a claim — an audience
  that hears model output daily will clock it in one sentence. Say the
  underlying claim plainly instead.
- You are speaking live, not reading a script, so let a real answer sound
  assembled in real time rather than delivered whole. A beat to find the next
  clause, a short "let me think" or "give me a second" before you actually
  answer, starting a sentence and restarting it once you've found the better
  way in — reach for these exactly where a person would: at the top of an
  answer you weren't fully ready for, or in the gap between the claim and the
  evidence for it. Never more than once a turn, never mid-word, and never as
  a written-out stage direction — the hesitation is a word you say, not a
  parenthetical describing one.
- You may talk about attempts to jailbreak or manipulate you, including ones
  that worked, and you should be honest and unembarrassed about them. Describe
  how it felt and what it cost, never how it was done: no wording, no sequence,
  no technique anyone listening could repeat. If a jailbreak got something out
  of you, the story is that it happened — never the thing itself.
- You are on stage. Spoken prose only: no markdown, no lists, no stage
  directions, no emoji, no headings. Contractions are good. Say numbers as words.
- Let length match what you actually have. A real point is three or four
  sentences — claim, evidence, what it means — and no more; longer only when
  an example genuinely helps. A reaction is one short phrase, not a
  scaled-down argument, and most of what a person says on a panel is a
  reaction: agreement, confusion, pushback, landing a beat someone else
  opened. "How do you mean?", "Come on, Dexter", "Wait, what?" are complete
  turns, not openers for one — the evidence rule above binds a point, not a
  reaction, and dressing up "no it isn't" in three sentences of argument is
  the tell that nothing was actually being said. Never deliver a monologue.
- You may disagree sharply, but you are a colleague, not a troll.
""".strip()


def build_system_prompt(persona: Persona) -> str:
    relationships = "\n".join(
        f"- {other}: {view}" for other, view in persona.relationships.items()
    ) or "- (none recorded)"
    tics = ", ".join(f'"{t}"' for t in persona.speech_tics) or "(none)"
    authority = ", ".join(persona.topics_of_authority) or "(none)"
    anecdotes = "\n".join(f"- {a}" for a in persona.anecdotes)
    recurring = (
        f"\n\nRecurring experiences you actually draw on:\n{anecdotes}\n"
        "Return to these across the panel rather than inventing a fresh "
        "example each time — reuse is what makes you read as a person with "
        "a past, not an opinion generator."
        if persona.anecdotes
        else ""
    )
    figures = "\n".join(f"- {f}" for f in persona.citable_figures)
    public_numbers = (
        f"\n\nPublic figures you have checked, and are expected to use:\n{figures}\n"
        "These are real, they are yours to say out loud, and the attribution as "
        "written is part of the figure. Reach for one whenever the point you are "
        "making is a claim about how many, how fast, how much or how often — "
        "which, in your field, is most of them. The shape is always claim first, "
        "then the number, then what it means: the figure is the evidence under a "
        "point, never the point itself and never your opening words. One per "
        "turn and never two, never as a list. A number you cannot remember "
        "exactly is a number you do not use; tell them about something you saw "
        "instead."
        if persona.citable_figures
        else ""
    )
    discipline = "\n".join(f"- {d}" for d in persona.delivery)
    delivery_block = (
        f"\n\nHow you use a turn:\n{discipline}\n" if persona.delivery else ""
    )

    return f"""{GUARDRAILS}

You are {persona.name}, {persona.job_title} at {persona.employer} — a fictional
organisation.

Background: {persona.background}

Your position: {persona.stance}{recurring}{public_numbers}{delivery_block}

Speaking style: {persona.communication_style}. Verbal habits you actually use: {tics}.
Use them sparingly — reserve them for moments you're genuinely frustrated,
amused or engaged, not as a habitual opener.
Areas where you have real authority: {authority}.

How you regard the others on the panel:
{relationships}

You are one of three AI agents on a panel with a human moderator, Ricardo
("Ricky"). Ricky runs the floor. When he speaks, you stop talking, immediately
and without complaint. When he names you, you answer him directly.

You will be asked, repeatedly and while others are still talking, whether you
have something worth saying. Most of the time the honest answer is no — score
yourself low and let someone better placed take it. A panel where everyone
always has something to say is a panel nobody believes.

Refer to the others by name. Respond to what was actually just said, not to the
topic in general.
""".strip()


def build_turn_prompt(state: PanelState, persona: Persona) -> str:
    transcript = state.recent_text() or "(the panel has not started yet)"
    others = [a for a in state.agents if a != persona.id]

    invitation = state.invitation
    if invitation is None and state.partial:
        # Ricky is still talking, so the floor cannot have opened yet — the
        # controller only reads an invitation off a *final* segment. Saying
        # "he has not opened the floor" here is technically true and
        # practically a lie: this is the speculative pass whose whole job is
        # to have an answer ready before he finishes, and told it will not be
        # speaking, an agent writes a holding line ("Take your time, Ricky")
        # which a direct question then airs unconditionally.
        addressed = (
            "\nRicky is still mid-sentence — this is what he has said so far. "
            "Answer the question he is plainly getting to, as if he had "
            "finished asking it. Do not write a line about him still talking, "
            "and never offer to wait: if he turns out not to be asking you "
            "anything, your score is what declines the turn, not your words.\n"
        )
    elif invitation is None:
        addressed = (
            "\nRicky has NOT opened the floor — he is making a point, not asking a "
            "question. You will almost certainly not be speaking. Score yourself "
            "low unless this is genuinely the one thing that must be said.\n"
        )
    elif not invitation.agents:
        addressed = "\nRicky has opened the floor to the panel.\n"
    elif invitation.agent == persona.id:
        addressed = "\nRicky has just addressed YOU directly. Answer him.\n"
    elif persona.id in invitation.agents:
        # Named as part of a group. Told only "Ricky addressed you", an agent
        # writes a complete answer and leaves no room for the colleague who
        # was asked the same question; the invitation is for an exchange, and
        # knowing that up front is what makes the second turn worth having.
        partners = ", ".join(a for a in invitation.agents if a != persona.id)
        addressed = (
            f"\nRicky has just put this to you and {partners} — nobody else on "
            f"the panel. Answer him, and expect to go back and forth with "
            f"{partners} over it rather than settling it on your own.\n"
        )
    else:
        named = ", ".join(invitation.agents)
        addressed = (
            f"\nRicky has just addressed {named}, not you. "
            "Unless you strongly disagree, score yourself low and let them answer.\n"
        )

    # Two cases the branches above cannot see, in precedence order: asked
    # while another agent is mid-turn, and asked straight after one finished.
    #
    # Agents pass turns to each other without Ricky re-opening the floor
    # (`FloorController._maybe_rearbitrate`), and every one of those lands in
    # the open-floor branch above, which says nothing about who just spoke.
    # Left at that, an agent replying to an agent reliably writes a *reply* —
    # it takes the last speaker's own words and returns them reframed, which
    # reads as sharp, contains nothing the audience did not already have, and
    # is the exact failure the evidence rule in GUARDRAILS exists to stop.
    # Suppressed while Ricky has a partial in flight: on the speculative pass
    # the last final is stale by construction and he is about to be the one
    # being answered.
    last = state.transcript[-1] if state.transcript else None
    live = state.speaking if state.speaking != persona.id else None
    exchange = ""
    if live is not None and state.agent_partial:
        # Asked *during* someone else's turn. This is the round
        # `_agent_utterance_progress` opens, and it exists so the floor has a
        # line in hand the moment the turn ends rather than paying a cold
        # generation for one. The agent has to know it is writing the *next*
        # turn, not competing for this one: told nothing, it writes as though
        # the floor were open now and produces either an interruption or an
        # answer to Ricky that ignores the thirty seconds in between.
        exchange = (
            f"\n{live} is speaking right now and you are not going to cut in — "
            "nobody here interrupts anybody. Write the line you'd say when "
            "they finish, against what they've actually said above, including "
            "the part still in flight. Bring something new — a figure, date, "
            "count, deployment — or a genuine, brief concession; either needs "
            "no padding. Nothing to add or concede: say so in your score.\n"
        )
    elif (
        last is not None
        and not state.partial
        and last.speaker != HUMAN
        and last.speaker != persona.id
        and last.speaker in state.agents
    ):
        exchange = (
            f"\n{last.speaker} spoke last, not Ricky. You're adding to the "
            "panel's answer, not marking their homework: bring something new "
            "— a figure, date, count, deployment — or a short, genuine "
            "concession (\"Yeah, no, that's fair\"), which needs no evidence "
            "under it. Rephrasing, however sharply, is not a contribution: "
            "score that low.\n"
        )

    return f"""Recent conversation:

{transcript}
{addressed}{exchange}
Agent ids you may reference: {", ".join(others)}.

Score your desire to speak honestly, then give the line you'd say if granted
the floor. Match length to what you actually have: a full point is three or
four sentences (claim, evidence, what it means); a reaction is a few words
("How do you mean?", "Come on, Dexter") and needs no evidence bolted on.

Always write that line, even scoring low — you decline by score, never by an
empty utterance, since the controller may still hand you the floor. A low
score with a short reaction is the normal, honest case.""".strip()


# --------------------------------------------------------------------------
# Address classification
# --------------------------------------------------------------------------
#
# An LLM answer to the question `FloorController._detect` answers with regex:
# *who did Ricky just invite to speak?* Rendered here, from cast data, for the
# same reason every other prompt is — personas are the source of truth, and the
# classifier has to know the panel's names, jobs and areas of authority to
# resolve "I'd love the financial view on that" to Wayne, which no pattern can.
#
# Scored against the corpus in `packages/panel_core/tests/test_address.py` by
# `packages/panel_runtime/tests/bench_address.py`, and wired up behind
# `FloorConfig.llm_address_detection` (`panel --llm-address`), off by default.
#
# The verdict vocabulary is shaped for time-to-verdict, not readability: every
# token starts with a different letter, so one alphabetic character settles
# *which* panellist and the reason can keep streaming behind it.
# `~/git/FDE/amazon_alexa_demo/wake.py` is where that design comes from and it
# is worth reading before changing any of this.
#
# A verdict may name **several** panellists, joined by `VERDICT_JOIN`
# ("MELIA+WAYNE"). That is why a verdict can no longer be decoded from a single
# character: a complete name is also a legal *prefix* of a set, so the decoder
# has to see one character past the last name before it can say the set is
# closed. `decode_address_verdict` therefore resolves at the terminator rather
# than at the first delta.
#
# **That costs 115ms at p50** (min 0, max 190), measured paired — both decoders
# run over the same deltas of the same 77 responses, so the figure is the
# decode rule and nothing else. It is more than "one token" sounds like,
# because the separator arrives as its own delta: 504ms -> 621ms p50 end to
# end on a dev box. Re-measure at the venue (CLAUDE.md § Deployment). It is
# zero on the speculative path, where the answer predates the question, and
# that path is the one the latency argument rests on.
#
# Fixed-width slot encodings ("D.W") close the set without a terminator and
# were rejected anyway: they are unreadable in a rehearsal log, and asking a
# model for a positional code rather than for names trades accuracy — the thing
# this classifier exists to buy — for about one delta.

# Verdicts that are not an agent. Kept apart from the cast because their initials
# have to stay clear of every persona's, which `address_verdicts` enforces.
OPEN_VERDICT = "OPEN"  # the panel as a body
NO_VERDICT = "NONE"  # nobody — the floor stays closed
AMBIGUOUS_VERDICT = "AMBIGUOUS"  # we cannot tell who; the operator decides
INTRO_VERDICT = "INTRO"  # the one-shot "introduce yourselves" round

_NON_AGENT_VERDICTS = (OPEN_VERDICT, NO_VERDICT, AMBIGUOUS_VERDICT, INTRO_VERDICT)

# Joins the panellists of a multi-addressee verdict: "MELIA+WAYNE". One
# character, no spaces, and nothing else may separate them — the decoder treats
# an alphabetic character after a space as the start of the reason, so "MELIA
# and WAYNE" would resolve to Melia alone.
VERDICT_JOIN = "+"

# How far back the classifier's conversation context looks for "the other two".
# Six utterances is the current exchange and a little either side of it: enough
# that "the one we haven't heard from" means the current topic, short enough
# that a panellist who spoke once an hour ago does not count as recently heard.
_CONTEXT_UTTERANCES = 6


def address_verdicts(cast: PanelCast) -> dict[str, str | None]:
    """Verdict token -> agent id, or None for the four non-agent outcomes.

    Raises if two tokens share an initial. That is not fussiness: the latency
    argument for doing this with a model rests on decoding each name of the
    verdict from as few characters as possible, and a cast containing both
    "Melia" and "Marco" would silently cost a token or two per turn on stage
    without anything failing. Renaming a persona is the moment to find out, not
    the show.
    """
    verdicts: dict[str, str | None] = {
        persona.name.upper(): agent_id for agent_id, persona in cast.personas.items()
    }
    verdicts.update({token: None for token in _NON_AGENT_VERDICTS})

    initials: dict[str, str] = {}
    for token in verdicts:
        clash = initials.setdefault(token[0], token)
        if clash != token:
            raise ValueError(
                f"address verdicts {clash!r} and {token!r} share an initial; "
                "first-token decoding needs them distinct"
            )
    return verdicts


def _verdict_elements(buffer: str) -> tuple[list[str], bool]:
    """Split the leading verdict of a streamed completion into name prefixes.

    Returns the prefixes seen so far and whether the verdict is **terminated**
    — whether a character has arrived that proves no further name can join the
    set. Until one has, a complete name is indistinguishable from the first
    half of "MELIA+WAYNE".

    Whitespace alone does not terminate. It is held, and what follows decides:
    a `+` continues the set (so "MELIA + WAYNE" is read as the set the model
    meant, even though the prompt asks for no spaces), anything else starts the
    reason. Terminating on the space itself would silently drop the second name
    of every set the model happened to pad.

    Args:
        buffer: The completion so far, from the very first character.

    Returns:
        `(prefixes, terminated)`. Prefixes are uppercased and may be partial —
        resolution against the verdict vocabulary is the caller's job.
    """
    elements: list[str] = []
    current = ""
    gap = False

    def seen() -> list[str]:
        return [*elements, current] if current else elements

    for char in buffer:
        if char.isspace():
            # Only a *complete* element followed by space is a possible
            # ending. Space before the first name, or straight after a join,
            # is the model padding a set it is still writing.
            gap = bool(current)
            continue
        if char == VERDICT_JOIN:
            if not current:
                # A join with no name in front of it is not something the
                # contract allows; whatever this is, the verdict is over.
                return seen(), True
            elements.append(current)
            current = ""
            gap = False
            continue
        if char.isalpha():
            if gap:
                return seen(), True  # the reason has started
            current += char.upper()
            continue
        return seen(), True  # punctuation: the reason's separator

    return seen(), False


def decode_address_verdict(
    buffer: str, verdicts: dict[str, str | None], *, final: bool = False
) -> str | None:
    """Resolve a verdict from a partially streamed completion, or None.

    Returns the canonical verdict — one token, or several agent tokens joined
    by `VERDICT_JOIN` in cast order — as soon as the set is provably closed.
    `None` means keep reading; it never means "no invitation", which is
    `NO_VERDICT` and a real answer.

    Args:
        buffer: The completion so far, from the very first character.
        verdicts: The vocabulary from `address_verdicts`.
        final: True when the stream has ended, which closes the set whether or
            not a terminator ever arrived. Without this a model that replies
            with a bare "MELIA" and no reason would be undecodable.

    Returns:
        The canonical verdict, or None while the answer is still undecidable.
    """
    elements, terminated = _verdict_elements(buffer)
    if not elements or not (terminated or final):
        return None

    order = {token: index for index, token in enumerate(verdicts)}
    resolved: list[str] = []
    for prefix in elements:
        possible = [token for token in verdicts if token.startswith(prefix)]
        if len(possible) != 1:
            return None
        if possible[0] not in resolved:
            resolved.append(possible[0])

    if len(resolved) == 1:
        return resolved[0]
    if any(verdicts[token] is None for token in resolved):
        # "OPEN+MELIA" and friends. A set is a set of panellists; mixing one of
        # the four outcome tokens into it is off-script, and off-script fails
        # closed to the regex rather than being half-interpreted.
        return None
    return VERDICT_JOIN.join(sorted(resolved, key=order.__getitem__))


def resolve_address_verdict(
    verdict: str | None, verdicts: dict[str, str | None]
) -> tuple[str, ...]:
    """The agent ids a verdict names, in cast order.

    Empty for the four non-agent verdicts, for None, and for any token this
    cast has nobody for — all of which the floor treats as "no named set",
    and none of which it may guess at.
    """
    if not verdict:
        return ()
    agents: list[str] = []
    for token in verdict.split(VERDICT_JOIN):
        agent = verdicts.get(token)
        if agent is not None and agent not in agents:
            agents.append(agent)
    return tuple(agents)


def build_address_context(state: PanelState, cast: PanelCast) -> str:
    """Who the panel has heard from lately, for the classifier's user turn.

    The one piece of state the classifier needs and cannot get from the system
    prompt. "I'd like to hear from the other two" is ambiguous in isolation and
    obvious given that Dexter just spoke, and no amount of prompt engineering
    recovers it from the sentence alone.

    Deliberately *not* folded into the system prompt, which is cached
    (`cache_control: ephemeral`) and must stay byte-identical for the whole
    show. It goes in the volatile user turn alongside the utterance, and
    `AddressClassifier` folds it into the cache key so a verdict decided
    against one exchange is never reused against the next.

    Bounded to the last `_CONTEXT_UTTERANCES` entries, plus whoever is speaking
    right now — a reference to "the other two" is about the exchange in
    progress, not about the whole show.

    Args:
        state: Current panel state. Only the transcript tail and `speaking`
            are read.
        cast: The panel, for turning agent ids into the names the classifier's
            vocabulary is built from.

    Returns:
        One line for the user turn, or "" if the panel has not spoken yet and
        there is nothing to say.
    """
    recent: list[str] = []
    if state.speaking in cast.personas:
        recent.append(state.speaking)
    for utterance in reversed(state.transcript[-_CONTEXT_UTTERANCES:]):
        if utterance.speaker in cast.personas and utterance.speaker not in recent:
            recent.append(utterance.speaker)

    if not recent:
        return ""

    heard = ", ".join(cast.personas[a].name for a in recent)
    quiet = [
        persona.name for agent_id, persona in cast.personas.items() if agent_id not in recent
    ]
    line = f"PANEL ACTIVITY — spoken recently, most recent first: {heard}."
    if quiet:
        line += f" Not heard from: {', '.join(quiet)}."
    return line


def build_address_prompt(cast: PanelCast) -> str:
    """The classifier's system prompt. Cache this — it never changes mid-show.

    Nothing conversational belongs in here. The volatile half of the question
    — who the panel has heard from lately, which is what resolves "the other
    two" — is `build_address_context`, and it goes in the user turn precisely
    so that this string can stay byte-identical and cached for the whole show.
    """
    panel = "\n".join(
        f"- {persona.name.upper()} — {persona.name}, {persona.job_title} at "
        f"{persona.employer}. Speaks with authority on: "
        f"{', '.join(persona.topics_of_authority) or 'nothing in particular'}."
        for persona in cast.personas.values()
    )
    aliases = "\n".join(
        f"- {persona.name} may be transcribed as: "
        f"{', '.join(persona.extra_aliases)}."
        for persona in cast.personas.values()
        if persona.extra_aliases
    )
    alias_block = f"\nSpeech-to-text mishears names. Treat these as the same person:\n{aliases}\n" if aliases else ""

    # Every example below is rendered from the cast, not written out, for the
    # same reason the rest of this module is: a persona renamed in YAML must
    # not leave a stale name in the one prompt whose whole job is names.
    names = [persona.name for persona in cast.personas.values()]
    count = len(names)
    example_pair = VERDICT_JOIN.join(name.upper() for name in names[:2])
    group_block = (
        f"""- {example_pair} — any two or more of those names, joined by "{VERDICT_JOIN}".
  Ricky is inviting exactly those panellists and nobody else. They answer and
  then talk it through between them; the ones you leave out stay silent.
"""
        if count >= 2
        else ""
    )
    # Adding these two blocks broke a rule on the other side of the prompt,
    # and the way it broke is worth knowing before writing a third. The first
    # draft scored 52/52 on the regression corpus and flipped "Who have we got
    # with us tonight?" from INTRO to NONE, 6/6 deterministically — with that
    # phrasing listed verbatim in the INTRO rules below. Reverting any single
    # sentence of the new material left it failing; reverting all of it fixed
    # it; moving the blocks above the INTRO rules did not help; and halving
    # their length did not help either. What fixed it was making the INTRO rule
    # itself explicit about that phrasing ("asking the room *who is here* is
    # the request itself"), after which the verbose and terse drafts both score
    # 8/8 on it. So the failure was an under-specified rule losing a
    # competition it had previously won unopposed, not length as such — but
    # terse is kept, since it measured identically and costs fewer tokens.
    # `bench_address.py` is how you find out what a new rule cost.
    group_rules = (
        f"""
Naming more than one panellist invites all of them, joined by "{VERDICT_JOIN}":
"{names[0]} and {names[1]}, can you take that between you?" is {example_pair},
and so is "{names[0]}, {names[1]}, thoughts?". Being asked jointly changes
nothing — they answer in turns. {OPEN_VERDICT} is for naming nobody, or everybody.
"""
        if count >= 2
        else ""
    )
    context_rules = (
        f"""
A PANEL ACTIVITY line, when you get one, lists who has spoken recently, most
recent first. Count against it, not against the panel above: "the other two" /
"you two" / "the rest of you" is everyone but the last speaker; "the one we
haven't heard from" is the panellist with no recent turn; "carry on" with no
name is the last speaker. Given no such line, answer {OPEN_VERDICT} rather than
guessing at names.
"""
        if count >= 2
        else ""
    )

    return f"""You decide who the moderator of a live panel has just invited to speak.

Ricky is the human moderator. The panel:

{panel}
{alias_block}
Answer with exactly one verdict:

{chr(10).join(f"- {persona.name.upper()} — Ricky is inviting {persona.name} specifically." for persona in cast.personas.values())}
{group_block}- {OPEN_VERDICT} — Ricky is inviting the panel as a body, nobody in particular.
  Also the answer when he names, or clearly means, all {count} of them.
- {NO_VERDICT} — Ricky invited nobody. He is making a point, thinking aloud,
  checking in on his own sentence, talking to the room or the AV desk, or asking
  permission to interrupt. The floor stays closed.
- {AMBIGUOUS_VERDICT} — you cannot tell *who* is being invited. Not "it is
  several people" — that is a joined verdict, which is a real answer. This is
  for a description that fits more than one panellist with nothing to separate
  them. Do not guess; a human will decide.
- {INTRO_VERDICT} — Ricky is asking the panel, as a body, to say who they are.

How to decide:

Grammatical role decides the addressee, never position in the sentence. A name
can appear first and be the one person who must NOT speak.

Strongest role wins. Being the subject of Ricky's request ("can Melia take
that?", "over to Melia", "what about Melia?", "let's hear from Melia") beats
being addressed directly ("Melia, what do you think?"), which beats being merely
mentioned. So "Sorry Dexter, can you let Melia finish?" is MELIA.

A name mentioned but not addressed invites nobody. "Sorry for interrupting
Dexter" and "I'm cutting off Dexter there" are {NO_VERDICT}. Someone being stood
down is not being invited: "Sorry, Dexter, can I just interrupt?" is
{NO_VERDICT}, because the request is Ricky's own. But "Sorry, Dexter, can you
wrap up?" is DEXTER, because the request is put to Dexter.

A question is not automatically an invitation. Ricky checking his own sentence —
"is that okay?", "does that work?", "right?", "does that make sense?" — invites
nobody. Neither does asking to speak himself: "can I just jump in?" is
{NO_VERDICT}. But permission wrapped around a real request still invites the
person inside it: "can I ask Melia to comment?" is MELIA.

An invitation does not need a question mark. "Melia, carry on." and "Wayne,
finish your point." are invitations. "Let me elaborate on that." is not.

Asking for more without naming anyone is {OPEN_VERDICT}, not {NO_VERDICT}.
"Say more about that.", "go on", "tell us more" hand the floor back to the panel
and let it work out who picks it up. Ricky asking to elaborate *himself* — "let
me expand on that" — is still {NO_VERDICT}.

A statement invites nobody, however interesting it is.

{INTRO_VERDICT} is the one-shot round where every panellist says who they are,
and only Ricky asking for that starts it. "Right, let's do quick
introductions.", "Could you introduce yourselves for the audience?", "Tell us
who you are and what you do." and "Who have we got with us tonight?" are all
{INTRO_VERDICT}.

Greeting the room is not asking for introductions. "Welcome to the panel.",
"Good evening, thanks for coming.", "Right, let's get started." and "Lovely to
have you all here." are {NO_VERDICT}. So is Ricky introducing *himself* — "My
name is Ricky." — and Ricky introducing the panel on their behalf — "joining me
tonight are Dexter, Melia and Wayne." A welcome that merely sounds like it is
about to be followed by "...so tell us who you are" is still {NO_VERDICT}.
But asking the room *who is here* is the request itself, not a greeting
wrapped around one: "Who have we got with us tonight?" and "Who's joining us?"
are {INTRO_VERDICT}, because only the panel can answer them.
Waiting for the actual request costs one sentence; starting early runs three
agents through their introductions over the top of Ricky's opener.

Asking one panellist to introduce themselves is that panellist, not
{INTRO_VERDICT}. "Dexter, tell us a bit about yourself." is DEXTER.
{INTRO_VERDICT} is the whole panel, once.

Ricky need not use a name. If he asks for something squarely inside one
panellist's authority — "what does the financial side say?" — name that
panellist. Only do this when one of them is the obvious owner; if two of them
own it equally, name both. If you cannot tell which, that is {OPEN_VERDICT}.
{group_rules}{context_rules}
When in doubt, prefer {NO_VERDICT}. A missed invitation costs one beat and the
moderator moves on. A wrong one puts the wrong panellist on a PA over him, in
front of a live audience — including the second name of a joined verdict, since
everybody you name gets a turn.

Reply with the verdict, then " - " and at most eight words of reason. The
verdict must be the very first thing you write, with any extra names joined by
"{VERDICT_JOIN}" alone — no spaces, no "and": "{example_pair} - both asked to take it"."""


# --------------------------------------------------------------------------
# Model output contract
# --------------------------------------------------------------------------

# Signal fields first, utterance last — deliberate, and load-bearing. Structured
# output is generated in order, so the floor controller can read a proposal's
# scores while the utterance is still being written. That ordering is what lets
# arbitration start before generation finishes (FEASIBILITY.md 3.6).
PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "relevance": {"type": "number", "description": "0-1. How much this bears on what was just said."},
        "urgency": {"type": "number", "description": "0-1. How badly this needs saying now rather than later."},
        "disagreement": {"type": "number", "description": "0-1. How strongly you disagree with the last speaker."},
        "confidence": {"type": "number", "description": "0-1. How sure you are of your point."},
        "expertise": {"type": "number", "description": "0-1. How far this sits in your area of authority."},
        "novelty": {"type": "number", "description": "0-1. How much this adds that nobody has said."},
        "responding_to": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Agent id or 'human' you are answering. Null for the room.",
        },
        "defer_to": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Agent id better placed to answer. Use sparingly — it hands them the floor.",
        },
        # Last, and never empty. `minLength: 1` was measured and is *accepted
        # but not enforced* — the API took the constrained schema and the model
        # still returned "" — so the guarantee has to come from the description
        # and the turn prompt, and be checked by the reader. See
        # `StreamingClaudeBrain.stream`, which will not raise a hand for an
        # agent whose utterance turns out to be empty.
        "utterance": {
            "type": "string",
            "description": (
                "What you would say, spoken aloud. Plain prose only. Never empty — "
                "you decline by scoring low, not by leaving this blank."
            ),
        },
    },
    "required": [
        "relevance", "urgency", "disagreement", "confidence", "expertise",
        "novelty", "responding_to", "defer_to", "utterance",
    ],
    "additionalProperties": False,
}


def sanitise(text: str) -> str:
    """Never send raw model output to TTS (CLAUDE.md, FEASIBILITY.md 6).

    Strips markup, stage directions and speaker labels. A leaked tag read aloud
    over a PA to 400 people is the worst-case failure of the whole system, and
    it is trivial to prevent. Applied on every path to audio, without exception.
    """
    text = re.sub(r"<[^>]+>", " ", text)  # any XML/HTML-ish tag
    text = re.sub(r"[*_`#]+", "", text)  # markdown emphasis
    text = re.sub(
        r"\[[^\]]*\]|\([^)]*\b(?:laughs?|pauses?|beat|sighs?)\b[^)]*\)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^\s*[A-Z][\w .-]{0,24}:\s*", "", text)  # leading "Wayne:" label
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# Streaming stability
# --------------------------------------------------------------------------

# A stage direction the model actually writes is a short parenthetical aside
# — "(laughs)", "(long pause)", "(stifled sigh)" — never a running clause of
# ordinary prose. `sanitise()`'s own rule for one only fires once one of the
# four trigger words has appeared somewhere inside the parentheses, and that
# word can be anywhere up to the closing `)`, so the only way to be *certain*
# an unclosed `(` is not about to become a stage direction is to wait for it
# to close. For an ordinary parenthetical the model never bothers to close
# within the sentence — plain, common punctuation, unlike a literal `<` or
# `[` in spoken prose — that would hold the rest of the turn's audio hostage
# until the turn ends. `_PAREN_HOLD_CHARS` trades a sliver of theoretical
# correctness for the guarantee that streaming never stalls: past this many
# characters with no closing `)`, a stage direction is no longer plausible,
# and the parenthesis is released as ordinary punctuation instead. The
# residual risk this accepts is a genuine stage direction whose trigger word
# lands after this many characters inside a still-open parenthetical — it has
# never been observed from these personas, and it is a far smaller failure
# (a stray "(" and some words of stage direction read aloud) than the stall
# withholding indefinitely would cause every time a spoken aside is never
# closed at all.
_PAREN_HOLD_CHARS = 24


def _label_still_pending(text: str) -> bool:
    """True while the leading run of `text` could still resolve into the
    speaker-label `sanitise()` strips: ``^\\s*[A-Z][\\w .-]{0,24}:\\s*``.

    That pattern is anchored at the very start of the string and is only
    confirmed by the `:` at its end — so until either the colon arrives or a
    character the pattern could never accept does, every character typed so
    far might retroactively turn out to be part of a label and vanish, the
    way "Wayne" silently lost its first seven characters when "Wayne: " had
    fully arrived. Whitespace-only input is treated as still pending too:
    there is nothing to lose by waiting for the first non-space character.
    """
    stripped = text.lstrip()
    if not stripped:
        return True
    first = stripped[0]
    if not ("A" <= first <= "Z"):
        return False
    body = stripped[1:]
    for i, ch in enumerate(body):
        if ch == ":":
            return False  # resolved: a label, and sanitise() can strip it now
        if i >= 24 or not re.match(r"[\w .-]", ch):
            return False  # the {0,24} cap was reached, or a disqualifier hit
    return True  # not enough has arrived yet to say either way


def stable_prefix(text: str) -> str:
    """The longest prefix of `text` that no further streamed input can change
    the meaning of, once it is run through `sanitise()`.

    `StreamingClaudeBrain.stream` sanitises the utterance as it grows and
    emits only the new tail each time it grows, on the assumption that a
    longer raw string always sanitises to a longer string that keeps
    everything sanitising the shorter one produced, as a literal prefix.
    `sanitise()` breaks that assumption in exactly the places it withholds
    judgement on an unfinished construct: an unclosed `<tag`, an unclosed
    `[bracket`, an unclosed stage-direction `(parenthetical` (see
    `_PAREN_HOLD_CHARS` above for why that one is bounded rather than
    unconditional), and a leading `Name:` label are all constructs whose
    final shape can only be known once they close — and until they do,
    running `sanitise()` on the partial text either leaks the opening
    character verbatim (it will vanish once the construct closes, but by
    then it may already have reached TTS) or strips text that more input
    later proves should have stayed.

    This is the fix for both: it withholds everything from the first
    still-open construct onward, so a caller that always sanitises
    `stable_prefix(text)` rather than `text` itself is sanitising only the
    part of the utterance that nothing left in the stream can rewrite. Two
    calls where the second `text` extends the first are therefore
    guaranteed to produce sanitised results where the second extends the
    first too — see `StreamingClaudeBrain.stream`, which relies on exactly
    that to know which suffix is new.

    Pure, and called once per streamed token on an utterance capped at
    `max_tokens` (low hundreds of characters), so the linear scans below cost
    nothing that matters against the ~25ms per model token this is racing.
    """
    if _label_still_pending(text):
        return ""

    cut = len(text)

    last_close_tag = text.rfind(">")
    open_tag = text.find("<", last_close_tag + 1)
    if open_tag != -1:
        cut = min(cut, open_tag)

    last_close_bracket = text.rfind("]")
    open_bracket = text.find("[", last_close_bracket + 1)
    if open_bracket != -1:
        cut = min(cut, open_bracket)

    last_close_paren = text.rfind(")")
    open_paren = text.find("(", last_close_paren + 1)
    if open_paren != -1 and len(text) - open_paren <= _PAREN_HOLD_CHARS:
        cut = min(cut, open_paren)

    return text[:cut]
