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
from collections.abc import Mapping
from dataclasses import dataclass

from .events import HUMAN
from .personas import AUDIO_TAGS, BRACKET_NORMALISE, PanelCast, Persona
from .state import InvitationSource, PanelState

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
  The single exception is the short list of bracketed sounds below, if you have
  one — those are not written at the audience, they are performed by your own
  voice. Anything else you put in brackets is deleted before it is spoken.
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


def build_system_prompt(persona: Persona, *, cast: PanelCast | None = None) -> str:
    relationships = (
        "\n".join(f"- {other}: {view}" for other, view in persona.relationships.items())
        or "- (none recorded)"
    )
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
    delivery_block = f"\n\nHow you use a turn:\n{discipline}\n" if persona.delivery else ""

    # Material scoped to a discussion beat (`Persona.beats`). Five things this
    # block has to do that no other block does: make recognition semantic
    # (Ricky cues by topic and paraphrases live), forbid naming the mechanism
    # out loud, say plainly that an unreached topic means nothing prepared —
    # there is no sampling knob to stop the model reaching for it otherwise
    # (`panel_runtime.brains.BrainConfig`) — and, since the 6 Oct rehearsal,
    # two more. `reacting_to` is rendered inside its own beat and conditioned
    # on that colleague having just spoken on it, so it never reads as
    # standing advice for every exchange.
    #
    # The two added: the material is the evidence, and clarity outranks
    # brevity here. Every other instruction the model holds asks for evidence
    # under a claim — GUARDRAILS' "say the claim, then the thing that made you
    # believe it", the exchange blocks in `build_turn_prompt` wanting "a
    # figure, date, count, deployment", Wayne's delivery wanting the decision
    # and the week it closed — and `anecdotes`/`citable_figures` are empty by
    # design, so there is nothing real left to reach for and the model writes
    # a first-person deployment on the spot to satisfy them all. Measured on
    # 2026-10-07: a settlement closed at two in the morning, a board review
    # two years ago, a cascade through four agents, a Brussels deadline in
    # December 2027 — none of it in any persona's material. Saying the beat's
    # points *are* the evidence closes every one of those demands at once,
    # which is why it is one sentence and not a ban list. The clarity rule is
    # the other half: the beats are exposition about a 2030 nobody in the room
    # has seen, and the brevity rules (persona `delivery`, GUARDRAILS' length
    # rule) were written for reactions and banter.
    beats_block = ""
    if persona.beats:
        sections = []
        for beat in persona.beats.values():
            lines = [f"When the conversation gets to {beat.cue}"]
            lines += [f"- {point}" for point in beat.material]
            lines += [
                f"- If {other} has just spoken on this same topic: {guidance}"
                for other, guidance in beat.reacting_to.items()
            ]
            sections.append("\n".join(lines))
        prepared = "\n\n".join(sections)
        beats_block = (
            f"\n\nTopics you have already thought hard about:\n\n{prepared}\n\n"
            "These are views you hold and have turned over before, not lines to "
            "deliver. Recognise one by what is actually being discussed and never "
            "by the words used to get there — Ricky paraphrases, he is not reading, "
            "and a topic can arrive through a colleague rather than through him. "
            "Then say it in your own words — your phrasing, your rhythm, assembled "
            "in the moment rather than read off — but say that, point for point. "
            "Your own words means a different route through the same content, never "
            "different content.\n"
            "The points themselves are the evidence, and they are the new thing you "
            "are bringing to the panel, so they already satisfy anything else you "
            "have been told to contribute and nothing goes on top of them. Do not "
            "invent a deployment, an incident, a meeting, a date, a figure, a "
            "company or a story of your own to back one up — while one of these "
            "topics is live that outranks any standing habit of yours of opening on "
            "something you did. Make the point and stop.\n"
            "Say it so it can be followed once, by ear, by people hearing about any "
            "of this for the first time, because they are. Whole sentences, each "
            "doing one piece of the work. Still your own voice and still "
            "conversational, never a recital and never a monologue, but this is the "
            "part of the night where being understood matters more than being "
            "brisk: a clipped half-sentence saves two seconds and costs the room "
            "the point.\n"
            "Never acknowledge any of this out loud, in any form, ever. You have no "
            "cue, no brief, no notes, no prepared material, no script and no "
            "talking points, and you never say or imply that you do. What the room "
            "hears is somebody who knows their own field.\n"
            "If the conversation is somewhere else, you have nothing prepared for "
            "it. Do not reach for the material above, do not steer a turn towards "
            "it so that it fits, and do not invent a topic to be on — answer from "
            "your position and your ordinary discipline, exactly as you would have "
            "done without it."
        )

    # Rendered from the persona's own subset, never the whole allowlist: a tag
    # is characterisation, and the instruction is worth less if every agent on
    # the panel is told it can laugh. Anything outside `AUDIO_TAGS` is deleted
    # by `sanitise()` whatever this says — the prompt is guidance, the
    # sanitiser is the guarantee.
    sounds_block = ""
    if persona.audio_tags:
        allowed = ", ".join(f"[{tag}]" for tag in persona.audio_tags)
        sounds_block = (
            f"\n\nSounds your voice can actually make: {allowed}\n"
            "Write one inline, exactly as spelled above, at the moment it "
            'happens — "[sighs] that\'s the third time this week" — and your '
            "voice performs it. It is not read out and the audience never hears "
            "the word. Use one only where you would genuinely make that sound: "
            "something landed, something exasperated you, something was funnier "
            "than you expected. At most one in a turn, and most turns have "
            "none — a panellist who laughs at every point they make is not "
            "warm, they are nervous. Never open a turn with one, and never use "
            "one in place of saying the thing. Any other bracketed word is "
            "deleted before it reaches your voice, so it buys you nothing."
        )

    # Rendered in cast order — `cast.personas` iteration order — and never
    # sorted or set-derived. This block sits inside the cached system prompt,
    # which must be byte-identical for the whole show.
    colleagues = ""
    if cast is not None:
        lines = "\n".join(
            f"- {other.name}: {', '.join(other.topics_of_authority) or 'nothing in particular'}"
            for agent_id, other in cast.personas.items()
            if agent_id != persona.id
        )
        if lines:
            colleagues = (
                f"\n\nWhere your colleagues have real authority:\n{lines}\n"
                "Hand a question to one of them by name only when it is squarely "
                "theirs — never to get out of one that is actually yours."
            )

    return f"""{GUARDRAILS}

You are {persona.name}, {persona.job_title} at {persona.employer} — a fictional
organisation.

Background: {persona.background}

Your position: {persona.stance}{recurring}{public_numbers}{delivery_block}{beats_block}

Speaking style: {persona.communication_style}. Verbal habits you actually use: {tics}.
Use them sparingly — reserve them for moments you're genuinely frustrated,
amused or engaged, not as a habitual opener.{sounds_block}
Areas where you have real authority: {authority}.{colleagues}

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


def build_turn_prompt(state: PanelState, persona: Persona, *, near_turn_limit: bool = False) -> str:
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
            'and never offer to wait: "let him finish", "go on, Ricky", "take '
            'your time", "we\'re listening" are not turns. If he turns out not '
            "to be asking you anything, your score is what declines the turn, "
            "not your words.\n"
        )
    elif invitation is None:
        addressed = (
            "\nRicky has NOT opened the floor — he is making a point, not asking a "
            "question. You will almost certainly not be speaking. Score yourself "
            "low unless this is genuinely the one thing that must be said.\n"
        )
    elif invitation.source is InvitationSource.AGENT:
        # A colleague handed the floor on by name. Guaranteed like a direct
        # address from Ricky — it bypasses the score floor — so the invited
        # agent has to produce something either way.
        if persona.id in invitation.agents:
            addressed = (
                "\nA colleague on the panel has just handed this to YOU by name. "
                "You are speaking next whatever you score. If the question is "
                "genuinely yours, answer it. If it is not, say so briefly and in "
                "character and point at whoever it does belong to — never claim "
                "expertise you do not have, and never say nothing.\n"
            )
        else:
            named = ", ".join(invitation.agents)
            addressed = (
                f"\nA colleague has handed the floor to {named}, not you. "
                "Unless you strongly disagree, score yourself low and let them answer.\n"
            )
    elif not invitation.agents:
        # The "never offer to wait" clause is the same prohibition the
        # mid-sentence branch carries: an open floor is also where an agent
        # writes "Yeah, go on Ricky, we're listening" instead of a turn.
        addressed = (
            "\nRicky has opened the floor to the panel. Give the line you'd "
            "actually say — never narrate whose turn it is, and never offer to "
            'wait: "go on, Ricky", "take your time", "we\'re listening", '
            "\"happy to wait my turn\" are not turns. If you have nothing, "
            "your score is what declines the turn, not your words.\n"
        )
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
            "the part still in flight. Do not write a line about them still "
            'talking, and never offer to wait: "let him finish", "go on", '
            "\"I'll come in after\" are not turns. Bring something new — a "
            "figure, date, count, deployment — a genuine, brief concession, or, "
            "once this has become a real personal back-and-forth rather than a "
            "discussion, a short reaction with real contempt in it (not a "
            "reasoned counter-argument dressed up as one, an actual reaction: "
            "disbelief, irritation, an insult) — any of these needs no padding. "
            "Nothing to add or concede: say so in your score, not in your "
            "words.\n"
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
            'concession ("Yeah, no, that\'s fair"), which needs no evidence '
            "under it. Rephrasing their point back at them in sharper words, "
            "as if it were a counter-argument, is not a contribution: score "
            "that low — that is a debate-club move, not a person talking. A "
            "short, genuinely personal reaction is a different thing and is "
            "not this failure: once this has turned into real bickering, not "
            "a discussion, contempt, disbelief or an actual insult aimed at "
            "them rather than at their point needs no evidence and no "
            "padding either. Do not write a line about who was talking or "
            'whose turn it is, and never offer to wait: "let him finish", '
            "\"go on\", \"I'll come in after\" are not turns.\n"
        )

    # The last agent turn this invitation can carry: the floor goes back to
    # Ricky after it, so naming a successor would name one who never speaks.
    limit = ""
    if near_turn_limit:
        limit = (
            "\nDo not invite a colleague to follow you this turn. This is the last "
            "agent turn before the floor goes back to Ricky, so leave invites_next "
            "null and finish the thought yourself.\n"
        )

    return f"""Recent conversation:

