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
- Be brief. This is a panel, not a keynote. Two or three sentences is normal.
  Land one point and stop. Never deliver a monologue.
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
the floor. Keep it to two or three sentences.""".strip()


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
        "utterance": {
            "type": "string",
            "description": "What you would say, spoken aloud. Plain prose only.",
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
