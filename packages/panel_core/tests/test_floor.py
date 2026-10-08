"""Floor-control tests.

These are the scoping doc's acceptance criteria, expressed as assertions.
Because the core is pure, each of these runs in microseconds and cannot flake.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from panel_core import (
    HUMAN,
    AgentAudioProgress,
    AgentProposal,
    AgentSpeechEnded,
    AgentSpeechStarted,
    AgentState,
    AgentUtteranceProgress,
    CueModerator,
    FloorConfig,
    FloorController,
    HandsRaised,
    HumanSpeechEnded,
    HumanSpeechStarted,
    OperatorAction,
    OperatorCommand,
    PanelCast,
    PanelState,
    RequestProposals,
    Signals,
    StartSpeech,
    StateChanged,
    StopReason,
    StopSpeech,
    Tick,
    TranscriptUpdated,
    TurnYielded,
    UnverifiedSpeechDetected,
)

# `CueReason` is the floor's own vocabulary and is not re-exported from the
# package root, which `panel_core` reserves for the event/command surface.
# The phrasing corpus that exercises `AddressRole` lives in test_address.py.
from panel_core.floor import CueReason
from panel_core.state import InvitationSource

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"


@pytest.fixture
def cast() -> PanelCast:
    return PanelCast.from_dir(PERSONA_DIR)


@pytest.fixture
def fc(cast: PanelCast) -> FloorController:
    return FloorController(cast, FloorConfig())


@pytest.fixture
def state(cast: PanelCast) -> PanelState:
    """Steady state: past introductions, like the rest of a real show.

    `intro_done=True` because that is where almost every test in this file
    lives — mid-panel floor mechanics, not the one-shot opening. Tests of the
    introduction round itself need the opposite starting point and build it
    explicitly with ``replace(state, intro_done=False)``.
    """
    return replace(PanelState.for_agents(cast.ids()), intro_done=True)


def run(fc: FloorController, state: PanelState, *events):
    """Fold a sequence of events, collecting every command emitted."""
    commands = []
    for event in events:
        state, cmds = fc.reduce(state, event)
        commands.extend(cmds)
    return state, commands


def strong(**overrides) -> Signals:
    base = {
        "relevance": 0.9,
        "urgency": 0.5,
        "disagreement": 0.3,
        "confidence": 0.9,
        "expertise": 0.6,
    }
    base.update(overrides)
    return Signals(**base)


def invite(t: float = 0.0, text: str = "What holds it back?") -> TranscriptUpdated:
    """Ricky opening the floor.

    Every test that expects an agent to speak must go through one of these.
    That is the point: without an invitation there is no turn, so the
    invitation is part of the acceptance criteria, not test scaffolding.
    """
    return TranscriptUpdated(t=t, speaker=HUMAN, text=text, is_final=True)


def weak() -> Signals:
    return Signals(relevance=0.1, urgency=0.05, disagreement=0.0, confidence=0.2, expertise=0.0)


# ---------------------------------------------------------------- human first


def speaking_agent(fc, state, agent="wayne", t=1.0):
    """Helper: get an agent onto the floor and speaking."""
    state, _ = run(
        fc,
        state,
        invite(t - 1.5),
        AgentProposal(t=t - 1.0, agent=agent, utterance="As I was saying...", signals=strong()),
        TurnYielded(t=t),
        AgentSpeechStarted(t=t + 0.1, agent=agent),
    )
    assert state.speaking == agent
    return state


def test_human_speech_alone_does_not_stop_the_agent(fc, state):
    """Endpointing cannot say whose voice it heard, so it stops nothing by itself.

    `HumanSpeechStarted` is the barge-in *reflex*'s event elsewhere in the
    runtime, but in here it carries no speaker and the audience and the PA
    bleeding back into the stage mic fire it exactly as readily as Ricky. The
    stop waits for STT to say this is him — see
    `test_transcript_evidence_stops_the_agent`.
    """
    state = speaking_agent(fc, state)
    state, cmds = fc.reduce(state, HumanSpeechStarted(t=2.0))

    assert not [c for c in cmds if isinstance(c, StopSpeech)]
    assert state.speaking == "wayne", "the agent is undisturbed"


def ricky_partial(t: float, text: str = "so, hold on") -> TranscriptUpdated:
    """One partial segment already attributed to Ricky by `panel_runtime.stt`."""
    return TranscriptUpdated(t=t, speaker=HUMAN, text=text, is_final=False)


def test_transcript_evidence_stops_the_agent(fc, state):
    """The whole interrupt rule, gated on STT rather than on endpointing.

    A `TranscriptUpdated` on the human channel is only ever built for a
    segment already attributed to Ricky (`panel_runtime.stt`), so a run of
    them arriving while an agent is speaking is this reducer learning it is
    him — and that is when the agent stops. Partials have to repeat
    (`interrupt_confirm_partials`); the streak test below is where that is
    pinned on its own.
    """
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))
    state, cmds = run(
        fc,
        state,
        *(ricky_partial(2.2 + 0.1 * i) for i in range(fc.config.interrupt_confirm_partials)),
    )

    stops = [c for c in cmds if isinstance(c, StopSpeech)]
    assert len(stops) == 1
    assert stops[0].agent == "wayne"
    assert stops[0].reason is StopReason.HUMAN_INTERRUPT
    assert state.speaking is None
    assert state.floor_holder == HUMAN


def test_an_acknowledgement_from_ricky_also_stops_the_agent(fc, state):
    """No backchannel classification survives. "mm-hm" stops the agent too.

    This used to duck and resume. Ricky has been told not to backchannel on
    stage, so the only thing a classifier could still do here was read a real
    interruption as an acknowledgement — "yeah, okay" is the shape of both —
    and leave an agent talking over the moderator. What is still asked of his
    words is only *how firmly* they are his, never what they mean.
    """
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))
    state, cmds = run(
        fc,
        state,
        *(
            ricky_partial(2.2 + 0.1 * i, "mm-hm")
            for i in range(fc.config.interrupt_confirm_partials)
        ),
    )

    stops = [c for c in cmds if isinstance(c, StopSpeech)]
    assert stops and stops[0].reason is StopReason.HUMAN_INTERRUPT
    assert state.speaking is None
    assert state.floor_holder == HUMAN


# --------------------------------------------- confirming a partial's speaker
#
# Diarisation can mislabel one short segment, and on the first-partial rule one
# such segment took the floor off an agent mid-sentence. A partial therefore has
# to repeat `FloorConfig.interrupt_confirm_partials` times before it is acted
# on; a final never waits, because the engine committing to an attribution is a
# stronger statement than any number of revisable ones.


def test_a_partial_short_of_the_threshold_leaves_the_agent_alone(fc, state):
    """One stray partial is exactly the misattribution this exists to absorb."""
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))

    for i in range(fc.config.interrupt_confirm_partials - 1):
        state, cmds = fc.reduce(state, ricky_partial(2.2 + 0.1 * i))
        assert not [c for c in cmds if isinstance(c, StopSpeech)], f"stopped on partial {i + 1}"
        assert state.speaking == "wayne"
        assert state.human_interrupt_streak == i + 1


def test_the_confirming_partial_stops_the_agent(fc, state):
    """...and the one that completes the run does stop it, on that same event."""
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))

    for i in range(fc.config.interrupt_confirm_partials - 1):
        state, _ = fc.reduce(state, ricky_partial(2.2 + 0.1 * i))

    state, cmds = fc.reduce(state, ricky_partial(3.0))
    stops = [c for c in cmds if isinstance(c, StopSpeech)]
    assert [c.agent for c in stops] == ["wayne"]
    assert stops[0].reason is StopReason.HUMAN_INTERRUPT
    assert state.speaking is None
    assert state.floor_holder == HUMAN
    assert state.human_interrupt_streak == 0, "spent, not carried into the next turn"


def test_a_final_never_waits_for_a_streak(fc, state):
    """A final is the engine committing to the attribution. It acts at once.

    Nothing has been confirmed when this lands — the streak is at zero — and
    the agent still stops on the first event. Making a final wait would put
    Ricky's interrupt behind two more segments for no gain in confidence.
    """
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))
    assert state.human_interrupt_streak == 0

    state, cmds = fc.reduce(
        state, TranscriptUpdated(t=2.2, speaker=HUMAN, text="no, hold on", is_final=True)
    )
    stops = [c for c in cmds if isinstance(c, StopSpeech)]
    assert [c.agent for c in stops] == ["wayne"]
    assert stops[0].reason is StopReason.HUMAN_INTERRUPT
    assert state.speaking is None


def test_a_closed_mic_makes_the_next_utterance_start_from_zero(fc, state):
    """A run that never finished is not two thirds of the next one.

    Two partials, then the burst ends with the agent still speaking. A later
    utterance has to earn the full count again — otherwise a single stray
    partial an hour later inherits an interrupt from a burst nobody acted on.
    """
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))
    state, cmds = run(fc, state, ricky_partial(2.2), ricky_partial(2.3))
    assert not [c for c in cmds if isinstance(c, StopSpeech)]
    assert state.human_interrupt_streak == 2

    state, cmds = fc.reduce(state, HumanSpeechEnded(t=2.5))
    assert not [c for c in cmds if isinstance(c, StopSpeech)]
    assert state.human_interrupt_streak == 0
    assert state.speaking == "wayne", "the agent held the floor throughout"

    # A fresh utterance, one partial short of the threshold: still nothing.
    state, _ = fc.reduce(state, HumanSpeechStarted(t=8.0))
    state, cmds = run(
        fc,
        state,
        *(ricky_partial(8.2 + 0.1 * i) for i in range(fc.config.interrupt_confirm_partials - 1)),
    )
    assert not [c for c in cmds if isinstance(c, StopSpeech)], "counted from zero again"
    assert state.speaking == "wayne"

    state, cmds = fc.reduce(state, ricky_partial(9.0))
    assert [c.agent for c in cmds if isinstance(c, StopSpeech)] == ["wayne"]


def test_a_new_agent_turn_does_not_inherit_the_previous_turn_s_streak(fc, state):
    """Partials counted against one speaker are not evidence about the next.

    Without the reset in `_agent_started`, a handover landing mid-burst would
    carry most of an interrupt into the next turn and cut it off on its first
    partial.
    """
    state = speaking_agent(fc, state, agent="wayne", t=1.0)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))
    state, _ = run(fc, state, ricky_partial(2.2), ricky_partial(2.3))
    assert state.human_interrupt_streak == 2

    state, _ = fc.reduce(state, AgentSpeechEnded(t=3.0, agent="wayne", completed=True))
    state, _ = fc.reduce(state, AgentSpeechStarted(t=3.1, agent="dex"))
    assert state.human_interrupt_streak == 0

    state, cmds = fc.reduce(state, ricky_partial(3.2))
    assert not [c for c in cmds if isinstance(c, StopSpeech)], "dex is not mid-interrupt"
    assert state.speaking == "dex"


def test_the_confirmation_threshold_is_configurable(cast, state):
    """The dial is read, not hardcoded — one partial is the old behaviour."""
    eager = FloorController(cast, FloorConfig(interrupt_confirm_partials=1))
    state = speaking_agent(eager, state)
    state, cmds = eager.reduce(state, ricky_partial(2.2))
    assert [c.agent for c in cmds if isinstance(c, StopSpeech)] == ["wayne"]

    patient = FloorController(cast, FloorConfig(interrupt_confirm_partials=2))
    later = speaking_agent(patient, replace(PanelState.for_agents(cast.ids()), intro_done=True))
    later, cmds = patient.reduce(later, ricky_partial(2.2))
    assert not [c for c in cmds if isinstance(c, StopSpeech)]
    later, cmds = patient.reduce(later, ricky_partial(2.3))
    assert [c.agent for c in cmds if isinstance(c, StopSpeech)] == ["wayne"]


def test_duration_alone_never_stops_an_agent(fc, state):
    """A voice going on and on is not evidence of anything.

    There was a duration promotion on `Tick` and another on
    `HumanSpeechEnded`: speech past `backchannel_max_duration_s` became a stop
    whatever the words were. Both are gone with the duck they promoted. A
    sustained voice with no attributed transcript behind it is the audience,
    and the only thing a clock could measure here is how long they have been
    talking.
    """
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))

    state, cmds = fc.reduce(state, Tick(t=5.0))
    assert not [c for c in cmds if isinstance(c, StopSpeech)]
    state, cmds = fc.reduce(state, HumanSpeechEnded(t=6.0))
    assert not [c for c in cmds if isinstance(c, StopSpeech)]
    assert state.speaking == "wayne", "the agent held the floor throughout"
    assert state.human_speaking is False


def test_substantive_words_commit_an_interrupt_even_when_brief(fc, state):
    """'Sorry Dex, let Wayne finish' is short but is not an acknowledgement."""
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))
    state, cmds = run(
        fc,
        state,
        *(
            ricky_partial(2.15 + 0.1 * i, "sorry, could you let Wayne finish")
            for i in range(fc.config.interrupt_confirm_partials)
        ),
    )
    stops = [c for c in cmds if isinstance(c, StopSpeech)]
    assert stops and stops[0].reason is StopReason.HUMAN_INTERRUPT
    assert state.speaking is None
    assert state.floor_holder == HUMAN


def test_a_transcript_arriving_after_the_mic_closed_still_interrupts(fc, state):
    """Safety net for the race: a burst with no words yet leaves the agent be,
    then the words arrive and stop it anyway.

    The stop is a property of the text, not of the burst it belonged to, so it
    does not matter that `HumanSpeechEnded` has already landed by the time STT
    catches up.
    """
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))
    state, cmds = fc.reduce(state, HumanSpeechEnded(t=2.2))
    assert not [c for c in cmds if isinstance(c, StopSpeech)]
    assert state.speaking == "wayne", "no words, so nothing to act on"

    state, cmds = fc.reduce(
        state, TranscriptUpdated(t=2.5, speaker=HUMAN, text="no, that is wrong", is_final=True)
    )
    assert [c for c in cmds if isinstance(c, StopSpeech)], "his words stop the agent regardless"
    assert state.speaking is None


def test_human_speech_with_no_agent_speaking_takes_the_floor_on_his_words(fc, state):
    """The onset says the mic is hot. The segment says the floor has moved."""
    state, cmds = fc.reduce(state, HumanSpeechStarted(t=1.0))
    assert not [c for c in cmds if isinstance(c, StopSpeech)]
    assert state.human_speaking is True
    assert state.floor_holder is None, "a voice in the room is not a floor claim"

    state, _ = fc.reduce(state, ricky_partial(1.2))
    assert state.floor_holder == HUMAN


# --------------------------------------------- an onset is not a floor claim
#
# `HumanSpeechStarted` is endpointing, with no speaker and no words on it: the
# audience, a breath and the PA bleeding back into the stage mic all fire it.
# It used to revoke the standing invitation on its own, and on 8 Oct 2026 that
# closed the floor 230ms after Ricky asked two agents a question — inside the
# beat `invited_agent_grace_s` exists to hold, and before either answer had
# finished generating. The claim now waits for a segment `panel_runtime.stt`
# has attributed to him, which is the same evidence the interrupt path uses.


def test_a_bare_onset_does_not_revoke_a_live_invitation(fc, state):
    """The rehearsal failure, in four events."""
    state, _ = run(
        fc,
        state,
        invite(20.0, "Melia, Wayne, what are your thoughts?"),
        TurnYielded(t=20.01),
    )
    assert set(state.invitation.agents) == {"melia", "wayne"}
    assert state.awaiting_agents, "the beat is being held for the pair of them"

    # A noise on the mic, 230ms in, while both answers are still generating.
    state, cmds = fc.reduce(state, HumanSpeechStarted(t=20.24))
    assert cmds == [], "nothing on the floor moved, so there is nothing to repaint"
    assert set(state.invitation.agents) == {"melia", "wayne"}, "the question still stands"
    assert state.awaiting_agents, "and the beat is still being held"

    # ...and the answer that was being written still gets the floor.
    state, _ = fc.reduce(state, HumanSpeechEnded(t=20.5))
    state, _ = fc.reduce(
        state,
        AgentProposal(
            t=20.9, input_t=20.0, agent="melia", utterance="Two things.", signals=strong()
        ),
    )
    _, cmds = fc.reduce(state, TurnYielded(t=21.0))
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["melia"]


def test_a_bare_onset_that_never_produces_words_leaves_nothing_behind(fc, state):
    """Noise, then silence. The floor is exactly where it started.

    The one surviving difference is the armed claim, which `_human_ended`
    deliberately does not disarm — see the late-final test below. It changes
    nothing on its own: only a segment attributed to Ricky can spend it.
    """
    state, _ = run(fc, state, invite(20.0, "So Wayne, where are we on the curve?"))
    before = state

    state, _ = run(fc, state, HumanSpeechStarted(t=20.3), HumanSpeechEnded(t=20.5))
    assert replace(state, human_onset_pending=False) == before, (
        "an onset with nothing behind it is not a floor event"
    )


def test_his_words_after_the_onset_revoke_the_invitation(fc, state):
    """Deferred, not dropped. The first segment does everything the onset did."""
    state, _ = run(
        fc,
        state,
        invite(20.0, "So Wayne, where are we on the adoption curve?"),
        # Dex answering a question put to Wayne wins nothing, so the floor
        # stays idle with the invitation live and a proposal in hand.
        AgentProposal(
            t=20.1, input_t=20.0, agent="dex", utterance="Historically...", signals=strong()
        ),
        TurnYielded(t=20.2),
        HumanSpeechStarted(t=20.3),
    )
    assert state.invitation is not None and state.proposals

    state, cmds = fc.reduce(state, ricky_partial(20.4, "actually, let me rephrase"))
    assert state.invitation is None, "he is talking; the question he asked is withdrawn"
    assert not state.proposals, "and the lines written against it go with it"
    assert state.floor_holder == HUMAN
    assert state.awaiting_agents == ()
    assert [c for c in cmds if isinstance(c, StateChanged)], "the console sees the floor move"


def test_a_final_can_be_the_first_segment_of_the_burst(fc, state):
    """Short utterances are finalised with no partial ahead of them.

    The claim is made by whichever segment arrives first, and must not sit
    waiting for a partial that is never coming.
    """
    state, _ = run(
        fc,
        state,
        invite(20.0, "So Wayne, where are we on the adoption curve?"),
        HumanSpeechStarted(t=20.3),
    )
    assert state.invitation is not None

    state, _ = fc.reduce(
        state, TranscriptUpdated(t=20.4, speaker=HUMAN, text="Sorry, one second.", is_final=True)
    )
    assert state.invitation is None
    assert state.floor_holder == HUMAN


def test_a_final_arriving_after_the_mic_closed_still_takes_the_floor(fc, state):
    """STT routinely finalises a segment after `HumanSpeechEnded`.

    The idle-floor counterpart of
    `test_a_transcript_arriving_after_the_mic_closed_still_interrupts`: the
    claim is a property of the words, not of the burst that carried them, so
    the end of the burst must not disarm it.
    """
    state, _ = run(
        fc,
        state,
        invite(20.0, "So Wayne, where are we on the adoption curve?"),
        HumanSpeechStarted(t=20.3),
        HumanSpeechEnded(t=20.5),
    )
    assert state.invitation is not None, "no words yet, so nothing has been claimed"

    state, _ = fc.reduce(
        state,
        TranscriptUpdated(t=20.7, speaker=HUMAN, text="Let me come back to that.", is_final=True),
    )
    assert state.invitation is None
    assert state.floor_holder == HUMAN


def test_one_burst_is_one_claim(fc, state):
    """A second segment must not wipe an invitation the first one installed.

    "Melia, can you continue? ... is that okay?" arrives as two segments of one
    burst. If every segment re-claimed the floor, the second would clear
    Melia's invitation and the vaguer reading would install unopposed — the
    collision `_INVITATION_PRECEDENCE` and the supersede window exist to
    settle. The claim is armed once per onset and spent once.
    """
    state, _ = run(
        fc,
        state,
        HumanSpeechStarted(t=10.0),
        TranscriptUpdated(t=10.4, speaker=HUMAN, text="Melia, can you continue?", is_final=True),
    )
    assert state.invitation is not None and state.invitation.agent == "melia"

    state, _ = fc.reduce(
        state, TranscriptUpdated(t=10.9, speaker=HUMAN, text="Is that okay?", is_final=True)
    )
    assert state.invitation is not None, "still the same burst, still Melia's question"
    assert state.invitation.agent == "melia", "a vaguer second reading must not displace it"


def test_an_onset_over_a_speaking_agent_still_arms_the_claim(fc, state):
    """He starts over the agent, the turn ends before his words land.

    The onset goes to the agent-speaking branch, which stops nothing (only a
    transcript may). By the time STT catches up the floor is idle, and that
    segment is still Ricky taking it.
    """
    state, _ = run(
        fc,
        state,
        invite(0.0, "What holds it back?"),
        AgentProposal(t=0.1, input_t=0.0, agent="wayne", utterance="Cost.", signals=strong()),
        TurnYielded(t=0.5),
        AgentSpeechStarted(t=0.6, agent="wayne"),
        HumanSpeechStarted(t=5.0),
    )
    state, _ = fc.reduce(state, AgentSpeechEnded(t=5.1, agent="wayne", completed=True))
    assert state.speaking is None

    state, _ = fc.reduce(state, ricky_partial(5.2, "right, so"))
    assert state.floor_holder == HUMAN
    assert state.invitation is None


# ------------------------------------------------- identity-gated interrupts
#
# Only Ricky may stop an agent. His mic is on a stage, so the audience and the
# PA bleeding back into the room both land on it; `panel_runtime.stt` gives a
# segment it could not attribute to the enrolled moderator no text at all and
# reports it as `UnverifiedSpeechDetected` instead.
#
# That is now the entire gate, and it lives entirely in the runtime. The
# reducer has no identity logic left of its own: a stranger produces no
# `TranscriptUpdated`, the only thing that can stop an agent is a
# `TranscriptUpdated`, so a stranger cannot stop an agent. `duck_confirmed`
# and `_unverified_speech` were the belt to that braces, and the belt could
# never actually fire — by the time the duck existed it was already confirmed
# as Ricky, so the `False` the handler wrote was unreachable. Both are gone;
# `UnverifiedSpeechDetected` still reaches this reducer and is now a plain
# no-op, which is what the tests below pin.


def test_a_stranger_alone_never_stops_an_agent(fc, state):
    """The feature. An audience voice leaves the agent completely undisturbed."""
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))
    state, cmds = fc.reduce(state, UnverifiedSpeechDetected(t=2.1, is_final=True))
    assert cmds == [], "a stranger is never a floor signal"

    # Well past any threshold that used to promote a duck to a stop.
    state, cmds = fc.reduce(state, Tick(t=5.0))
    assert not [c for c in cmds if isinstance(c, StopSpeech)], "his words never arrived"
    assert state.speaking == "wayne"

    state, cmds = fc.reduce(state, HumanSpeechEnded(t=6.0))
    assert not [c for c in cmds if isinstance(c, StopSpeech)]
    assert state.speaking == "wayne", "the agent kept the floor throughout, undisturbed"


def test_ricky_words_stop_an_agent_even_after_stranger_evidence(fc, state):
    """The content path needs no gate, and this proves it is already safe.

    Strangers produce no `TranscriptUpdated` at all (`panel_runtime.stt`), so
    any transcript reaching this reducer is Ricky by construction. Here a
    stranger is heard first and Ricky speaks over them; his words win.
    """
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))
    state, _ = fc.reduce(state, UnverifiedSpeechDetected(t=2.1, is_final=True))

    state, cmds = fc.reduce(
        state,
        TranscriptUpdated(t=2.3, speaker=HUMAN, text="sorry, let Melia answer", is_final=True),
    )
    stops = [c for c in cmds if isinstance(c, StopSpeech)]
    assert stops and stops[0].reason is StopReason.HUMAN_INTERRUPT
    assert state.speaking is None
    assert state.floor_holder == HUMAN


def test_unverified_speech_with_nobody_on_the_pa_changes_nothing(fc, state):
    """A stranger talking into a silent room is not a floor event of any kind."""
    before = state
    state, cmds = fc.reduce(state, UnverifiedSpeechDetected(t=1.0, is_final=True))
    assert cmds == []
    assert state == before
    assert state.floor_holder is None



def test_human_turn_invalidates_speculative_proposals(fc, state):
    state, _ = run(
        fc, state, AgentProposal(t=0.0, agent="dex", utterance="Historically...", signals=strong())
    )
    assert state.proposals
    state, _ = fc.reduce(state, HumanSpeechStarted(t=0.5))
    assert state.proposals, "a voice in the room is not yet a human turn"
    state, _ = fc.reduce(state, ricky_partial(0.6))
    assert not state.proposals


# ------------------------------------------------------------- direct address


def test_addressed_agent_gets_the_floor_over_a_higher_score(fc, state):
    """'So Wayne, what do you think' beats Dex's better score."""
    state, _ = run(
        fc,
        state,
        invite(0.0, "So Wayne, what about human oversight?"),
        AgentProposal(t=0.5, agent="dex", utterance="Historically...", signals=strong()),
        AgentProposal(t=0.6, agent="wayne", utterance="Completely useless.", signals=weak()),
    )
    assert state.invitation.agent == "wayne"

    state, cmds = fc.reduce(state, TurnYielded(t=1.0))
    starts = [c for c in cmds if isinstance(c, StartSpeech)]
    assert [s.agent for s in starts] == ["wayne"]
    assert not state.invitation.is_live(), (
        "a direct question to Wayne alone is spent the moment he answers it — "
        "there is nobody else this invitation could ever admit"
    )
    assert state.invitation.spoken == ("wayne",)


