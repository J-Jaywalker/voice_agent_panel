"""Floor-control tests.

These are the scoping doc's acceptance criteria, expressed as assertions.
Because the core is pure, each of these runs in microseconds and cannot flake.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from panel_core import (
    HUMAN,
    AgentProposal,
    AgentSpeechEnded,
    AgentSpeechStarted,
    CueModerator,
    DuckSpeech,
    FloorConfig,
    FloorController,
    HandsRaised,
    HumanSpeechEnded,
    HumanSpeechStarted,
    InjectDirective,
    OperatorAction,
    OperatorCommand,
    PanelCast,
    PanelState,
    RequestProposals,
    ResumeSpeech,
    Signals,
    StartSpeech,
    StopReason,
    StopSpeech,
    Tick,
    TranscriptUpdated,
    TurnYielded,
)

# `CueReason` is the floor's own vocabulary and is not re-exported from the
# package root, which `panel_core` reserves for the event/command surface.
# The phrasing corpus that exercises `AddressRole` lives in test_address.py.
from panel_core.floor import CueReason

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"


@pytest.fixture
def cast() -> PanelCast:
    return PanelCast.from_dir(PERSONA_DIR)


@pytest.fixture
def fc(cast: PanelCast) -> FloorController:
    return FloorController(cast, FloorConfig())


@pytest.fixture
def state(cast: PanelCast) -> PanelState:
    return PanelState.for_agents(cast.ids())


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


def test_human_speech_ducks_the_agent_within_one_buffer(fc, state):
    """The reflex. We cannot know yet whether this is a barge-in — duck anyway.

    Ducking rather than stopping is what lets us be fast AND correct: the
    transcript is ~300ms behind, so waiting to classify would cost
    responsiveness, and assuming "interrupt" would stutter on every "mm-hm".
    """
    state = speaking_agent(fc, state)
    state, cmds = fc.reduce(state, HumanSpeechStarted(t=2.0))

    ducks = [c for c in cmds if isinstance(c, DuckSpeech)]
    assert len(ducks) == 1
    assert ducks[0].agent == "wayne"
    assert ducks[0].gain_db < 0
    assert not [c for c in cmds if isinstance(c, StopSpeech)], "classification has not run yet"
    assert state.speaking == "wayne", "the agent is quieter, not stopped"
    assert state.ducked_agent == "wayne"


def test_backchannel_resumes_the_agent(fc, state):
    """Ricky says 'mm-hm'. The agent must not stop."""
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))
    state, _ = fc.reduce(
        state, TranscriptUpdated(t=2.2, speaker=HUMAN, text="mm-hm", is_final=False)
    )
    state, cmds = fc.reduce(state, HumanSpeechEnded(t=2.3))

    assert [c for c in cmds if isinstance(c, ResumeSpeech)]
    assert not [c for c in cmds if isinstance(c, StopSpeech)]
    assert state.speaking == "wayne", "agent keeps the floor through a backchannel"
    assert state.ducked_agent is None


def test_sustained_speech_commits_an_interrupt_on_duration(fc, state):
    """Long enough is a bid for the floor, whatever the words turn out to be."""
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))
    state, cmds = fc.reduce(state, Tick(t=2.0 + fc.config.backchannel_max_duration_s + 0.01))

    stops = [c for c in cmds if isinstance(c, StopSpeech)]
    assert stops and stops[0].reason is StopReason.HUMAN_INTERRUPT
    assert stops[0].overlap_ms == 0, "must never talk over the moderator"
    assert state.speaking is None
    assert state.floor_holder == HUMAN


def test_substantive_words_commit_an_interrupt_even_when_brief(fc, state):
    """'Sorry Dex, let Wayne finish' is short but is not an acknowledgement."""
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))
    state, cmds = fc.reduce(
        state,
        TranscriptUpdated(
            t=2.15, speaker=HUMAN, text="sorry, could you let Wayne finish", is_final=False
        ),
    )
    stops = [c for c in cmds if isinstance(c, StopSpeech)]
    assert stops and stops[0].reason is StopReason.HUMAN_INTERRUPT
    assert state.speaking is None
    assert state.floor_holder == HUMAN


def test_late_substantive_transcript_still_interrupts_after_resume(fc, state):
    """Safety net for the race: short burst resumes, then the words arrive."""
    state = speaking_agent(fc, state)
    state, _ = fc.reduce(state, HumanSpeechStarted(t=2.0))
    state, cmds = fc.reduce(state, HumanSpeechEnded(t=2.2))
    assert [c for c in cmds if isinstance(c, ResumeSpeech)], "resumed on duration alone"

    state, cmds = fc.reduce(
        state, TranscriptUpdated(t=2.5, speaker=HUMAN, text="no, that is wrong", is_final=True)
    )
    assert [c for c in cmds if isinstance(c, StopSpeech)], "content overrides the resume"
    assert state.speaking is None


def test_human_speech_with_no_agent_speaking_just_takes_the_floor(fc, state):
    state, cmds = fc.reduce(state, HumanSpeechStarted(t=1.0))
    assert not [c for c in cmds if isinstance(c, (DuckSpeech, StopSpeech))]
    assert state.floor_holder == HUMAN


def test_human_turn_invalidates_speculative_proposals(fc, state):
    state, _ = run(
        fc, state, AgentProposal(t=0.0, agent="dex", utterance="Historically...", signals=strong())
    )
    assert state.proposals
    state, _ = fc.reduce(state, HumanSpeechStarted(t=0.5))
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
    assert not state.invitation.is_live(), "invitation is spent by the grant"


def test_a_named_agent_answers_alone(fc, state):
    """Asking Wayne is not asking the panel. Dex does not get to answer for him."""
    state, _ = run(
        fc,
        state,
        invite(0.0, "Wayne, does oversight actually scale?"),
        AgentProposal(t=0.5, agent="dex", utterance="Historically...", signals=strong()),
    )
    _, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert not [c for c in cmds if isinstance(c, StartSpeech)]
    assert [c for c in cmds if isinstance(c, CueModerator)]


def test_an_unnamed_question_opens_the_floor_to_the_panel(fc, state):
    state, _ = fc.reduce(state, invite(0.0, "What do you all think?"))
    assert state.invitation is not None
    assert state.invitation.agent is None, "open to the room, not to one agent"


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


# --------------------------------------------------------------- interrupting


def test_strong_disagreement_interrupts_a_speaking_agent(fc, state):
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
            t=5.0,  # past the interrupt grace window
            agent="dex",
            utterance="Sorry, I've got to disagree there.",
            signals=strong(disagreement=0.96, urgency=0.9),
        ),
    )
    stops = [c for c in cmds if isinstance(c, StopSpeech)]
    starts = [c for c in cmds if isinstance(c, StartSpeech)]
    assert stops[0].agent == "wayne"
    assert stops[0].reason is StopReason.AGENT_INTERRUPT
    assert stops[0].overlap_ms > 0, "brief overlap is what reads as a real argument"
    assert starts[0].agent == "dex"


def test_stale_end_of_an_interrupted_turn_does_not_disturb_the_challenger(fc, state):
    """The interrupted agent's own AgentSpeechEnded arrives after the
    challenger already has the floor. It must not re-trigger continuation
    logic (extra RequestProposals, popping an introduction queue, etc.) —
    the challenger, not the arbitrator, owns what happens next."""
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
            t=5.0,
            agent="dex",
            utterance="Sorry, I've got to disagree there.",
            signals=strong(disagreement=0.96, urgency=0.9),
        ),
    )
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["dex"]
    state, _ = fc.reduce(state, AgentSpeechStarted(t=5.05, agent="dex"))
    assert state.speaking == "dex"

    state, cmds = fc.reduce(
        state,
        AgentSpeechEnded(t=5.1, agent="wayne", completed=False, utterance="Completely..."),
    )
    assert state.speaking == "dex", "the challenger keeps the floor"
    assert not [c for c in cmds if isinstance(c, RequestProposals)]
    assert not [c for c in cmds if isinstance(c, CueModerator)]


def test_no_interrupt_inside_the_grace_window(fc, state):
    """Cutting in half a second into a turn just looks broken."""
    state, _ = run(
        fc,
        state,
        invite(0.0),
        AgentProposal(t=0.0, agent="wayne", utterance="Look —", signals=strong()),
        TurnYielded(t=1.0),
        AgentSpeechStarted(t=1.1, agent="wayne"),
    )
    _, cmds = fc.reduce(
        state,
        AgentProposal(
            t=1.5, agent="dex", utterance="No.", signals=strong(disagreement=1.0, urgency=1.0)
        ),
    )
    assert not [c for c in cmds if isinstance(c, StopSpeech)]


def test_mild_disagreement_does_not_interrupt(fc, state):
    state, _ = run(
        fc,
        state,
        invite(0.0),
        AgentProposal(t=0.0, agent="wayne", utterance="Look —", signals=strong()),
        TurnYielded(t=1.0),
        AgentSpeechStarted(t=1.1, agent="wayne"),
    )
    _, cmds = fc.reduce(
        state,
        AgentProposal(
            t=6.0, agent="melia", utterance="Mm.", signals=strong(disagreement=0.4, urgency=0.4)
        ),
    )
    assert not [c for c in cmds if isinstance(c, StopSpeech)]


# --------------------------------------------------------------- turn length


def test_wrap_up_directive_then_hard_stop(fc, state):
    """The 45-second monologue is the classic LLM-panel failure."""
    state, _ = run(
        fc,
        state,
        invite(0.0),
        AgentProposal(t=0.0, agent="wayne", utterance="Look —", signals=strong()),
        TurnYielded(t=1.0),
        AgentSpeechStarted(t=1.0, agent="wayne"),
    )
    persona = fc.cast["wayne"]

    state, cmds = fc.reduce(state, Tick(t=1.0 + persona.target_turn_seconds + 0.1))
    assert [c for c in cmds if isinstance(c, InjectDirective)]

    # Only sent once.
    state, cmds = fc.reduce(state, Tick(t=1.0 + persona.target_turn_seconds + 1.0))
    assert not [c for c in cmds if isinstance(c, InjectDirective)]

    state, cmds = fc.reduce(state, Tick(t=1.0 + persona.max_turn_seconds + 0.1))
    stops = [c for c in cmds if isinstance(c, StopSpeech)]
    assert stops and stops[0].reason is StopReason.TURN_LIMIT
    assert state.speaking is None


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
        state, TranscriptUpdated(t=10.0, speaker=HUMAN, text="So what", is_final=False)
    )
    assert [c for c in cmds if isinstance(c, RequestProposals)]

    # Too soon — debounced.
    state, cmds = fc.reduce(
        state, TranscriptUpdated(t=10.1, speaker=HUMAN, text="So what do", is_final=False)
    )
    assert not [c for c in cmds if isinstance(c, RequestProposals)]

    state, cmds = fc.reduce(
        state,
        TranscriptUpdated(t=10.0 + fc.config.speculation_interval_s + 0.01, speaker=HUMAN,
                          text="So what do you think", is_final=False),
    )
    assert [c for c in cmds if isinstance(c, RequestProposals)]


def test_final_transcript_always_requests_proposals(fc, state):
    """Regression: the debounce must throttle partials only.

    A human turn landing inside the debounce window — which is exactly what
    happens on the turn straight after an agent finishes — was getting no
    candidates at all, so the panel fell silent.
    """
    state, _ = fc.reduce(
        state, TranscriptUpdated(t=10.0, speaker=HUMAN, text="So", is_final=False)
    )
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


# ------------------------------------------------------------------- replay


def test_reduce_is_deterministic(fc, cast):
    """Same events in, same commands out — the basis of replayable rehearsals."""
    events = [
        TranscriptUpdated(t=0.0, speaker=HUMAN, text="So Wayne, oversight?", is_final=True),
        AgentProposal(t=0.5, agent="wayne", utterance="Useless.", signals=strong()),
        AgentProposal(t=0.6, agent="dex", utterance="Historically...", signals=strong()),
        TurnYielded(t=1.0),
        AgentSpeechStarted(t=1.1, agent="wayne"),
        AgentProposal(t=6.0, agent="dex", utterance="Disagree.",
                      signals=strong(disagreement=0.96, urgency=0.95)),
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
    state, _ = fc.reduce(state, _introduce(0.0))
    assert state.invitation is not None
    assert state.invitation.agent is None
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
    state, _ = fc.reduce(state, TranscriptUpdated(t=0.0, speaker=HUMAN, text=text, is_final=True))
    assert state.intro_queue is None


def _run_introduction_round(
    fc: FloorController, state: PanelState, *, start_t: float = 0.0
) -> tuple[PanelState, list[tuple[str, str]]]:
    """Drive a whole introduction round the way the runtime now does: no
    `AgentProposal`, ever. The floor grants each fixed line itself the moment
    the phrase is detected and again every time one agent finishes, so the
    only events a caller needs to supply are the ones confirming that audio
    actually started and stopped. Returns the final state and the
    ``(agent, utterance)`` pairs in the order they were granted.
    """
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
    state, cmds = fc.reduce(state, _introduce(0.0))
    starts = [c for c in cmds if isinstance(c, StartSpeech)]
    assert len(starts) == 1
    first = tuple(fc.cast.ids())[0]
    assert starts[0].agent == first
    assert starts[0].utterance == fc.cast[first].introduction
    assert not [c for c in cmds if isinstance(c, RequestProposals)], (
        "the introduction round must never ask the model for anything"
    )


def test_every_agent_gets_exactly_one_introduction_turn(fc, state):
    """All three must speak, with no repeats, and the round latches shut."""
    state, order = _run_introduction_round(fc, state)
    spoken = [agent for agent, _ in order]

    assert set(spoken) == set(fc.cast.ids())
    assert len(spoken) == len(set(spoken)), "no repeats"
    assert state.invitation is None
    assert state.intro_done is True


def test_introduction_round_speaks_each_persona_fixed_text_in_cast_order(fc, state):
    """Order is the cast's own order, not a score — and every word spoken is
    `Persona.introduction`, verbatim, never anything a model produced."""
    state, order = _run_introduction_round(fc, state)
    assert [agent for agent, _ in order] == list(fc.cast.ids())
    assert dict(order) == {a: fc.cast[a].introduction for a in fc.cast.ids()}


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
    assert set(agent for agent, _ in order) == set(fc.cast.ids())
    assert state.intro_done is True


def test_a_turn_limit_cutoff_still_advances_the_introduction_round(fc, state):
    """Regression: a rambling first introduction hitting the hard turn limit
    must not strand the round. `AgentSpeechEnded(completed=False)` for a
    turn-limit stop must still pop the queue and grant the next agent
    directly — only an agent interrupt (where a challenger already holds the
    floor) should skip that."""
    state, cmds = fc.reduce(state, _introduce(0.0))
    first = cmds[0].agent
    utterance = cmds[0].utterance
    state, _ = fc.reduce(state, AgentSpeechStarted(t=0.2, agent=first))

    # The floor hard-stops the agent for running long — mirrors `_tick`'s
    # TURN_LIMIT branch: `speaking` is cleared before the StopSpeech is even
    # issued, same shape as the runtime's real turn-limit path.
    state, cmds = fc.reduce(state, Tick(t=0.2 + fc.cast[first].max_turn_seconds + 0.1))
    assert [c for c in cmds if isinstance(c, StopSpeech)]
    assert state.speaking is None

    state, cmds = fc.reduce(
        state,
        AgentSpeechEnded(
            t=0.2 + fc.cast[first].max_turn_seconds + 0.2,
            agent=first,
            completed=False,
            utterance=utterance[:10],
        ),
    )
    assert first not in (state.intro_queue or ()), "must still advance, not stall"
    next_starts = [c for c in cmds if isinstance(c, StartSpeech)]
    assert next_starts and next_starts[0].agent != first, "the next agent is granted directly"
    assert not [c for c in cmds if isinstance(c, RequestProposals)]


def test_ricky_interrupting_the_round_allows_a_clean_retry(fc, state):
    """An abandoned round has not 'been done' — the safety latch must not
    engage, or the show is stuck with two agents introduced and no way to
    finish."""
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
