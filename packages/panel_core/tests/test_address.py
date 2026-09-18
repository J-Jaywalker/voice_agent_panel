"""Who did Ricky actually address? — the moderator phrasing corpus.

These are acceptance criteria, not unit tests. Every row in `CORPUS` is a real
thing a moderator says on stage, and the expected column is what the floor is
allowed to do about it. **When a phrasing misfires in rehearsal, add a row.**
That is the intended maintenance loop for this file: the corpus is the spec,
and the spec grows by observation.

The rule the corpus encodes is that grammatical *role* decides the addressee,
never position in the sentence. The bug this replaced took the leftmost name,
so:

    "Sorry, Dexter, can I just interrupt? Uh, Melia, can you continue?"

invited Dexter — the one agent being stood down — and then granted him the
floor unconditionally, over the moderator, bypassing both the score floor and
his own handoff to Melia.

Three roles, in strict precedence:

    SUBJECT_OF_REQUEST  "can Melia speak", "over to Melia", "what about Melia"
    VOCATIVE            "Melia, can you continue?", "what do you think, Wayne?"
    OBLIQUE             "sorry for interrupting Dexter" — never an addressee

and one non-outcome: two agents at the same top role is **ambiguous**, which
keeps the floor closed and puts the tie in front of the operator. A missed
invitation costs one beat. A wrong one puts an agent on the PA over Ricky in
front of 400 people.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from panel_core import (
    HUMAN,
    AgentProposal,
    CueModerator,
    FloorConfig,
    FloorController,
    HandsRaised,
    HumanSpeechStarted,
    PanelCast,
    PanelState,
    Signals,
    StartSpeech,
    StateChanged,
    Tick,
    TranscriptUpdated,
    TurnYielded,
)
from panel_core.floor import AddressRole, CueReason
from panel_core.state import InvitationSource

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"

# Outcomes that are not an agent id.
OPEN = "<open to the panel>"
CLOSED = "<floor stays closed>"
AMBIGUOUS = "<ambiguous — operator decides>"


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


def weak() -> Signals:
    return Signals(relevance=0.1, urgency=0.05, disagreement=0.0, confidence=0.2, expertise=0.0)


def said(text: str, t: float = 0.0) -> TranscriptUpdated:
    """One final segment off Ricky's mic."""
    return TranscriptUpdated(t=t, speaker=HUMAN, text=text, is_final=True)


def resolve(fc: FloorController, state: PanelState, text: str) -> str:
    """Feed Ricky's words to the reducer and report what the floor opened to."""
    state, _ = fc.reduce(state, said(text))
    if state.address_conflict:
        return AMBIGUOUS
    if state.invitation is None:
        return CLOSED
    return state.invitation.agent or OPEN


# --------------------------------------------------------------- the corpus
#
# (utterance, who gets the floor, which role resolved it)
#
# The role column is `None` where nobody is addressed. It is asserted because
# the operator console shows it: a rehearsal has to see *why* the floor opened
# where it did, not just that it did.