def test_a_named_invitation_never_opens_to_the_rest_of_the_panel(fc, state):
    """Ricky names Wayne. Dexter and Melia must not take the floor off the
    back of his question, however strong their proposals score — not while
    Wayne is still speaking, and not after he has finished either. Wayne
    answers once and the floor goes straight back to Ricky — it does not keep
    re-granting Wayne for lack of anyone else to compete with him.
    """
    state, _ = run(
        fc,
        state,
        invite(0.0, "So Wayne, what about human oversight?"),
        AgentProposal(t=0.5, agent="wayne", utterance="Fine, mostly.", signals=strong()),
    )
    assert state.invitation.agent == "wayne"

    state, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["wayne"]
    state, _ = fc.reduce(state, AgentSpeechStarted(t=1.1, agent="wayne"))

    # Granting Wayne the floor does NOT open the invitation up to the others —
    # `admits()` confines a named invitation to its own set for its whole
    # life — and for a lone addressee that set is spent the instant he has
    # had his turn, not reusable, or he would just keep re-winning arbitration
    # against nobody until `address_invitation_turns` ran out from under him.
    assert not state.invitation.admits("dex")
    assert not state.invitation.admits("melia")
    assert not state.invitation.admits("wayne"), "already had his one turn"

    state, _ = fc.reduce(
        state,
        AgentProposal(t=1.5, agent="dex", utterance="Historically, no.", signals=strong()),
    )
    state, cmds = fc.reduce(state, AgentSpeechEnded(t=6.0, agent="wayne", completed=True))
    assert not [c for c in cmds if isinstance(c, StartSpeech)], (
        "nobody is left this invitation could ever admit — not Dex, who was "
        "never addressed even on the stronger-scoring proposal, and not "
        "Wayne, who already answered"
    )
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [CueReason.INVITATION_SPENT]
    assert state.invitation is None


def test_a_named_agent_answers_alone(fc, state):
    """Asking Wayne is not asking the panel. Dex does not get to answer for him."""
    state, _ = run(
        fc,
        state,
        invite(0.0, "Wayne, does oversight actually scale?"),
        AgentProposal(t=0.5, agent="dex", utterance="Historically...", signals=strong()),
    )
    state, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert not [c for c in cmds if isinstance(c, StartSpeech)]
    # Wayne is given the beat first; the floor still ends up back with Ricky.
    _, cmds = fc.reduce(state, Tick(t=1.0 + fc.config.invited_agent_grace_s))
    assert [c for c in cmds if isinstance(c, CueModerator)]


def test_a_named_agent_may_not_answer_with_a_line_written_before_the_question(fc, state):
    """The failure this guard exists for, from a live run.

    Ricky said "So, Wayne, uh, where are we actually on the adoption curve?".
    Speculation fired on the opening fragment, so Wayne's brain was asked before
    anything had been asked of it and wrote "Take your time, Ricky — we'll be
    here." A named invitation bypasses the score floor, so that went on the PA
    in answer to a direct question. Silence plus a cue is recoverable; this is
    not. The same guard covers the slower version: a proposal left over from a
    previous turn that ended without a grant, so nothing cleared it.

    Stated on the input clock — `AgentProposal.input_t`, the transcript
    timestamp the generation was started from — because that is the only clock
    that means anything here: what makes this line unairable is when it was
    *written*, not when it happened to turn up. The sibling test below is the
    case where those two disagree, which is what made this guard stop working.
    """
    state, _ = run(
        fc,
        state,
        AgentProposal(
            t=0.2,
            input_t=0.0,
            agent="wayne",
            utterance="Take your time, Ricky.",
            signals=strong(),
        ),
        invite(20.0, "So Wayne, where are we on the adoption curve?"),
    )
    assert state.invitation.agent == "wayne"

    state, cmds = fc.reduce(state, TurnYielded(t=20.5))
    assert not [c for c in cmds if isinstance(c, StartSpeech)]
    state, cmds = fc.reduce(state, Tick(t=20.5 + fc.config.invited_agent_grace_s))
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [
        CueReason.INVITED_AGENT_SILENT
    ]
    assert state.invitation.is_live(), "the invitation survives so a fresh answer can take it"


