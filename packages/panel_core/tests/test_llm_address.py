"""The classifier address path, at the reducer.

`FloorConfig.llm_address_detection` moves exactly one decision — *who did Ricky
just invite to speak?* — out of the regex in `FloorController._detect` and into
a model in `panel_runtime.address`. The model's answer comes back as an
`AddressDetected` event, and that is the whole point of the design: `panel_core`
stays a pure function of events, so a recorded session still replays identically
through modified floor logic and the LLM never sits inside the reducer.

These tests are about the seam, not about the model. No network, no API key,
no timing: every verdict is handed to the reducer directly, which is exactly how
it arrives on stage. `packages/panel_runtime/tests/bench_address.py` is where
the model's accuracy is measured, and `test_address.py` next door is the regex's
acceptance corpus — which stays the bar, because `verdict=None` falls back to it
and every row of it must come out unchanged.

Two properties matter more than the individual verdict mappings:

1. **Off is byte-for-byte what it was.** The flag defaults to off, the regex
   still runs on a human final, and an `AddressDetected` arriving anyway is
   ignored rather than applied on top — otherwise one recording replayed with
   the flag off would apply two detections to the same final and which won
   would depend on the supersede window.
2. **The event is what opens the floor.** With the flag on, a human final alone
   invites nobody, however clearly it names an agent. If that ever stops being
   true there are two detectors running again.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from panel_core import (
    HUMAN,
    AddressDetected,
    AgentProposal,
    AgentSpeechEnded,
    AgentSpeechStarted,
    CueModerator,
    FloorConfig,
    FloorController,
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
from panel_core.floor import CueReason
from panel_core.prompts import (
    AMBIGUOUS_VERDICT,
    INTRO_VERDICT,
    NO_VERDICT,
    OPEN_VERDICT,
)
from panel_core.state import InvitationSource

# The regex's acceptance corpus, single-sourced rather than copied: the
# `verdict=None` fallback has to reproduce it exactly, and two versions of an
# eval set means the one you are not looking at is wrong.
from test_address import AMBIGUOUS, CLOSED, CORPUS, OPEN

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"


@pytest.fixture
def cast() -> PanelCast:
    return PanelCast.from_dir(PERSONA_DIR)


@pytest.fixture
def llm(cast: PanelCast) -> FloorController:
    """The floor with the classifier path switched on."""
    return FloorController(cast, FloorConfig(llm_address_detection=True))


@pytest.fixture
def regex(cast: PanelCast) -> FloorController:
    """The floor as shipped: regex detection, flag at its default."""
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


def said(text: str, t: float = 0.0) -> TranscriptUpdated:
    """One final segment off Ricky's mic."""
    return TranscriptUpdated(t=t, speaker=HUMAN, text=text, is_final=True)


def verdict(
    token: str | None,
    *agents: str,
    text: str = "Melia, can you continue?",
    t: float = 0.1,
    conflict: tuple[str, ...] = (),
) -> AddressDetected:
    """What the runtime emits once the classifier has answered.

    `agents` is varargs because a verdict can name more than one panellist:
    `verdict("MELIA+WAYNE", "melia", "wayne")` is Ricky asking two of them to
    take something between them, which is an invitation to both and not a tie.
    `conflict` is the tie, and the two never co-occur.
    """
    return AddressDetected(
        t=t,
        text=text,
        verdict=token,
        agents=agents,
        conflict=conflict,
        reason="test",
        latency_ms=12.0,
        source="fresh",
    )


def outcome(state: PanelState) -> str:
    """What the floor opened to, in the corpus's own vocabulary."""
    if state.address_conflict:
        return AMBIGUOUS
    if state.invitation is None:
        return CLOSED
    return "+".join(state.invitation.agents) or OPEN


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


# ------------------------------------------------- the flag, in both states


def test_a_human_final_alone_opens_no_invitation(llm, state):
    """With the classifier on, the *event* is what opens the floor.

    The text names Melia as unambiguously as the corpus ever does, and the
    reducer must still do nothing with it: the regex did not run. If this ever
    starts passing an invitation back, two detectors are live at once and every
    verdict is racing a pattern match.
    """
    state, commands = llm.reduce(state, said("Melia, can you continue on that?"))
    assert state.invitation is None
    assert state.address_conflict == ()
    assert not [c for c in commands if isinstance(c, StartSpeech)]


