"""Agent brains — the only part of the sim that talks to a model.

One request per proposal returns *both* the floor signals and the candidate
utterance (FEASIBILITY.md section 6). The signal fields come first in the schema
so they arrive early in the stream while the utterance is still generating —
that ordering is what the production path will depend on, so the sim mirrors it
even though latency does not matter here.
"""

from __future__ import annotations

import os
import random
from typing import Protocol

from panel_core import PanelState, Persona, Signals

from .prompts import build_system_prompt, build_turn_prompt

# Signals first, utterance last — deliberate ordering.
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


class Brain(Protocol):
    def propose(self, persona: Persona, state: PanelState) -> tuple[str, Signals] | None: ...


class StubBrain:
    """Deterministic offline brain. Runs with no network and no credentials.

    Exists so the floor logic can be exercised and demoed on a plane. It is not
    trying to be convincing — that is what the live brain is for.
    """

    def __init__(self, seed: int = 0) -> None:
        self.rng = random.Random(seed)

    def propose(self, persona: Persona, state: PanelState) -> tuple[str, Signals] | None:
        last = state.transcript[-1].text if state.transcript else ""
        hot = any(word in last.lower() for word in persona.topics_of_authority)
        tic = persona.speech_tics[0] if persona.speech_tics else ""

        signals = Signals(
            relevance=self.rng.uniform(0.5, 1.0) if hot else self.rng.uniform(0.1, 0.6),
            urgency=self.rng.uniform(0.2, 0.9) * (0.5 + persona.interrupt_tendency),
            disagreement=self.rng.uniform(0.0, 1.0) * persona.interrupt_tendency,
            confidence=self.rng.uniform(0.6, 1.0),
            expertise=0.9 if hot else 0.2,
            novelty=self.rng.uniform(0.2, 0.7),
            responding_to="human",
        )
        return f"[{persona.name}] {tic} …stub response to “{last[:60]}”", signals


class ClaudeBrain:
    """Live brain. Mirrors the production request shape."""

    def __init__(self, model: str = "claude-opus-5", effort: str = "medium") -> None:
        import anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set — run with --stub instead")
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort

    def propose(self, persona: Persona, state: PanelState) -> tuple[str, Signals] | None:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1200,
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": PROPOSAL_SCHEMA},
            },
            system=[
                {
                    "type": "text",
                    "text": build_system_prompt(persona),
                    # The stable prefix. In production this is what makes
                    # speculative generation affordable.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": build_turn_prompt(state, persona)}],
        )

        if response.stop_reason == "refusal":
            return None

        import json

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            return None
        data = json.loads(text)

        utterance = sanitise(data.pop("utterance", ""))
        if not utterance:
            return None
        return utterance, Signals(**data)


def sanitise(text: str) -> str:
    """Never send raw model output to TTS (FEASIBILITY.md section 6).

    Strips markup, stage directions and speaker labels. A leaked tag read aloud
    over a PA to 400 people is the worst-case failure and is trivial to prevent.
    """
    import re

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
