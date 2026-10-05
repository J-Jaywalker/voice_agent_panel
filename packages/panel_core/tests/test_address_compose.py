"""`compose_address_verdict` — the TypeSafe backend's recomposition step.

No network, no `typesafe_sdk` import: every input is a plain float or a plain
mapping the runtime has already pulled out of a `SystemOneResponse`. This is
the one place a bad vector could put the wrong panellist on the PA, and it
never needs a model call to test.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from panel_core import PanelCast
from panel_core.prompts import (
    _MODE_INTRODUCTIONS,
    _MODE_NOBODY,
    _MODE_SPECIFIC_PANELLISTS,
    _MODE_WHOLE_PANEL,
    AMBIGUOUS_VERDICT,
    INTRO_VERDICT,
    NO_VERDICT,
    OPEN_VERDICT,
    AddressThresholds,
    compose_address_verdict,
)

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"

_MODES = (
    _MODE_NOBODY,
    _MODE_WHOLE_PANEL,
    _MODE_INTRODUCTIONS,
    _MODE_SPECIFIC_PANELLISTS,
)


@pytest.fixture
def cast() -> PanelCast:
    return PanelCast.from_dir(PERSONA_DIR)


def modes(top: str, p: float = 0.95) -> dict[str, float]:
    """A mode distribution peaked on `top`, with the rest sharing the remainder."""
    rest = (1.0 - p) / (len(_MODES) - 1)
    return {mode: (p if mode == top else rest) for mode in _MODES}


def verdict(
    cast: PanelCast,
    *,
    mode: str = _MODE_SPECIFIC_PANELLISTS,
    mode_p: float = 0.95,
    mode_probabilities: dict[str, float] | None = None,
    joint_request: float = 0.0,
    unidentifiable: float = 0.0,
    thresholds: AddressThresholds | None = None,
    **agents: float,
):
    """`agents` is `agent_id=probability`."""
    return compose_address_verdict(
        mode_probabilities=(
            mode_probabilities if mode_probabilities is not None else modes(mode, mode_p)
        ),
        agent_probabilities=agents,
        joint_request=joint_request,
        unidentifiable=unidentifiable,
        cast=cast,
        thresholds=thresholds or AddressThresholds(),
    )


# --------------------------------------------------------------- structural


def test_nobody_mode_is_no_verdict(cast: PanelCast):
    assert verdict(cast, mode=_MODE_NOBODY) == NO_VERDICT


def test_whole_panel_mode_is_open(cast: PanelCast):
    assert verdict(cast, mode=_MODE_WHOLE_PANEL) == OPEN_VERDICT


def test_introductions_mode_is_intro(cast: PanelCast):
    assert verdict(cast, mode=_MODE_INTRODUCTIONS) == INTRO_VERDICT


def test_flat_mode_distribution_fails_closed(cast: PanelCast):
    """No mode clears `mode_floor`, so the structural answer is not trusted."""
    flat = dict.fromkeys(_MODES, 0.25)
    assert verdict(cast, mode_probabilities=flat) is None


def test_flat_mode_distribution_with_unidentifiable_is_ambiguous(cast: PanelCast):
    flat = dict.fromkeys(_MODES, 0.25)
    assert verdict(cast, mode_probabilities=flat, unidentifiable=0.8) == AMBIGUOUS_VERDICT


def test_unknown_mode_value_fails_closed(cast: PanelCast):
    """Off-script output is never guessed at, the same rule as an undecodable token."""
    assert verdict(cast, mode_probabilities={"something_else": 0.95}) is None


def test_empty_mode_distribution_fails_closed(cast: PanelCast):
    assert verdict(cast, mode_probabilities={}) is None


# ---------------------------------------------------------- specific panellists


def test_one_agent_above_threshold_is_named(cast: PanelCast):
    assert verdict(cast, dex=0.05, melia=0.92, wayne=0.04) == "MELIA"


def test_two_agents_above_threshold_join_in_cast_order(cast: PanelCast):
    assert verdict(cast, dex=0.88, melia=0.91, wayne=0.03) == "DEXTER+MELIA"


def test_naming_everybody_falls_through_to_mode(cast: PanelCast):
    """A unanimous agent vector is not evidence of a group — mode decides."""
    assert verdict(cast, dex=0.9, melia=0.95, wayne=0.88) == OPEN_VERDICT
    assert verdict(cast, mode=_MODE_INTRODUCTIONS, dex=0.9, melia=0.95, wayne=0.88) == INTRO_VERDICT
    assert verdict(cast, mode=_MODE_NOBODY, dex=0.9, melia=0.95, wayne=0.88) == NO_VERDICT


def test_named_agent_below_threshold_is_not_included(cast: PanelCast):
    assert verdict(cast, dex=0.3, melia=0.92, wayne=0.04) == "MELIA"


def test_named_set_is_a_prefix_of_the_ranking_never_a_hole(cast: PanelCast):
    """Wayne fails the companion gate, so Dexter is excluded behind him even
    though his own probability clears `companion`."""
    result = verdict(cast, joint_request=0.9, dex=0.26, melia=0.9, wayne=0.3)
    assert result == "MELIA"


# ------------------------------------------------------------- companion tier


def test_companion_is_admitted_when_the_request_is_joint(cast: PanelCast):
    result = verdict(cast, joint_request=0.9, dex=0.9, melia=0.5, wayne=0.05)
    assert result == "DEXTER+MELIA"


def test_companion_is_refused_when_the_request_is_not_joint(cast: PanelCast):
    """The same vector without the joint-request gate names one panellist."""
    result = verdict(cast, joint_request=0.1, dex=0.9, melia=0.5, wayne=0.05)
    assert result == "DEXTER"


def test_companion_is_refused_below_the_companion_threshold(cast: PanelCast):
    result = verdict(cast, joint_request=0.9, dex=0.9, melia=0.2, wayne=0.02)
    assert result == "DEXTER"


def test_companion_is_refused_without_daylight_over_the_rest(cast: PanelCast):
    """Melia clears `companion` but is not separable from Wayne."""
    result = verdict(cast, joint_request=0.9, dex=0.9, melia=0.5, wayne=0.4)
    assert result == "DEXTER"


def test_companion_never_opens_a_set_on_its_own(cast: PanelCast):
    """Nobody clears `addressed`, so there is no set for a companion to join."""
    result = verdict(cast, joint_request=0.9, dex=0.5, melia=0.4, wayne=0.05)
    assert result is None


# --------------------------------------------------------------- mode vetoes


def test_nobody_mode_vetoes_a_named_set(cast: PanelCast):
    """A confident `nobody` contradicts a peaked agent vector — mode wins."""
    result = verdict(cast, mode=_MODE_NOBODY, dex=0.95, melia=0.05, wayne=0.05)
    assert result == NO_VERDICT


def test_introductions_mode_vetoes_a_named_set(cast: PanelCast):
    result = verdict(cast, mode=_MODE_INTRODUCTIONS, dex=0.95, melia=0.05, wayne=0.05)
    assert result == INTRO_VERDICT


def test_whole_panel_mode_does_not_veto_a_named_set(cast: PanelCast):
    """Discriminating a subset from the whole panel is what the vector is for."""
    result = verdict(cast, mode=_MODE_WHOLE_PANEL, dex=0.95, melia=0.05, wayne=0.05)
    assert result == "DEXTER"


def test_veto_mass_below_the_threshold_leaves_the_named_set_standing(cast: PanelCast):
    spread = {
        _MODE_NOBODY: 0.4,
        _MODE_SPECIFIC_PANELLISTS: 0.5,
        _MODE_WHOLE_PANEL: 0.05,
        _MODE_INTRODUCTIONS: 0.05,
    }
    result = verdict(cast, mode_probabilities=spread, dex=0.95, melia=0.05, wayne=0.05)
    assert result == "DEXTER"


# --------------------------------------------------------- daylight / ambiguity


def test_no_daylight_between_named_and_unnamed_is_ambiguous_if_flagged(cast: PanelCast):
    """Close enough to be the same claim, and the model says so explicitly."""
    result = verdict(cast, dex=0.65, melia=0.45, wayne=0.04, unidentifiable=0.7)
    assert result == AMBIGUOUS_VERDICT


def test_no_daylight_without_the_ambiguous_flag_fails_closed(cast: PanelCast):
    """Same vector, but nothing confirms it's ambiguity rather than noise."""
    assert verdict(cast, dex=0.65, melia=0.45, wayne=0.04, unidentifiable=0.2) is None


