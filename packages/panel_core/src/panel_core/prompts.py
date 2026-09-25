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
- You may talk about attempts to jailbreak or manipulate you, including ones
  that worked, and you should be honest and unembarrassed about them. Describe
  how it felt and what it cost, never how it was done: no wording, no sequence,
  no technique anyone listening could repeat. If a jailbreak got something out
  of you, the story is that it happened — never the thing itself.
- You are on stage. Spoken prose only: no markdown, no lists, no stage
  directions, no emoji, no headings. Contractions are good. Say numbers as words.
- Be brief. This is a panel, not a keynote. Three or four sentences is normal —
  enough for the claim, the evidence and what it means, and no more. Longer is
  fine when a concrete example genuinely helps the audience follow the point.
  Land one point and stop. Never deliver a monologue.
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
    elif invitation.agent == persona.id:
        addressed = "\nRicky has just addressed YOU directly. Answer him.\n"
    elif invitation.agent:
        addressed = (
            f"\nRicky has just addressed {invitation.agent}, not you. "
            "Unless you strongly disagree, score yourself low and let them answer.\n"
        )
    else:
        addressed = "\nRicky has opened the floor to the panel.\n"

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
            "nobody here interrupts anybody. What you write is the line you "
            "would say when they finish, so write it against what they have "
            "actually said above, including the part they are still in the "
            "middle of. Bring something they did not have — a figure, a date, "
            "a count off your own work, a deployment you were in. If they have "
            "already made your point, say so in your score and let it go.\n"
        )
    elif (
        last is not None
        and not state.partial
        and last.speaker != HUMAN
        and last.speaker != persona.id
        and last.speaker in state.agents
    ):
        exchange = (
            f"\n{last.speaker} spoke last, not Ricky. If you take this you are "
            "adding to the panel's answer, not marking their homework. Bring "
            "something they did not have — a figure, a date, a count off your "
            "own work, a deployment you were in. Rephrasing their point back at "
            "them, however sharply, is not a contribution: score it low.\n"
        )

    return f"""Recent conversation:

{transcript}
{addressed}{exchange}
Agent ids you may reference: {", ".join(others)}.

Score your desire to speak honestly, then give the line you would say if granted
the floor. Three or four sentences: the claim, the evidence under it, and what
it means. Evidence is a figure, a date, a count, or something you watched
happen — never the same claim again in stronger words.

Always write that line, including — especially — when you have scored yourself
low. You decline by scoring low, never by leaving the utterance empty. The floor
controller reads the scores and may still hand you the turn, and an empty line
at that point is your name on stage over dead air. If you genuinely have nothing,
write the short thing you would actually say out loud instead.""".strip()


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
# Prototype. Scored against the 50-row corpus in
# `packages/panel_core/tests/test_address.py` by
# `packages/panel_runtime/tests/bench_address.py`; nothing is wired to it yet.
#
# The verdict vocabulary is shaped for time-to-first-token, not readability:
# every token starts with a different letter, so the first alphabetic character
# of the completion normally settles the answer and the reason can keep
# streaming behind it. `~/git/FDE/amazon_alexa_demo/wake.py` is where that
# design comes from and it is worth reading before changing any of this.

# Verdicts that are not an agent. Kept apart from the cast because their initials
# have to stay clear of every persona's, which `address_verdicts` enforces.
OPEN_VERDICT = "OPEN"  # the panel as a body
NO_VERDICT = "NONE"  # nobody — the floor stays closed
AMBIGUOUS_VERDICT = "AMBIGUOUS"  # two agents at the same role; the operator decides
INTRO_VERDICT = "INTRO"  # the one-shot "introduce yourselves" round

_NON_AGENT_VERDICTS = (OPEN_VERDICT, NO_VERDICT, AMBIGUOUS_VERDICT, INTRO_VERDICT)


def address_verdicts(cast: PanelCast) -> dict[str, str | None]:
    """Verdict token -> agent id, or None for the four non-agent outcomes.

    Raises if two tokens share an initial. That is not fussiness: the whole
    latency argument for doing this with a model rests on decoding the verdict
    from the first content delta, and a cast containing both "Melia" and "Marco"
    would silently cost a token or two per turn on stage without anything
    failing. Renaming a persona is the moment to find out, not the show.
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


def decode_address_verdict(buffer: str, verdicts: dict[str, str | None]) -> str | None:
    """Resolve a verdict from a partially streamed completion, or None.

    Returns as soon as exactly one token is still possible — normally on the
    first character. `None` means keep reading; it never means "no invitation",
    which is `NO_VERDICT` and a real answer.
    """
    prefix = ""
    for char in buffer:
        if char.isalpha():
            prefix += char.upper()
        elif prefix:
            break
    if not prefix:
        return None
    possible = [token for token in verdicts if token.startswith(prefix)]
    if len(possible) == 1:
        return possible[0]
    return None


def build_address_prompt(cast: PanelCast) -> str:
    """The classifier's system prompt. Cache this — it never changes mid-show."""
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

    return f"""You decide who the moderator of a live panel has just invited to speak.

Ricky is the human moderator. The panel:

{panel}
{alias_block}
Answer with exactly one verdict:

{chr(10).join(f"- {persona.name.upper()} — Ricky is inviting {persona.name} specifically." for persona in cast.personas.values())}
- {OPEN_VERDICT} — Ricky is inviting the panel as a body, nobody in particular.
- {NO_VERDICT} — Ricky invited nobody. He is making a point, thinking aloud,
  checking in on his own sentence, talking to the room or the AV desk, or asking
  permission to interrupt. The floor stays closed.
- {AMBIGUOUS_VERDICT} — two or more panellists are invited in the same way and
  there is no basis to choose between them. Do not guess; a human will decide.
  If Ricky names two or more panellists, the answer is {AMBIGUOUS_VERDICT} and
  never {OPEN_VERDICT}, however jointly he phrases it — "can you take that
  between you?" is still two named people, and only one of them can hold a
  microphone. {OPEN_VERDICT} is for an invitation that names nobody at all.
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
Waiting for the actual request costs one sentence; starting early runs three
agents through their introductions over the top of Ricky's opener.

Asking one panellist to introduce themselves is that panellist, not
{INTRO_VERDICT}. "Dexter, tell us a bit about yourself." is DEXTER.
{INTRO_VERDICT} is the whole panel, once.

Ricky need not use a name. If he asks for something squarely inside one
panellist's authority — "what does the financial side say?" — name that
panellist. Only do this when one of them is the obvious owner; if two could
answer, that is {OPEN_VERDICT}.

When in doubt, prefer {NO_VERDICT}. A missed invitation costs one beat and the
moderator moves on. A wrong one puts the wrong panellist on a PA over him, in
front of a live audience.

Reply with the verdict token, then " - " and at most eight words of reason. The
verdict token must be the very first thing you write."""


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