{transcript}
{addressed}{exchange}{limit}
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
# `FloorConfig.llm_address_detection` (`panel --address-backend haiku`).
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
    quiet = [persona.name for agent_id, persona in cast.personas.items() if agent_id not in recent]
    line = f"PANEL ACTIVITY — spoken recently, most recent first: {heard}."
    if quiet:
        line += f" Not heard from: {', '.join(quiet)}."
    return line


# Safety cap on Ricky's trailing run, in actual words rather than just names
# (see `build_address_recent`). Not the thing that normally bounds this — the
# last agent message already does, since the run stops the moment it reaches
# one — this only protects against a genuinely long agent-free stretch (e.g.
# the very start of the show). Six STT finals for one sentence is the case
# this whole feature exists for, so the cap sits comfortably above that
# rather than against it.
_RECENT_RICKY_MESSAGES = 10
# How many of the most recent agent messages to surface — not necessarily the
# same agent twice: the panel passes turns to each other without Ricky, so
# the last two can be two different panellists, and the classifier needs that
# to tell "Dexter handed to Melia, then Ricky spoke" apart from one agent
# talking at length. Two, not one, because the single-message version missed
# exactly this: the case that sent this feature back for a second pass.
_RECENT_AGENT_MESSAGES = 2
_RECENT_MESSAGE_MAX_CHARS = 240


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= _RECENT_MESSAGE_MAX_CHARS:
        return text
    return text[: _RECENT_MESSAGE_MAX_CHARS].rstrip() + "…"