def test_the_regex_still_detects_when_the_flag_is_off(regex, state):
    """No regression: off is the shipped path and must be untouched."""
    state, _ = regex.reduce(state, said("Melia, can you continue on that?"))
    assert state.invitation is not None
    assert state.invitation.agent == "melia"
    assert state.invitation.source is InvitationSource.ADDRESS


def test_address_detected_is_ignored_when_the_flag_is_off(regex, state):
    """A flag-off replay of a flag-on recording must not double-apply.

    The log carries both the final and the verdict. With the flag off the regex
    has already decided on the final, so honouring the verdict too would apply
    two detections to one utterance — and which one survived would depend on
    `invitation_supersede_window_s`, which is untunable by construction.
    """
    state, _ = regex.reduce(state, said("Thanks, everybody."))
    assert state.invitation is None

    after, commands = regex.reduce(state, verdict("MELIA", "melia"))
    assert after.invitation is None
    assert after == state
    assert commands == []


# ---------------------------------------------------- verdict -> floor state


def test_an_agent_verdict_invites_that_agent(llm, state):
    state, commands = run(
        llm,
        state,
        said("What does the financial side make of that?"),
        verdict("WAYNE", "wayne", text="What does the financial side make of that?"),
    )
    invitation = state.invitation
    assert invitation is not None
    assert invitation.agent == "wayne"
    assert invitation.source is InvitationSource.ADDRESS
    assert invitation.turns_remaining == llm.config.address_invitation_turns
    # Provenance: the operator console and the rehearsal log have to be able to
    # see that a model decided this, and which token it wrote.
    assert invitation.role == "llm_address"
    assert invitation.rule == "llm_verdict:WAYNE"
    assert [c for c in commands if isinstance(c, StateChanged)]


def test_an_open_verdict_invites_the_panel(llm, state):
    state, _ = run(llm, state, said("Tell us more."), verdict(OPEN_VERDICT))
    invitation = state.invitation
    assert invitation is not None
    assert invitation.agents == ()
    assert invitation.source is InvitationSource.OPEN
    assert invitation.turns_remaining == llm.config.open_invitation_turns
    assert invitation.role == "llm_open"
    assert invitation.rule == f"llm_verdict:{OPEN_VERDICT}"


def test_a_joined_verdict_invites_exactly_those_agents(llm, state):
    """ "I'd like to hear from the other two" is a real invitation to two people.

    The sentence names nobody and is only resolvable against who just spoke,
    which is why the classifier is given `prompts.build_address_context` and
    why this is the capability the regex cannot reach. At the reducer the
    verdict is simply a set, and the floor opens to that set.
    """
    text = "I'd like to hear from the other two."
    state, commands = run(
        llm,
        state,
        said(text),
        verdict("MELIA+WAYNE", "melia", "wayne", text=text),
    )
    invitation = state.invitation
    assert invitation is not None
    assert invitation.agents == ("melia", "wayne")
    assert invitation.is_group
    assert invitation.agent is None, "a group has no single addressee"
    assert invitation.source is InvitationSource.ADDRESS
    assert invitation.turns_remaining == llm.config.address_invitation_turns
    assert invitation.role == "llm_address"
    assert invitation.rule == "llm_verdict:MELIA+WAYNE"
    assert state.address_conflict == (), "naming two is not a tie"

    painted = [c for c in commands if isinstance(c, StateChanged)][-1].extra
    assert painted["invited_agents"] == ("melia", "wayne")


