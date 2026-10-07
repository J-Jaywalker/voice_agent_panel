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
from panel_core import Beat, PanelCast, Persona

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
        introduction="x",
        intro_position=0,
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
        introduction="x",
        intro_position=0,
        voice_id="x",
        communication_style="x",
    )
    assert persona.extra_aliases == []
    assert persona.sounds_like == []


def test_beats_default_to_empty() -> None:
    """A persona with no `beats:` in its YAML carries no prepared material —
    the same shape as `anecdotes`, so an added persona is safe by default."""
    persona = Persona(
        id="dex",
        name="Dexter",
        job_title="x",
        employer="x",
        background="x",
        stance="x",
        introduction="x",
        intro_position=0,
        voice_id="x",
        communication_style="x",
    )
    assert persona.beats == {}


def test_a_beat_needs_only_a_cue() -> None:
    """`material` and `reacting_to` are both optional, because a beat is
    authored incrementally: the topic is known before the substance is
    written, and a half-authored beat must parse rather than block the cast
    from loading at all."""
    beat = Beat(cue="what changed when agents started dealing with agents")
    assert beat.material == []
    assert beat.reacting_to == {}


def test_the_real_cast_carries_beats_keyed_by_question(cast: PanelCast) -> None:
    """Question one is an open question to the panel, so all three have to
    have something prepared for it — an open floor with two agents holding
    material and one holding none is a turn that goes to whoever is loudest."""
    for persona in cast.personas.values():
        assert "q1" in persona.beats, f"{persona.id} has no q1 beat"
        assert persona.beats["q1"].material


def test_the_real_cast_also_carries_beats_for_question_two(cast: PanelCast) -> None:
    """Question two (what ASR has to become) is Dexter-led rather than open
    floor, but the follow-up still asks Wayne and Melia for their reactions,
    so all three need material."""
    for persona in cast.personas.values():
        assert "q2" in persona.beats, f"{persona.id} has no q2 beat"
        assert persona.beats["q2"].material


def test_dexters_q1_stands_up_without_a_colleague_having_spoken(cast: PanelCast) -> None:
    """His q1 claim is a general one about agent-to-agent delegation —
    identity proves who, not what they are authorised to do — so it has to
    survive nobody having set it up. Written as "the delegation half is true
    and you say so first" it produced a filler line on his first opportunity
    (Wayne had not spoken yet) and then a turn framed entirely around Wayne's
    anecdote once he had (rehearsal, 7 Oct 2026).

    So the material names no colleague at all, and everything conditional on
    one having spoken lives in `reacting_to`, which `build_system_prompt`
    renders behind "if X has just spoken on this same topic".
    """
    beat = cast["dex"].beats["q1"]
    colleagues = [n.lower() for a, p in cast.personas.items() if a != "dex" for n in (a, p.name)]
    for text in (beat.cue, *beat.material):
        named = [c for c in colleagues if c in text.lower()]
        assert not named, f"q1 material depends on {named}: {text}"
    joined = " ".join(beat.material).lower()
    assert "2030" in joined and "identity" in joined, "the standalone 2030/identity claim is missing"
    assert "wayne" in beat.reacting_to


def test_dexters_reaction_to_wayne_is_additive_not_corrective(cast: PanelCast) -> None:
    """Beat q1's dominant register is three genuine opinions landing side by
    side, not a chain of rebuttals — Dexter's own mid-thought self-correction
    ("actually", "no, no") is a separate, established part of his character
    (`communication_style`, `delivery`) and is untouched here. What this pins
    is narrower: his stated *relationship to Wayne's point* has to read as
    "in addition to", never as "but, actually, that's wrong"."""
    reaction = cast["dex"].beats["q1"].reacting_to["wayne"].lower()
    assert "rebuttal" in reaction, "additive-not-rebuttal framing got lost"
    assert any(kw in reaction for kw in ("on top of", "alongside", "in addition"))


def test_melias_wayne_friction_in_q1_stays_sharp(cast: PanelCast) -> None:
    """The one deliberate disagreement the beat keeps (CLAUDE.md / the script's
    PANEL DYNAMIC note: "some room for bickering") is Melia going after Wayne's
    own word "latency" from his introduction, and him not conceding. Retuning
    the rest of q1 toward addition must not soften this one exchange."""
    beat = cast["melia"].beats["q1"]
    assert "latency" in " ".join(beat.material).lower()
    reaction = beat.reacting_to["wayne"].lower()
    assert "not concede" in reaction or "will not concede" in reaction
    assert "soften" in reaction


def test_dexters_q2_frames_asr_as_moving_into_the_control_layer(cast: PanelCast) -> None:
    """The substantive content of q2: ASR stops being just transcription and
    becomes part of the decision/orchestration/control layer. This is the
    user's explicit main point for the beat, so pin it as content, not just
    presence."""
    joined = " ".join(cast["dex"].beats["q2"].material).lower()
    assert "transcription layer" in joined
    assert "orchestration" in joined or "control layer" in joined


def test_dexters_q2_does_not_leak_q1_or_q3_vocabulary(cast: PanelCast) -> None:
    """q2 is ASR's role — not q1's identity/authority/handshake framing and
    not q3's jailbreaking/security-attack framing. The beat's own material
    tells Dexter explicitly to stay off both (so a model straying there is
    disobeying an instruction, not an unguarded gap); this pins that the
    instruction itself never does what it's warning against by reusing their
    vocabulary wholesale. Terms chosen to be distinctive to those beats
    rather than generic English that could land here innocently."""
    beat = cast["dex"].beats["q2"]
    text = (beat.cue + " " + " ".join(beat.material)).lower()
    for banned in ("identity", "authority", "handshake", "jailbreak"):
        assert banned not in text, f"q2 leaks q1/q3 vocabulary: {banned!r}"


def test_melias_and_waynes_q2_bickering_is_not_q1s_additive_register(cast: PanelCast) -> None:
    """q1's reaction register was explicitly additive — never a rebuttal. q2's
    Wayne/Melia exchange is the opposite on purpose: sharp, personal,
    bickering, and the YAML has to say so rather than silently reusing q1's
    tone."""
    wayne_reaction = cast["wayne"].beats["q2"].reacting_to["melia"].lower()
    melia_reaction = cast["melia"].beats["q2"].reacting_to["wayne"].lower()
    for reaction in (wayne_reaction, melia_reaction):
        assert "not q1" in reaction or "not additive" in reaction
        assert "additive" in reaction  # named explicitly, as the thing being departed from


def test_melias_q2_sandbox_line_survives(cast: PanelCast) -> None:
    """Melia's strongest line in q2 — asking Dexter, not Wayne, to put Wayne
    in a sandbox — is specific enough to be worth preserving close to
    verbatim as a style target."""
    reaction = cast["melia"].beats["q2"].reacting_to["wayne"].lower()
    assert "sandbox" in reaction
    assert "dexter" in reaction, "the line is addressed to Dexter, not Wayne — that's the joke"


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


def test_only_melia_and_wayne_declare_sounds_like_today(cast: PanelCast) -> None:
    """Dexter is a common English name already well represented in STT
    vocabularies — see the comment in his YAML for why he deliberately
    carries no `sounds_like` hints yet. Melia's and Wayne's both fix real
    misrecognitions (see their own YAML comments)."""
    with_hints = {p.id for p in cast.personas.values() if p.sounds_like}
    assert with_hints == {"melia", "wayne"}