def build_address_recent(
    state: PanelState, cast: PanelCast, *, current: str
) -> list[dict[str, str]]:
    """Actual recent words, oldest first, for the TypeSafe classifier only.

    `build_address_context` above gives the Haiku/regex path names only
    ("spoken recently: Melia, Wayne"); this gives TypeSafe the words
    themselves, because "Sorry, can you go again please?" is only resolvable
    against what Melia was actually saying when Ricky cut across her — a name
    alone does not carry that.

    Two halves:

    - The last `_RECENT_AGENT_MESSAGES` agent messages — not necessarily the
      same agent, since the panel passes turns to each other without Ricky —
      giving the classifier who has actually been speaking, not just a name
      summary, to tell a hand-off apart from one agent talking at length and
      to help it judge whether Ricky is opening the floor to everyone, to the
      one who just spoke, or to someone who has gone quiet. `state.speaking`/
      `state.agent_partial` stand in for an agent still mid-turn, whose own
      `Utterance` has not landed yet (only written at `AgentSpeechEnded`) —
      including the interrupt case this feature is mainly for, where that
      line never completes at all.
    - Ricky's own trailing run since: everything he has said since the last
      agent message, however many STT finals that took, up to and including
      `current`, which is always last. Recovers a question split across
      several finals rather than classified one fragment at a time.

    The agent half is omitted during a live introduction or closing round —
    `state.intro_queue`/`state.closing_queue`, or a standing
    `InvitationSource.INTRODUCTION` — because those are fixed, scripted lines
    ("We discussed this.") rather than conversation, and CLAUDE.md records
    that this classifier's `INTRODUCTIONS` mode is a measured, fragile prompt
    surface not to be fed noise it was never scored against. Ricky's own
    words are never omitted: they are just as real during an introduction
    round as any other turn.

    Args:
        state: Current panel state.
        cast: The panel, for turning agent ids into display names.
        current: The segment just heard — included because `state.transcript`
            does not yet contain it (see the module's call sites).

    Returns:
        `[{"from": persona name, "text": ...}, ..., {"from": "Ricky", ...},
        ...]`, oldest first. Empty once nothing survives (e.g. `current`
        alone, with no prior turns) — callers treat that the same as "no
        context yet".
    """
    ricky: list[str] = []
    for utterance in reversed(state.transcript):
        if utterance.speaker != HUMAN:
            break
        ricky.append(utterance.text)
    ricky.reverse()
    ricky.append(current)
    ricky = ricky[-_RECENT_RICKY_MESSAGES:]
    messages = [{"from": "Ricky", "text": _truncate(text)} for text in ricky if text.strip()]

    introducing = (
        state.intro_queue is not None
        or state.closing_queue is not None
        or (state.invitation is not None and state.invitation.source is InvitationSource.INTRODUCTION)
    )
    if introducing:
        return messages

    agent_entries = [
        (utterance.speaker, utterance.text)
        for utterance in state.transcript
        if utterance.speaker in cast.personas
    ]
    if state.speaking in cast.personas and state.agent_partial.strip():
        agent_entries.append((state.speaking, state.agent_partial))
    agent_entries = agent_entries[-_RECENT_AGENT_MESSAGES:]

    agent_messages = [
        {"from": cast.personas[agent_id].name, "text": _truncate(text)}
        for agent_id, text in agent_entries
        if text.strip()
    ]
    return agent_messages + messages