def test_a_named_agent_may_not_answer_with_a_line_that_raced_past_the_question(fc, state):
    """The same guard, on the live run where it silently stopped working.

    To the timestamps, off the console log:

        12:54:47.461  speculation fires on the partial "Thanks, guys. Um, so
                      we'll get straight into it." — no question in it at all.
                      Wayne's generation starts against that text.
        12:54:50.670  "...Wayne, where do you think we actually are on the
                      adoption curve?" lands, and invites Wayne.
        12:54:51.437  that 3967ms generation finally finishes — 0.77s *after*
                      the invitation.
        12:54:52.233  Wayne airs "Happy to let Ricky finish setting the table
                      —" in answer to a direct question, because it was the
                      only proposal in hand.
        12:54:53.054  the real answer exists. 1.6s too late.

    `_stale` measured `invitation.t - proposal.t`, and `proposal.t` is the
    *arrival* time, so a line whose input predated the question by 3.2s scored
    as maximally fresh. The guard had been passing by accident: until
    generations were allowed to race, a final cancelled everything started
    against older text, so a line with stale input could not arrive late
    because it could not arrive at all.

    Address detection was correct in this run and is not what is under test —
    the invitation is asserted, not the phrasing.
    """
    state, _ = fc.reduce(
        state,
        invite(50.670, "Wayne, where do you think we actually are on the adoption curve?"),
    )
    assert state.invitation.agent == "wayne"

    state, cmds = fc.reduce(state, TurnYielded(t=50.671))
    assert not [c for c in cmds if isinstance(c, StartSpeech)]
    assert state.awaiting_agents == ("wayne",), "the beat is held for an answer still being written"

    # The holding line arrives after the invitation, having been written 3.2s
    # before the question.
    state, cmds = fc.reduce(
        state,
        AgentProposal(
            t=51.437,
            input_t=47.461,
            agent="wayne",
            utterance="Happy to let Ricky finish setting the table —",
            signals=strong(),
            epoch=1,
        ),
    )
    assert not [c for c in cmds if isinstance(c, StartSpeech)], "a proposal is not a floor claim"

    # The runtime re-drives arbitration on every proposal that lands
    # (`PanelRuntime._maybe_rearbitrate`), and that is where it used to go on
    # the PA.
    state, cmds = fc.reduce(state, TurnYielded(t=51.44))
    assert not [c for c in cmds if isinstance(c, StartSpeech)], (
        "a line written before the question was aired in answer to it"
    )
    assert state.proposals["wayne"].utterance.startswith("Happy to let Ricky"), (
        "refused for airing, but kept — nothing here throws a fallback away"
    )

    # Ricky is cued instead, and the invitation survives so the real answer can
    # still take it.
    state, cmds = fc.reduce(state, Tick(t=51.44 + fc.config.invited_agent_grace_s))
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [
        CueReason.INVITED_AGENT_SILENT
    ]
    assert state.invitation.is_live()

    # ...and 1.6s later it does. `input_t` is the final's own timestamp, so
    # this is the generation the invitation's own `RequestProposals` started.
    state, _ = fc.reduce(
        state,
        AgentProposal(
            t=53.054,
            input_t=50.670,
            agent="wayne",
            utterance="Faster than the room thinks.",
            signals=strong(),
            epoch=2,
        ),
    )
    _, cmds = fc.reduce(state, TurnYielded(t=53.06))
    granted = [c for c in cmds if isinstance(c, StartSpeech)]
    assert [c.agent for c in granted] == ["wayne"]
    assert granted[0].epoch == 2, "the right generation's words, too"


def test_a_named_agent_answers_with_a_line_written_during_the_question(fc, state):
    """...and the latency win speculation exists for is preserved.

    The half of `_stale` that must NOT change. A generation started against a
    *partial of the question itself* is an answer to it, and those rounds are
    the whole reason the post-turn gap is short: they run 1-3s ahead of the
    final, so their output routinely lands after it. Fixing the staleness clock
    by refusing everything speculative would put a full model round trip after
    every direct address, which is exactly what speculation buys back.
    """
    state, _ = run(
        fc,
        state,
        # Ricky is mid-question and the partial already carries it, so this is
        # the round whose answer is worth having.
        TranscriptUpdated(
            t=19.4, speaker=HUMAN, text="So Wayne, where are we on the adoption", is_final=False
        ),
        invite(20.0, "So Wayne, where are we on the adoption curve?"),
        # ...and it finishes after the final that opened the floor.
        AgentProposal(
            t=21.2,
            input_t=19.4,
            agent="wayne",
            utterance="About a third of it.",
            signals=weak(),
            epoch=1,
        ),
    )
    _, cmds = fc.reduce(state, TurnYielded(t=21.25))
    granted = [c for c in cmds if isinstance(c, StartSpeech)]
    assert [c.agent for c in granted] == ["wayne"], (
        "speculation on the question itself must still be airable"
    )
    assert granted[0].epoch == 1


def test_a_proposal_with_no_input_time_falls_back_to_its_arrival_time(fc, state):
    """The compatibility hinge, asserted rather than assumed.

    `AgentProposal.input_t` defaults to None so that an emitter which does not
    set it — an older recorded log being replayed, or a future one that forgets
    — degrades to the behaviour from before the field existed rather than to a
    wrong answer in either direction. Every emitter in this repo sets it; this
    is what keeps that a belt rather than a load-bearing assumption.
    """
    stale = AgentProposal(t=0.0, agent="wayne", utterance="Take your time.", signals=strong())
    fresh = AgentProposal(t=19.5, agent="wayne", utterance="A third.", signals=strong())
    assert stale.input_t is None and fresh.input_t is None

    refused, _ = run(fc, state, stale, invite(20.0, "So Wayne, where are we?"))
    _, cmds = fc.reduce(refused, TurnYielded(t=20.5))
    assert not [c for c in cmds if isinstance(c, StartSpeech)], "20s before the question"

    accepted, _ = run(fc, state, fresh, invite(20.0, "So Wayne, where are we?"))
    _, cmds = fc.reduce(accepted, TurnYielded(t=20.5))
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["wayne"]


def test_a_silent_arbitration_asks_the_panel_again(fc, state):
    """Nobody had anything — so ask, now that the turn is complete.

    Recovery used to depend on a stream that happened to still be in flight.
    When the only generation for the turn had already finished — against a
    partial, or before Ricky had asked anything — nothing re-drove arbitration
    and a live invitation sat on the floor until its 25s TTL.
    """
    state, _ = fc.reduce(state, invite(20.0, "So Wayne, where are we on the curve?"))
    state, cmds = fc.reduce(state, TurnYielded(t=21.0))
    # The re-ask is the point of this test and it happens immediately — it must
    # not wait out the moderator-cue beat, or the beat would be spent waiting
    # for an answer nobody had been asked for.
    requests = [c for c in cmds if isinstance(c, RequestProposals)]
    assert [c.reason for c in requests] == ["post_turn"]
    assert "wayne" in requests[0].agents


def test_repeated_silent_arbitrations_do_not_spam_the_brains(fc, state):
    """A refused proposal re-opens arbitration; the debounce bounds the churn."""
    state, _ = fc.reduce(state, invite(20.0, "So Wayne, where are we on the curve?"))
    state, _ = fc.reduce(state, TurnYielded(t=21.0))
    _, cmds = fc.reduce(state, TurnYielded(t=21.1))
    assert not [c for c in cmds if isinstance(c, RequestProposals)]


def test_an_unnamed_question_opens_the_floor_to_the_panel(fc, state):
    state, _ = fc.reduce(state, invite(0.0, "What do you all think?"))
    assert state.invitation is not None
    assert state.invitation.agents == (), "open to the room, not to one agent"


# ------------------------------------------------------- the floor is closed


def test_a_statement_invites_nobody(fc, state):
    """The bug this whole model exists to fix.

    Ricky talking about the product is not a cue. Agents may want the floor —
    they just do not get to take it.
    """
    state, cmds = run(
        fc,
        state,
        TranscriptUpdated(
            t=0.0,
            speaker=HUMAN,
            text="We have been doing real-time transcription for a decade now.",
            is_final=True,
        ),
        AgentProposal(t=0.5, agent="dex", utterance="Historically...", signals=strong()),
        AgentProposal(t=0.6, agent="wayne", utterance="Rubbish.", signals=strong()),
    )
    assert state.invitation is None

    _, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert not [c for c in cmds if isinstance(c, StartSpeech)], "nobody was invited"
    assert [c for c in cmds if isinstance(c, CueModerator)]


def test_wanting_the_floor_is_surfaced_to_the_operator(fc, state):
    """A raised hand is visible, not self-served — Ricky decides."""
    state, _ = run(
        fc,
        state,
        TranscriptUpdated(t=0.0, speaker=HUMAN, text="That is the state of it.", is_final=True),
        AgentProposal(t=0.5, agent="dex", utterance="Historically...", signals=strong()),
        AgentProposal(t=0.6, agent="melia", utterance="Mm.", signals=weak()),
    )
    _, cmds = fc.reduce(state, TurnYielded(t=1.0))
    hands = [c for c in cmds if isinstance(c, HandsRaised)]
    assert hands, "the operator console needs to see interest it is holding back"
    assert hands[0].agents[0][0] == "dex", "ranked, best case first"


def test_operator_can_open_a_floor_the_patterns_missed(fc, state):
    """The backstop. Detection is deliberately conservative, so this must work.

    The prompt used to be "Say more about that.", which detection now reads as
    a handover — continuation cues became invitations deliberately (an agent
    told to elaborate should elaborate). So this exercises the backstop with a
    phrasing that genuinely is not an invitation on its face.
    """
    state, _ = run(
        fc,
        state,
        TranscriptUpdated(
            t=0.0, speaker=HUMAN, text="That is roughly where the market sits.", is_final=True
        ),
        AgentProposal(t=0.5, agent="dex", utterance="Historically...", signals=strong()),
    )
    assert state.invitation is None

    state, _ = fc.reduce(
        state, OperatorCommand(t=0.9, action=OperatorAction.OPEN_FLOOR, agent=None, turns=1)
    )
    _, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["dex"]


def test_an_open_invitation_is_spent_and_the_floor_goes_back(fc, state):
    """Two agents on one question, then back to Ricky — not an infinite relay."""
    config = FloorConfig(open_invitation_turns=1)
    fc = FloorController(fc.cast, config)
    state, _ = run(
        fc,
        state,
        invite(0.0),
        AgentProposal(t=0.5, agent="dex", utterance="Historically...", signals=strong()),
        TurnYielded(t=1.0),
        AgentSpeechStarted(t=1.1, agent="dex"),
    )
    state, cmds = fc.reduce(state, AgentSpeechEnded(t=6.0, agent="dex", completed=True))
    assert state.invitation is None
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == ["invitation_spent"]
    assert not [c for c in cmds if isinstance(c, RequestProposals)]


# ------------------------------------------------------------------ scoring


def test_open_question_grants_floor_to_strongest_case(fc, state):
    state, _ = run(
        fc,
        state,
        TranscriptUpdated(t=0.0, speaker=HUMAN, text="Where are we now?", is_final=True),
        AgentProposal(t=0.5, agent="dex", utterance="Historically...", signals=strong()),
        AgentProposal(t=0.6, agent="melia", utterance="Mm.", signals=weak()),
    )
    _, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["dex"]


def test_silence_is_a_legitimate_outcome(fc, state):
    """Weak proposals lose to saying nothing, even on an open invitation.

    The reason is `below_floor`, not the old catch-all `no_candidate`: a
    rehearsal needs to tell "the panel had nothing" from "the panel had
    something and it was not good enough" from "the one agent Ricky named said
    nothing", because those are three different fixes.
    """
    state, _ = run(
        fc, state, invite(0.0), AgentProposal(t=0.5, agent="melia", utterance="Mm.", signals=weak())
    )
    _, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert not [c for c in cmds if isinstance(c, StartSpeech)]
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [CueReason.BELOW_FLOOR]


def test_agent_can_hand_off_to_a_better_placed_colleague(fc, state):
    """'That's actually your area, Wayne.'"""
    state, _ = run(
        fc,
        state,
        invite(0.0),
        AgentProposal(
            t=0.5, agent="dex", utterance="That's your area.", signals=strong(defer_to="wayne")
        ),
        AgentProposal(t=0.6, agent="wayne", utterance="Well, that's where —", signals=weak()),
    )
    _, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["wayne"]


# --------------------------------------------------- agents never cut in


def test_an_agent_never_interrupts_a_speaking_agent(fc, state):
    """A maximal interruption bid is stored, not aired.

    Agents pass turns; they never cut each other off (scoped out 21 Sept 2026,
    and the config that allowed it is gone). Wayne keeps the floor, Dexter's
    proposal stays in `state.proposals` where the next arbitration can find it,
    and nothing is thrown away — storing the bid rather than refusing it is the
    behaviour worth pinning here.
    """
    state, _ = run(
        fc,
        state,
        invite(0.0),
        AgentProposal(t=0.0, agent="wayne", utterance="Completely useless.", signals=strong()),
        TurnYielded(t=1.0),
        AgentSpeechStarted(t=1.1, agent="wayne"),
    )

    state, cmds = fc.reduce(
        state,
        AgentProposal(
            t=5.0,  # past the grace window; would interrupt if it were allowed
            agent="dex",
            utterance="Sorry, I've got to disagree there.",
            signals=strong(disagreement=1.0, urgency=1.0),
        ),
    )
    assert not [c for c in cmds if isinstance(c, StopSpeech)]
    assert not [c for c in cmds if isinstance(c, StartSpeech)]
    assert state.speaking == "wayne", "the speaker runs to the end of their turn"
    assert "dex" in state.proposals, "the bid is kept for the next arbitration"


def test_a_speaking_agent_finishes_even_against_a_maximal_bid(fc, state):
    """No signal strength, and no elapsed time, buys a cut-in.

    The removed path was gated on disagreement x urgency clearing a threshold
    once a grace window had passed. Both extremes are pinned here so a future
    re-introduction has to delete a test rather than quietly flip a default:
    maximal signals well past any plausible grace window still change nothing.
    """
    state, _ = run(
        fc,
        state,
        invite(0.0),
        AgentProposal(t=0.0, agent="wayne", utterance="Look —", signals=strong()),
        TurnYielded(t=1.0),
        AgentSpeechStarted(t=1.1, agent="wayne"),
    )
    for t, challenger in ((1.5, "dex"), (6.0, "melia"), (30.0, "dex")):
        _, cmds = fc.reduce(
            state,
            AgentProposal(
                t=t,
                agent=challenger,
                utterance="No.",
                signals=strong(disagreement=1.0, urgency=1.0),
            ),
        )
        assert not [c for c in cmds if isinstance(c, StopSpeech)], f"cut in at t={t}"
        assert not [c for c in cmds if isinstance(c, StartSpeech)], f"granted at t={t}"


def test_stop_reasons_do_not_include_an_agent_interrupt():
    """The vocabulary itself is the guarantee.

    `StopReason` is the operator console's and the video wall's vocabulary, so
    an agent-interrupt value reappearing there is the signal that the path came
    back with it.

    The set is exhaustive on purpose — a new reason has to be added here
    deliberately, which is what makes the tripwire worth anything. `stalled` is
    one such addition (`FloorController._stalled_speaker`): it stops an agent
    that is no longer producing audio, which is neither an interrupt nor a
    length limit. Adding a value is fine; an agent-interrupt value is not.
    """
    assert {r.value for r in StopReason} == {
        "human_interrupt",
        "operator",
        "kill",
        "stalled",
    }


# ------------------------------------------------------------- safety valves


def test_floor_returns_to_moderator_after_consecutive_agent_turns(fc, state):
    """The panel must not drift into an unbounded machine-to-machine loop."""
    t = 0.0
    for i in range(fc.config.max_consecutive_agent_turns):
        agent = ("dex", "wayne", "melia")[i % 3]
        state, _ = run(
            fc,
            state,
            invite(t - 0.1),
            AgentProposal(t=t, agent=agent, utterance="...", signals=strong()),
            TurnYielded(t=t + 0.1),
            AgentSpeechStarted(t=t + 0.2, agent=agent),
            AgentSpeechEnded(t=t + 5.0, agent=agent, completed=True),
        )
        t += 30.0

    assert state.consecutive_agent_turns >= fc.config.max_consecutive_agent_turns
    state, cmds = run(
        fc,
        state,
        invite(t),
        AgentProposal(t=t, agent="dex", utterance="...", signals=strong()),
        TurnYielded(t=t + 1),
    )
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == ["agent_turn_limit"]
    assert not [c for c in cmds if isinstance(c, StartSpeech)]


def test_kill_switch_silences_everything_and_blocks_new_turns(fc, state):
    state, _ = run(
        fc,
        state,
        invite(0.0),
        AgentProposal(t=0.0, agent="wayne", utterance="...", signals=strong()),
        TurnYielded(t=1.0),
        AgentSpeechStarted(t=1.1, agent="wayne"),
    )
    state, cmds = fc.reduce(state, OperatorCommand(t=2.0, action=OperatorAction.KILL_ALL))
    assert [c for c in cmds if isinstance(c, StopSpeech) and c.reason is StopReason.KILL]

    _, cmds = run(
        fc,
        state,
        invite(3.0),
        AgentProposal(t=3.0, agent="dex", utterance="...", signals=strong()),
        TurnYielded(t=4.0),
    )
    assert not [c for c in cmds if isinstance(c, StartSpeech)], "killed panel stays silent"


