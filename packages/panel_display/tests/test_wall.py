"""What the wall shows, tested without a browser.

The point of keeping `WallState` pure is that these can exist at all. A video
wall is otherwise a thing you can only check by looking at it, in a venue,
once, on the night — and the states most worth checking (an agent ducked
mid-turn, a hand raised that nobody took, the floor open to the whole panel)
are exactly the ones that will not happen to occur while someone is watching.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from panel_core import (
    HUMAN,
    AgentSpeechEnded,
    AgentSpeechStarted,
    CueModerator,
    DuckSpeech,
    HandsRaised,
    HumanSpeechEnded,
    HumanSpeechStarted,
    PanelCast,
    RequestProposals,
    ResumeSpeech,
    StartSpeech,
    StateChanged,
    StopReason,
    StopSpeech,
    TranscriptUpdated,
)
from panel_display.wall import (
    ACCENTS,
    DUCKED,
    IDLE,
    SPEAKING,
    THINKING,
    TRANSCRIPT_LINES,
    WallState,
)

PERSONAS = Path(__file__).resolve().parents[3] / "personas"


@pytest.fixture(scope="module")
def cast() -> PanelCast:
    return PanelCast.from_dir(PERSONAS)


@pytest.fixture
def wall(cast: PanelCast) -> WallState:
    return WallState.for_cast(cast)


def painted(wall: WallState, agent: str) -> dict:
    return next(a for a in wall.snapshot()["agents"] if a["id"] == agent)


def state_changed(**extra) -> StateChanged:
    """A `StateChanged` with the keys `_paint` always sets."""
    base = {
        "invited": None,
        "invitation_source": None,
        "invitation_role": None,
        "invitation_rule": None,
        "address_conflict": (),
        "awaiting": None,
        "killed": False,
        "intro_remaining": None,
        "intro_done": False,
    }
    return StateChanged(floor_holder=None, speaking=None, turn_id=1, extra=base | extra)


# ---------------------------------------------------------------- the cast


def test_every_agent_gets_a_lane_and_a_distinct_accent(wall: WallState, cast: PanelCast):
    assert tuple(wall.order) == cast.ids()
    accents = [view.accent for view in wall.agents.values()]
    assert accents == list(ACCENTS[: len(accents)])
    assert len(set(accents)) == len(accents)


def test_the_snapshot_carries_the_cast_in_stage_order(wall: WallState, cast: PanelCast):
    painted_ids = [a["id"] for a in wall.snapshot()["agents"]]
    assert painted_ids == list(cast.ids())


# -------------------------------------------------------------- who speaks


def test_speaking_lights_the_orb_and_ending_puts_it_out(wall: WallState):
    wall.apply_event(AgentSpeechStarted(t=1.0, agent="wayne"))
    assert painted(wall, "wayne")["state"] == SPEAKING

    wall.apply_event(AgentSpeechEnded(t=2.0, agent="wayne", completed=True))
    assert painted(wall, "wayne")["state"] == IDLE


def test_only_one_agent_can_be_shown_speaking(wall: WallState):
    """A dropped `AgentSpeechEnded` must not leave two orbs lit.

    Nothing deliberately overlaps two agents — agent-to-agent interrupts were
    removed — so two lit orbs is always a bug somewhere upstream. The wall
    should render the truth (one voice on the PA) rather than propagate it.
    """
    wall.apply_event(AgentSpeechStarted(t=1.0, agent="dex"))
    wall.apply_event(AgentSpeechStarted(t=2.0, agent="melia"))

    assert painted(wall, "dex")["state"] == IDLE
    assert painted(wall, "melia")["state"] == SPEAKING


def test_a_backchannel_ducks_the_orb_without_putting_it_out(wall: WallState):
    """`DuckSpeech` never touches `PanelState`, so only the command path sees it."""
    wall.apply_event(AgentSpeechStarted(t=1.0, agent="melia"))
    wall.apply_command(DuckSpeech(agent="melia", gain_db=-12.0, ramp_ms=120))
    assert painted(wall, "melia")["state"] == DUCKED

    wall.apply_command(ResumeSpeech(agent="melia", ramp_ms=180))
    assert painted(wall, "melia")["state"] == SPEAKING


def test_a_duck_on_a_silent_agent_changes_nothing(wall: WallState):
    assert wall.apply_command(DuckSpeech(agent="dex", gain_db=-12.0, ramp_ms=120)) is False
    assert painted(wall, "dex")["state"] == IDLE


def test_an_interrupt_puts_the_orb_out(wall: WallState):
    wall.apply_event(AgentSpeechStarted(t=1.0, agent="dex"))
    wall.apply_command(StopSpeech(agent="dex", reason=StopReason.HUMAN_INTERRUPT))
    assert painted(wall, "dex")["state"] == IDLE


def test_the_orb_lights_on_audio_not_on_the_grant(wall: WallState):
    """`StartSpeech` can carry a silent `lead_in_s`, and an orb lit during it
    is an orb lit before the voice."""
    wall.apply_command(StartSpeech(agent="dex", utterance="", turn_id=1, lead_in_s=0.4))
    assert painted(wall, "dex")["state"] == IDLE

    wall.apply_event(AgentSpeechStarted(t=1.0, agent="dex"))
    assert painted(wall, "dex")["state"] == SPEAKING


# ------------------------------------------------------------ who is asked


def test_being_asked_for_a_proposal_reads_as_thinking(wall: WallState):
    wall.apply_command(RequestProposals(agents=("dex", "melia"), reason="partial"))
    assert painted(wall, "dex")["state"] == THINKING
    assert painted(wall, "melia")["state"] == THINKING
    assert painted(wall, "wayne")["state"] == IDLE


def test_thinking_never_interrupts_a_live_turn(wall: WallState):
    """An agent holding the floor is asked for proposals routinely."""
    wall.apply_event(AgentSpeechStarted(t=1.0, agent="wayne"))
    wall.apply_command(RequestProposals(agents=("wayne",), reason="partial"))
    assert painted(wall, "wayne")["state"] == SPEAKING


def test_an_invitation_marks_the_agent_without_lighting_the_orb(wall: WallState):
    wall.apply_command(state_changed(invited="melia", invitation_source="address"))
    assert painted(wall, "melia")["invited"] is True
    assert painted(wall, "melia")["state"] == IDLE
    assert painted(wall, "dex")["invited"] is False
    assert wall.snapshot()["open_floor"] is False


def test_an_open_floor_marks_the_whole_panel(wall: WallState):
    """`invited=None` with a live invitation is open to everyone, not nobody."""
    wall.apply_command(state_changed(invited=None, invitation_source="open_question"))
    assert wall.snapshot()["open_floor"] is True
    assert all(a["invited"] for a in wall.snapshot()["agents"])


def test_a_closed_floor_clears_every_mark(wall: WallState):
    wall.apply_command(state_changed(invited="dex", invitation_source="address"))
    wall.apply_command(state_changed(invited=None, invitation_source=None))
    assert not any(a["invited"] for a in wall.snapshot()["agents"])
    assert wall.snapshot()["open_floor"] is False


def test_awaiting_an_invited_agent_reads_as_thinking(wall: WallState):
    wall.apply_command(state_changed(invited="dex", invitation_source="address", awaiting="dex"))
    assert painted(wall, "dex")["state"] == THINKING


def test_a_raised_hand_is_shown_and_cleared(wall: WallState):
    wall.apply_command(HandsRaised(agents=(("dex", 0.82), ("wayne", 0.41))))
    assert painted(wall, "dex")["hand"] == 0.82
    assert painted(wall, "wayne")["hand"] == 0.41
    assert painted(wall, "melia")["hand"] is None

    wall.apply_command(CueModerator(reason="no_invitation"))
    assert all(a["hand"] is None for a in wall.snapshot()["agents"])


def test_a_hand_is_dropped_once_that_agent_speaks(wall: WallState):
    wall.apply_command(HandsRaised(agents=(("dex", 0.82),)))
    wall.apply_event(AgentSpeechStarted(t=1.0, agent="dex"))
    assert painted(wall, "dex")["hand"] is None


# ------------------------------------------------------------- the moderator


def test_the_cue_shows_and_clears_when_anyone_speaks(wall: WallState):
    wall.apply_command(CueModerator(reason="invitation_spent"))
    assert wall.snapshot()["cue"] == "invitation_spent"

    wall.apply_event(AgentSpeechStarted(t=1.0, agent="dex"))
    assert wall.snapshot()["cue"] is None


def test_the_cue_clears_when_ricky_starts_talking(wall: WallState):
    wall.apply_command(CueModerator(reason="invitation_spent"))
    wall.apply_event(HumanSpeechStarted(t=1.0))
    assert wall.snapshot()["cue"] is None
    assert wall.snapshot()["human_speaking"] is True

    wall.apply_event(HumanSpeechEnded(t=2.0))
    assert wall.snapshot()["human_speaking"] is False


def test_a_cue_reason_survives_being_a_plain_string(wall: WallState):
    """`CueReason` is a str-mixin Enum in the runtime and a bare str in the
    demo driver. Both have to render."""
    wall.apply_command(CueModerator(reason="beat_complete"))
    assert wall.snapshot()["cue"] == "beat_complete"


# -------------------------------------------------------------- transcript


def test_partials_replace_and_finals_append(wall: WallState):
    wall.apply_event(TranscriptUpdated(t=1.0, speaker=HUMAN, text="so let's", is_final=False))
    assert wall.snapshot()["partial"] == "so let's"

    wall.apply_event(TranscriptUpdated(t=1.2, speaker=HUMAN, text="so let's start", is_final=False))
    assert wall.snapshot()["partial"] == "so let's start"
    assert wall.snapshot()["lines"] == []

    wall.apply_event(TranscriptUpdated(t=1.5, speaker=HUMAN, text="So let's start.", is_final=True))
    assert wall.snapshot()["partial"] == ""
    assert wall.snapshot()["lines"] == ["So let's start."]


def test_agent_speech_never_enters_the_transcript(wall: WallState):
    """Agent turns are a deferred feature of this lane, not an accidental one.

    They arrive complete, after the fact, and would land on the wall as a
    paragraph appearing at once once the agent had already stopped talking.
    """
    wall.apply_event(TranscriptUpdated(t=1.0, speaker="wayne", text="Look —", is_final=True))
    assert wall.snapshot()["lines"] == []


def test_an_empty_final_is_not_a_line(wall: WallState):
    wall.apply_event(TranscriptUpdated(t=1.0, speaker=HUMAN, text="   ", is_final=True))
    assert wall.snapshot()["lines"] == []
    assert wall.snapshot()["line_seq"] == 0


def test_the_window_slides_and_the_sequence_keeps_counting(wall: WallState):
    """`line_seq` counts every line ever; `lines` is only what still fits.

    The client reconciles on the difference between the two, which is what
    lets it append a new line instead of re-animating the whole lane.
    """
    total = TRANSCRIPT_LINES + 5
    for index in range(total):
        wall.apply_event(
            TranscriptUpdated(t=float(index), speaker=HUMAN, text=f"line {index}", is_final=True)
        )

    snapshot = wall.snapshot()
    assert snapshot["line_seq"] == total
    assert len(snapshot["lines"]) == TRANSCRIPT_LINES
    assert snapshot["lines"][0] == f"line {total - TRANSCRIPT_LINES}"
    assert snapshot["lines"][-1] == f"line {total - 1}"


# ------------------------------------------------------------------ repaint


def test_a_repaint_that_changes_nothing_reports_no_change(wall: WallState):
    """`StateChanged` fires on nearly every transition; most change no pixels."""
    assert wall.apply_command(state_changed(invited="dex", invitation_source="address")) is True
    assert wall.apply_command(state_changed(invited="dex", invitation_source="address")) is False


def test_the_revision_only_moves_when_the_picture_does(wall: WallState):
    start = wall.revision
    wall.apply_command(state_changed())
    unchanged = wall.revision

    wall.apply_event(AgentSpeechStarted(t=1.0, agent="dex"))
    assert wall.revision > unchanged >= start


def test_a_tick_is_not_a_repaint(wall: WallState):
    from panel_core import Tick

    assert wall.apply_event(Tick(t=1.0)) is False


def test_the_kill_switch_reaches_the_wall(wall: WallState):
    wall.apply_command(state_changed(killed=True))
    assert wall.snapshot()["killed"] is True
