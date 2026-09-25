"""Conversation state.

Immutable. Every transition returns a new state, which is what lets a recorded
event log replay deterministically through modified floor logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from .events import HUMAN, Signals


class AgentState(str, Enum):
    IDLE = "idle"
    WANTS_FLOOR = "wants_floor"
    SPEAKING = "speaking"
    MUTED = "muted"


class InvitationSource(str, Enum):
    ADDRESS = "address"  # Ricky named an agent and asked them something
    OPEN = "open"  # Ricky asked the room
    OPERATOR = "operator"  # the console opened the floor by hand
    INTRODUCTION = "introduction"  # the one-shot "introduce yourselves" round


# How specific an invitation is. A vaguer invitation must never quietly
# displace a more specific one: "Melia, can you continue? ... is that okay?"
# arrives as two segments, and the second must not downgrade the first from
# "Melia answers" to "whoever scores best answers". Compared in
# `FloorController._transcript`; see also `invitation_supersede_window_s`.
_INVITATION_PRECEDENCE: dict[InvitationSource, int] = {
    InvitationSource.OPEN: 1,
    InvitationSource.ADDRESS: 2,
    InvitationSource.OPERATOR: 2,  # a deliberate human act, as specific as a name
    InvitationSource.INTRODUCTION: 3,  # a bounded round; nothing may cut across it
}


@dataclass(frozen=True, slots=True)
class Invitation:
    """Permission for an agent — or the panel — to take the floor.

    The floor is closed by default. This is the object that opens it, and it is
    spent as it is used. Without one, ``TurnYielded`` returns the floor to the
    moderator and the panel stays quiet however much it wants to speak.

    ``role`` and ``rule`` are provenance, not behaviour: they record *why* the
    floor opened (which grammatical role the addressee held, and which pattern
    matched) so the operator console and the rehearsal log can show the
    reasoning rather than just the outcome.
    """

    agent: str | None  # None = open to the whole panel
    turns_remaining: int
    source: InvitationSource
    # The moment Ricky opened the floor. This never moves. It is the question a
    # proposal has to be an answer to (``FloorController._stale``) and the
    # anchor for the supersede window, and both of those are about *when the
    # invitation was made*, not about how recently it has been used.
    t: float
    role: str = ""  # AddressRole value, or "open"/"introduction"/"operator"
    rule: str = ""  # the specific pattern that matched, for the console
    # The TTL clock, and a different question from ``t``: when this invitation
    # was last acted on. None until it has produced a turn. Kept apart because
    # one field cannot answer both — ``spent()`` used to refresh ``t`` itself,
    # which meant an invitation that produced turns slid its own freshness and
    # supersede windows forward with it.
    last_active_t: float | None = None

    def spent(self, *, t: float | None = None) -> Invitation:
        """Consume one turn.

        ``t`` refreshes the *activity* clock, not ``self.t``. An invitation that
        is actually producing turns is live conversation and must not age out
        mid-exchange; the TTL exists for one that never produces a turn at all
        (see ``FloorConfig.invitation_ttl_s``).
        """
        remaining = max(0, self.turns_remaining - 1)
        if t is None:
            return replace(self, turns_remaining=remaining)
        return replace(self, turns_remaining=remaining, last_active_t=t)

    def touched(self, *, t: float) -> Invitation:
        """Record activity without consuming a turn.

        ``spent()`` stamps the clock when a turn *starts*. A turn longer than
        ``FloorConfig.invitation_ttl_s`` therefore left the invitation already
        past its deadline the moment it finished, and the next tick reaped an
        exchange that was plainly still live. Seen on stage 21 Sept 2026: a
        29.9s turn against a 25s TTL, cue 89ms after the audio stopped.
        """
        return replace(self, last_active_t=t)

    def is_live(self) -> bool:
        return self.turns_remaining > 0

    def admits(self, agent_id: str) -> bool:
        return self.agent is None or self.agent == agent_id

    def precedence(self) -> int:
        """How specific this invitation is. Higher wins a collision."""
        return _INVITATION_PRECEDENCE[self.source]

    def expires_at(self, ttl_s: float) -> float | None:
        """When this invitation goes stale, or None if it never does.

        Measured from the last activity, falling back to when the invitation
        was made if it has never produced a turn — which is the case the TTL
        exists for.

        The introduction round is exempt: it is bounded by the cast size and
        cutting it short strands agents who have not spoken yet.
        """
        if self.source is InvitationSource.INTRODUCTION:
            return None
        return (self.t if self.last_active_t is None else self.last_active_t) + ttl_s


@dataclass(frozen=True, slots=True)
class Proposal:
    agent: str
    utterance: str
    signals: Signals
    # When the finished line *arrived*. Not what decides whether it is a stale
    # answer — see `input_t` and `written_against_t`.
    t: float
    # The `speculation_epoch` this was generated against. Only the newest
    # generation per agent is kept in `PanelState.proposals`; see
    # `FloorController._proposal` for why arrival order cannot be trusted.
    epoch: int = 0
    # The transcript timestamp this generation's input was frozen at — which
    # question it is an answer to. Carried straight off `AgentProposal.input_t`,
    # where the reasoning lives. None means the emitter supplied none.
    input_t: float | None = None

    @property
    def written_against_t(self) -> float:
        """The moment in the conversation this line is an answer to.

        `FloorController._stale` measures from here, never from `t`. A
        proposal's arrival time says nothing about the age of the question it
        answers: generations race, so the slowest one in a turn finishes last
        while having had the oldest input.

        Falls back to `t` when no `input_t` was supplied, which is the
        behaviour from before the field existed. A missing input time must
        degrade to the old answer, not to a wrong one.
        """
        return self.t if self.input_t is None else self.input_t


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    id: str
    state: AgentState = AgentState.IDLE
    last_spoke_at: float | None = None
    speaking_since: float | None = None
    muted: bool = False


@dataclass(frozen=True, slots=True)
class Utterance:
    speaker: str
    text: str
    t: float


@dataclass(frozen=True, slots=True)
class PanelState:
    """The whole world, as the floor controller sees it."""

    agents: dict[str, AgentRuntime] = field(default_factory=dict)
    proposals: dict[str, Proposal] = field(default_factory=dict)

    floor_holder: str | None = None  # agent id, HUMAN, or None (open)
    speaking: str | None = None  # agent id currently producing audio
    # The floor is closed unless this is set. See FEASIBILITY.md 3.4 for who
    # decides that it opens (the classifier, or the regex) versus who wins it
    # once open (scoring), and CLAUDE.md "Floor closed by default".
    invitation: Invitation | None = None
    # Agents that tied for "the one Ricky addressed", when the utterance named
    # more than one in the same grammatical role. Ambiguity is an outcome, not
    # an error: the floor stays CLOSED and the tie is surfaced to the operator
    # rather than guessed at. A missed invitation costs one beat; a wrong one
    # puts an agent on the PA over the moderator.
    address_conflict: tuple[str, ...] = ()
    human_speaking: bool = False

    transcript: tuple[Utterance, ...] = ()
    partial: str = ""

    # The speaking agent's turn so far — the agent-side counterpart of
    # ``partial``, and a live partial in exactly the same sense: text the room
    # is in the middle of hearing, not yet part of the record. It becomes an
    # ``Utterance`` in ``transcript`` when ``AgentSpeechEnded`` lands, which is
    # why nothing here is ever appended to ``transcript`` and why this must be
    # cleared at both ends of a turn or the next turn would double-count it.
    #
    # Whose it is is not stored, because ``speaking`` already answers that and
    # only one agent is ever on the PA. Both are cleared together.
    agent_partial: str = ""

    # Set while an agent is ducked pending backchannel classification.
    ducked_agent: str | None = None
    human_speech_started_at: float | None = None

    # When audio was last observed leaving the agent in `speaking`, or None if
    # none has been observed for this turn yet. Written only by
    # `FloorController._agent_audio_progress` (from `AgentAudioProgress`) and
    # reset to None by `_agent_started`, so it always describes the *current*
    # turn and a late heartbeat from the previous speaker cannot refresh it.
    #
    # None versus a timestamp is what picks the deadline: before any audio has
    # arrived the clock runs from `AgentRuntime.speaking_since` against
    # `FloorConfig.agent_first_audio_timeout_s` (TTS never delivered anything),
    # and afterwards from here against `agent_audio_stall_timeout_s` (it
    # delivered and then stopped). Two failures, two budgets, one field — see
    # `FloorController._stalled_speaker`.
    last_audio_progress_t: float | None = None

    turn_id: int = 0
    consecutive_agent_turns: int = 0
    beat_index: int = 0

    # The transcript timestamp of the most recent `RequestProposals`, i.e. the
    # input every generation in the current round was frozen against. Two jobs,
    # and both are why it may only ever be written by
    # `FloorController._ask_for_proposals`:
    #   * it is the debounce's clock (`FloorConfig.speculation_interval_s`);
    #   * the runtime reads it back out to stamp `AgentProposal.input_t`, which
    #     is what `FloorController._stale` measures.
    last_proposal_request_t: float = -999.0
    # Bumped once per `RequestProposals`, by `_ask_for_proposals` and nowhere
    # else, so `(agent, epoch)` names exactly one generation. That is what lets
    # the runtime start a fresh generation for an agent whose previous one is
    # still running — without it, every speculative generation in one human
    # turn shared a label, the slowest agent could never be re-asked inside its
    # own turn, and so the slower the agent the staler the input its winning
    # line had. See `panel_runtime.panel.PanelRuntime._request_proposals`.
    #
    # It labels generations; it does not judge them. `_stale` does that, off
    # `last_proposal_request_t` above.
    speculation_epoch: int = 0
    killed: bool = False

    # --- the beat before the floor goes back to Ricky ---
    #
    # Set when arbitration found nothing for an agent Ricky named by name.
    # Rather than telling him to fill the silence in the same millisecond his
    # question landed, the floor waits `FloorConfig.invited_agent_grace_s` for
    # the answer that is almost certainly still being written, and only cues him
    # if it never turns up. On stage the difference is a panellist taking a
    # breath versus a panellist who is not there.
    #
    # `moderator_cued` latches for the life of one invitation. Every proposal
    # that lands re-opens arbitration (`PanelRuntime._maybe_rearbitrate`), so a
    # single unanswered question used to cue Ricky once per proposal — three
    # times over, in the run that prompted this. Ricky needs telling once.
    awaiting_agent: str | None = None
    awaiting_since: float | None = None
    moderator_cued: bool = False

    # Agents still owed a turn in the current introduction round, or None if
    # no round is active. `intro_done` latches permanently once the round
    # completes and is never reset — the round may run exactly once per show.
    intro_queue: tuple[str, ...] | None = None
    intro_done: bool = False

    # ---------------------------------------------------------------- helpers

    @classmethod
    def for_agents(cls, agent_ids: tuple[str, ...]) -> PanelState:
        return cls(agents={a: AgentRuntime(id=a) for a in agent_ids})

    def with_agent(self, agent_id: str, **changes: object) -> PanelState:
        current = self.agents[agent_id]
        agents = dict(self.agents)
        agents[agent_id] = replace(current, **changes)  # type: ignore[arg-type]
        return replace(self, agents=agents)

    def without_proposal(self, agent_id: str) -> PanelState:
        proposals = {k: v for k, v in self.proposals.items() if k != agent_id}
        return replace(self, proposals=proposals)

    def with_proposal(self, proposal: Proposal) -> PanelState:
        proposals = dict(self.proposals)
        proposals[proposal.agent] = proposal
        return replace(self, proposals=proposals)

    def cleared_proposals(self) -> PanelState:
        return replace(self, proposals={})

    def proposals_written_since(self, t: float) -> PanelState:
        """Drop proposals answering a moment older than ``t``.

        Measured on ``Proposal.written_against_t``, never on arrival: a line
        that took four seconds to generate is still an answer to the moment it
        was started from. See ``FloorController._agent_ended``, the one caller.
        """
        proposals = {a: p for a, p in self.proposals.items() if p.written_against_t >= t}
        return replace(self, proposals=proposals)

    def not_awaiting(self) -> PanelState:
        """Stand down the beat before the moderator cue.

        Called wherever the wait is over however it ended — the answer arrived,
        someone took the floor, Ricky spoke again, or the cue finally fired.
        """
        return replace(self, awaiting_agent=None, awaiting_since=None)

    def recent_text(self, limit: int = 12) -> str:
        lines = [f"{u.speaker}: {u.text}" for u in self.transcript[-limit:]]
        if self.agent_partial and self.speaking:
            # Ahead of the human's, because an agent only holds the floor when
            # the human is not on it: if both are somehow set, the human is
            # interrupting and is the later event.
            lines.append(f"{self.speaking} (speaking): {self.agent_partial}")
        if self.partial:
            lines.append(f"{HUMAN} (speaking): {self.partial}")
        return "\n".join(lines)

    def idle_agents(self) -> tuple[str, ...]:
        return tuple(
            a.id for a in self.agents.values() if not a.muted and a.state != AgentState.SPEAKING
        )