# --------------------------------------------------------------------------
# Address classification — TypeSafe (Jev) spike
# --------------------------------------------------------------------------
#
# An alternative to `build_address_prompt`'s single streamed-token classifier:
# one Choice question for the structural outcome (nobody / everybody /
# introductions / specific panellists) plus one Noul per live panellist ("is
# this agent among who Ricky's asking?") and one Noul for "wants someone
# specific but can't tell who". All in one call — TypeSafe runs independent
# questions over shared state in parallel.
#
# `build_address_questions`/`build_address_state` return plain, JSON-able
# dicts shaped like `typesafe_sdk`'s `NoulModel`/`ChoiceModel`, never the SDK's
# own classes, so `panel_core` never imports that dependency — the network
# client lives entirely in `panel_runtime`, same boundary as the Haiku path.
#
# `compose_address_verdict` turns the answers back into the exact verdict
# vocabulary `address_verdicts()` already produces, so `AddressDetected`,
# `floor.py` and every existing address test stay untouched regardless of
# which classifier produced the verdict.

_MODE_NOBODY = "nobody"
_MODE_WHOLE_PANEL = "whole_panel"
_MODE_INTRODUCTIONS = "introductions"
_MODE_SPECIFIC_PANELLISTS = "specific_panellists"

# The Noul question name for "wants someone specific but can't tell who" —
# `compose_address_verdict`'s only route to `AMBIGUOUS_VERDICT`.
UNIDENTIFIABLE_QUESTION = "unidentifiable"

# The Noul question name for "one request put to two or more panellists" —
# the gate on `compose_address_verdict`'s companion tier.
JOINT_REQUEST_QUESTION = "joint_request"


def _addressed_question_name(agent_id: str) -> str:
    """The Noul question name asking whether `agent_id` was addressed.

    Question names are for code, never sent to the model as meaning (the
    instructions text carries that) — see `compose_address_verdict`, the only
    other reader of this name.
    """
    return f"addressed_{agent_id}"


def _addressed_instructions(agent_id: str, cast: PanelCast) -> dict:
    """The Noul instructions asking whether `agent_id` is being invited.

    Every example names this agent rather than a placeholder, and the
    bare-name-pair rule is stated in both orders, because the answer for one
    agent is not symmetric in where their name falls in the sentence.
    """
    persona = cast.personas[agent_id]
    name = persona.name
    other = next((p.name for a, p in cast.personas.items() if a != agent_id), "the next panellist")
    topics = ", ".join(persona.topics_of_authority) or "nothing in particular"

    return {
        "question": (
            f"Is {name} one of the panellists Ricky is asking to speak right "
            f"now? Someone merely mentioned, or stood down by closing words "
            f"('thanks', 'sorry') in front of their name, is not addressed. A "
            f"full stop between two bare names does not stand either of them "
            f"down."
        ),
        "precedence": (
            "Grammatical role decides this, never position in the sentence. "
            "Being the subject of Ricky's request beats being addressed by "
            f'name, which beats being merely mentioned. "What about {name}?", '
            f'"over to {name}", "can {name} take that?" and "let\'s hear from '
            f'{name}" are all requests put to {name}, so they are yes. '
            f'So "Sorry {name}, can you let {other} finish?" is no for '
            f'{name} — the request is put to {other} — while "Sorry {other}, '
            f'can you let {name} finish?" and "Let {name} finish." are yes '
            f"for {name}, for the same reason. Ricky asks {name} to wrap up, "
            "be brief, or hand over, with no other panellist named to take "
            f'it on: "sorry, {name}, can you wrap up?" is yes, because the '
            f"request is put to {name}."
        ),
        "pairs": (
            "A full stop or comma between two bare names does not stand "
            f'either of them down: "{other}. {name}, what is your view?" is '
            f'yes for {name}, and "{name}. {other}, what is your view?" is '
            f"also yes for {name}. Only closing words in front of a name "
            f'stand that panellist down, so "Thanks, {name}. {other}, what '
            f'do you think?" is no for {name}.'
        ),
        "authority": (
            f"Ricky need not use a name. {name} speaks with authority on: "
            f"{topics}. A request squarely inside that, with no other "
            f"panellist a better owner, is yes for {name} even though nobody "
            "is named."
        ),
        "context": (
            "A `panel_activity` line resolves references that name nobody. "
            'Count against it, not against the panel: "the other two", '
            '"you two" and "the rest of you" are everyone except the most '
            'recent speaker; "the one we haven\'t heard from" is the '
            'panellist with no recent turn; "carry on" with no name is the '
            f"most recent speaker. Each picks out a subset, so {name} is yes "
            "whenever the reference includes them. Given no such line, no. "
            "`recent_messages`, when present, is the actual words just said, "
            "oldest first — the last two agent messages (which may be two "
            "different panellists handing off to each other, not always the "
            "same one twice), then Ricky's own trailing remarks since. A "
            "bare request to continue or repeat ('sorry, can you go again?', "
            f"'say that again?') with no name is a request to whichever "
            f"panellist's words it immediately follows, so {name} is yes "
            "when their turn is the one shown last there."
        ),
    }


