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
    UnverifiedSpeechDetected,
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


def test_a_group_invitation_marks_the_two_it_names_and_not_the_third(wall: WallState):
    """Ricky naming two panellists is a fourth state, not an open floor.

    `invited` is None for a group as well as for an open floor, so reading it
    alone would light all three and tell the audience the floor was open to
    everyone when it was not. `invited_agents` is the authoritative set.
    """
    wall.apply_command(
        state_changed(
            invited=None,
            invited_agents=("melia", "wayne"),
            invitation_source="address",
        )
    )
    assert wall.snapshot()["open_floor"] is False
    assert painted(wall, "melia")["invited"] is True
    assert painted(wall, "wayne")["invited"] is True
    assert painted(wall, "dex")["invited"] is False


def test_a_group_beat_reads_as_thinking_for_both(wall: WallState):
    wall.apply_command(
        state_changed(
            invited=None,
            invited_agents=("melia", "wayne"),
            invitation_source="address",
            awaiting=None,
            awaiting_agents=("melia", "wayne"),
        )
    )
    assert painted(wall, "melia")["state"] == THINKING
    assert painted(wall, "wayne")["state"] == THINKING
    assert painted(wall, "dex")["state"] == IDLE


def test_a_log_written_before_the_set_existed_still_replays(wall: WallState):
    """`invited` alone is the pre-2-Oct shape, and a rehearsal log keeps it."""
    wall.apply_command(
        state_changed(invited="melia", invitation_source="address", awaiting="melia")
    )
    assert wall.snapshot()["open_floor"] is False
    assert painted(wall, "melia")["invited"] is True
    assert painted(wall, "melia")["state"] == THINKING
    assert painted(wall, "dex")["invited"] is False


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


def line(speaker: str, text: str) -> dict:
    return {"speaker": speaker, "text": text}


def partial(speaker: str, text: str) -> dict:
    return {"speaker": speaker, "text": text}


def test_partials_replace_and_finals_append(wall: WallState):
    wall.apply_event(TranscriptUpdated(t=1.0, speaker=HUMAN, text="so let's", is_final=False))
    assert wall.snapshot()["partials"] == [partial(HUMAN, "so let's")]

    wall.apply_event(TranscriptUpdated(t=1.2, speaker=HUMAN, text="so let's start", is_final=False))
    assert wall.snapshot()["partials"] == [partial(HUMAN, "so let's start")]
    assert wall.snapshot()["lines"] == []

    wall.apply_event(TranscriptUpdated(t=1.5, speaker=HUMAN, text="So let's start.", is_final=True))
    assert wall.snapshot()["partials"] == []
    assert wall.snapshot()["lines"] == [line(HUMAN, "So let's start.")]


def test_an_agent_transcript_before_it_ever_held_the_floor_is_dropped(wall: WallState):
    """Agent lines now come from a real transcription session over that
    agent's own played audio — but only audio that was actually played.

    An agent that has never been put on the PA has produced no sound, so a
    `TranscriptUpdated` naming it is not a real line and must not paint one.
    `last_on_air` is what makes that distinguishable without a clock.
    """
    wall.apply_event(TranscriptUpdated(t=1.0, speaker="wayne", text="Look —", is_final=True))
    assert wall.snapshot()["lines"] == []
    assert wall.snapshot()["partials"] == []


def test_a_transcript_for_an_unknown_speaker_is_dropped(wall: WallState):
    wall.apply_event(TranscriptUpdated(t=1.0, speaker="nobody", text="Hello.", is_final=True))
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
    assert snapshot["lines"][0] == line(HUMAN, f"line {total - TRANSCRIPT_LINES}")
    assert snapshot["lines"][-1] == line(HUMAN, f"line {total - 1}")