def test_a_joined_verdict_naming_everyone_is_an_open_floor(llm, state):
    """There is nobody left to bar, so it is the open floor it looks like.

    Kept as a *normalisation* rather than left as a three-name scope because
    the two behave differently: an open floor owes every agent a turn and
    closes once they have each had one, while a group runs its turn budget and
    lets its members speak twice. A set covering the whole panel must get the
    first of those.
    """
    text = "All three of you, then."
    state, _ = run(
        llm,
        state,
        said(text),
        verdict("DEXTER+MELIA+WAYNE", "dex", "melia", "wayne", text=text),
    )
    invitation = state.invitation
    assert invitation is not None
    assert invitation.agents == ()
    assert not invitation.is_group
    assert invitation.source is InvitationSource.OPEN
    assert invitation.turns_remaining == llm.config.open_invitation_turns
    assert invitation.role == "llm_open"
    # ...and the rule still records the verdict that produced it, so a
    # rehearsal can see that the model named three people and the floor read
    # that as open.
    assert invitation.rule == "llm_verdict:DEXTER+MELIA+WAYNE"


def test_a_joined_verdict_bars_the_agent_it_left_out(llm, state):
    """The point of the extra name is the panellist it excludes."""
    text = "Melia and Wayne, between you?"
    state, _ = run(
        llm,
        state,
        said(text),
        verdict("MELIA+WAYNE", "melia", "wayne", text=text),
        AgentProposal(t=0.5, agent="dex", utterance="Security, surely.", signals=strong()),
    )
    _, commands = llm.reduce(state, TurnYielded(t=1.0))
    assert not [c for c in commands if isinstance(c, StartSpeech)]


def test_a_verdict_naming_one_unknown_agent_invites_the_known_one(llm, state):
    """A set is intersected with the cast, not refused wholesale.

    A replay log from a different cast, or a model that wrote one good name and
    one bad one, must not cost the invitation the good name earned. Refusing
    the whole set would send a direct question to the regex, and the regex
    cannot see "the other two" at all.
    """
    text = "Melia and Ricky, between you?"
    state, _ = run(llm, state, said(text), verdict("MELIA+RICKY", "melia", "ricky", text=text))
    assert state.invitation is not None
    assert state.invitation.agents == ("melia",)
    assert state.invitation.role == "llm_address", "...and it is still the model's verdict"


def test_a_none_verdict_leaves_the_floor_closed(llm, state):
    """NONE is a real answer, not a failure. Nothing happens, loudly."""
    state, commands = run(llm, state, said("Adoption is uneven."), verdict(NO_VERDICT))
    assert state.invitation is None
    assert state.address_conflict == ()
    assert not [c for c in commands if isinstance(c, StartSpeech)]


def test_an_ambiguous_verdict_shows_the_tie_and_stays_closed(llm, state):
    """Ambiguity is an outcome: closed floor, and the operator decides."""
    state, _ = run(
        llm,
        state,
        said("Melia and Wayne, can you take that between you?"),
        verdict(AMBIGUOUS_VERDICT, conflict=("melia", "wayne")),
    )
    assert state.invitation is None
    assert state.address_conflict == ("melia", "wayne")

    _, commands = llm.reduce(state, TurnYielded(t=1.0))
    assert [c.reason for c in commands if isinstance(c, CueModerator)] == [
        CueReason.AMBIGUOUS_ADDRESS
    ]


def test_an_ambiguous_verdict_with_no_named_tie_still_reports_ambiguity(llm, state):
    """The verdict token is one word and cannot name who tied.

    An empty conflict must not quietly degrade to `NO_INVITATION`, which would
    show the operator nothing at all. The whole cast stands in for "ambiguous,
    and we cannot say between whom".
    """
    state, _ = run(llm, state, said("Both of you, then?"), verdict(AMBIGUOUS_VERDICT))
    assert state.invitation is None
    assert state.address_conflict == tuple(state.agents)

    _, commands = llm.reduce(state, TurnYielded(t=1.0))
    assert [c.reason for c in commands if isinstance(c, CueModerator)] == [
        CueReason.AMBIGUOUS_ADDRESS
    ]


