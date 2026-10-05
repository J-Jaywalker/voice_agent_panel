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
    AGENT = "agent"  # a panellist handed the floor to a colleague on their way out


# How specific an invitation is. A vaguer invitation must never quietly
# displace a more specific one: "Melia, can you continue? ... is that okay?"
# arrives as two segments, and the second must not downgrade the first from
# "Melia answers" to "whoever scores best answers". Compared in
# `FloorController._transcript`; see also `invitation_supersede_window_s`.
_INVITATION_PRECEDENCE: dict[InvitationSource, int] = {
    # Lowest of all: the panel inviting itself must never hold off anything
    # Ricky said, including a fresh open floor.
    InvitationSource.AGENT: 0,
    InvitationSource.OPEN: 1,
    InvitationSource.ADDRESS: 2,
    InvitationSource.OPERATOR: 2,  # a deliberate human act, as specific as a name
    InvitationSource.INTRODUCTION: 3,  # a bounded round; nothing may cut across it
}


@dataclass(frozen=True, slots=True)
class Invitation:
    """Permission for some agents — or the panel — to take the floor.

    The floor is closed by default. This is the object that opens it, and it is
    spent as it is used. Without one, ``TurnYielded`` returns the floor to the
    moderator and the panel stays quiet however much it wants to speak.

    ``role`` and ``rule`` are provenance, not behaviour: they record *why* the
    floor opened (which grammatical role the addressee held, and which pattern
    matched) so the operator console and the rehearsal log can show the
    reasoning rather than just the outcome.
    """

    # Who the invitation names, in cast order. Empty means the whole panel.
    # One name is a direct question. Two or more is Ricky asking exactly those
    # panellists to take something between them, which is a different thing
    # from either — see ``admits()``, which is where the difference lives.
    agents: tuple[str, ...]
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
    # Agents already granted a turn under this invitation, in the order they
    # spoke. Empty until the first grant. This is what lets a named
    # invitation open up to the rest of the panel once its addressee has
    # actually answered (see ``admits()``), and what ``FloorController._grant``
    # checks to close the invitation the instant every live agent has had a
    # turn, rather than waiting on ``turns_remaining`` to happen to reach
    # zero. CLAUDE.md: "every agent chips in once per prompt."
    spoken: tuple[str, ...] = ()

    def spent(self, *, t: float | None = None, agent_id: str | None = None) -> Invitation:
        """Consume one turn, and record who took it.

        ``t`` refreshes the *activity* clock, not ``self.t``. An invitation that
        is actually producing turns is live conversation and must not age out
        mid-exchange; the TTL exists for one that never produces a turn at all
        (see ``FloorConfig.invitation_ttl_s``).

        ``agent_id`` is who was just granted the floor. It is appended to
        ``spoken`` (once — a repeat grant, which should not happen, does not
        duplicate the entry) so ``admits()`` and the "everyone's had a turn"
        check both see it. ``None`` is the introduction round's fixed-text
        grants, which do not participate in this bookkeeping at all.
        """
        remaining = max(0, self.turns_remaining - 1)
        spoken = (
            self.spoken
            if agent_id is None or agent_id in self.spoken
            else self.spoken + (agent_id,)
        )
        if t is None:
            return replace(self, turns_remaining=remaining, spoken=spoken)
        return replace(self, turns_remaining=remaining, last_active_t=t, spoken=spoken)

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

    @property
    def agent(self) -> str | None:
        """The sole addressee, or None.

        None covers two different invitations — an open floor and a named
        *group* — so nothing may read this to mean "open". Code that has to
        tell those apart reads ``agents`` directly; this exists because the
        one-name case is still by far the commonest and "the agent Ricky
        named" is the question most callers are actually asking.
        """
        return self.agents[0] if len(self.agents) == 1 else None

    @property
    def is_group(self) -> bool:
        """Did Ricky name more than one panellist, and not the whole panel?"""
        return len(self.agents) > 1

    def admits(self, agent_id: str) -> bool:
        """May this agent be granted the floor under this invitation, next?

        Three shapes of invitation, three rules:

        * **Open** (``agents`` empty). Anyone who has not yet spoken under it —
          every agent chips in once before the floor goes back to Ricky.
        * **One name.** The addressee, once, and nobody else — the rest of the
          panel is *excluded*, not merely deprioritised, so an unaddressed
          agent can never take the floor off the back of someone else's
          question, which is indistinguishable on stage from an agent cutting
          in. And the addressee is excluded too once they have answered: a
          direct question wants one answer, not the same agent re-winning
          arbitration against nobody turn after turn because he is the only
          candidate left standing. That was tried — admitting him
          indefinitely — and the only thing that stopped a solo address
          running away was `address_invitation_turns` happening to run out,
          four turns and about ninety seconds into what should have been one.
          A colleague may still be handed the floor explicitly
          (``Signals.defer_to`` / ``invites_next``, which install their own
          invitation and do not go through this check); nobody may take it by
          simply out-scoring the agent Ricky actually asked.
        * **Several names.** Those panellists and nobody else, with no
          once-each limit: Ricky naming two of them is Ricky asking those two
          to take it between them, so alternating turns is the point of the
          invitation rather than an overrun. ``recency_penalty`` in the
          scoring and ``FloorConfig.max_consecutive_agent_turns`` are what
          keep that exchange from running away instead.
        """
        if self.is_group:
            return agent_id in self.agents
        if self.agents:
            return agent_id == self.agent and agent_id not in self.spoken
        return agent_id not in self.spoken

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
    # The colleague the agent currently on the PA invited to follow it, or
    # None. Written by `FloorController._grant` from the winning proposal's
    # `Signals.invites_next` (validated there), read and cleared by
    # `_agent_ended`, and cleared by every path that takes the floor back off
    # an agent. Defaults to None so a log recorded before this existed replays
    # identically.
    pending_invite: str | None = None
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

    # Whose voice opened the *current* duck, as far as speaker identification
    # has established it. Three states, and the third is the whole reason this
    # is not a bool:
    #
    #   None  — no evidence yet. The transcript is ~300ms behind the VAD, so
    #           this is the ordinary state for the first fraction of a second
    #           of every duck, including every one of Ricky's own interrupts.
    #   True  — at least one segment in this duck was identified as the
    #           enrolled moderator. Latches: room noise arriving after Ricky
    #           has been confirmed must not downgrade him.
    #   False — segments arrived and every one of them was somebody else.
    #
    # Only `False` changes any outcome, and it only ever *blocks* a stop (see
    # `FloorController._human_ended` and `_tick`). `None` behaves exactly as
    # this field's absence did, which is what keeps Ricky's own barge-in
    # latency unchanged: confirmation is never a precondition for stopping an
    # agent, because requiring it would put a network round-trip in the
    # interrupt path that CLAUDE.md keeps out of it.
    #
    # Reset to None wherever a duck begins or ends, so it always describes the
    # duck in progress and a previous duck's verdict can never be read against
    # this one.
    duck_confirmed: bool | None = None

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

    # `Proposal.written_against_t` of the line currently being spoken, or None
    # for a turn that came from fixed text (introductions) or from no proposal
    # at all. Written by `FloorController._grant`, read once by `_agent_ended`,
    # cleared there.
    #
    # It is the cutoff that decides which *other* proposals survive the turn,
    # and it exists because `speaking_since` was the wrong clock for that job.
    # The round that produces the grant also produces the other two agents'
    # lines, and it necessarily opens *before* the winner reaches the PA —
    # arbitration and TTS first-audio sit in between. Measured from
    # `speaking_since`, those siblings were therefore always "written before
    # this turn" and always discarded: on the run that found this (1 Oct 2026)
    # a full set of proposals missed the cutoff by 110ms, the panel sat on them
    # for 29 seconds and then paid a fresh 1.6s generation at the boundary.
    # Measured from here they are what they are — lines written against the
    # same moment as the one that just aired, and the fallback when nothing
    # fresher exists. Anything genuinely older still goes.
    turn_input_t: float | None = None

    # When the last *mid-turn* speculation round opened, or None if none has
    # opened in the current turn. Reset by `_agent_started`/`_agent_ended`, so
    # it only ever describes the turn in progress, and deliberately separate
    # from `last_proposal_request_t` below — see
    # `FloorController._agent_utterance_progress`.
    agent_turn_request_t: float | None = None

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
    # Set when arbitration found nothing for the agents Ricky named by name.
    # Rather than telling him to fill the silence in the same millisecond his
    # question landed, the floor waits `FloorConfig.invited_agent_grace_s` for
    # the answer that is almost certainly still being written, and only cues him
    # if it never turns up. On stage the difference is a panellist taking a
    # breath versus a panellist who is not there.
    #
    # A tuple, not one id, because Ricky can name two: "Melia and Wayne, take
    # that between you" is one invitation whose beat is held for either of
    # them, and a line from *either* stands the cue down (`_proposal`). Empty
    # means no beat is being held.
    #
    # `moderator_cued` latches for the life of one invitation. Every proposal
    # that lands re-opens arbitration (`PanelRuntime._maybe_rearbitrate`), so a
    # single unanswered question used to cue Ricky once per proposal — three
    # times over, in the run that prompted this. Ricky needs telling once.
    awaiting_agents: tuple[str, ...] = ()
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
        return replace(self, awaiting_agents=(), awaiting_since=None)

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