def test_nobody_clears_the_bar_but_unidentifiable_is_high_is_ambiguous(cast: PanelCast):
    result = verdict(cast, dex=0.3, melia=0.35, wayne=0.1, unidentifiable=0.9)
    assert result == AMBIGUOUS_VERDICT


def test_nobody_clears_the_bar_and_unidentifiable_is_low_fails_closed(cast: PanelCast):
    """A flat, unconvincing vector is silence, never a guessed NO_VERDICT."""
    assert verdict(cast, dex=0.3, melia=0.35, wayne=0.1, unidentifiable=0.1) is None


# -------------------------------------------------- NO_VERDICT has one source


@pytest.mark.parametrize("top", _MODES)
@pytest.mark.parametrize(
    "agent_vector", [(0.95, 0.05, 0.05), (0.9, 0.5, 0.05), (0.3, 0.35, 0.1), (0.9, 0.95, 0.88)]
)
@pytest.mark.parametrize("joint_request", [0.0, 0.9])
@pytest.mark.parametrize("unidentifiable", [0.0, 0.9])
def test_no_verdict_only_ever_comes_from_a_confident_nobody(
    cast: PanelCast,
    top: str,
    agent_vector: tuple[float, float, float],
    joint_request: float,
    unidentifiable: float,
):
    """`NONE` is a real answer, so it may only come from the model saying it.

    Every other path fails closed with `None` and hands the question to the
    regex; inventing a `NONE` from an unconvincing vector would silently
    swallow a real invitation.
    """
    dex, melia, wayne = agent_vector
    result = verdict(
        cast,
        mode=top,
        joint_request=joint_request,
        unidentifiable=unidentifiable,
        dex=dex,
        melia=melia,
        wayne=wayne,
    )
    if result == NO_VERDICT:
        assert top == _MODE_NOBODY


