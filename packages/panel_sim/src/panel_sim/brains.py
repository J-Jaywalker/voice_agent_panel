"""Agent brains for the text harness.

One request per proposal returns *both* the floor signals and the candidate
utterance. The prompt, the output schema and `sanitise()` all come from
`panel_core.prompts` — the same ones the live runtime uses, so a persona tuned
here behaves identically on stage.
"""

from __future__ import annotations

import json
import os
import random
from typing import Protocol

from panel_core import (
    PROPOSAL_SCHEMA,
    PanelState,
    Persona,
    Signals,
    build_system_prompt,
    build_turn_prompt,
    sanitise,
)


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

        # Having nothing to say is a real outcome and the stub has to be able to
        # express it, or the sim reads as three agents straining at the leash on
        # every remark — which is not what a real brain does and not what the
        # floor controller should be tuned against.
        if not hot and self.rng.random() > 0.25 + 0.5 * persona.interrupt_tendency:
            return None

        signals = Signals(
            relevance=self.rng.uniform(0.5, 1.0) if hot else self.rng.uniform(0.05, 0.35),
            urgency=self.rng.uniform(0.2, 0.9) * (0.5 + persona.interrupt_tendency),
            disagreement=self.rng.uniform(0.0, 1.0) * persona.interrupt_tendency,
            confidence=self.rng.uniform(0.6, 1.0),
            expertise=0.9 if hot else 0.15,
            novelty=self.rng.uniform(0.2, 0.7),
            responding_to="human",
        )
        return f"[{persona.name}] {tic} …stub response to \u201c{last[:60]}\u201d", signals


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

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            return None
        data = json.loads(text)

        utterance = sanitise(data.pop("utterance", ""))
        if not utterance:
            return None
        return utterance, Signals(**data)