def test_muted_agent_never_wins_the_floor(fc, state):
    state, _ = fc.reduce(
        state, OperatorCommand(t=0.0, action=OperatorAction.MUTE_AGENT, agent="wayne")
    )
    state, _ = run(
        fc,
        state,
        invite(0.0, "Wayne, what about oversight?"),
        AgentProposal(t=0.5, agent="wayne", utterance="...", signals=strong()),
    )
    _, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert not [c for c in cmds if isinstance(c, StartSpeech)]


# -------------------------------------------------------------- speculation


def test_partials_trigger_debounced_proposal_requests(fc, state):
    state, cmds = fc.reduce(
        state, TranscriptUpdated(t=10.0, speaker=HUMAN, text="So what do you think", is_final=False)
    )
    assert [c for c in cmds if isinstance(c, RequestProposals)]

    # Too soon — debounced.
    state, cmds = fc.reduce(
        state,
        TranscriptUpdated(t=10.1, speaker=HUMAN, text="So what do you think about", is_final=False),
    )
    assert not [c for c in cmds if isinstance(c, RequestProposals)]

    state, cmds = fc.reduce(
        state,
        TranscriptUpdated(
            t=10.0 + fc.config.speculation_interval_s + 0.01,
            speaker=HUMAN,
            text="So what do you think about oversight",
            is_final=False,
        ),
    )
    assert [c for c in cmds if isinstance(c, RequestProposals)]


def test_a_partial_too_short_to_answer_is_not_worth_asking_about(fc, state):
    """Finals land on pauses, so every turn opens with a fragment.

    "So, Wayne, uh" is not a question, and an agent asked to propose against it
    writes a holding line — which a direct question then airs unconditionally,
    because a named invitation bypasses the score floor. The cheapest fix is to
    not ask. The debounce alone does not cover this: it is the *first* partial
    of a turn, so nothing has been requested for seconds and it is always due.
    """
    state, cmds = fc.reduce(
        state, TranscriptUpdated(t=10.0, speaker=HUMAN, text="So, Wayne, uh", is_final=False)
    )
    assert not [c for c in cmds if isinstance(c, RequestProposals)]

    # Enough of the sentence to be answerable, and the debounce has not been
    # reset by the fragment, so this asks immediately rather than 0.8s later.
    state, cmds = fc.reduce(
        state,
        TranscriptUpdated(
            t=10.2, speaker=HUMAN, text="So, Wayne, uh, where are we", is_final=False
        ),
    )
    assert [c for c in cmds if isinstance(c, RequestProposals)]


def test_a_short_final_always_asks_however_few_words(fc, state):
    """The word gate is for partials only.

    "Wayne, thoughts?" is a real thing Ricky says, and by the time it is final
    the invitation exists — gating it would leave the named agent permanently
    silent on the shortest direct questions.
    """
    _, cmds = fc.reduce(
        state, TranscriptUpdated(t=10.0, speaker=HUMAN, text="Wayne, thoughts?", is_final=True)
    )
    assert [c for c in cmds if isinstance(c, RequestProposals)]


def test_every_proposal_request_takes_a_fresh_epoch_partials_included(fc, state):
    """The generation label: one bump per round, and none without a round.

    This used to bump once per *final* and never on a partial, on the reasoning
    that a generation per 0.8s partial was spend without a matching gain. The
    gain is fresher input, and the counter-evidence is the live run in
    `test_a_named_agent_may_not_answer_with_a_line_that_raced_past_the_question`:
    one label per human turn meant `PanelRuntime._request_proposals` skipped any
    agent whose single generation for that label was still running, so the
    slowest agent could never be re-asked inside the turn and its answer was
    always written against the turn's oldest input. Cost is not a constraint
    here (CLAUDE.md); a stale answer to a direct question is.

    `(agent, epoch)` has to name exactly one generation for the runtime to be
    able to start a second one for the same agent, so the invariant is
    epoch-moves-if-and-only-if-a-round-opened — in both directions. A label
    handed out with no round behind it would make the runtime treat a
    generation it never started as having superseded a live one.
    """

    def ask(st, event):
        st, cmds = fc.reduce(st, event)
        return st, bool([c for c in cmds if isinstance(c, RequestProposals)])

    def partial(t: float, text: str) -> TranscriptUpdated:
        return TranscriptUpdated(t=t, speaker=HUMAN, text=text, is_final=False)

    # Too short to be worth asking about (`speculation_min_words`): no round,
    # so no new label either.
    state, asked = ask(state, partial(10.0, "So, Wayne, uh"))
    assert not asked
    assert state.speculation_epoch == 0

    # A partial long enough to answer opens a round — and takes a label. This
    # is the assertion the old test had inverted.
    state, asked = ask(state, partial(10.2, "So, Wayne, uh, where are we"))
    assert asked
    assert state.speculation_epoch == 1, "a speculative round must be labelled too"

    # Debounced (`speculation_interval_s`): no round, so no label.
    state, asked = ask(state, partial(10.3, "So, Wayne, uh, where are we on"))
    assert not asked
    assert state.speculation_epoch == 1

    # Past the debounce, so a new round against newer text — which is the point
    # of the change: the slow agent gets asked again without its in-flight
    # generation being cancelled or its key being occupied.
    state, asked = ask(
        state,
        partial(
            10.2 + fc.config.speculation_interval_s + 0.01,
            "So, Wayne, uh, where are we on the adoption curve",
        ),
    )
    assert asked
    assert state.speculation_epoch == 2

    # A final always asks, however short, so it always relabels.
    state, asked = ask(
        state, TranscriptUpdated(t=11.2, speaker=HUMAN, text="Wayne?", is_final=True)
    )
    assert asked
    assert state.speculation_epoch == 3


def test_every_proposal_request_stamps_its_input_time_and_takes_a_label(fc, state):
    """Whatever opens a round, the same three things move together.

    `PanelRuntime._request_proposals` has no other way to know what its
    generations are answering: it snapshots the state the reducer just
    returned, reads `last_proposal_request_t` off it, and stamps that onto
    every `AgentProposal` as `input_t` — while keying the generation by
    `speculation_epoch` from the same snapshot. A site that built the command
    without setting both would hand the floor a generation whose label and
    input time came from different rounds, which is precisely what
    `_ask_for_proposals` exists to make impossible.

    So this walks every emitter in the file and checks the pair lands on the
    triggering event's own timestamp. `operator_forced` is here because it is
    the site that used to build its command by hand.
    """
    seen: list[tuple[str, float, int]] = []

    def record(st, event):
        st, cmds = fc.reduce(st, event)
        for command in cmds:
            if isinstance(command, RequestProposals):
                seen.append((command.reason, st.last_proposal_request_t, st.speculation_epoch))
        return st

    state = record(
        state,
        TranscriptUpdated(
            t=10.0, speaker=HUMAN, text="So where are we on adoption", is_final=False
        ),
    )
    state = record(state, invite(11.0))
    # Arbitration found nothing, so the completed turn is asked again.
    state = record(state, TurnYielded(t=12.0))
    state, _ = fc.reduce(
        state,
        AgentProposal(t=12.1, input_t=11.0, agent="dex", utterance="Trust.", signals=strong()),
    )
    state = record(state, TurnYielded(t=12.2))
    state, _ = fc.reduce(state, AgentSpeechStarted(t=12.3, agent="dex"))
    # A granted turn ending, with the open invitation still worth another one.
    state = record(state, AgentSpeechEnded(t=18.0, agent="dex", completed=True, utterance="Trust."))
    # ...and the operator backstop, forcing an agent with nothing queued.
    state = record(state, OperatorCommand(t=20.0, action=OperatorAction.FORCE_AGENT, agent="melia"))

    assert [reason for reason, _, _ in seen] == [
        "speculation",
        "final",
        "post_turn",
        "agent_turn_ended",
        "operator_forced",
    ]
    assert [t for _, t, _ in seen] == [10.0, 11.0, 12.0, 18.0, 20.0], (
        "the input time must be the triggering event's own, not the previous round's"
    )
    epochs = [epoch for _, _, epoch in seen]
    assert epochs == sorted(set(epochs)), "one fresh label per round, never reused"
    assert epochs[0] >= 1, "no round may go out unlabelled"


def test_final_transcript_always_requests_proposals(fc, state):
    """Regression: the debounce must throttle partials only.

    A human turn landing inside the debounce window — which is exactly what
    happens on the turn straight after an agent finishes — was getting no
    candidates at all, so the panel fell silent.
    """
    state, _ = fc.reduce(state, TranscriptUpdated(t=10.0, speaker=HUMAN, text="So", is_final=False))
    state, cmds = fc.reduce(
        state, TranscriptUpdated(t=10.05, speaker=HUMAN, text="So what?", is_final=True)
    )
    assert [c for c in cmds if isinstance(c, RequestProposals)]


def test_turn_immediately_after_an_agent_finishes_is_not_starved(fc, state):
    """End-to-end shape of the bug the smoke test caught."""
    state, _ = run(
        fc,
        state,
        invite(0.0),
        AgentProposal(t=0.0, agent="wayne", utterance="Look —", signals=strong()),
        TurnYielded(t=0.1),
        AgentSpeechStarted(t=0.2, agent="wayne"),
        AgentSpeechEnded(t=5.0, agent="wayne", completed=True),
    )
    # Ricky comes straight back in at the same instant the agent stopped.
    state, cmds = run(
        fc,
        state,
        HumanSpeechStarted(t=5.0),
        TranscriptUpdated(t=5.0, speaker=HUMAN, text="What holds it back?", is_final=True),
    )
    assert [c for c in cmds if isinstance(c, RequestProposals)]


# ------------------------------------------------- the turn boundary, 21 Sept


def _open_turn(fc, state, agent="dex", *, invited_at=0.0, started=0.5, ended=30.0):
    """Ricky opens the floor, one agent takes it and speaks for `ended - started`.

    Turn lengths on stage run 20-30s, which is the whole point of the fixtures
    below: the numbers here are not arbitrary, they straddle `invitation_ttl_s`.
    """
    state, _ = run(
        fc,
        state,
        invite(invited_at, "Where are we on the adoption curve?"),
        AgentProposal(
            t=invited_at + 0.1,
            input_t=invited_at,
            agent=agent,
            utterance="Unevenly.",
            signals=strong(),
        ),
        TurnYielded(t=started),
        AgentSpeechStarted(t=started + 0.1, agent=agent),
    )
    assert state.speaking == agent
    return state


def test_a_long_turn_does_not_age_out_its_own_invitation(fc, state):
    """From the stage, 21 Sept 2026.

        16:40:07.975  floor: invited the panel — open/llm_open
        16:40:07.976  Dexter granted; the TTL clock is stamped here, at the
                      *start* of the turn, and nowhere else.
        16:40:37.851  Dexter finishes — 29.9s, against a 25s TTL. The
                      invitation has been past its deadline for five seconds
                      and nothing noticed, because `_expire_invitation` is
                      held off while anyone is on the PA.
        16:40:37.940  first tick afterwards: `invitation_expired`, floor
                      closed, Ricky told to fill.
        16:40:39.695  the other two agents' lines arrive, to a closed floor.

    Nobody was unwilling. The invitation died of old age at the exact moment
    the exchange it was granting became possible.
    """
    state = _open_turn(fc, state)
    state, cmds = fc.reduce(state, AgentSpeechEnded(t=30.0, agent="dex", completed=True))
    assert state.invitation is not None and state.invitation.is_live()

    state, cmds = fc.reduce(state, Tick(t=30.1))
    assert not [c for c in cmds if isinstance(c, CueModerator)]
    assert state.invitation is not None, "a turn that just ended is not an invitation nobody used"


def test_an_invitation_nobody_ever_acts_on_still_expires(fc, state):
    """The behaviour the TTL is actually for, unchanged by the above."""
    state, _ = fc.reduce(state, invite(0.0))
    assert state.invitation is not None

    state, cmds = fc.reduce(state, Tick(t=fc.config.invitation_ttl_s + 1.0))
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [CueReason.INVITATION_EXPIRED]
    assert state.invitation is None


def test_a_bid_raised_during_a_turn_survives_it_and_is_taken_at_once(fc, state):
    """The other half of the same 30 seconds.

    Wayne and Melia both had a line ready 1.7s into Dexter's turn. The turn
    boundary cleared every proposal unconditionally, so the panel then sent a
    fresh `RequestProposals` and waited out another 2s generation for lines it
    was already holding. Keeping them is only useful if they can be used
    immediately: the reducer never re-arbitrates on its own and the runtime
    only does so when a proposal *arrives*, so the grant has to happen here.
    """
    state = _open_turn(fc, state)
    state, _ = fc.reduce(
        state,
        AgentProposal(
            t=2.5, input_t=2.2, agent="wayne", utterance="I've been bitten too.", signals=strong()
        ),
    )
    state, cmds = fc.reduce(state, AgentSpeechEnded(t=30.0, agent="dex", completed=True))

    starts = [c for c in cmds if isinstance(c, StartSpeech)]
    assert [c.agent for c in starts] == ["wayne"]
    assert starts[0].utterance == "I've been bitten too."
    assert not [c for c in cmds if isinstance(c, RequestProposals)], (
        "no point asking for a line we are about to talk over"
    )


def test_a_bid_written_before_the_turn_does_not_survive_it(fc, state):
    """Kept means "written during the turn", not "kept forever".

    A proposal older than the turn was answering a moment two turns back and
    has already been passed over once. Measured on the input clock, so a slow
    generation that started before the turn is still refused however late it
    happened to arrive.

    The turn opens at 1.0 rather than the fixture's default 0.0 to leave room
    for a bid that is genuinely older than the round that granted it. The
    cutoff is `turn_input_t` — the moment Dexter's own line was written
    against — not `speaking_since`, so "before the turn" has to mean before
    that round, not merely before the audio. A bid between the two clocks is
    a sibling of the granted line and is kept on purpose; see
    `test_the_opening_rounds_siblings_survive_the_turn_they_opened`.
    """
    state = _open_turn(fc, state, invited_at=1.0, started=1.5)
    state, _ = fc.reduce(
        state,
        AgentProposal(
            t=2.5,
            input_t=0.05,  # answering a moment before this turn's round opened
            agent="wayne",
            utterance="Take your time, Ricky.",
            signals=strong(),
        ),
    )
    state, cmds = fc.reduce(state, AgentSpeechEnded(t=30.0, agent="dex", completed=True))

    assert "wayne" not in state.proposals
    assert not [c for c in cmds if isinstance(c, StartSpeech)]
    assert [c for c in cmds if isinstance(c, RequestProposals)]