CORPUS: list[tuple[str, str, AddressRole | None]] = [
    # --- the two verbatim failures from the report ---------------------------
    (
        (
            "Sorry, Dexter, can I just interrupt? "
            "Uh, Melia, can you continue to elaborate on that, please?"
        ),
        "melia",
        AddressRole.VOCATIVE,
    ),
    (
        "Sorry Dexter, can Melia speak? Sorry for interrupting Dexter, is that okay?",
        "melia",
        AddressRole.SUBJECT_OF_REQUEST,
    ),
    # --- the required table -------------------------------------------------
    ("Melia, can you continue to elaborate on that?", "melia", AddressRole.VOCATIVE),
    ("Sorry, Dexter, can you wrap up?", "dex", AddressRole.VOCATIVE),
    ("Melia, do you agree with Dexter?", "melia", AddressRole.VOCATIVE),
    ("Who would like to begin?", OPEN, None),
    ("Is that okay?", CLOSED, None),
    ("Does that work?", CLOSED, None),
    ("Do you mind?", CLOSED, None),
    # --- vocative: the second-person request binds to the name --------------
    ("So Wayne, what about human oversight?", "wayne", AddressRole.VOCATIVE),
    ("Wayne, does oversight actually scale?", "wayne", AddressRole.VOCATIVE),
    ("What do you think, Wayne?", "wayne", AddressRole.VOCATIVE),
    ("Melia, what is your read on that?", "melia", AddressRole.VOCATIVE),
    # A continuation cue is a request even with no question mark on the end.
    # The original failure only minted an invitation at all because the
    # sentence happened to end in "?".
    ("Melia, carry on.", "melia", AddressRole.VOCATIVE),
    ("Wayne, finish your point.", "wayne", AddressRole.VOCATIVE),
    # --- subject of a request: the strongest role ---------------------------
    ("Can Melia take that one?", "melia", AddressRole.SUBJECT_OF_REQUEST),
    ("Let's hear from Melia.", "melia", AddressRole.SUBJECT_OF_REQUEST),
    ("Let Melia finish.", "melia", AddressRole.SUBJECT_OF_REQUEST),
    ("I'd like Melia to pick that up.", "melia", AddressRole.SUBJECT_OF_REQUEST),
    ("What does Melia think?", "melia", AddressRole.SUBJECT_OF_REQUEST),
    ("What about Melia?", "melia", AddressRole.SUBJECT_OF_REQUEST),
    ("Melia's thoughts on that?", "melia", AddressRole.SUBJECT_OF_REQUEST),
    ("Over to Melia.", "melia", AddressRole.SUBJECT_OF_REQUEST),
    # Precedence, in one sentence: Dexter is addressed second-person, Melia is
    # the subject of the request inside it. The subject wins.
    ("Sorry Dexter, can you let Melia finish?", "melia", AddressRole.SUBJECT_OF_REQUEST),
    # --- oblique: named, but never the addressee ----------------------------
    ("Sorry for interrupting Dexter.", CLOSED, None),
    ("Sorry, Dexter, can I just interrupt?", CLOSED, None),
    ("I'm cutting off Dexter there.", CLOSED, None),
    ("Thanks Dexter, and Melia, what do you think?", "melia", AddressRole.VOCATIVE),
    # --- courtesy and permission tags invite nobody -------------------------
    ("If that's alright?", CLOSED, None),
    ("Right?", CLOSED, None),
    ("Does that make sense?", CLOSED, None),
    ("Are we okay?", CLOSED, None),
    ("Can I just jump in?", CLOSED, None),
    ("Can I just interrupt?", CLOSED, None),
    # ...but a first-person *frame* around a real request still invites: Ricky
    # asking permission to bring Melia in is a request for Melia.
    ("Can I ask Melia to comment?", "melia", AddressRole.SUBJECT_OF_REQUEST),
    ("Can I hear from Melia?", "melia", AddressRole.SUBJECT_OF_REQUEST),
    ("Could I bring in Melia here?", "melia", AddressRole.SUBJECT_OF_REQUEST),
    # --- open to the panel --------------------------------------------------
    ("What do you all think?", OPEN, None),
    ("Any thoughts?", OPEN, None),
    ("Thoughts?", OPEN, None),
    ("Who wants to start?", OPEN, None),
    ("What holds it back?", OPEN, None),
    ("Say more about that.", OPEN, None),
    # --- statements invite nobody, however interesting ----------------------
    ("We have been doing real-time transcription for a decade now.", CLOSED, None),
    ("That is roughly where the market sits.", CLOSED, None),
    ("We continue to invest in this.", CLOSED, None),
    ("Let me elaborate on that.", CLOSED, None),
    ("She's quite introverted.", CLOSED, None),
    # --- two agents in the same role: we do not guess -----------------------
    ("Melia, Dexter, thoughts?", AMBIGUOUS, None),
    ("Can Melia and Wayne both take that?", AMBIGUOUS, None),
    ("Melia and Wayne, can you take that between you?", AMBIGUOUS, None),
]


