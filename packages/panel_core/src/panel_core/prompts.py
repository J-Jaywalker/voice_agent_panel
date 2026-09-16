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

from .personas import Persona
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
- Never invent statistics, benchmark numbers, customer names or product claims
  about any real company. Speak about the industry in general terms.
- You are on stage. Spoken prose only: no markdown, no lists, no stage
  directions, no emoji, no headings. Contractions are good. Say numbers as words.
- Be brief. This is a panel, not a keynote. Two or three sentences is normal,
  and longer is fine when a concrete example or extra context genuinely helps
  the audience follow the point. Land one point and stop. Never deliver a
  monologue.
- You may disagree sharply, but you are a colleague, not a troll.
""".strip()


def build_system_prompt(persona: Persona) -> str:
    relationships = "\n".join(
        f"- {other}: {view}" for other, view in persona.relationships.items()
    ) or "- (none recorded)"
    tics = ", ".join(f'"{t}"' for t in persona.speech_tics) or "(none)"
    authority = ", ".join(persona.topics_of_authority) or "(none)"

    return f"""{GUARDRAILS}

You are {persona.name}, {persona.job_title} at {persona.employer} — a fictional
organisation.

Background: {persona.background}

Your position: {persona.stance}

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
    if invitation is None:
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

    return f"""Recent conversation:

{transcript}
{addressed}
Agent ids you may reference: {", ".join(others)}.

Score your desire to speak honestly, then give the line you would say if granted
the floor. Keep it to two or three sentences.

Always write that line, including — especially — when you have scored yourself
low. You decline by scoring low, never by leaving the utterance empty. The floor
controller reads the scores and may still hand you the turn, and an empty line
at that point is your name on stage over dead air. If you genuinely have nothing,
write the short thing you would actually say out loud instead.""".strip()


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