def test_the_opening_rounds_siblings_survive_the_turn_they_opened(fc, state):
    """The 110ms case, 1 Oct 2026. The other half of the test above.

    The round that grants a turn also writes the other agents' lines, and it
    necessarily opens *before* the winner reaches the PA — the address
    classifier hold, arbitration and TTS first-audio all sit in between.
    Measured from `speaking_since` those siblings were always "older than the
    turn" and were always discarded, so the filter could only ever return
    empty and every handover paid a cold generation. Measured from
    `turn_input_t` they are what they are: answers to the same moment as the
    line the room just heard, and the fallback whenever nothing fresher
    exists.

    This is the half a revert to `speaking_since` would break silently —
    `test_a_bid_raised_during_a_turn_survives_it_and_is_taken_at_once` writes
    its bid *during* the turn and so passes under either clock.
    """
    state = _open_turn(fc, state)  # Dexter granted off input_t=0.0
    state, _ = fc.reduce(
        state,
        AgentProposal(
            t=0.15,
            input_t=0.0,  # same round as Dexter's line, before the grant
            agent="wayne",
            utterance="Not on the numbers I'm looking at.",
            signals=strong(),
        ),
    )
    state, cmds = fc.reduce(state, AgentSpeechEnded(t=30.0, agent="dex", completed=True))

    starts = [c for c in cmds if isinstance(c, StartSpeech)]
    assert [c.agent for c in starts] == ["wayne"], (
        "a sibling of the granted line is the free handover, not a leftover"
    )
    assert not [c for c in cmds if isinstance(c, RequestProposals)], (
        "no point asking for a line we are about to talk over"
    )


def test_the_agent_who_just_spoke_may_not_take_the_next_turn_off_its_own_bid(fc, state):
    """A line written while holding the floor may not win the floor back.

    `max_consecutive_agent_turns` bounds a relay; it does not stop one voice
    taking both turns of a single open invitation, which is what keeping
    mid-turn proposals would otherwise allow. Dexter is asked again on the
    request path — a genuine continuation goes through a fresh line.
    """
    state = _open_turn(fc, state)
    state, _ = fc.reduce(
        state,
        AgentProposal(
            t=2.5, input_t=2.2, agent="dex", utterance="And another thing.", signals=strong()
        ),
    )
    state, cmds = fc.reduce(state, AgentSpeechEnded(t=30.0, agent="dex", completed=True))

    assert not [c for c in cmds if isinstance(c, StartSpeech)]
    assert [c for c in cmds if isinstance(c, RequestProposals)]


def test_a_line_written_before_the_question_does_not_win_an_open_floor(fc, state):
    """`_stale`, on the open floor it never covered. From the stage, 21 Sept.

        15:12:58.247  "Um, so let's just dive right into it. Where do you think
                      we are on the adoption curve? Who wants to go first?"
        15:12:58.420  floor invited, and Melia airs "Mm, I'll wait to hear
                      where Ricky's actually pointing this before I stake out
                      ground." — one millisecond later, so necessarily written
                      against the preamble, since generation measures 1.7-2.4s.

    The open floor was guarded only by `min_floor_priority`, and a confident
    holding line clears that comfortably. Note the window here does not by
    itself catch that particular line — Melia's input was frozen ~1.9s before
    the final, inside `open_proposal_lookback_s`; the prompt is what stops an
    agent writing it. This is the backstop for the grossly old, which the turn
    boundary now deliberately keeps around.
    """
    state, _ = run(
        fc,
        state,
        TranscriptUpdated(t=0.0, speaker=HUMAN, text="Um, so", is_final=False),
        AgentProposal(
            t=1.0,
            input_t=0.0,
            agent="melia",
            utterance="I'll wait to hear where Ricky's pointing this.",
            signals=strong(),
        ),
        invite(10.0, "Where do you think we are on the adoption curve?"),
    )
    assert state.invitation is not None and state.invitation.agents == ()

    state, cmds = fc.reduce(state, TurnYielded(t=10.2))
    assert not [c for c in cmds if isinstance(c, StartSpeech)]
    assert state.invitation.is_live(), "the floor stays open for a real answer"


def test_a_line_written_during_the_previous_turn_still_wins_an_open_floor(fc, state):
    """The two fixes have to coexist: the backstop must not eat what we keep.

    A bid raised mid-turn is 30 seconds older than the *grant* but newer than
    the invitation, and `Invitation.t` no longer moves as turns are spent — so
    it measures as fresh, which is the entire point of keeping it.
    """
    state = _open_turn(fc, state)
    state, _ = fc.reduce(
        state,
        AgentProposal(
            t=29.0, input_t=28.5, agent="melia", utterance="Flag it all you want.", signals=strong()
        ),
    )
    state, cmds = fc.reduce(state, AgentSpeechEnded(t=30.0, agent="dex", completed=True))
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["melia"]


# ------------------------------------------------------------------- replay


def test_reduce_is_deterministic(fc, cast):
    """Same events in, same commands out — the basis of replayable rehearsals."""
    events = [
        TranscriptUpdated(t=0.0, speaker=HUMAN, text="So Wayne, oversight?", is_final=True),
        AgentProposal(t=0.5, agent="wayne", utterance="Useless.", signals=strong()),
        AgentProposal(t=0.6, agent="dex", utterance="Historically...", signals=strong()),
        TurnYielded(t=1.0),
        AgentSpeechStarted(t=1.1, agent="wayne"),
        AgentProposal(
            t=6.0,
            agent="dex",
            utterance="Disagree.",
            signals=strong(disagreement=0.96, urgency=0.95),
        ),
    ]
    a = run(fc, PanelState.for_agents(cast.ids()), *events)[1]
    b = run(fc, PanelState.for_agents(cast.ids()), *events)[1]
    assert a == b


def test_agent_speech_enters_the_transcript_without_stt(fc, state):
    """The rule that keeps the feedback loop closed.

    An agent's words reach conversation state on the event that ends its turn —
    never by being heard back through a microphone.
    """
    state, _ = run(
        fc,
        state,
        invite(0.0),
        AgentProposal(t=0.5, agent="dex", utterance="Historically, no.", signals=strong()),
        TurnYielded(t=1.0),
        AgentSpeechStarted(t=1.1, agent="dex"),
        AgentSpeechEnded(t=6.0, agent="dex", completed=True, utterance="Historically, no."),
    )
    assert state.transcript[-1].speaker == "dex"
    assert state.transcript[-1].text == "Historically, no."


# --------------------------------------------------------- introduction round


def _introduce(t: float = 0.0) -> TranscriptUpdated:
    return TranscriptUpdated(
        t=t, speaker=HUMAN, text="Let's have all of you introduce yourselves.", is_final=True
    )


def test_introduce_yourselves_invites_the_whole_panel(fc, state):
    state = replace(state, intro_done=False)
    state, _ = fc.reduce(state, _introduce(0.0))
    assert state.invitation is not None
    assert state.invitation.agents == ()
    assert set(state.intro_queue) == set(fc.cast.ids())


@pytest.mark.parametrize(
    "text",
    [
        "Let's have all of you introduce yourselves.",
        "Can everyone give a quick intro?",
        "Let's do intros before we start.",
        "Time for introductions.",
        "Could you introduce yourself to the crowd?",
        "Why don't you each give an introduction.",
        "Go ahead and introduce yourself, Wayne.",
    ],
)
def test_introduction_trigger_is_broad(fc, state, text):
    """Any variation on introduce/introduction/intro must trigger the round —
    deliberately cast wide, since the one-shot latch is what makes a stray
    hit cheap and missing the real cue on stage is the worse failure."""
    state = replace(state, intro_done=False)
    state, _ = fc.reduce(state, TranscriptUpdated(t=0.0, speaker=HUMAN, text=text, is_final=True))
    assert state.intro_queue is not None
    assert set(state.intro_queue) == set(fc.cast.ids())


@pytest.mark.parametrize(
    "text",
    [
        "She's quite introverted.",
        "That was an introspective answer.",
        "We have been doing real-time transcription for a decade now.",
    ],
)
def test_introduction_trigger_does_not_false_positive(fc, state, text):
    state = replace(state, intro_done=False)
    state, _ = fc.reduce(state, TranscriptUpdated(t=0.0, speaker=HUMAN, text=text, is_final=True))
    assert state.intro_queue is None


# ----------------------------------- nothing answers before introductions


def test_named_address_before_introductions_installs_no_invitation(fc, state):
    """Wayne cannot be invited to speak until Ricky has walked the panel
    through introductions — every route to the floor funnels through
    `FloorController._install_invitation`, and that is where this is refused."""
    state = replace(state, intro_done=False)
    state, _ = fc.reduce(state, invite(0.0, "So Wayne, what about human oversight?"))
    assert state.invitation is None


def test_open_floor_before_introductions_installs_no_invitation(fc, state):
    state = replace(state, intro_done=False)
    state, _ = fc.reduce(state, invite(0.0, "What holds it back?"))
    assert state.invitation is None


def test_no_proposals_are_requested_before_introductions(fc, state):
    """Not just "nobody is granted the floor" — nobody is even asked to
    generate, so an un-introduced agent cannot burn a model round trip on a
    question it will never be allowed to answer."""
    state = replace(state, intro_done=False)
    state, cmds = fc.reduce(state, invite(0.0, "What holds it back?"))
    assert not [c for c in cmds if isinstance(c, RequestProposals)]


def test_turn_yielded_before_introductions_cues_the_moderator_like_a_plain_remark(fc, state):
    """A refused invitation leaves `state.invitation` exactly as a plain
    remark would — `None` — so `TurnYielded` takes the same branch it always
    has and the operator sees `NO_INVITATION`, not silence with no reason."""
    state = replace(state, intro_done=False)
    state, _ = fc.reduce(state, invite(0.0, "So Wayne, what about human oversight?"))
    state, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert not [c for c in cmds if isinstance(c, StartSpeech)]
    assert CueModerator(reason=CueReason.NO_INVITATION) in cmds


def test_address_works_again_once_introductions_are_done(fc, state):
    """The gate lifts the moment `intro_done` latches — same sentence, same
    agent, the only difference is which side of the round it lands on."""
    state, _ = _run_introduction_round(fc, state)
    assert state.intro_done is True

    state, _ = fc.reduce(state, invite(10.0, "So Wayne, what about human oversight?"))
    assert state.invitation is not None
    assert state.invitation.agents == ("wayne",)


def _intro_order(fc: FloorController) -> list[str]:
    """`Persona.intro_position` order — Dexter, Wayne, Melia — independent of
    `fc.cast.ids()` (alphabetical, shared with the video wall's lane order)."""
    return sorted(fc.cast.ids(), key=lambda a: fc.cast[a].intro_position)


def _closing_order(fc: FloorController) -> list[str]:
    """`Persona.closing_position` order — Wayne, Melia, Dexter."""
    return sorted(
        (a for a in fc.cast.ids() if fc.cast[a].closing_position is not None),
        key=lambda a: fc.cast[a].closing_position,
    )


def _run_introduction_round(
    fc: FloorController, state: PanelState, *, start_t: float = 0.0
) -> tuple[PanelState, list[tuple[str, str]]]:
    """Drive the whole fixed opening the way the runtime now does: no
    `AgentProposal`, ever. The floor grants each fixed line itself the moment
    the phrase is detected and again every time one agent finishes, so the
    only events a caller needs to supply are the ones confirming that audio
    actually started and stopped. Runs straight through the introduction
    round *and* the fixed exchange that follows it (`_start_closing`) — they
    are one continuous one-shot sequence now, not two. Returns the final
    state and the ``(agent, utterance)`` pairs in the order they were
    granted.
    """
    state = replace(state, intro_done=False)
    state, cmds = fc.reduce(state, _introduce(start_t))
    order: list[tuple[str, str]] = []
    t = start_t + 1.0
    starts = [c for c in cmds if isinstance(c, StartSpeech)]
    while starts:
        assert len(starts) == 1, "never more than one turn granted at a time"
        start = starts[0]
        order.append((start.agent, start.utterance))
        state, _ = fc.reduce(state, AgentSpeechStarted(t=t, agent=start.agent))
        state, cmds = fc.reduce(
            state,
            AgentSpeechEnded(
                t=t + 1.0, agent=start.agent, completed=True, utterance=start.utterance
            ),
        )
        starts = [c for c in cmds if isinstance(c, StartSpeech)]
        t += 2.0
    return state, order


def test_introduction_phrase_immediately_grants_the_first_agent(fc, state):
    """The whole point: no model call, no waiting on a proposal. The floor
    controller already holds the text — it is on the persona — so the very
    reduce() call that detects the phrase also produces the first `StartSpeech`."""
    state = replace(state, intro_done=False)
    state, cmds = fc.reduce(state, _introduce(0.0))
    starts = [c for c in cmds if isinstance(c, StartSpeech)]
    assert len(starts) == 1
    first = _intro_order(fc)[0]
    assert starts[0].agent == first
    assert starts[0].utterance == fc.cast[first].introduction
    assert not [c for c in cmds if isinstance(c, RequestProposals)], (
        "the introduction round must never ask the model for anything"
    )


def test_every_agent_gets_exactly_one_introduction_turn(fc, state):
    """All three must speak in the introduction round proper, with no
    repeats there, before the fixed exchange that follows it starts; the
    whole sequence latches shut only once both have run."""
    state, order = _run_introduction_round(fc, state)
    intro_spoken = [agent for agent, _ in order[: len(fc.cast.ids())]]

    assert set(intro_spoken) == set(fc.cast.ids())
    assert len(intro_spoken) == len(set(intro_spoken)), "no repeats in the introductions"
    assert state.invitation is None
    assert state.intro_done is True


def test_introduction_round_speaks_each_persona_fixed_text_in_intro_order(fc, state):
    """Order is `Persona.intro_position` (Dexter, Wayne, Melia), not a score
    and not the cast's own (alphabetical) order — and every word spoken in
    this phase is `Persona.introduction`, verbatim, never anything a model
    produced."""
    state, order = _run_introduction_round(fc, state)
    intro_order = _intro_order(fc)
    intro_turns = order[: len(intro_order)]
    assert [agent for agent, _ in intro_turns] == intro_order
    assert dict(intro_turns) == {a: fc.cast[a].introduction for a in intro_order}


def test_closing_exchange_runs_immediately_after_introductions(fc, state):
    """The fixed "We discussed this." / "Repeatedly." / "[sighs] here we go
    again." exchange is scripted the same way as the introductions and runs
    straight on from them, in `Persona.closing_position` order, with no
    `AgentProposal` involved."""
    state, order = _run_introduction_round(fc, state)
    closing_order = _closing_order(fc)
    closing_turns = order[len(_intro_order(fc)) :]
    assert [agent for agent, _ in closing_turns] == closing_order
    assert dict(closing_turns) == {a: fc.cast[a].closing_line for a in closing_order}
    assert state.intro_done is True
    assert state.closing_queue is None
    assert state.invitation is None


def test_introductions_cannot_be_retriggered_once_complete(fc, state):
    state, _ = _run_introduction_round(fc, state)
    assert state.intro_done is True

    # Safety: saying it again does nothing, ever.
    state, cmds = fc.reduce(state, _introduce(100.0))
    assert state.invitation is None
    assert state.intro_queue is None
    assert not [c for c in cmds if isinstance(c, StartSpeech)]


def test_introduction_round_ignores_the_consecutive_turn_safety_valve(fc, state):
    """Three agents is exactly `max_consecutive_agent_turns` in this config.
    The valve that stops a machine-to-machine relay must not cut the round
    short — and by construction it cannot: the introduction round grants
    itself directly (`_advance_introductions`) rather than going through
    `_turn_yielded`'s arbitration, which is the only place that valve is
    ever consulted."""
    config = FloorConfig(max_consecutive_agent_turns=2)
    fc = FloorController(fc.cast, config)
    state, order = _run_introduction_round(fc, state)
    assert {agent for agent, _ in order} == set(fc.cast.ids())
    assert state.intro_done is True