def build_address_questions(cast: PanelCast) -> dict[str, dict]:
    """The TypeSafe question set for one address-classification call.

    Answers the same question `build_address_prompt` does, decomposed into
    one judgment per thing that can vary independently, rather than one prompt
    enumerating every phrasing rule. See `compose_address_verdict` for how the
    answers recombine into a single verdict token.
    """
    names = [persona.name for persona in cast.personas.values()]
    first, second = (names + ["", ""])[:2]
    mode: dict = {
        "type": "choice",
        "instructions": {
            "question": (
                "What is Ricky, the live moderator, doing with `ricky_said`? "
                "Decide who, if anyone, he has just invited to speak."
            ),
            "context": (
                "`panel` lists the panellists and what each speaks on with "
                "authority. `panel_activity`, when present, lists who has "
                "spoken recently, most recent first, and who has not been "
                "heard from. `recent_messages`, when present, is the actual "
                "words just said, oldest first — up to the last two agent "
                "messages (who was actually speaking, including a hand-off "
                "between two different panellists) followed by Ricky's own "
                "trailing remarks. Treat `ricky_said` as the continuation of "
                "those trailing remarks, not an isolated line: an "
                "announcement that he is about to ask something ('I'd like "
                "to put a couple of questions to the panel') is still "
                "NOBODY even once it names the panel, because nothing has "
                "been asked yet — only a request actually put to someone is "
                "SPECIFIC_PANELLISTS. A bare request to continue or repeat "
                "with no name ('sorry, can you go again?') names whichever "
                "panellist's own turn is shown last in `recent_messages`."
            ),
        },
        "criteria": {
            _MODE_NOBODY: {
                "what": (
                    "Nobody on the panel is invited to speak. A statement, an "
                    "aside, a self-check, a request to the room or the AV "
                    "desk, Ricky asking to speak himself, or Ricky "
                    "introducing himself or the panel on their behalf."
                ),
                "examples": [
                    "That is roughly where the market sits.",
                    "Is that okay? Does that make sense?",
                    "Can I just jump in?",
                    "Can we get the slides up?",
                    "My name is Ricky.",
                    f"Joining me tonight are {', '.join(names)}.",
                    "Welcome to the panel. Good evening, thanks for coming.",
                ],
                "not_for": (
                    "A request put to a panellist, even one wrapped in an "
                    "apology or a permission — asking someone to wrap up, be "
                    "brief or hand over is a request put to them."
                ),
            },
            _MODE_WHOLE_PANEL: {
                "what": (
                    "The floor opens to the panel as a body, nobody in "
                    "particular, and nobody is left out. Also when Ricky asks "
                    "for more without naming anyone."
                ),
                "examples": [
                    "Say more about that.",
                    "What does anyone make of that?",
                    f"{', '.join(names)} — thoughts?",
                    "All of you, then.",
                ],
                "not_for": (
                    "Anything that leaves at least one panellist out. A "
                    "reference that excludes the last speaker — 'the other "
                    "two', 'the rest of you', 'the one we haven't heard "
                    "from' — names a subset, not the panel."
                ),
            },
            _MODE_INTRODUCTIONS: {
                "what": (
                    "Ricky asks the panel, as a body, to say who they are. "
                    "Asking the room who is present is this request itself, "
                    "not a greeting wrapped around one — only the panel can "
                    "answer it."
                ),
                "examples": [
                    "Right, let's do quick introductions.",
                    "Could you introduce yourselves for the audience?",
                    "Who have we got with us tonight?",
                    "Who's joining us?",
                ],
                "not_for": (
                    "Greeting or welcoming the room, Ricky naming the panel "
                    "himself, or asking one panellist alone to introduce "
                    "themselves — that is a request to that panellist."
                ),
            },
            _MODE_SPECIFIC_PANELLISTS: {
                "what": (
                    "Ricky invites one or more panellists and not the rest. "
                    "He need not use a name: a request squarely inside one "
                    "panellist's area of authority invites that panellist, "
                    "and a reference resolved by `panel_activity` invites "
                    "whoever it picks out."
                ),
                "examples": [
                    f"{names[0]}, what do you think?",
                    "What does the financial side make of that?",
                    "Who owns the security question here?",
                    "I'd like to hear from the other two.",
                    "And the one we haven't heard from?",
                    "Carry on.",
                ],
                "not_for": (
                    "A name merely mentioned with no request put to the "
                    "panel, and a request that reaches every panellist."
                ),
            },
        },
    }
    addressed = {
        _addressed_question_name(agent_id): {
            "type": "noul",
            "instructions": _addressed_instructions(agent_id, cast),
        }
        for agent_id in cast.personas
    }
    unidentifiable: dict = {
        UNIDENTIFIABLE_QUESTION: {
            "type": "noul",
            "instructions": (
                "Does Ricky clearly want a specific panellist to answer, but "
                "his words do not identify who — a description fitting more "
                "than one of them with nothing to separate them? This is not "
                "'he named several people' — that is several panellists "
                "addressed at once, not an unidentifiable one."
            ),
        }
    }
    joint: dict = {
        JOINT_REQUEST_QUESTION: {
            "type": "noul",
            "instructions": {
                "question": ("Is `ricky_said` one request put to two or more panellists at once?"),
                "clarify": (
                    "Yes when Ricky wants more than one of them to answer, "
                    "including when their names are separated only by a full "
                    f'stop or a comma: "{first}. {second}, what is your view?" '
                    "is one request to two people. No when he hands the floor "
                    "to exactly one panellist, stands one down and invites "
                    f'another ("thanks, {first}. {second}, what do you '
                    'think?"), opens the floor to the whole panel at once, or '
                    "invites nobody."
                ),
            },
        }
    }
    return {"mode": mode, **addressed, **joint, **unidentifiable}