def test_a_resolved_conflict_does_not_haunt_later_turns(llm, state):
    """A tie is about one utterance, not about the rest of the show.

    A conflict left standing turns every later "Ricky made a remark" cue into
    a spurious `ambiguous_address`, which would send the operator hunting for
    a tie that no longer exists. Lived in `test_address.py` until the regex
    stopped producing ties at all; the property is the reducer's either way,
    and `AMBIGUOUS_VERDICT` is now the only thing that can arm it.
    """
    state, _ = run(llm, state, said("Which of you owns that?"), verdict(AMBIGUOUS_VERDICT))
    assert state.address_conflict

    state, _ = run(llm, state, said("Melia, take it.", t=1.0), verdict("MELIA", "melia", t=1.1))
    assert state.address_conflict == (), "a clean address resolves the tie"
    assert state.invitation.agent == "melia"

    # ...and so does Ricky simply carrying on talking.
    state, _ = run(llm, state, said("Which of you?", t=2.0), verdict(AMBIGUOUS_VERDICT, t=2.1))
    assert state.address_conflict
    state, _ = llm.reduce(state, HumanSpeechStarted(t=3.0))
    assert state.address_conflict == ()

    state, _ = run(
        llm, state, said("That is roughly where we are.", t=4.0), verdict(NO_VERDICT, t=4.1)
    )
    _, commands = llm.reduce(state, TurnYielded(t=5.0))
    assert [c.reason for c in commands if isinstance(c, CueModerator)] == [CueReason.NO_INVITATION]


def test_an_invited_agent_actually_gets_the_floor(llm, state):
    """The invitation has to be real, not merely present."""
    state, _ = run(
        llm,
        state,
        said("What does the financial side make of that?"),
        verdict("WAYNE", "wayne", text="What does the financial side make of that?"),
        AgentProposal(t=0.5, agent="wayne", utterance="", signals=strong()),
    )
    _, commands = llm.reduce(state, TurnYielded(t=1.0))
    assert [c.agent for c in commands if isinstance(c, StartSpeech)] == ["wayne"]


# ------------------------------------------------- the introduction round


def _complete_round(fc: FloorController, state: PanelState, commands: list, t: float = 1.0):
    """Play every granted introduction through to `AgentSpeechEnded`."""
    granted: list[str] = []
    pending = [c.agent for c in commands if isinstance(c, StartSpeech)]
    while pending:
        agent = pending.pop(0)
        granted.append(agent)
        state, cmds = run(
            fc,
            state,
            AgentSpeechStarted(t=t, agent=agent),
            AgentSpeechEnded(t=t + 1.0, agent=agent, completed=True, utterance="Hello."),
        )
        t += 2.0
        pending.extend(c.agent for c in cmds if isinstance(c, StartSpeech))
    return state, granted


def test_an_intro_verdict_starts_the_round_exactly_once(llm, state):
    """The one-shot latch is the reducer's, and the verdict does not bypass it.

    Three ways it could fire twice, all closed here: a second verdict while the
    round is live, a second verdict after it has completed, and — the reason
    the regex intro check is gated behind the flag at all — the regex and the
    classifier each starting a round of their own for the same sentence.
    """
    state, commands = run(
        llm,
        state,
        said("Right, let's do quick introductions."),
        verdict(INTRO_VERDICT, text="Right, let's do quick introductions."),
    )
    first = [c.agent for c in commands if isinstance(c, StartSpeech)]
    assert len(first) == 1, "the round grants one fixed line at a time"
    assert state.intro_queue is not None

    # A second verdict mid-round changes nothing.
    mid, mid_cmds = llm.reduce(state, verdict(INTRO_VERDICT, t=0.2))
    assert not [c for c in mid_cmds if isinstance(c, StartSpeech)]
    assert mid.intro_queue == state.intro_queue

    state, granted = _complete_round(llm, state, commands)
    assert granted == list(state.agents), "every agent introduces itself, once"
    assert state.intro_done
    assert state.intro_queue is None

    # ...and the latch never reopens.
    after, after_cmds = llm.reduce(state, verdict(INTRO_VERDICT, t=99.0))
    assert not [c for c in after_cmds if isinstance(c, StartSpeech)]
    assert after.intro_queue is None


def test_the_regex_intro_latch_does_not_fire_under_the_flag(llm, state):
    """ "Introductions" in the text must not start a round on its own.

    Both detectors being able to start the one-shot round is the specific
    hazard that put the intro check behind the flag: the regex fires on "intro"
    anywhere in a sentence, which is precisely the over-trigger the classifier
    exists to replace.
    """
    state, commands = llm.reduce(state, said("She's quite introverted, actually."))
    assert not [c for c in commands if isinstance(c, StartSpeech)]
    assert state.intro_queue is None
    assert not state.intro_done