def test_ricky_interrupting_the_round_allows_a_clean_retry(fc, state):
    """An abandoned round has not 'been done' — the safety latch must not
    engage, or the show is stuck with two agents introduced and no way to
    finish."""
    state = replace(state, intro_done=False)
    state, cmds = fc.reduce(state, _introduce(0.0))
    winner = cmds[0].agent
    state, _ = fc.reduce(state, AgentSpeechStarted(t=0.2, agent=winner))

    # Ricky cuts in mid-round.
    state, _ = fc.reduce(state, HumanSpeechStarted(t=0.5))
    state, _ = fc.reduce(
        state, TranscriptUpdated(t=0.6, speaker=HUMAN, text="hang on, stop", is_final=True)
    )
    assert state.invitation is None
    assert state.intro_queue is None
    assert state.intro_done is False, "not done — only abandoned"

    state, cmds = fc.reduce(state, _introduce(2.0))
    assert state.invitation is not None
    assert set(state.intro_queue) == set(fc.cast.ids()), "starts over, nobody is skipped"
    assert [c for c in cmds if isinstance(c, StartSpeech)], "the retry grants immediately too"


def test_muted_agent_is_skipped_not_granted_during_introductions(fc, state):
    """`OperatorAction.MUTE_AGENT` is an absolute veto everywhere else in the
    floor. Fixed text has no proposal for a muted agent's turn to fail to
    produce, so the introduction round has to enforce the mute itself."""
    state, _ = fc.reduce(
        state, OperatorCommand(t=0.0, action=OperatorAction.MUTE_AGENT, agent="dex")
    )
    state, order = _run_introduction_round(fc, state)
    spoken = [agent for agent, _ in order]
    assert "dex" not in spoken
    assert set(spoken) == set(fc.cast.ids()) - {"dex"}


def test_an_introduced_agent_cannot_repeat_even_with_a_stray_proposal(fc, state):
    """The old, score-based version of this round could in principle have let
    a strong late proposal from an already-introduced agent win a second
    time. Fixed text has no score to win with: `_advance_introductions` only
    ever reads `intro_queue`, never `state.proposals`, so a stray proposal
    changes nothing."""
    state = replace(state, intro_done=False)
    state, cmds = fc.reduce(state, _introduce(0.0))
    first = cmds[0].agent
    state, _ = fc.reduce(state, AgentSpeechStarted(t=0.2, agent=first))
    state, cmds = fc.reduce(
        state, AgentSpeechEnded(t=1.0, agent=first, completed=True, utterance=cmds[0].utterance)
    )
    assert first not in state.intro_queue

    state, cmds2 = fc.reduce(
        state, AgentProposal(t=1.1, agent=first, utterance="Also...", signals=strong())
    )
    assert not [c for c in cmds2 if isinstance(c, StartSpeech)], "a proposal alone grants nothing"
    assert first not in (state.intro_queue or ())


def test_agents_hear_each_other(fc, state):
    """A second agent's proposal is built against the first agent's words."""
    state, _ = run(
        fc,
        state,
        invite(0.0),
        AgentProposal(t=0.5, agent="dex", utterance="Trust is the blocker.", signals=strong()),
        TurnYielded(t=1.0),
        AgentSpeechStarted(t=1.1, agent="dex"),
        AgentSpeechEnded(t=6.0, agent="dex", completed=True, utterance="Trust is the blocker."),
    )
    assert "Trust is the blocker." in state.recent_text()


# ------------------------------------------------- the beat before the cue


def test_a_named_agent_answering_within_the_beat_takes_the_floor(fc, state):
    """The fix for the six-second hole, stated as the behaviour that was missing.

    Speechmatics' `EndOfTurn` lands within a few milliseconds of the final that
    names an agent, so the first arbitration of every invitation runs before any
    generation started against that final could have produced signals. The old
    reducer read that as "Wayne had nothing", cued Ricky, and the answer arrived
    moments later into a floor that had already moved on.
    """
    state, cmds = run(
        fc,
        state,
        invite(20.0, "So Wayne, where are we on the adoption curve?"),
        TurnYielded(t=20.01),  # EndOfTurn, effectively simultaneous
    )
    assert not [c for c in cmds if isinstance(c, CueModerator)], (
        "Ricky must not be told to fill a gap nobody has had time to fill"
    )
    assert state.awaiting_agents == ("wayne",)

    # Wayne's stream finishes inside the beat.
    state, cmds = fc.reduce(
        state,
        AgentProposal(t=20.4, agent="wayne", utterance="", signals=strong(), epoch=1),
    )
    assert state.awaiting_agents == (), "the answer arrived; stand the cue down"

    state, cmds = fc.reduce(state, TurnYielded(t=20.45))
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["wayne"]

    # ...and the cue that was armed never fires, however long we wait.
    _, cmds = fc.reduce(state, Tick(t=30.0))
    assert not [c for c in cmds if isinstance(c, CueModerator)]


def test_the_moderator_is_cued_once_per_invitation_not_once_per_proposal(fc, state):
    """The live run printed `back to Ricky` three times for one question.

    Every proposal re-opens arbitration from the runtime
    (`PanelRuntime._maybe_rearbitrate`), so each of the other two agents landing
    a proposal Wayne's invitation does not admit produced another cue.
    """
    state, _ = run(
        fc,
        state,
        invite(20.0, "So Wayne, where are we on the adoption curve?"),
        TurnYielded(t=20.01),
    )
    state, cmds = fc.reduce(state, Tick(t=20.01 + fc.config.invited_agent_grace_s))
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [
        CueReason.INVITED_AGENT_SILENT
    ]

    # Dexter and Melia answer a question that was not put to them.
    state, cmds = run(
        fc,
        state,
        AgentProposal(t=22.0, agent="dex", utterance="", signals=strong(), epoch=1),
        TurnYielded(t=22.01),
        AgentProposal(t=22.3, agent="melia", utterance="", signals=strong(), epoch=1),
        TurnYielded(t=22.31),
        Tick(t=24.0),
    )
    assert not [c for c in cmds if isinstance(c, CueModerator)], "Ricky needs telling once"


def test_an_open_invitation_with_nothing_cues_at_once(fc, state):
    """The beat is for a named agent only.

    An open invitation nobody wants is a real answer — the panel declining as a
    body, decided correctly by the score floor. Waiting on it would delay a
    decision rather than allow one.
    """
    state, cmds = run(fc, state, invite(0.0, "What holds it back?"), TurnYielded(t=1.0))
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [CueReason.NO_PROPOSALS]
    assert state.awaiting_agents == ()


def test_ricky_speaking_during_the_beat_stands_the_cue_down(fc, state):
    """He filled the gap himself, which is what the beat was hedging against."""
    state, _ = run(
        fc,
        state,
        invite(20.0, "So Wayne, where are we on the adoption curve?"),
        TurnYielded(t=20.01),
    )
    assert state.awaiting_agents == ("wayne",)

    state, _ = fc.reduce(state, HumanSpeechStarted(t=20.3))
    assert state.awaiting_agents == ("wayne",), (
        "a voice on the mic is not him filling the gap — the beat is short "
        "enough that a cough would otherwise end it"
    )
    state, _ = fc.reduce(state, ricky_partial(20.4, "right, let me put it another way"))
    assert state.awaiting_agents == ()

    _, cmds = fc.reduce(state, Tick(t=25.0))
    assert not [c for c in cmds if isinstance(c, CueModerator)], (
        "cueing Ricky to do what he is already doing prints a stale instruction"
    )


def test_a_later_question_re_arms_the_cue(fc, state):
    """The latch is per invitation, not per show."""
    state, _ = run(
        fc,
        state,
        invite(20.0, "So Wayne, where are we on the adoption curve?"),
        TurnYielded(t=20.01),
        Tick(t=20.01 + fc.config.invited_agent_grace_s),
    )
    assert state.moderator_cued

    state, _ = fc.reduce(state, invite(30.0, "Melia, does that match your read?"))
    assert not state.moderator_cued, "a fresh question is a fresh chance to be told"

    state, _ = fc.reduce(state, TurnYielded(t=30.01))
    _, cmds = fc.reduce(state, Tick(t=30.01 + fc.config.invited_agent_grace_s))
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [
        CueReason.INVITED_AGENT_SILENT
    ]


# --------------------------------------------------- racing generations


def test_a_slower_older_generation_does_not_overwrite_a_newer_answer(fc, state):
    """Generations race, so arrival order is not generation order.

    A stream started against Ricky's preamble can finish *after* one started
    against his actual question. Keeping the newest by epoch rather than by
    arrival is what stops the worse answer winning on a technicality.
    """
    state, _ = run(
        fc,
        state,
        invite(20.0, "So Wayne, where are we on the adoption curve?"),
        # The answer to the real question lands first.
        AgentProposal(t=20.3, input_t=19.6, agent="wayne", utterance="", signals=strong(), epoch=2),
        # The answer to "Thanks, everybody. Um, so," straggles in behind it.
        AgentProposal(t=20.4, input_t=18.0, agent="wayne", utterance="", signals=strong(), epoch=1),
    )
    assert state.proposals["wayne"].epoch == 2, "the older generation overwrote the newer one"

    _, cmds = fc.reduce(state, TurnYielded(t=20.5))
    granted = [c for c in cmds if isinstance(c, StartSpeech)]
    assert [c.agent for c in granted] == ["wayne"]
    assert granted[0].epoch == 2, "the runtime would have spoken the wrong generation's words"


def test_an_earlier_generation_is_still_usable_when_it_is_all_there_is(fc, state):
    """The fallback the old teardown destroyed.

    An answer written against the partials of the question is 1-3s ahead of the
    final that opens the floor, and `named_proposal_lookback_s` exists to admit
    exactly that. Cancelling on every final made this window unreachable.
    """
    state, _ = run(
        fc,
        state,
        AgentProposal(t=18.5, input_t=18.5, agent="wayne", utterance="", signals=strong(), epoch=1),
        invite(20.0, "So Wayne, where are we on the adoption curve?"),
    )
    _, cmds = fc.reduce(state, TurnYielded(t=20.01))
    granted = [c for c in cmds if isinstance(c, StartSpeech)]
    assert [c.agent for c in granted] == ["wayne"], (
        "speculation from inside the lookback window must still be airable"
    )
    assert granted[0].epoch == 1


# ------------------------------------------------------- the liveness watchdog
#
# `state.speaking` used to be cleared by exactly one event, `AgentSpeechEnded`,
# and every runtime path that can fail to emit it pinned the floor to a silent
# agent for the rest of the show. These pin the recovery, and — just as
# importantly — pin that it is a *silence* watchdog and not the turn-length
# ceiling that was cut on 11 Sept 2026.


def test_a_speaking_agent_producing_audio_keeps_the_floor_indefinitely(fc, state):
    """The settled decision this must not quietly reverse.

    Turn length is a prompt instruction with no orchestrator-enforced ceiling
    (CLAUDE.md). An agent that keeps reporting audio holds the floor for as
    long as it likes — two minutes here, far past any plausible turn — and the
    watchdog must never be the thing that ends it.
    """
    state = speaking_agent(fc, state, agent="wayne", t=1.0)

    t = 1.1
    while t < 121.0:
        state, cmds = run(
            fc,
            state,
            AgentAudioProgress(t=t, agent="wayne"),
            Tick(t=t + 0.5),
        )
        assert not [c for c in cmds if isinstance(c, StopSpeech)], f"cut off at t={t}"
        t += 0.5

    assert state.speaking == "wayne"


def test_a_speaking_agent_that_goes_silent_loses_the_floor(fc, state):
    """The failure that used to end the show.

    No `AgentAudioProgress` and no `AgentSpeechEnded` — a dead TTS socket, a
    crashed speak task, a faulted audio device all look like this from here.
    """
    state = speaking_agent(fc, state, agent="wayne", t=1.0)
    state, _ = fc.reduce(state, AgentAudioProgress(t=1.5, agent="wayne"))

    # Inside the stall budget: nothing happens.
    state, cmds = fc.reduce(state, Tick(t=1.5 + fc.config.agent_audio_stall_timeout_s - 0.01))
    assert not cmds
    assert state.speaking == "wayne"

    state, cmds = fc.reduce(state, Tick(t=1.5 + fc.config.agent_audio_stall_timeout_s))
    stops = [c for c in cmds if isinstance(c, StopSpeech)]
    assert [c.agent for c in stops] == ["wayne"]
    assert stops[0].reason is StopReason.STALLED
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [CueReason.AGENT_STALLED]
    assert state.speaking is None, "the floor must not stay pinned to a silent agent"
    assert state.floor_holder == HUMAN


def test_an_agent_that_never_produces_any_audio_gets_the_longer_budget(fc, state):
    """Two failures, two budgets.

    "TTS never delivered anything" is measured from the grant and gets
    `agent_first_audio_timeout_s`; "it delivered and then stopped" is measured
    from the last heartbeat and gets the much tighter stall budget. With no
    heartbeat at all, the tight one must not apply — a slow first byte is not
    a broken turn.
    """
    state = speaking_agent(fc, state, agent="wayne", t=1.0)
    started = state.agents["wayne"].speaking_since
    assert started is not None

    state, cmds = fc.reduce(state, Tick(t=started + fc.config.agent_audio_stall_timeout_s + 0.5))
    assert not cmds, "the stall budget must not apply before any audio has been heard"
    assert state.speaking == "wayne"

    state, cmds = fc.reduce(state, Tick(t=started + fc.config.agent_first_audio_timeout_s))
    assert [c.agent for c in cmds if isinstance(c, StopSpeech)] == ["wayne"]
    assert state.speaking is None


def test_a_stalled_turn_never_enters_the_transcript(fc, state):
    """What the room heard is unknown, so nothing is recorded.

    The words exist — they are in the runtime's `Candidate` — and writing them
    down is how the rest of the panel ends up answering a speech that never
    happened.
    """
    state = speaking_agent(fc, state, agent="wayne", t=1.0)
    before = state.transcript

    state, _ = run(fc, state, Tick(t=1.0 + fc.config.agent_first_audio_timeout_s + 1.0))
    assert state.transcript == before
    assert not [u for u in state.transcript if u.speaker == "wayne"]


def test_a_heartbeat_for_a_non_speaking_agent_is_ignored(fc, state):
    """Heartbeats come off the audio path and the floor moves underneath them.

    A late heartbeat for the previous speaker must not extend the *current*
    speaker's budget, which is what an unfiltered refresh would do.
    """
    state = speaking_agent(fc, state, agent="wayne", t=1.0)
    state, cmds = fc.reduce(state, AgentAudioProgress(t=1.5, agent="dex"))
    assert not cmds
    assert state.last_audio_progress_t is None, "only the speaker's audio counts"

    started = state.agents["wayne"].speaking_since
    assert started is not None
    state, cmds = fc.reduce(state, Tick(t=started + fc.config.agent_first_audio_timeout_s))
    assert [c.agent for c in cmds if isinstance(c, StopSpeech)] == ["wayne"], (
        "another agent's heartbeat kept a silent speaker on the floor"
    )


def test_the_watchdog_clock_does_not_carry_across_turns(fc, state):
    """Each turn arms its own budget.

    Wayne's healthy turn must not leave a fresh timestamp behind that gives
    Dexter's broken one a head start it did not earn.
    """
    state = speaking_agent(fc, state, agent="wayne", t=1.0)
    state, _ = run(
        fc,
        state,
        AgentAudioProgress(t=1.5, agent="wayne"),
        AgentSpeechEnded(t=2.0, agent="wayne", completed=True, utterance="Done."),
    )
    assert state.last_audio_progress_t is None

    state, _ = fc.reduce(state, AgentSpeechStarted(t=2.1, agent="dex"))
    assert state.last_audio_progress_t is None, "a new turn starts with no audio observed"
    assert state.agents["dex"].speaking_since == 2.1