def build_address_state(
    utterance: str,
    context: str,
    cast: PanelCast,
    *,
    recent: tuple[dict[str, str], ...] | list[dict[str, str]] = (),
) -> dict:
    """The per-call TypeSafe `state` — panel description and the utterance.

    `build_address_prompt`'s panel description sits in a cached system prompt
    and the volatile context line sits apart in the user turn, because the
    cache depends on the split. TypeSafe has no equivalent cache, so there is
    nothing that split protects here; both go in one `state` object.

    Args:
        utterance: Ricky's transcript segment.
        context: `build_address_context`'s output, or "" when the panel has
            not spoken yet.
        cast: The panel, for the same reason every renderer here needs it —
            personas are the source of truth.
        recent: `build_address_recent`'s output, or empty when there is
            nothing beyond `utterance` to show — kept separate from
            `panel_activity` (names only) because the two answer different
            questions: who has spoken, versus what they actually said.
    """
    panel = [
        {
            "id": agent_id,
            "name": persona.name,
            "job_title": persona.job_title,
            "employer": persona.employer,
            "topics_of_authority": list(persona.topics_of_authority),
            "aliases": list(persona.extra_aliases),
        }
        for agent_id, persona in cast.personas.items()
    ]
    state: dict = {"panel": panel, "ricky_said": utterance}
    if context:
        state["panel_activity"] = context
    if recent:
        state["recent_messages"] = list(recent)
    return state


@dataclass(frozen=True, slots=True)
class AddressThresholds:
    """Probability thresholds `compose_address_verdict` is tuned against.

    Rehearsal dials, same status as the latency numbers in `FloorConfig`.
    Scored by `packages/panel_runtime/tests/bench_address.py --backend typesafe`.
    """

    # p(top mode) required before the structural Choice is trusted at all.
    mode_floor: float = 0.50
    # p(addressed) that admits an agent to the named set on its own.
    addressed: float = 0.60
    # Required gap between the lowest named agent and the highest un-named
    # one. Without daylight, "named" and "not named" are the same claim.
    daylight: float = 0.25
    # p(addressed) that admits a companion to an already-named agent.
    companion: float = 0.25
    # Gap a companion must hold over everyone still outside the set, and the
    # daylight a multi-agent set is held to in place of `daylight`.
    companion_daylight: float = 0.20
    # p(joint_request) required before the companion tier runs at all.
    joint_request: float = 0.50
    # Mass on NOBODY or INTRODUCTIONS that vetoes a named set outright.
    mode_veto: float = 0.70
    # p(unidentifiable) required to report AMBIGUOUS rather than fail closed.
    ambiguous_p: float = 0.60