def test_an_agents_own_transcription_paints_its_lines(wall: WallState):
    """Partials show live and finals append, exactly as Ricky's do — same
    event, same protocol, a different socket."""
    wall.apply_event(AgentSpeechStarted(t=1.0, agent="wayne"))

    wall.apply_event(
        TranscriptUpdated(t=1.1, speaker="wayne", text="adoption already", is_final=False)
    )
    assert wall.snapshot()["partials"] == [partial("wayne", "adoption already")]
    assert wall.snapshot()["lines"] == []

    wall.apply_event(
        TranscriptUpdated(
            t=1.4, speaker="wayne", text="Adoption already happened.", is_final=True
        )
    )
    wall.apply_event(
        TranscriptUpdated(t=1.9, speaker="wayne", text="Nobody noticed.", is_final=True)
    )

    assert wall.snapshot()["partials"] == []
    assert wall.snapshot()["lines"] == [
        line("wayne", "Adoption already happened."),
        line("wayne", "Nobody noticed."),
    ]
    assert wall.snapshot()["line_seq"] == 2


def test_the_last_line_of_a_turn_arrives_after_the_turn_ends(wall: WallState):
    """The reason the guard is `last_on_air` and not `state == SPEAKING`.

    An agent's session finalises the tail of its audio only once that audio
    has run out, so `AgentSpeechEnded` *always* beats the final carrying the
    last sentence. A state test would therefore drop the closing line of every
    single turn on the wall.
    """
    wall.apply_event(AgentSpeechStarted(t=1.0, agent="wayne"))
    wall.apply_event(AgentSpeechEnded(t=3.0, agent="wayne", completed=True))
    wall.apply_event(
        TranscriptUpdated(t=3.4, speaker="wayne", text="…and that is the point.", is_final=True)
    )
    assert wall.snapshot()["lines"] == [line("wayne", "…and that is the point.")]


def test_a_straggler_still_lands_once_another_agent_is_on_air(wall: WallState):
    """The race this exists for: a handover that beats the previous speaker's
    own STT session to its last sentence.

    Agent-to-agent handover can be sub-millisecond (a speculative proposal
    already parked), while an agent's own display-only STT session only
    finalises a segment after the audio has played *and* the endpointer has
    seen trailing silence — hundreds of ms behind. `AgentSpeechStarted` for
    the next agent routinely beats that final back. Each agent has its own
    dedicated session, so a late segment is still unambiguously that agent's
    own words, never the new speaker's — it is real, not a misattribution,
    and dropping it was losing speech that was actually said.
    """
    wall.apply_event(AgentSpeechStarted(t=1.0, agent="wayne"))
    wall.apply_event(AgentSpeechEnded(t=2.0, agent="wayne", completed=False))
    wall.apply_event(AgentSpeechStarted(t=2.1, agent="dex"))
    wall.apply_event(
        TranscriptUpdated(t=2.3, speaker="wayne", text="Cut off mid-thought.", is_final=True)
    )
    assert wall.snapshot()["lines"] == [line("wayne", "Cut off mid-thought.")]


def test_a_handover_leaves_the_previous_speakers_partial_in_place(wall: WallState):
    """A handover is no longer the thing that bounds a partial's life.

    The previous speaker is still owed either a final or `PARTIAL_TTL_S`
    (`server.py`) — ending it outright at the moment of handover was
    indistinguishable from a socket that genuinely dropped mid-sentence, and
    discarded sentences that were simply still arriving.
    """
    wall.apply_event(AgentSpeechStarted(t=1.0, agent="wayne"))
    wall.apply_event(
        TranscriptUpdated(t=1.1, speaker="wayne", text="never finished", is_final=False)
    )
    assert wall.snapshot()["partials"] == [partial("wayne", "never finished")]

    wall.apply_event(AgentSpeechStarted(t=2.0, agent="dex"))
    assert wall.snapshot()["partials"] == [partial("wayne", "never finished")]

    wall.apply_event(
        TranscriptUpdated(t=2.3, speaker="wayne", text="Never finished, after all.", is_final=True)
    )
    assert wall.snapshot()["partials"] == []
    assert wall.snapshot()["lines"] == [line("wayne", "Never finished, after all.")]


def test_a_stranger_on_the_mic_closes_ricky_unfinished_line(wall: WallState):
    """The leak the agents never had.

    An agent's abandoned partial is bounded by the next handover; Ricky's was
    bounded by nothing, so a question of his that never finalised — because
    the segment carrying its final came back attributed to the audience or to
    the PA bleeding into the room — stayed under the band for the whole show.
    `UnverifiedSpeechDetected` is the exact moment his socket moves on to
    somebody else's segment, and therefore the moment that line stops being
    in progress.
    """
    wall.apply_event(
        TranscriptUpdated(t=1.0, speaker=HUMAN, text="Dexter, is any of this", is_final=False)
    )
    assert wall.snapshot()["partials"] == [partial(HUMAN, "Dexter, is any of this")]

    wall.apply_event(UnverifiedSpeechDetected(t=1.4, is_final=True))
    assert wall.snapshot()["partials"] == []
    # And it contributes nothing of its own. The event has no text field and
    # never may — the words were never written down anywhere in the process.
    assert wall.snapshot()["lines"] == []