def test_the_panel_carries_on_after_a_stall(fc, state):
    """Recovery is the point: the next question must behave normally.

    A stall is not a state the panel can be left in — Ricky re-asks, the floor
    opens, an agent answers. If any of the stall bookkeeping leaked, this is
    where it shows up.
    """
    state = speaking_agent(fc, state, agent="wayne", t=1.0)
    state, _ = fc.reduce(state, Tick(t=1.0 + fc.config.agent_first_audio_timeout_s + 1.0))
    assert state.speaking is None
    assert state.invitation is None
    assert state.proposals == {}
    assert state.agents["wayne"].state is AgentState.IDLE

    state, cmds = run(
        fc,
        state,
        invite(20.0, "Dexter, what does that do to the architecture?"),
        AgentProposal(t=20.1, input_t=20.0, agent="dex", utterance="", signals=strong()),
        TurnYielded(t=20.2),
    )
    granted = [c for c in cmds if isinstance(c, StartSpeech)]
    assert [c.agent for c in granted] == ["dex"], "the panel did not recover"


def test_a_stall_leaves_an_unfinished_introduction_round_restartable(fc, state):
    """Abandoned, not spent — the same rule a human interrupt follows.

    The round has not "been done", so the one-shot latch must stay open and
    the phrase must be able to restart it cleanly.
    """
    state = replace(state, intro_done=False)
    state, _ = fc.reduce(state, _introduce(0.0))
    first = state.intro_queue[0] if state.intro_queue else None
    assert first is not None
    state, _ = fc.reduce(state, AgentSpeechStarted(t=0.5, agent=first))

    state, _ = fc.reduce(state, Tick(t=0.5 + fc.config.agent_first_audio_timeout_s + 1.0))
    assert state.intro_queue is None
    assert not state.intro_done, "a stalled round must not latch as complete"

    state, _ = fc.reduce(state, _introduce(30.0))
    assert state.intro_queue is not None
    assert set(state.intro_queue) == set(fc.cast.ids())


# ------------------------------------------------- speculation during a turn
#
# Added 25 Sept 2026. The failure these pin down was measured on a live run:
# Melia finished a 32s turn at 12:59:57.233 and the panel then sat silent for
# 2.43s while a proposal was generated from cold, even though Dexter and Wayne
# had both had finished lines in hand since 12:59:26. Two causes, and the
# second only bites because of the first:
#
#   1. Nothing asked for a proposal between `AgentSpeechStarted` and
#      `AgentSpeechEnded`. Every `_ask_for_proposals` site needed a *human*
#      event, and agent speech never produces one — it never goes near STT.
#   2. `_agent_ended` keeps only proposals whose input is newer than
#      `speaking_since`. The round that opens the floor is always older than
#      the turn it opens (the classifier hold, arbitration and TTS first audio
#      sit in between — 632ms on that run), so with (1) in place the filter
#      could only ever return empty.


def _mid_turn(fc, state, speaker="melia", t=1.0):
    """`speaking_agent`, but with the two idle agents holding nothing."""
    return speaking_agent(fc, state, agent=speaker, t=t)


def test_a_spoken_sentence_asks_the_other_agents_for_a_line(fc, state):
    """The fix for (1). A turn now generates its own successor."""
    state = _mid_turn(fc, state)
    state, cmds = fc.reduce(
        state,
        AgentUtteranceProgress(
            t=1.1 + fc.config.agent_turn_speculation_interval_s,
            agent="melia",
            text="Reported adoption and actual adoption are different curves.",
        ),
    )
    requests = [c for c in cmds if isinstance(c, RequestProposals)]
    assert len(requests) == 1
    assert requests[0].reason == "agent_turn"
    # Never the speaker: asking Melia mid-turn is asking her to follow herself.
    assert set(requests[0].agents) == {"dex", "wayne"}


def test_mid_turn_rounds_are_debounced_on_their_own_dial(fc, state):
    """Sentences arrive faster than a generation completes (2.0-2.9s to
    `SignalsReady`), so asking per sentence buys concurrency, not freshness.
    The interval is separate from the human one because the turns are not the
    same length — see `FloorConfig.agent_turn_speculation_interval_s`."""
    state = _mid_turn(fc, state)
    interval = fc.config.agent_turn_speculation_interval_s
    state, first = fc.reduce(
        state,
        AgentUtteranceProgress(t=1.1 + interval, agent="melia", text="One two three four five."),
    )
    state, second = fc.reduce(
        state,
        AgentUtteranceProgress(t=1.2 + interval, agent="melia", text="Six seven eight nine ten."),
    )
    state, third = fc.reduce(
        state,
        # +0.5 rather than exactly 2x: (1.1+5.0)-(1.1+2.5) is 2.4999999999999996
        # in binary floating point, and a debounce test that turns on the last
        # bit of a float is testing the wrong thing.
        AgentUtteranceProgress(t=1.6 + 2 * interval, agent="melia", text="Eleven twelve thirteen."),
    )
    assert [isinstance(c, RequestProposals) for c in first] == [True]
    assert second == []
    assert [isinstance(c, RequestProposals) for c in third] == [True]


def test_a_scrap_of_a_turn_is_not_worth_asking_against(fc, state):
    """Same reasoning as `speculation_min_words` on the human path: an agent
    opening with "Mm." is not a question anybody can answer, and a line
    written against one is a holding line."""
    state = _mid_turn(fc, state)
    state, cmds = fc.reduce(
        state,
        AgentUtteranceProgress(
            t=1.1 + fc.config.agent_turn_speculation_interval_s, agent="melia", text="Mm."
        ),
    )
    assert cmds == []
    assert state.agent_partial == "Mm."


def test_a_sentence_from_an_agent_who_is_not_speaking_is_ignored(fc, state):
    """A cancelled turn's `speak()` task can still be unwinding. Folding its
    sentences in would attribute the wrong words to the wrong agent in every
    prompt built for the rest of the exchange."""
    state = _mid_turn(fc, state)
    state, cmds = fc.reduce(
        state,
        AgentUtteranceProgress(t=9.0, agent="wayne", text="A line from a turn that is over."),
    )
    assert cmds == []
    assert state.agent_partial == ""


def test_the_running_text_is_cleared_at_both_ends_of_a_turn(fc, state):
    """Either end alone leaves a window in which the previous speaker's words
    are attributed to the current one."""
    state = _mid_turn(fc, state)
    state, _ = fc.reduce(
        state, AgentUtteranceProgress(t=2.0, agent="melia", text="The gap has been measured.")
    )
    assert state.agent_partial == "The gap has been measured."
    assert "melia (speaking): The gap has been measured." in state.recent_text()

    state, _ = fc.reduce(
        state, AgentSpeechEnded(t=9.0, agent="melia", completed=True, utterance="The gap.")
    )
    assert state.agent_partial == ""
    # ...and the record took over from the partial, without duplicating it.
    assert state.transcript[-1].text == "The gap."
    assert "(speaking)" not in state.recent_text()


def test_the_introduction_round_never_speculates_mid_turn(fc, state):
    """Every line in that round is fixed text the model never sees, so a
    speculative candidate is a 2-4s round trip nobody reads. Same carve-out
    the human path already has."""
    state = replace(state, intro_done=False)
    state, _ = run(fc, state, invite(0.0, "Right, let's do quick introductions."))
    assert state.intro_queue is not None
    state, _ = run(fc, state, AgentSpeechStarted(t=1.0, agent=state.speaking or "dex"))
    state, cmds = fc.reduce(
        state,
        AgentUtteranceProgress(
            t=1.0 + fc.config.agent_turn_speculation_interval_s,
            agent=state.speaking,
            text="I'll go first, I'm Dexter, and I run inference infrastructure.",
        ),
    )
    assert [c for c in cmds if isinstance(c, RequestProposals)] == []


def test_a_mid_turn_proposal_survives_the_boundary_and_is_granted(fc, state):
    """The regression this whole change exists for.

    A proposal written *during* the turn passes `proposals_written_since`, so
    `_agent_ended` arbitrates it immediately instead of asking from cold. No
    `RequestProposals` at the boundary at all — that request is the 2.43s of
    dead air.
    """
    state = _mid_turn(fc, state, speaker="melia", t=1.0)
    state, cmds = fc.reduce(
        state,
        AgentUtteranceProgress(
            t=1.1 + fc.config.agent_turn_speculation_interval_s,
            agent="melia",
            text="Reported adoption and actual adoption are different curves.",
        ),
    )
    request = next(c for c in cmds if isinstance(c, RequestProposals))
    assert request.reason == "agent_turn"
    written_against = state.last_proposal_request_t
    assert written_against > state.agents["melia"].speaking_since

    state, _ = fc.reduce(
        state,
        AgentProposal(
            t=written_against + 2.4,
            agent="dex",
            utterance="The cost curve is the part nobody argues with.",
            signals=strong(),
            epoch=state.speculation_epoch,
            input_t=written_against,
        ),
    )
    state, cmds = fc.reduce(
        state, AgentSpeechEnded(t=30.0, agent="melia", completed=True, utterance="…")
    )
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["dex"]
    assert [c for c in cmds if isinstance(c, RequestProposals)] == []


def test_a_proposal_older_than_the_granted_line_does_not_survive_the_boundary(fc, state):
    """The other half, and the reason the filter is not simply removed: a line
    written against an *earlier* moment than the one that just aired is an
    answer to a question the speaker has since spent half a minute answering.
    It goes, and the boundary asks for a fresh one — the old behaviour, now
    correctly the exception rather than every handover.

    Dexter's line here is not stale by `open_proposal_lookback_s` (1.5s < 2.0s
    against the invitation), so this pins the cutoff itself and not the
    freshness test standing in for it.
    """
    state, cmds = run(
        fc,
        state,
        invite(0.0),
        AgentProposal(
            t=0.5,
            agent="dex",
            utterance="Written against the preamble.",
            signals=weak(),
            epoch=1,
            input_t=-1.5,
        ),
        AgentProposal(
            t=0.6,
            agent="melia",
            utterance="Written against the question.",
            signals=strong(),
            epoch=1,
            input_t=0.0,
        ),
        TurnYielded(t=1.0),
    )
    # `_grant` only emits `StartSpeech`; `floor_holder` is not set until the
    # runtime reports the audio actually started.
    granted = next(c for c in cmds if isinstance(c, StartSpeech))
    assert granted.agent == "melia"
    state, _ = run(fc, state, AgentSpeechStarted(t=1.6, agent="melia"))
    assert state.turn_input_t == 0.0
    state, cmds = fc.reduce(
        state, AgentSpeechEnded(t=30.0, agent="melia", completed=True, utterance="…")
    )
    assert [c for c in cmds if isinstance(c, StartSpeech)] == []
    assert [c.reason for c in cmds if isinstance(c, RequestProposals)] == ["agent_turn_ended"]


# ------------------------------------------- the handover gap, second pass
#
# Added 1 Oct 2026, after three consecutive handovers on a live run each paid
# the full cold generation the section above exists to remove. Both causes are
# clock bugs, and neither is visible from the reducer alone — they only appear
# once you line the floor's timestamps up against where the runtime actually
# emits `AgentUtteranceProgress` (at the TTS push, i.e. at generation pace):
#
#   1. The mid-turn debounce measured from `last_proposal_request_t`, which
#      the boundary round sets 0.1-0.7s *before* the grant. The turn's whole
#      text is pushed within ~1.5s of the grant, so every sentence in the turn
#      fell inside one 2.5s window and no turn opened a round at all.
#   2. `_agent_ended` cut proposals at `speaking_since`, so the other two
#      agents' lines from the round that produced the grant — written 110ms
#      before it on the measured run — were discarded as leftovers.


def test_the_first_sentence_of_a_turn_always_asks(fc, state):
    """Cause (1). The round that granted the turn cannot hold off the round
    that follows it: different clocks, and this one starts at the turn."""
    state = _mid_turn(fc, state, speaker="melia", t=1.0)
    started = state.agents["melia"].speaking_since
    # Where every boundary round lands, measured: just before the grant.
    state = replace(state, last_proposal_request_t=started - 0.1)
    assert state.agent_turn_request_t is None

    state, cmds = fc.reduce(
        state,
        AgentUtteranceProgress(
            t=started + 0.4,
            agent="melia",
            text="Reported adoption and actual adoption are different curves.",
        ),
    )
    assert [c.reason for c in cmds if isinstance(c, RequestProposals)] == ["agent_turn"]
    assert state.agent_turn_request_t == started + 0.4

    # ...and the debounce still holds for the rest of the burst, which is the
    # half of the old behaviour that was right.
    state, cmds = fc.reduce(
        state,
        AgentUtteranceProgress(
            t=started + 0.9, agent="melia", text="The gap between them is the whole story."
        ),
    )
    assert [c for c in cmds if isinstance(c, RequestProposals)] == []


def test_the_mid_turn_clock_is_reset_by_the_next_turn(fc, state):
    """The clock is per-turn or it is the bug it replaces: a round opened late
    in one turn must not swallow the first round of the next."""
    state = _mid_turn(fc, state, speaker="melia", t=1.0)
    state, _ = fc.reduce(
        state,
        AgentUtteranceProgress(t=2.0, agent="melia", text="One two three four five six."),
    )
    assert state.agent_turn_request_t == 2.0
    state, _ = fc.reduce(
        state, AgentSpeechEnded(t=30.0, agent="melia", completed=True, utterance="…")
    )
    assert state.agent_turn_request_t is None

    state, _ = fc.reduce(state, AgentSpeechStarted(t=31.0, agent="dex"))
    state, cmds = fc.reduce(
        state,
        # 0.4s into the new turn, and well inside the interval measured from
        # the previous turn's round.
        AgentUtteranceProgress(t=31.4, agent="dex", text="Six seven eight nine ten eleven."),
    )
    assert [c.reason for c in cmds if isinstance(c, RequestProposals)] == ["agent_turn"]


def test_a_sibling_of_the_granted_line_survives_the_boundary(fc, state):
    """Cause (2). One round produces all three lines; one of them is spoken.
    The other two answer the same moment as the line the room just heard, so
    they are the floor under the handover when no mid-turn round beat them to
    it — not leftovers from a moment that has passed."""
    state, cmds = run(
        fc,
        state,
        invite(0.0),
        AgentProposal(
            t=2.0,
            agent="dex",
            utterance="Mine is the fleet audit.",
            signals=strong(),
            epoch=1,
            input_t=0.0,
        ),
        AgentProposal(
            t=2.1,
            agent="melia",
            utterance="Mine is the cladding.",
            signals=strong(relevance=0.8),
            epoch=1,
            input_t=0.0,
        ),
        TurnYielded(t=2.2),
    )
    granted = next(c for c in cmds if isinstance(c, StartSpeech))
    assert granted.agent == "dex"
    # The grant lands after the round that wrote it — which is the whole
    # reason `speaking_since` was the wrong cutoff.
    state, _ = run(fc, state, AgentSpeechStarted(t=2.9, agent="dex"))
    assert state.turn_input_t == 0.0
    assert state.turn_input_t < state.agents["dex"].speaking_since

    # No mid-turn round completes: the turn is 30s of audio written in one
    # burst, so there is nothing fresher than Melia's sibling line.
    state, cmds = fc.reduce(
        state, AgentSpeechEnded(t=32.0, agent="dex", completed=True, utterance="…")
    )
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["melia"]
    assert [c for c in cmds if isinstance(c, RequestProposals)] == []