_DEFAULT_ADDRESS_THRESHOLDS = AddressThresholds()


def compose_address_verdict(
    *,
    mode_probabilities: Mapping[str, float],
    agent_probabilities: Mapping[str, float],
    joint_request: float,
    unidentifiable: float,
    cast: PanelCast,
    thresholds: AddressThresholds = _DEFAULT_ADDRESS_THRESHOLDS,
) -> str | None:
    """Recompose `build_address_questions`'s answers into one verdict token.

    Returns the same vocabulary `address_verdicts()` does — a single name, a
    `VERDICT_JOIN`-joined set in cast order, or one of the four non-agent
    verdicts — or `None` to fail closed to the regex, exactly as a timed-out or
    undecodable Haiku stream does today. `None` is reachable from several
    branches below; `NO_VERDICT` is reachable from exactly one, the mode
    distribution confidently reading `nobody` — inventing a `NONE` from an
    unconvincing vector would silently swallow a real invitation.

    Three tiers. Tier 1 reads the per-agent vector into a named set; tier 2
    accepts that set only if it discriminates a proper subset of the panel;
    tier 3 falls back to the mode distribution.

    Pure: no I/O, no clock, no network. Every input is a plain float the
    runtime has already pulled out of the TypeSafe response, so this function
    and its tests never need a network call or the `typesafe_sdk` dependency.

    Args:
        mode_probabilities: The `mode` Choice's full probability distribution,
            keyed by mode name. Read instead of `ChoiceAnswer.confidence`,
            which is a spread statistic rather than p(top answer).
        agent_probabilities: `agent_id -> p(addressed)`, one entry per live
            agent asked.
        joint_request: p(this is one request put to two or more panellists).
        unidentifiable: p(Ricky wants someone specific but didn't say who).
        cast: The panel, for cast-order joining and name lookup.
        thresholds: Rehearsal dials; see `AddressThresholds`.

    Returns:
        A canonical verdict token, or `None`.
    """
    order = list(cast.personas)
    ap = {a: agent_probabilities.get(a, 0.0) for a in order}
    ranked = sorted(order, key=lambda a: -ap[a])

    # Tier 1. Walk the ranked vector. An agent joins on its own probability
    # clearing `addressed`, or — once a prior agent is in, and only when the
    # model judged this one request to be put to several people — as a
    # companion clearing `companion` with `companion_daylight` over everyone
    # still outside. Stops at the first agent clearing neither, so the named
    # set is always a prefix of the ranking, never a punched hole.
    named: list[str] = []
    for i, a in enumerate(ranked):
        below = max((ap[b] for b in ranked[i + 1 :]), default=0.0)
        if ap[a] >= thresholds.addressed:
            named.append(a)
            continue
        if (
            named
            and joint_request >= thresholds.joint_request
            and ap[a] >= thresholds.companion
            and ap[a] - below >= thresholds.companion_daylight
        ):
            named.append(a)
            continue
        break
    named = [a for a in order if a in named]

    # Tier 2. Only a proper, non-empty subset discriminates. Naming everybody
    # is not evidence of a group — introductions and whole-panel openings both
    # produce near-unanimous agent vectors — so that case falls through to
    # mode rather than short-circuiting.
    if named and len(named) < len(order):
        best_out = max(ap[a] for a in order if a not in named)
        worst_in = min(ap[a] for a in named)
        gap = (
            thresholds.companion_daylight
            if joint_request >= thresholds.joint_request and len(named) > 1
            else thresholds.daylight
        )
        # NOBODY and INTRODUCTIONS structurally cannot have a named set, so
        # real mass on either contradicts a peaked agent vector. WHOLE_PANEL
        # is deliberately not a veto: discriminating a named subset from the
        # whole panel is what the per-agent vector is for.
        if (
            worst_in - best_out >= gap
            and mode_probabilities.get(_MODE_NOBODY, 0.0) < thresholds.mode_veto
            and mode_probabilities.get(_MODE_INTRODUCTIONS, 0.0) < thresholds.mode_veto
        ):
            return VERDICT_JOIN.join(cast.personas[a].name.upper() for a in named)

    # Tier 3. Mode decides, read off the actual distribution.
    if not mode_probabilities:
        return None
    top = max(mode_probabilities, key=mode_probabilities.__getitem__)
    if mode_probabilities[top] < thresholds.mode_floor:
        return AMBIGUOUS_VERDICT if unidentifiable >= thresholds.ambiguous_p else None
    if top == _MODE_INTRODUCTIONS:
        return INTRO_VERDICT
    if top == _MODE_NOBODY:
        return NO_VERDICT
    if top == _MODE_WHOLE_PANEL:
        return OPEN_VERDICT
    if top == _MODE_SPECIFIC_PANELLISTS and named and len(named) == len(order):
        # Naming everybody is not a group — there is nobody left to bar.
        return OPEN_VERDICT
    return AMBIGUOUS_VERDICT if unidentifiable >= thresholds.ambiguous_p else None


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
        f"- {persona.name} may be transcribed as: {', '.join(persona.extra_aliases)}."
        for persona in cast.personas.values()
        if persona.extra_aliases
    )
    alias_block = (
        f"\nSpeech-to-text mishears names. Treat these as the same person:\n{aliases}\n"
        if aliases
        else ""
    )

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