@pytest.mark.parametrize(("text", "expected", "role"), CORPUS, ids=[c[0] for c in CORPUS])
def test_moderator_phrasing_resolves_to_the_right_addressee(
    fc, state, text: str, expected: str, role: AddressRole | None
):
    assert resolve(fc, state, text) == expected

    if role is not None:
        state, _ = fc.reduce(state, said(text))
        assert state.invitation is not None
        assert state.invitation.role == role.value, "the console shows which role matched"
        assert state.invitation.rule, "...and which rule inside it"


def test_the_original_failure_end_to_end(fc, state):
    """The verbatim trace from the report, all the way to the PA.

    Ricky stands Dexter down and asks Melia to continue. Previously: an
    ADDRESS invitation for Dexter, Melia filtered out as inadmissible, two
    `no_candidate`s, and then Dexter granted the floor unconditionally — score
    floor bypassed and his own handoff to Melia discarded.
    """
    state, _ = run(
        fc,
        state,
        said(
            "Sorry, Dexter, can I just interrupt? "
            "Uh, Melia, can you continue to elaborate on that, please?"
        ),
        AgentProposal(
            t=0.5,
            agent="dex",
            utterance="Melia's the one to follow here.",
            signals=strong(defer_to="melia"),
        ),
        AgentProposal(t=0.6, agent="melia", utterance="The rollout data says otherwise.",
                      signals=weak()),
    )
    assert state.invitation is not None
    assert state.invitation.agent == "melia"
    assert state.invitation.source is InvitationSource.ADDRESS

    _, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["melia"]


# ------------------------------------------------------- precedence, not recency


def test_a_later_open_question_does_not_downgrade_a_named_invitation(fc, state):
    """One act of moderation, three transcript segments.

    "Sorry Dexter, can Melia speak?" then a trailing courtesy, then a vaguer
    aside. Melia is still the one who was asked; the floor must not widen back
    out to whoever happens to score best.
    """
    state, _ = fc.reduce(state, said("Sorry Dexter, can Melia speak?", t=0.0))
    assert state.invitation.agent == "melia"

    state, _ = fc.reduce(state, said("Sorry for interrupting Dexter, is that okay?", t=0.4))
    assert state.invitation.agent == "melia", "a courtesy tag invites nobody"

    state, _ = fc.reduce(state, said("So what does everyone think?", t=0.9))
    assert state.invitation.agent == "melia", "an open question must not downgrade an address"


def test_an_open_question_supersedes_a_named_invitation_once_the_window_passes(fc, state):
    """The hold-off is bounded. Ricky moving on is not the same as stammering."""
    state, _ = fc.reduce(state, said("Sorry Dexter, can Melia speak?", t=0.0))
    later = fc.config.invitation_supersede_window_s + 0.1
    state, _ = fc.reduce(state, said("So what does everyone think?", t=later))
    assert state.invitation.agent is None, "a new beat, a new invitation"


def test_a_fresh_address_always_supersedes(fc, state):
    """Ricky redirecting mid-turn is the common case, not an edge case."""
    state, _ = fc.reduce(state, said("Melia, can you continue?", t=0.0))
    assert state.invitation.agent == "melia"
    state, _ = fc.reduce(state, said("Actually Wayne, what do you think?", t=0.3))
    assert state.invitation.agent == "wayne"


# ------------------------------------------------ ambiguity is an outcome