def test_a_mid_turn_proposal_still_beats_its_own_sibling(fc, state):
    """The two fixes in one turn, in the order of preference they encode: the
    sibling is kept, and then superseded by a line written against what the
    speaker actually said. `_proposal`'s newest-epoch rule does the choosing;
    all `_agent_ended` has to do is not throw the fallback away first."""
    state, cmds = run(
        fc,
        state,
        invite(0.0),
        AgentProposal(
            t=2.0,
            agent="dex",
            utterance="Mine is the fleet audit.",
            signals=strong(),
            epoch=1,
            input_t=0.0,
        ),
        AgentProposal(
            t=2.1,
            agent="melia",
            utterance="Written before the turn.",
            signals=strong(relevance=0.8),
            epoch=1,
            input_t=0.0,
        ),
        TurnYielded(t=2.2),
        AgentSpeechStarted(t=2.9, agent="dex"),
    )
    assert next(c for c in cmds if isinstance(c, StartSpeech)).agent == "dex"

    state, cmds = fc.reduce(
        state,
        AgentUtteranceProgress(
            t=3.3, agent="dex", text="Nine hundred agents, approved on thirty simulated calls."
        ),
    )
    assert [c.reason for c in cmds if isinstance(c, RequestProposals)] == ["agent_turn"]
    written_against = state.last_proposal_request_t
    state, _ = fc.reduce(
        state,
        AgentProposal(
            t=5.4,
            agent="melia",
            utterance="Written against the turn.",
            signals=strong(relevance=0.8),
            epoch=state.speculation_epoch,
            input_t=written_against,
        ),
    )

    state, cmds = fc.reduce(
        state, AgentSpeechEnded(t=32.0, agent="dex", completed=True, utterance="…")
    )
    start = next(c for c in cmds if isinstance(c, StartSpeech))
    assert start.agent == "melia"
    assert start.utterance == "Written against the turn."


# ------------------------------------------------- agents inviting agents


@pytest.fixture
def agent_fc(cast: PanelCast) -> FloorController:
    """Explicit `agent_invitations=True`, same as the default — kept separate
    from `fc` so these tests still read the flag they depend on."""
    return FloorController(cast, FloorConfig(agent_invitations=True))


def _turn_with_invite(
    fc: FloorController,
    state: PanelState,
    *,
    speaker: str = "dex",
    invites: str | None = "wayne",
    invited_at: float = 0.0,
    started: float = 1.0,
):
    """Ricky opens the floor; `speaker` wins it with a line naming `invites`."""
    state, cmds = run(
        fc,
        state,
        invite(invited_at, "Where are we on the adoption curve?"),
        AgentProposal(
            t=invited_at + 0.1,
            input_t=invited_at,
            agent=speaker,
            utterance="Unevenly.",
            signals=strong(invites_next=invites),
        ),
        TurnYielded(t=started),
        AgentSpeechStarted(t=started + 0.1, agent=speaker),
    )
    assert state.speaking == speaker
    return state, cmds


def _mid_turn_line(agent: str, *, t: float = 5.0, invites: str | None = None) -> AgentProposal:
    return AgentProposal(
        t=t,
        input_t=t - 1.0,
        agent=agent,
        utterance=f"{agent} has one ready.",
        signals=strong(invites_next=invites),
    )


def test_an_invited_colleague_takes_the_floor_when_the_turn_ends(agent_fc, state):
    """The handoff the feature is for: no cold generation at the boundary."""
    state, _ = _turn_with_invite(agent_fc, state)
    assert state.pending_invite == "wayne"

    state, _ = agent_fc.reduce(state, _mid_turn_line("wayne", t=5.0))
    state, cmds = agent_fc.reduce(
        state, AgentSpeechEnded(t=31.0, agent="dex", completed=True, utterance="Said.")
    )

    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["wayne"]
    assert not [c for c in cmds if isinstance(c, RequestProposals)], (
        "the invited agent already had a line; asking for another is the gap"
    )
    assert state.invitation is not None
    assert state.invitation.source is InvitationSource.AGENT
    assert state.pending_invite is None


def test_a_mid_turn_line_is_fresh_enough_for_a_peer_handoff(agent_fc, state):
    """The freshness exemption. A handoff is decided at the *end* of a turn.

    Wayne's line was written three seconds into a thirty-second turn, so it is
    twenty-seven seconds older than the invitation it wins — far outside
    `named_proposal_lookback_s`. Measuring it against that window would refuse
    the whole mid-turn speculation round and put a cold generation on every
    handover, which is the thing the round exists to remove.
    """
    state, _ = _turn_with_invite(agent_fc, state)
    state, _ = agent_fc.reduce(state, _mid_turn_line("wayne", t=4.0))
    assert agent_fc.config.named_proposal_lookback_s < 27.0

    state, cmds = agent_fc.reduce(
        state, AgentSpeechEnded(t=31.0, agent="dex", completed=True, utterance="Said.")
    )
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["wayne"]


@pytest.mark.parametrize("target", ["dex", "nobody", ""])
def test_an_unusable_invite_is_dropped_at_the_grant(agent_fc, state, target):
    """Itself, a name this cast has nobody for, or nothing at all."""
    state, _ = _turn_with_invite(agent_fc, state, invites=target)
    assert state.pending_invite is None

    state, _ = agent_fc.reduce(
        state, AgentSpeechEnded(t=31.0, agent="dex", completed=True, utterance="Said.")
    )
    assert state.invitation is not None
    assert state.invitation.source is InvitationSource.OPEN


def test_an_invite_to_a_muted_agent_is_dropped_at_the_grant(agent_fc, state):
    state, _ = agent_fc.reduce(
        state, OperatorCommand(t=-0.5, action=OperatorAction.MUTE_AGENT, agent="wayne")
    )
    state, _ = _turn_with_invite(agent_fc, state)
    assert state.pending_invite is None


def test_an_agent_muted_mid_turn_does_not_get_the_handoff(agent_fc, state):
    """Validated again at turn end, not only when the invite was recorded."""
    state, _ = _turn_with_invite(agent_fc, state)
    assert state.pending_invite == "wayne"

    state = state.with_agent("wayne", muted=True, state=AgentState.MUTED)
    state, cmds = agent_fc.reduce(
        state, AgentSpeechEnded(t=31.0, agent="dex", completed=True, utterance="Said.")
    )
    assert state.pending_invite is None
    assert state.invitation is not None
    assert state.invitation.source is not InvitationSource.AGENT
    assert not [c for c in cmds if isinstance(c, StartSpeech)]


def test_the_operator_muting_the_target_clears_the_pending_invite(agent_fc, state):
    state, _ = _turn_with_invite(agent_fc, state)
    state, _ = agent_fc.reduce(
        state, OperatorCommand(t=5.0, action=OperatorAction.MUTE_AGENT, agent="wayne")
    )
    assert state.pending_invite is None


def test_kill_all_clears_the_pending_invite(agent_fc, state):
    state, _ = _turn_with_invite(agent_fc, state)
    state, _ = agent_fc.reduce(state, OperatorCommand(t=5.0, action=OperatorAction.KILL_ALL))
    assert state.pending_invite is None


def test_ricky_speaking_mid_turn_cancels_the_handoff(agent_fc, state):
    """VAD alone. The floor is his the moment he starts talking."""
    state, _ = _turn_with_invite(agent_fc, state)
    state, _ = agent_fc.reduce(state, HumanSpeechStarted(t=6.0))
    assert state.pending_invite is None

    state, _ = agent_fc.reduce(
        state, AgentSpeechEnded(t=31.0, agent="dex", completed=True, utterance="Said.")
    )
    assert state.invitation is None or state.invitation.source is not InvitationSource.AGENT


def test_a_committed_interrupt_cancels_the_handoff(agent_fc, state):
    """Substantive words from Ricky over the turn — `_commit_human_interrupt`."""
    state, _ = _turn_with_invite(agent_fc, state)
    state, cmds = agent_fc.reduce(
        state,
        TranscriptUpdated(
            t=6.0, speaker=HUMAN, text="Hold on, let me stop you there.", is_final=True
        ),
    )
    assert [c for c in cmds if isinstance(c, StopSpeech)]
    assert state.pending_invite is None
    assert state.invitation is None

    state, _ = agent_fc.reduce(
        state, AgentSpeechEnded(t=6.2, agent="dex", completed=False, utterance="Un…")
    )
    assert state.invitation is None or state.invitation.source is not InvitationSource.AGENT


def test_a_stalled_speaker_clears_the_pending_invite(agent_fc, state):
    state, _ = _turn_with_invite(agent_fc, state)
    state, cmds = agent_fc.reduce(
        state, Tick(t=1.1 + agent_fc.config.agent_first_audio_timeout_s + 0.1)
    )
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [CueReason.AGENT_STALLED]
    assert state.pending_invite is None


@pytest.mark.parametrize("text", ["Where does that leave us?", "Wayne, what do you make of that?"])
def test_ricky_supersedes_a_standing_peer_invitation(agent_fc, state, text):
    """Lowest precedence of all: an open floor from Ricky displaces it too."""
    state, _ = _turn_with_invite(agent_fc, state, invites="melia")
    state, _ = agent_fc.reduce(state, _mid_turn_line("melia", t=5.0))
    state, _ = agent_fc.reduce(
        state, AgentSpeechEnded(t=31.0, agent="dex", completed=True, utterance="Said.")
    )
    assert state.invitation.source is InvitationSource.AGENT

    state, _ = agent_fc.reduce(
        state, TranscriptUpdated(t=31.5, speaker=HUMAN, text=text, is_final=True)
    )
    assert state.invitation.source in (InvitationSource.OPEN, InvitationSource.ADDRESS)


def test_at_the_turn_cap_the_invite_is_dropped_and_ricky_gets_the_floor(agent_fc, state):
    """The `AGENT_TURN_LIMIT` hand-back must not be left holding an invite."""
    state = replace(state, consecutive_agent_turns=agent_fc.config.max_consecutive_agent_turns - 1)
    state, _ = _turn_with_invite(agent_fc, state)
    assert state.consecutive_agent_turns == agent_fc.config.max_consecutive_agent_turns
    state, _ = agent_fc.reduce(state, _mid_turn_line("wayne", t=5.0))

    state, cmds = agent_fc.reduce(
        state, AgentSpeechEnded(t=31.0, agent="dex", completed=True, utterance="Said.")
    )
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [CueReason.AGENT_TURN_LIMIT]
    assert not [c for c in cmds if isinstance(c, StartSpeech)]
    assert state.invitation is None
    assert state.pending_invite is None


def test_a_ping_pong_of_invitations_terminates_at_the_cap(agent_fc, state):
    """Two agents handing the floor back and forth are still bounded."""
    state, cmds = _turn_with_invite(agent_fc, state)
    granted = ["dex"]
    cues: list[CueReason] = []
    t = 1.0
    while True:
        speaker = granted[-1]
        other = "wayne" if speaker == "dex" else "dex"
        state, _ = agent_fc.reduce(state, _mid_turn_line(other, t=t + 5.0, invites=speaker))
        state, cmds = agent_fc.reduce(
            state, AgentSpeechEnded(t=t + 30.0, agent=speaker, completed=True, utterance="Said.")
        )
        cues.extend(c.reason for c in cmds if isinstance(c, CueModerator))
        starts = [c.agent for c in cmds if isinstance(c, StartSpeech)]
        if not starts:
            break
        assert len(starts) == 1
        granted.append(starts[0])
        t += 40.0
        state, _ = agent_fc.reduce(state, AgentSpeechStarted(t=t, agent=starts[0]))

    assert granted == ["dex", "wayne", "dex", "wayne", "dex"]
    assert len(granted) == agent_fc.config.max_consecutive_agent_turns
    assert cues[-1] is CueReason.AGENT_TURN_LIMIT
    assert state.invitation is None


def test_an_unanswered_peer_invitation_falls_open_without_cueing_ricky(agent_fc, state):
    """Ricky did not ask this question and must not be told to fill the gap."""
    state, _ = _turn_with_invite(agent_fc, state)
    state, cmds = agent_fc.reduce(
        state, AgentSpeechEnded(t=31.0, agent="dex", completed=True, utterance="Said.")
    )
    assert not [c for c in cmds if isinstance(c, StartSpeech)], "Wayne had nothing ready"
    assert not [c for c in cmds if isinstance(c, CueModerator)]
    assert state.invitation.source is InvitationSource.AGENT

    # The runtime re-drives arbitration when a proposal lands; Wayne still has
    # nothing and somebody else does.
    state, _ = agent_fc.reduce(state, _mid_turn_line("melia", t=32.0))
    state, cmds = agent_fc.reduce(state, TurnYielded(t=32.5))

    assert not [c for c in cmds if isinstance(c, CueModerator)]
    assert state.moderator_cued is False
    assert state.awaiting_agents == ()
    assert state.invitation is not None
    assert state.invitation.source is InvitationSource.OPEN
    assert state.invitation.agents == ()


def test_the_introduction_round_ignores_a_pending_invite(agent_fc, state):
    """Fixed text invites nobody, and nothing may cut across the round."""
    state = replace(state, intro_done=False)
    state, cmds = agent_fc.reduce(state, _introduce(0.0))
    first = next(c for c in cmds if isinstance(c, StartSpeech)).agent
    state, _ = agent_fc.reduce(state, AgentSpeechStarted(t=1.0, agent=first))

    # Nothing in the reducer can set this during a round — `_grant_introduction`
    # clears it at every grant — so it is planted here to prove the round is
    # guarded rather than merely never reached.
    state = replace(state, pending_invite="wayne")
    state, cmds = agent_fc.reduce(
        state, AgentSpeechEnded(t=2.0, agent=first, completed=True, utterance="Hello.")
    )

    assert state.pending_invite is None
    assert state.invitation is not None
    assert state.invitation.source is InvitationSource.INTRODUCTION
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] != [], "the round continues"


def test_a_log_without_the_new_fields_replays_identically(fc, state):
    """Both defaults hold, so an older recording reduces to the same state."""
    assert Signals(relevance=0.9).invites_next is None
    assert PanelState.for_agents(("dex",)).pending_invite is None

    old = Signals(relevance=0.9, urgency=0.5, disagreement=0.3, confidence=0.9, expertise=0.6)
    new = replace(old, invites_next=None)
    events = lambda signals: (
        invite(0.0),
        AgentProposal(t=0.1, input_t=0.0, agent="dex", utterance="Unevenly.", signals=signals),
        TurnYielded(t=1.0),
        AgentSpeechStarted(t=1.1, agent="dex"),
        AgentSpeechEnded(t=31.0, agent="dex", completed=True, utterance="Said."),
    )
    before, before_cmds = run(fc, state, *events(old))
    after, after_cmds = run(fc, state, *events(new))
    assert before == after
    assert before_cmds == after_cmds


def test_agent_invitations_are_on_by_default(cast: PanelCast, state):
    """A plain `FloorConfig()` is the default — the feature just works."""
    assert FloorConfig().agent_invitations is True
    default_fc = FloorController(cast, FloorConfig())
    state, _ = _turn_with_invite(default_fc, state)

    state, _ = default_fc.reduce(state, _mid_turn_line("wayne", t=5.0))
    state, cmds = default_fc.reduce(
        state, AgentSpeechEnded(t=31.0, agent="dex", completed=True, utterance="Said.")
    )
    start = next(c for c in cmds if isinstance(c, StartSpeech))
    assert start.agent == "wayne"


def test_agent_invitations_can_be_disabled(cast: PanelCast, state):
    """The flag ships as a kill switch: the field is read and stored, never acted on."""
    off_fc = FloorController(cast, FloorConfig(agent_invitations=False))
    state, _ = _turn_with_invite(off_fc, state)
    assert state.pending_invite == "wayne"

    state, _ = off_fc.reduce(state, _mid_turn_line("wayne", t=5.0))
    state, _ = off_fc.reduce(
        state, AgentSpeechEnded(t=31.0, agent="dex", completed=True, utterance="Said.")
    )
    assert state.pending_invite is None
    assert state.invitation is not None
    assert state.invitation.source is InvitationSource.OPEN
