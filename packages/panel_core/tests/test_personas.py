"""Persona schema tests: aliases and pronunciation data.

Regression coverage for the STT misrecognition failure (Ricky said "Melia",
Speechmatics transcribed "Amelia", which the floor's `\\b(amelia|melia)\\b`
address pattern cannot match because the leading "A" removes the word
boundary — see CLAUDE.md and `panel_runtime.stt`'s module docstring).

The fix moved pronunciation data into the personas themselves rather than
hardcoding it into the STT session config, per CLAUDE.md's "personas are
data": `extra_aliases` (YAML key `aliases`) widens what the floor accepts in
text, and `sounds_like` biases what STT emits in the first place.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from panel_core import PanelCast, Persona

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"


@pytest.fixture
def cast() -> PanelCast:
    return PanelCast.from_dir(PERSONA_DIR)


def test_aliases_includes_yaml_declared_alias(cast: PanelCast) -> None:
    """`amelia` is declared in personas/melia.yaml as a belt-and-braces
    fallback for whatever the STT vocabulary bias still misses."""
    assert "amelia" in cast["melia"].aliases()


def test_aliases_is_the_name_the_id_and_whatever_the_yaml_declares() -> None:
    """`aliases()`'s full contract, spelled out: `floor.py` builds its
    address regexes from this and nothing else, so the set has to be
    exactly the display name, the short id Ricky and the operator console
    use, and any spellings the YAML declares. Nothing is derived from the
    name's shape — a name a human might say differently is a fact about
    the persona and belongs in `aliases:`.
    """
    persona = Persona(
        id="melia",
        name="Melia",
        job_title="x",
        employer="x",
        background="x",
        stance="x",
        voice_id="x",
        communication_style="x",
        aliases=["amelia"],
    )
    aliases = persona.aliases()
    assert set(aliases) == {"melia", "amelia"}
    assert all(a == a.lower() for a in aliases), "floor.py assumes lowercase"


def test_aliases_returns_a_list_of_str(cast: PanelCast) -> None:
    """`floor.py` calls `persona.aliases()` and iterates it directly into a
    regex; the return type is part of the contract, not an implementation
    detail."""
    for persona in cast.personas.values():
        aliases = persona.aliases()
        assert isinstance(aliases, list)
        assert all(isinstance(a, str) for a in aliases)


def test_extra_aliases_defaults_to_empty() -> None:
    """A persona with no `aliases:` in its YAML gets no surprises."""
    persona = Persona(
        id="dex",
        name="Dexter",
        job_title="x",
        employer="x",
        background="x",
        stance="x",
        voice_id="x",
        communication_style="x",
    )
    assert persona.extra_aliases == []
    assert persona.sounds_like == []


def test_canonical_name_matches_the_alias_floor_addresses_use(cast: PanelCast) -> None:
    """`canonical_name` is what STT vocabulary bias targets (see
    `vocab_from_cast`) and `aliases()` is what the floor will accept in the
    resulting transcript. If those two ever disagreed, we would be nudging
    STT towards a spelling the address pattern then couldn't match — the
    exact failure the "Amelia" incident was, arrived at by a different
    route. Asserted across the real cast, because it is a property of the
    personas as shipped, not of the schema.
    """
    for persona in cast.personas.values():
        assert persona.canonical_name.lower() in persona.aliases()


def test_only_melia_declares_sounds_like_today(cast: PanelCast) -> None:
    """Dexter and Wayne are common English names already well represented
    in STT vocabularies — see the comments in their YAML files for why they
    deliberately carry no `sounds_like` hints yet."""
    with_hints = {p.id for p in cast.personas.values() if p.sounds_like}
    assert with_hints == {"melia"}