# --------------------------------------------------- the regex fallback


@pytest.mark.parametrize(("text", "expected"), [(row[0], row[1]) for row in CORPUS])
def test_no_verdict_reproduces_the_regex_exactly(llm, regex, state, text, expected):
    """`verdict=None` means "classifier unavailable" — so the regex decides.

    Asserted against the whole acceptance corpus rather than a sample, and
    against the regex controller running the same text, because the guarantee
    is equality with today's behaviour and not merely plausibility. This is the
    path every timeout on stage takes.
    """
    # `t=0.0` is not incidental: the runtime stamps `AddressDetected` with the
    # *final's* timestamp, not the verdict's arrival time, so the invitation is
    # dated to the question on both paths and `named_proposal_lookback_s` and
    # the TTL keep measuring the same thing. Equality of the whole `Invitation`
    # below is what holds that in place.
    fallback, _ = run(llm, state, said(text), verdict(None, text=text, t=0.0))
    straight, _ = regex.reduce(state, said(text))

    assert outcome(fallback) == expected
    assert outcome(fallback) == outcome(straight)
    assert fallback.invitation == straight.invitation
    assert fallback.address_conflict == straight.address_conflict


def test_a_verdict_for_an_unknown_agent_falls_back_to_the_regex(llm, state):
    """A token this cast has nobody for is unavailable, not a guess.

    An invitation naming an agent who does not exist is unanswerable: the floor
    would sit closed behind it until the TTL reaped it, with the console showing
    a live invitation the whole time.
    """
    text = "Melia, can you continue on that?"
    state, _ = run(llm, state, said(text), verdict("RICKY", "ricky", text=text))
    assert state.invitation is not None
    assert state.invitation.agent == "melia"
    # ...and it came from the regex, not from the verdict.
    assert state.invitation.role != "llm_address"


# ------------------------------------ everything downstream is shared code


def test_a_later_open_verdict_does_not_downgrade_a_named_one(llm, state):
    """Precedence, not recency — the same rule the regex path obeys.

    One act of moderation arrives as several finals, so a trailing vaguer
    verdict must not turn "Melia answers" into "whoever scores best answers".
    """
    state, _ = run(
        llm,
        state,
        said("Melia, can you continue?"),
        verdict("MELIA", "melia", t=0.2),
        said("Is that okay?", t=1.0),
        verdict(OPEN_VERDICT, text="Is that okay?", t=1.2),
    )
    assert state.invitation is not None
    assert state.invitation.agent == "melia"
    assert state.invitation.source is InvitationSource.ADDRESS


def test_a_fresh_verdict_stands_the_moderator_cue_back_down(llm, state):
    """A new question is a new chance for the panel to answer it.

    `moderator_cued` latches for the life of one invitation so an unanswered
    question cues Ricky once rather than once per proposal. A fresh invitation
    has to clear it, or the first unanswered question of the show would be the
    only one he is ever told about — and that reset lives in the shared
    `_install_invitation`, so it has to hold on this path too.
    """
    state, _ = run(
        llm,
        state,
        said("Wayne, does oversight scale?"),
        verdict("WAYNE", "wayne", t=0.2),
    )
    # Nobody proposed, so the floor holds a beat for Wayne rather than cueing
    # Ricky in the same millisecond his question landed...
    state, _ = llm.reduce(state, TurnYielded(t=1.0))
    assert state.awaiting_agents == ("wayne",)
    # ...and only cues him once the grace runs out.
    state, commands = llm.reduce(state, Tick(t=1.0 + llm.config.invited_agent_grace_s))
    assert [c.reason for c in commands if isinstance(c, CueModerator)] == [
        CueReason.INVITED_AGENT_SILENT
    ]
    assert state.moderator_cued

    state, _ = llm.reduce(state, verdict("MELIA", "melia", t=3.0))
    assert not state.moderator_cued
    assert state.awaiting_agents == ()
