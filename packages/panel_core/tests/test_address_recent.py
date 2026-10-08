"""`prompts.build_address_recent` — actual recent words, for TypeSafe only.

`build_address_context` (tested alongside the corpus in `test_address.py` and
`test_llm_address.py`) gives the classifier names only. This is the richer,
TypeSafe-only companion: the words themselves, so "Sorry, can you go again
please?" is resolvable against what the last agent was actually saying, and a
question split across several STT finals is seen whole rather than one
fragment at a time. See `prompts.build_address_recent`'s docstring for the
design; these tests pin the behaviour it promises.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from panel_core import HUMAN, PanelCast, PanelState
from panel_core.prompts import build_address_recent
from panel_core.state import Invitation, InvitationSource, Utterance

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"


@pytest.fixture
def cast() -> PanelCast:
    return PanelCast.from_dir(PERSONA_DIR)


@pytest.fixture
def state(cast: PanelCast) -> PanelState:
    return PanelState.for_agents(cast.ids())


def test_a_question_split_across_several_finals_comes_back_whole(state, cast):
    """The split-turn case the feature exists for: several Ricky finals in a
    row, none of them a question alone, with the segment just heard (not yet
    in `state.transcript` — the reducer lags one task behind) appended last."""
    state = replace(
        state,
        transcript=(
            Utterance(speaker=HUMAN, text="So I wanted to", t=1.0),
            Utterance(speaker=HUMAN, text="ask about", t=1.1),
        ),
    )
    recent = build_address_recent(state, cast, current="the security side of things?")
    assert recent == [
        {"from": "Ricky", "text": "So I wanted to"},
        {"from": "Ricky", "text": "ask about"},
        {"from": "Ricky", "text": "the security side of things?"},
    ]


def test_the_last_agents_own_last_two_turns_are_surfaced_first(state, cast):
    """Melia spoke, Ricky cuts across her mid-sentence: her own last two turns
    come first (what is being re-addressed), then Ricky's reaction — not a
    strict timestamp merge, because her cut-short `Utterance` is only written
    at `AgentSpeechEnded`, after the words that interrupted it."""
    state = replace(
        state,
        transcript=(
            Utterance(speaker="dex", text="earlier, unrelated turn", t=0.0),
            Utterance(speaker="melia", text="the way I see latency budgets", t=1.0),
            Utterance(speaker="melia", text="is that most teams under-", t=1.5),
            Utterance(speaker=HUMAN, text="Sorry,", t=2.0),
        ),
    )
    recent = build_address_recent(state, cast, current="can you go again please?")
    assert recent == [
        {"from": "Melia", "text": "the way I see latency budgets"},
        {"from": "Melia", "text": "is that most teams under-"},
        {"from": "Ricky", "text": "Sorry,"},
        {"from": "Ricky", "text": "can you go again please?"},
    ]


def test_a_mid_turn_agent_contributes_their_live_partial(state, cast):
    """An agent still speaking has no `Utterance` yet for the line in
    progress — `state.speaking`/`state.agent_partial` stand in for it."""
    state = replace(
        state,
        transcript=(Utterance(speaker="melia", text="the key number here is", t=1.0),),
        speaking="melia",
        agent_partial="forty milliseconds, which",
    )
    recent = build_address_recent(state, cast, current="sorry, go again?")
    assert recent == [
        {"from": "Melia", "text": "the key number here is"},
        {"from": "Melia", "text": "forty milliseconds, which"},
        {"from": "Ricky", "text": "sorry, go again?"},
    ]


def test_the_agent_half_is_suppressed_during_a_live_introduction_round(state, cast):
    """CLAUDE.md: `INTRODUCTIONS` is a measured, fragile prompt surface — the
    scripted intro lines are not conversation and must not be fed in as if
    they were. Ricky's own words are never suppressed."""
    state = replace(
        state,
        transcript=(
            Utterance(speaker="dex", text="We discussed this.", t=1.0),
            Utterance(speaker=HUMAN, text="And", t=2.0),
        ),
        intro_queue=("melia", "wayne"),
    )
    recent = build_address_recent(state, cast, current="who else have we got?")
    assert recent == [
        {"from": "Ricky", "text": "And"},
        {"from": "Ricky", "text": "who else have we got?"},
    ]


def test_the_agent_half_is_suppressed_under_a_standing_introduction_invitation(state, cast):
    state = replace(
        state,
        transcript=(Utterance(speaker="dex", text="We discussed this.", t=1.0),),
        invitation=Invitation(
            agents=("dex", "melia", "wayne"),
            turns_remaining=3,
            source=InvitationSource.INTRODUCTION,
            t=1.0,
        ),
    )
    recent = build_address_recent(state, cast, current="go on then")
    assert recent == [{"from": "Ricky", "text": "go on then"}]


def test_nothing_before_the_current_segment_is_empty_not_a_crash(state, cast):
    assert build_address_recent(state, cast, current="") == []