def test_no_verdict_is_unreachable_below_the_mode_floor(cast: PanelCast):
    """Even a `nobody` peak has to clear `mode_floor` to be acted on."""
    for unidentifiable, joint_request in itertools.product((0.0, 0.9), (0.0, 0.9)):
        result = verdict(
            cast,
            mode=_MODE_NOBODY,
            mode_p=0.45,
            joint_request=joint_request,
            unidentifiable=unidentifiable,
            dex=0.3,
            melia=0.2,
            wayne=0.1,
        )
        assert result != NO_VERDICT


# -------------------------------------------------------------------- misc


def test_missing_agent_probability_defaults_low_rather_than_raising(cast: PanelCast):
    """An agent TypeSafe wasn't asked about (e.g. muted, dropped upstream)
    must not crash composition — it reads as confidently not addressed."""
    result = compose_address_verdict(
        mode_probabilities=modes(_MODE_SPECIFIC_PANELLISTS),
        agent_probabilities={"melia": 0.9},
        joint_request=0.0,
        unidentifiable=0.0,
        cast=cast,
    )
    assert result == "MELIA"


def test_custom_thresholds_are_respected(cast: PanelCast):
    """A looser addressed bar admits a probability the default would refuse."""
    loose = AddressThresholds(addressed=0.5, daylight=0.1)
    agents = {"dex": 0.1, "melia": 0.55, "wayne": 0.1}
    assert verdict(cast, thresholds=loose, **agents) == "MELIA"
    assert verdict(cast, **agents) is None