def test_a_stranger_does_not_disturb_an_agent_mid_sentence(wall: WallState):
    """It is Ricky's channel the stranger was heard on, so it is Ricky's line
    that is settled by it. An agent's own socket is unaffected and still owes
    the band a final."""
    wall.apply_event(AgentSpeechStarted(t=1.0, agent="wayne"))
    wall.apply_event(
        TranscriptUpdated(t=1.1, speaker="wayne", text="the sign-off is", is_final=False)
    )
    wall.apply_event(UnverifiedSpeechDetected(t=1.2, is_final=False))
    assert wall.snapshot()["partials"] == [partial("wayne", "the sign-off is")]


def test_dropping_a_partial_reports_whether_there_was_one(wall: WallState):
    """`DisplayServer._expire_partials` repaints off the return value, so a
    sweep that found nothing must not mark the wall dirty thirty times a
    second."""
    wall.apply_event(
        TranscriptUpdated(t=1.0, speaker=HUMAN, text="stopped arriving", is_final=False)
    )
    assert wall.drop_partial(HUMAN) is True
    assert wall.snapshot()["partials"] == []
    assert wall.drop_partial(HUMAN) is False


def test_a_ducked_agent_still_writes_to_the_band(wall: WallState):
    wall.apply_event(AgentSpeechStarted(t=1.0, agent="wayne"))
    wall.apply_command(DuckSpeech(agent="wayne", gain_db=-18.0, ramp_ms=80))
    wall.apply_event(
        TranscriptUpdated(
            t=1.1, speaker="wayne", text="Still talking, just quieter.", is_final=True
        )
    )
    assert wall.snapshot()["lines"] == [line("wayne", "Still talking, just quieter.")]


def test_two_voices_mid_sentence_both_show(wall: WallState):
    """The barge-in beat, and the reason `partials` is keyed by speaker.

    Ricky's first partials land while the interrupted agent's socket is still
    returning partials for audio the room has already heard. One shared string
    would flicker between them; two rows read the way the room sounds. Agents
    come first and the moderator last, so his in-progress line sits closest to
    where the finals land.
    """
    wall.apply_event(AgentSpeechStarted(t=1.0, agent="wayne"))
    wall.apply_event(
        TranscriptUpdated(t=1.1, speaker="wayne", text="the sign-off is", is_final=False)
    )
    wall.apply_event(TranscriptUpdated(t=1.2, speaker=HUMAN, text="hold on", is_final=False))

    assert wall.snapshot()["partials"] == [
        partial("wayne", "the sign-off is"),
        partial(HUMAN, "hold on"),
    ]

    wall.apply_event(
        TranscriptUpdated(t=1.3, speaker="wayne", text="The sign-off is theatre.", is_final=True)
    )
    assert wall.snapshot()["partials"] == [partial(HUMAN, "hold on")]
    assert wall.snapshot()["lines"] == [line("wayne", "The sign-off is theatre.")]


def test_moderator_and_agent_lines_interleave_in_speaking_order(wall: WallState):
    wall.apply_event(TranscriptUpdated(t=1.0, speaker=HUMAN, text="Wayne, go.", is_final=True))
    wall.apply_event(AgentSpeechStarted(t=2.0, agent="wayne"))
    wall.apply_event(TranscriptUpdated(t=2.1, speaker="wayne", text="Happy to.", is_final=True))
    wall.apply_event(AgentSpeechEnded(t=3.0, agent="wayne", completed=True))
    wall.apply_event(TranscriptUpdated(t=4.0, speaker=HUMAN, text="Thanks.", is_final=True))

    assert wall.snapshot()["lines"] == [
        line(HUMAN, "Wayne, go."),
        line("wayne", "Happy to."),
        line(HUMAN, "Thanks."),
    ]


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
