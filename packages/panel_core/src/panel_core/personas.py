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

    # --- fixed opening ---
    # Word-for-word, spoken every time, never generated. The introduction
    # round used to ask the model for this live and it once came back
    # completely empty on stage-adjacent testing — an agent granted the
    # floor with nothing under it. Fixed text removes the failure class
    # outright rather than tuning around it, and it is what FEASIBILITY.md
    # 4.5 wants the opening to become anyway (pre-rendered to audio) — see
    # `FloorController._grant_introduction`, which is the only thing that
    # ever reads this field. Two or three sentences, written to be read
    # aloud, and — because a fixed round's speaking order cannot safely be
    # assumed by its own content (see `FloorController._start_introductions`
    # for why the order is fixed but the text still does not lean on it) —
    # written to stand on its own rather than responding to another
    # panellist's introduction.
    introduction: str = Field(
        min_length=1,
        description="Fixed, verbatim text for the introduction round. Never sent to a model.",
    )

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
    # Brevity is a prompt instruction (GUARDRAILS in prompts.py) — turn length
    # has no orchestrator-enforced ceiling; the moderator handles the rest live.
    verbosity: str = "low"

    # --- relationships ---
    # Generic disagreement sounds generic. This is what makes agent-to-agent
    # exchanges read as colleagues rather than as two models (FEASIBILITY.md 4.3).
    relationships: dict[str, str] = Field(default_factory=dict)

    # --- guardrails ---
    forbidden_topics: list[str] = Field(default_factory=list)

    # --- pronunciation ---
    # Belt-and-braces for the STT misrecognition class of failure (e.g.
    # "Melia" transcribed as "Amelia", which the address regex's leading
    # `\b` then can't match): `extra_aliases` widens what the *floor*
    # accepts in text; `sounds_like` biases what *STT* emits in the first
    # place. Both are facts about the persona's name, not about a
    # transcription session or a regex — CLAUDE.md "personas are data".
    #
    # The YAML key is `aliases`; the Python attribute is `extra_aliases`
    # so it doesn't collide with the `aliases()` method below, whose
    # signature/return type other code (`floor.py`) depends on.
    extra_aliases: list[str] = Field(
        default_factory=list,
        alias="aliases",
        description=(
            "Extra written spellings a human (or a misrecognising STT "
            "engine) might use for this persona, e.g. 'amelia' for "
            "Melia. Keep conservative — a false match hands the floor "
            "to the wrong agent."
        ),
    )
    sounds_like: list[str] = Field(
        default_factory=list,
        description=(
            "Phonetic hints for this persona's canonical name, fed to "
            "STT as `additional_vocab` (see panel_runtime.stt). Empty "
            "means no vocabulary bias is requested for this persona."
        ),
    )

    @property
    def canonical_name(self) -> str:
        """The spoken form STT vocabulary bias should target.

        Every persona's `name` is now the single word a human on stage
        actually says, so this is `name` verbatim. It stays a named concept
        rather than callers reaching for `name` directly because what STT is
        biased towards (`panel_runtime.stt.vocab_from_cast`) and what the
        floor will accept in the resulting transcript (`aliases()`) have to
        agree, and that agreement is the thing worth naming. Anything a
        human might say *other* than this belongs in `extra_aliases`, where
        it is declared rather than derived.
        """
        return self.name

    def aliases(self) -> list[str]:
        """Names a human might use to address this agent directly."""
        aliases = {self.name.lower(), self.id.lower()}
        aliases.update(alias.lower() for alias in self.extra_aliases)
        return list(aliases)


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