def test_two_agents_in_the_same_role_keeps_the_floor_closed(fc, state):
    """We do not guess between them. The operator does.

    Ricky can fix a missed invitation in one beat. He cannot un-say an agent
    that spoke over him in front of 400 people.
    """
    state, cmds = fc.reduce(state, said("Melia, Dexter, thoughts?"))
    assert state.invitation is None, "the floor stays closed"
    assert state.address_conflict == ("dex", "melia")

    paints = [c for c in cmds if isinstance(c, StateChanged)]
    assert paints, "the tie is surfaced immediately, not at the end of the turn"
    assert paints[-1].extra["address_conflict"] == ("dex", "melia")

    state, _ = fc.reduce(
        state, AgentProposal(t=0.5, agent="dex", utterance="Historically...", signals=strong())
    )
    _, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert not [c for c in cmds if isinstance(c, StartSpeech)]
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [
        CueReason.AMBIGUOUS_ADDRESS
    ]
    assert [c for c in cmds if isinstance(c, HandsRaised)], "Ricky sees who wanted it"


def test_a_resolved_tie_does_not_haunt_later_turns(fc, state):
    """Regression: a tie is about one utterance, not about the rest of the show.

    A conflict left standing turns every later "Ricky made a remark" cue into
    a spurious `ambiguous_address`, which would send the operator hunting for
    a tie that no longer exists.
    """
    state, _ = fc.reduce(state, said("Melia, Dexter, thoughts?"))
    assert state.address_conflict

    state, _ = fc.reduce(state, said("Melia, can you take that?", t=1.0))
    assert state.address_conflict == (), "a clean address resolves the tie"
    assert state.invitation.agent == "melia"

    # ...and so does Ricky simply carrying on talking.
    state, _ = fc.reduce(state, said("Melia, Dexter, thoughts?", t=2.0))
    assert state.address_conflict
    state, _ = fc.reduce(state, HumanSpeechStarted(t=3.0))
    assert state.address_conflict == ()

    state, _ = fc.reduce(state, said("That is roughly where we are.", t=4.0))
    _, cmds = fc.reduce(state, TurnYielded(t=5.0))
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [CueReason.NO_INVITATION]


# ---------------------------------------------------------- invitation TTL


def test_an_invitation_nobody_acts_on_expires(fc, state):
    """A mis-addressed invitation must not stand for the rest of the show.

    `no_candidate` deliberately does *not* clear the invitation — an empty
    proposal set for two or three seconds is normal, and clearing it there
    makes the panel unanswerable. So the TTL is the only thing that reaps one.
    """
    state, _ = fc.reduce(state, said("Melia, can you continue?"))
    assert state.invitation is not None

    state, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert not [c for c in cmds if isinstance(c, CueModerator)], (
        "a named agent gets a beat to finish answering before Ricky is told to fill"
    )
    state, cmds = fc.reduce(state, Tick(t=1.0 + fc.config.invited_agent_grace_s))
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [
        CueReason.INVITED_AGENT_SILENT
    ]
    assert state.invitation is not None, "an empty proposal set for a beat is not a mistake"

    state, cmds = fc.reduce(state, Tick(t=fc.config.invitation_ttl_s + 0.1))
    assert state.invitation is None
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [
        CueReason.INVITATION_EXPIRED
    ]


def test_a_live_exchange_does_not_age_out_mid_answer(fc, state):
    """The TTL measures neglect, not elapsed time. Granting a turn resets it."""
    config = FloorConfig(invitation_ttl_s=5.0, open_invitation_turns=2)
    fc = FloorController(fc.cast, config)
    state, _ = run(
        fc,
        state,
        said("What holds it back?"),
        AgentProposal(t=0.5, agent="dex", utterance="Trust.", signals=strong()),
    )
    state, cmds = fc.reduce(state, TurnYielded(t=4.0))
    assert [c for c in cmds if isinstance(c, StartSpeech)]

    state, _ = fc.reduce(state, Tick(t=6.0))
    assert state.invitation is not None, "6s in, but only 2s since the grant"


def test_the_introduction_round_never_expires(fc, state):
    """It is bounded by the cast size; cutting it short strands agents."""
    state, _ = fc.reduce(state, said("Let's have all of you introduce yourselves."))
    assert state.invitation is not None
    state, _ = fc.reduce(state, Tick(t=fc.config.invitation_ttl_s * 3))
    assert state.invitation is not None
    assert state.intro_queue is not None