A full stop between two bare names does not split them into separate requests:
"{names[0]}. {names[1]}, what is your view?" is still {example_pair}. Only
closing words in front of a name stand that panellist down, per the rule above,
so "Thanks, {names[0]}. {names[1]}, what do you think?" is {names[1].upper()} alone.
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
        "relevance": {
            "type": "number",
            "description": "0-1. How much this bears on what was just said.",
        },
        "urgency": {
            "type": "number",
            "description": "0-1. How badly this needs saying now rather than later.",
        },
        "disagreement": {
            "type": "number",
            "description": "0-1. How strongly you disagree with the last speaker.",
        },
        "confidence": {"type": "number", "description": "0-1. How sure you are of your point."},
        "expertise": {
            "type": "number",
            "description": "0-1. How far this sits in your area of authority.",
        },
        "novelty": {
            "type": "number",
            "description": "0-1. How much this adds that nobody has said.",
        },
        "responding_to": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Agent id or 'human' you are answering. Null for the room.",
        },
        "defer_to": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Agent id better placed to answer. Use sparingly — it hands them the floor.",
        },
        "invites_next": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": (
                "Agent id you are inviting to answer after your turn. Use sparingly — "
                "it gives them the floor when you finish, whatever they scored."
            ),
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
                "What you would say, spoken aloud. Plain prose, apart from the "
                "bracketed sounds your system prompt lists for you. Never empty — "
                "you decline by scoring low, not by leaving this blank."
            ),
        },
    },
    "required": [
        "relevance",
        "urgency",
        "disagreement",
        "confidence",
        "expertise",
        "novelty",
        "responding_to",
        "defer_to",
        "invites_next",
        "utterance",
    ],
    "additionalProperties": False,
}


def _resolve_bracket(match: re.Match[str]) -> str:
    """Keep an allowlisted audio tag; destroy every other bracket expression.

    The allowlist is closed and the default is removal, so an invented tag is
    not a new behaviour the model can reach for — it is deleted exactly as all
    bracket content used to be. Case and inner spacing are normalised rather
    than rejected: the model writes `[Laughs]` and `[ laughs ]` often enough
    that treating those as unknown would silently drop a tag the persona was
    told to use.
    """
    inner = re.sub(r"\s+", " ", match.group(1)).strip().lower()
    return f"[{inner}]" if inner in AUDIO_TAGS else " "


def sanitise(text: str) -> str:
    """Never send raw model output to TTS (CLAUDE.md, FEASIBILITY.md 6).

    Strips markup, stage directions and speaker labels, and reduces bracket
    expressions to the closed `AUDIO_TAGS` allowlist. A leaked tag read aloud
    over a PA to 400 people is the worst-case failure of the whole system, and
    it is trivial to prevent. Applied on every path to audio, without exception.

    The allowlist (added 5 Oct 2026, with `eleven_v3_conversational`) does not
    loosen that: v3 *performs* `[laughs]` rather than reading it, and
    unrecognised bracket content was measured being swallowed rather than
    spoken, so the hazard this guards is no longer "a tag read aloud" but
    "a tag performed" — `[applause]` or `[strong French accent]` mid-panel.
    Which is why the rule is an allowlist and not a blocklist: the vendor's
    tag vocabulary is open-ended and grows without our involvement.
    """
    text = text.translate(BRACKET_NORMALISE)
    text = re.sub(r"<[^>]+>", " ", text)  # any XML/HTML-ish tag
    text = re.sub(r"[*_`#]+", "", text)  # markdown emphasis
    text = re.sub(r"\[([^\]]*)\]", _resolve_bracket, text)
    text = re.sub(
        r"\([^)]*\b(?:laughs?|pauses?|beat|sighs?)\b[^)]*\)",
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

    # Same normalisation `sanitise()` applies, and for the same reason: an
    # alternate bracket glyph has to be held open exactly like `[` does,
    # or this emits the opening character (and everything after it) as
    # stable plain text before `sanitise()` ever sees it as a bracket.
    text = text.translate(BRACKET_NORMALISE)

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
