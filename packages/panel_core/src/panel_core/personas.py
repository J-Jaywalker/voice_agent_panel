"""Persona schema.

Personas are structured data, not prose, because the floor controller reads
them at runtime to make moment-to-moment decisions. The system prompt is
generated from this — the data is the source of truth.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Persona(BaseModel):
    id: str
    name: str
    job_title: str
    employer: str = Field(
        description="Always fictional. Never a real organisation — see FEASIBILITY.md 4.1."
    )
    background: str
    stance: str = Field(description="This agent's position on AI adoption.")

    # --- voice ---
    voice_id: str
    communication_style: str
    speech_tics: list[str] = Field(
        default_factory=list,
        description="Short verbal habits. Distinctiveness survives a PA better than timbre.",
    )

    # --- floor behaviour ---
    interrupt_tendency: float = Field(0.3, ge=0.0, le=1.0)
    yield_tendency: float = Field(
        0.7, ge=0.0, le=1.0, description="Modelled separately — not the inverse of interrupting."
    )
    topics_of_authority: list[str] = Field(default_factory=list)

    # --- length discipline ---
    # The most common failure mode of an LLM panel is a 45-second monologue.
    # Enforced by the orchestrator, not by prompting.
    target_turn_seconds: float = 18.0
    max_turn_seconds: float = 30.0
    verbosity: str = "low"

    # --- relationships ---
    # Generic disagreement sounds generic. This is what makes agent-to-agent
    # exchanges read as colleagues rather than as two models (FEASIBILITY.md 4.3).
    relationships: dict[str, str] = Field(default_factory=dict)

    # --- guardrails ---
    forbidden_topics: list[str] = Field(default_factory=list)

    def aliases(self) -> list[str]:
        """Names a human might use to address this agent directly."""
        first = self.name.split("-")[0].split(".")[-1].strip()
        return list({self.name.lower(), self.id.lower(), first.lower()})


class PanelCast(BaseModel):
    personas: dict[str, Persona]

    @classmethod
    def from_dir(cls, directory: Path) -> PanelCast:
        personas: dict[str, Persona] = {}
        for path in sorted(directory.glob("*.yaml")):
            data = yaml.safe_load(path.read_text())
            persona = Persona.model_validate(data)
            personas[persona.id] = persona
        if not personas:
            raise ValueError(f"no persona YAML files found in {directory}")
        return cls(personas=personas)

    def ids(self) -> tuple[str, ...]:
        return tuple(self.personas)

    def __getitem__(self, agent_id: str) -> Persona:
        return self.personas[agent_id]