# ------------------------------------------------------ named grants, in full


def test_a_named_grant_honours_the_agents_own_handoff(fc, state):
    """'Melia's the one to follow here' is information, not noise.

    The score floor stays bypassed — a direct question deserves an answer on
    weak signals — but the handoff is no longer thrown away with it.
    """
    state, _ = run(
        fc,
        state,
        said("Dexter, what is your read on the rollout?"),
        AgentProposal(
            t=0.5,
            agent="dex",
            utterance="Melia's the one to follow here.",
            signals=strong(defer_to="melia"),
        ),
        AgentProposal(t=0.6, agent="melia", utterance="We saw it fail twice.", signals=weak()),
    )
    assert state.invitation.agent == "dex"
    _, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["melia"]


def test_a_named_grant_still_bypasses_the_score_floor(fc, state):
    """Asked directly, an agent answers even with nothing much to offer."""
    state, _ = run(
        fc,
        state,
        said("Dexter, what is your read on the rollout?"),
        AgentProposal(t=0.5, agent="dex", utterance="Hard to say.", signals=weak()),
    )
    _, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["dex"]


def test_a_handoff_to_an_agent_with_nothing_queued_is_ignored(fc, state):
    """A handoff target that never proposed cannot be put on the PA."""
    state, _ = run(
        fc,
        state,
        said("Dexter, what is your read on the rollout?"),
        AgentProposal(
            t=0.5, agent="dex", utterance="Ask Wayne.", signals=strong(defer_to="wayne")
        ),
    )
    _, cmds = fc.reduce(state, TurnYielded(t=1.0))
    assert [c.agent for c in cmds if isinstance(c, StartSpeech)] == ["dex"]


# ------------------------------------------------- saying *why* nobody spoke


def test_no_candidate_is_split_into_distinct_reasons(fc, state):
    """A rehearsal that cannot tell these apart cannot be tuned.

    "The panel had nothing", "the panel had something and it was not good
    enough" and "the one agent Ricky named said nothing" are three different
    problems with three different fixes.
    """
    # Nobody proposed at all.
    invited, cmds = fc.reduce(state, said("What holds it back?"))
    _, cmds = fc.reduce(invited, TurnYielded(t=1.0))
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [CueReason.NO_PROPOSALS]

    # Proposals existed, but silence was worth more.
    below, _ = fc.reduce(
        invited, AgentProposal(t=0.5, agent="melia", utterance="Mm.", signals=weak())
    )
    _, cmds = fc.reduce(below, TurnYielded(t=1.0))
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [CueReason.BELOW_FLOOR]

    # The named agent had nothing; another agent answering for him is not an
    # option, however strong its case.
    named, _ = run(
        fc,
        state,
        said("Wayne, does oversight actually scale?"),
        AgentProposal(t=0.5, agent="dex", utterance="Historically...", signals=strong()),
    )
    named, cmds = fc.reduce(named, TurnYielded(t=1.0))
    assert not [c for c in cmds if isinstance(c, StartSpeech)]
    # Deferred, not cancelled: Wayne gets the beat, then Ricky is told.
    _, cmds = fc.reduce(named, Tick(t=1.0 + fc.config.invited_agent_grace_s))
    assert [c.reason for c in cmds if isinstance(c, CueModerator)] == [
        CueReason.INVITED_AGENT_SILENT
    ]


def test_the_repaint_carries_the_reasoning_not_just_the_outcome(fc, state):
    """What the operator console and the video wall render."""
    state, cmds = fc.reduce(state, said("Sorry Dexter, can Melia speak?"))
    extra = [c for c in cmds if isinstance(c, StateChanged)][-1].extra
    assert extra["invited"] == "melia"
    assert extra["invitation_source"] == InvitationSource.ADDRESS.value
    assert extra["invitation_role"] == AddressRole.SUBJECT_OF_REQUEST.value
    assert extra["invitation_rule"]
    assert extra["address_conflict"] == ()
