"""Events and commands for the panel orchestrator.

Events are things that happened. Commands are things the runtime must do.

Nothing here performs I/O or reads the clock: every event carries its own
timestamp (monotonic seconds, supplied by the runtime). That is what makes a
recorded session replay identically through modified floor logic — see
FEASIBILITY.md section 5.3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

HUMAN = "human"


# --------------------------------------------------------------------------
# Signals — an agent's own account of why it wants the floor
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signals:
    """An agent's self-reported reasons for wanting to speak.

    Produced by the agent's LLM call alongside the candidate utterance, so a
    proposal costs one request rather than two. All values are 0.0-1.0.
    """

    relevance: float = 0.0
    urgency: float = 0.0
    disagreement: float = 0.0
    confidence: float = 0.0
    expertise: float = 0.0
    novelty: float = 0.0

    # Who this contribution is aimed at: an agent id, HUMAN, or None for the room.
    responding_to: str | None = None
    # Agent id this agent thinks is better placed to answer — drives handoffs.
    defer_to: str | None = None


# --------------------------------------------------------------------------
# Events (inbound)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HumanSpeechStarted:
    """VAD detected energy on a human mic channel. The barge-in reflex."""

    t: float
    speaker: str = HUMAN


@dataclass(frozen=True, slots=True)
class HumanSpeechEnded:
    """VAD detected silence. NOT end-of-turn — that is TurnYielded."""

    t: float
    speaker: str = HUMAN


@dataclass(frozen=True, slots=True)
class TranscriptUpdated:
    """A partial or final transcript segment from Speechmatics."""

    t: float
    speaker: str
    text: str
    is_final: bool


@dataclass(frozen=True, slots=True)
class AddressDetected:
    """Who the moderator just invited, as decided by a model not a regex.

    The classifier lives in the runtime (`panel_runtime.address`) because it
    makes a network call and `panel_core` may not. Its answer arrives back here
    as an *event*, which is the whole point: the reducer stays a pure function
    of events, so a recorded session still replays identically through modified
    floor logic (FEASIBILITY.md 5.3) — the verdict is replayed, never re-asked.

    Only read when `FloorConfig.llm_address_detection` is on. With the flag off
    the regex path in `FloorController._transcript` owns the decision and this
    event is ignored, so one recording replays cleanly both ways and neither
    run applies two detections to the same final.

    Attributes:
        t: Timestamp of the *final transcript segment* the verdict was computed
            from, not of the verdict's arrival. The invitation is dated to the
            question, which keeps `named_proposal_lookback_s` and the
            invitation TTL measuring exactly what they measure on the regex
            path.
        text: The final it was computed from, verbatim, so a replay can be
            audited against the verdict it produced.
        verdict: A verdict token from `prompts.address_verdicts()`, or None
            meaning the classifier was unavailable — timed out, errored, or
            went off-script — and the regex must decide instead. None is not
            "nobody was invited"; that is `NO_VERDICT`, which is a real answer.
        agent: The agent id `verdict` resolved to, or None.
        conflict: The agents that tied for addressee, for `AMBIGUOUS_VERDICT`.
            The verdict token cannot name them — it is one word — so the
            runtime supplies the candidates it could not choose between.
        reason: The model's own short justification, for the operator console.
            Routinely empty: it streams *behind* the verdict and the floor
            never waits on prose, so only a verdict that was already cached
            carries one.
        latency_ms: Time to the verdict token, not to completion. Zero on a
            cache hit, because the decision predates the question.
        source: How the verdict was obtained — "speculative_hit", "joined",
            "fresh", "recomputed", "unavailable" or "timeout". The on-stage
            cache-hit rate is read off this field, and it is currently the
            main open question about the whole approach.
    """

    t: float
    text: str
    verdict: str | None = None
    agent: str | None = None
    conflict: tuple[str, ...] = ()
    reason: str = ""
    latency_ms: float = 0.0
    source: str = ""


@dataclass(frozen=True, slots=True)
class TurnYielded:
    """End-of-turn confirmed. The floor is open for arbitration."""

    t: float


@dataclass(frozen=True, slots=True)
class AgentProposal:
    """A speculative candidate turn from one agent.

    Produced during the human's turn (see FEASIBILITY.md 3.6) so the floor
    controller already holds scored candidates when TurnYielded fires.

    ``epoch`` is the ``PanelState.speculation_epoch`` the generation was started
    against — which round of speculation this is an answer to. Several
    generations for one agent now run at the same time (every ``RequestProposals``
    starts a fresh one without killing the ones already running), so proposals
    for the same agent can arrive out of order and the reducer needs to tell
    "newer" from "older" rather than trusting arrival order. See
    ``FloorController._proposal``.

    Attributes:
        t: When the finished proposal *arrived*. Useful for the log and for
            nothing else in the freshness decision — see ``input_t``.
        agent: Which agent wrote it.
        utterance: The candidate turn. Always ``""`` in the live runtime, where
            the words live in a ``panel_runtime.panel.Candidate`` still being
            filled by the stream; non-empty in ``panel_sim``.
        signals: The agent's own account of why it wants the floor.
        epoch: The generation label — see above.
        input_t: The transcript timestamp this generation's input was frozen
            at, i.e. *what question it is an answer to*. Set by the emitter
            from ``PanelState.last_proposal_request_t``, which
            ``FloorController._ask_for_proposals`` stamped in the same
            ``reduce()`` call that asked for this round.

            This, not ``t``, is what ``FloorController._stale`` measures.
            Arrival time is the wrong clock and measuring it was a live-stage
            bug: generations race, so a line written against Ricky's preamble
            3.2s before his question can finish 0.77s *after* the invitation
            and score as maximally fresh. The freshness of an answer is a
            property of its input, not of when it turned up.

            ``None`` means "no emitter supplied one", and
            ``state.Proposal.written_against_t`` then falls back to ``t`` —
            the behaviour from before this field existed, kept so an older
            recorded log still replays and so a future emitter that forgets
            the field gets the old answer rather than a wrong one. Every
            emitter in this repo supplies it.
    """

    t: float
    agent: str
    utterance: str
    signals: Signals
    epoch: int = 0
    input_t: float | None = None


@dataclass(frozen=True, slots=True)
class AgentSpeechStarted:
    """An agent's audio has begun."""

    t: float
    agent: str


@dataclass(frozen=True, slots=True)
class AgentAudioProgress:
    """Sound is still coming out of a speaking agent. The liveness heartbeat.

    Emitted periodically by the runtime for whichever agent is on the PA, and
    only on *evidence*: new audio arrived from the provider, or the mixer still
    has buffered samples left to play. It is deliberately not a "the speak task
    is alive" ping — a task blocked forever on a queue nobody will close is
    alive in that sense, and is exactly the failure this exists to catch.

    What the reducer does with the absence of these is `_stalled_speaker`. Note
    what it is *not*: a turn-length ceiling. Mid-turn steering was cut on
    11 Sept 2026 and turn length is a prompt instruction with no orchestrator
    ceiling (CLAUDE.md) — so the watchdog this feeds measures silence, never
    duration. A long turn that keeps producing audio is never touched, however
    long it runs.
    """

    t: float
    agent: str


@dataclass(frozen=True, slots=True)
class AgentSpeechEnded:
    """An agent's turn is over, carrying what it actually said.

    This is how an agent turn enters conversation state. Agent speech never
    goes near STT — we generated it, so we know it verbatim (CLAUDE.md).
    Recording it here rather than at the start means an interrupted turn
    records the words that were spoken, not the ones that were planned.
    """

    t: float
    agent: str
    completed: bool  # False when cut off by an interrupt or the turn limit
    utterance: str = ""


@dataclass(frozen=True, slots=True)
class Tick:
    """Periodic clock pulse. Drives turn-length deadlines and cooldowns."""

    t: float


class OperatorAction(str, Enum):
    FORCE_AGENT = "force_agent"  # give this agent the floor now
    MUTE_AGENT = "mute_agent"
    UNMUTE_AGENT = "unmute_agent"
    KILL_ALL = "kill_all"  # emergency: silence everything
    RELEASE_KILL = "release_kill"
    ADVANCE_BEAT = "advance_beat"
    HAND_TO_MODERATOR = "hand_to_moderator"
    OPEN_FLOOR = "open_floor"  # invite an agent, or the panel if agent is None
    CLOSE_FLOOR = "close_floor"  # revoke a live invitation


@dataclass(frozen=True, slots=True)
class OperatorCommand:
    t: float
    action: OperatorAction
    agent: str | None = None
    turns: int = 1  # OPEN_FLOOR only: how many turns the invitation is good for


Event = (
    HumanSpeechStarted
    | HumanSpeechEnded
    | TranscriptUpdated
    | AddressDetected
    | TurnYielded
    | AgentProposal
    | AgentSpeechStarted
    | AgentAudioProgress
    | AgentSpeechEnded
    | Tick
    | OperatorCommand
)


# --------------------------------------------------------------------------
# Commands (outbound)
# --------------------------------------------------------------------------


class StopReason(str, Enum):
    HUMAN_INTERRUPT = "human_interrupt"
    OPERATOR = "operator"
    KILL = "kill"
    # The agent held the floor but stopped producing audio. Not an interrupt
    # and not a length limit: something downstream broke and the turn is being
    # taken off a speaker who is no longer speaking. See `AgentAudioProgress`.
    STALLED = "stalled"


@dataclass(frozen=True, slots=True)
class StartSpeech:
    """Grant an agent the floor.

    ``lead_in_s`` is a silent beat the runtime waits out before the agent's
    first word — zero for an ordinary grant, non-zero only for the
    introduction round (`FloorController._grant_introduction`), where an
    instantaneous jump from Ricky's cue to Dexter's first word read as a
    glitch rather than a person taking a breath. Advisory only: this is data
    on a command, not a clock read or an await, so the reducer stays pure
    (CLAUDE.md `panel_core` invariants) — the runtime decides how to honour
    it.
    """

    agent: str
    utterance: str
    turn_id: int
    lead_in_s: float = 0.0
    # Which generation's words to speak. Several generations per agent can be
    # in flight at once, each with its own half-written utterance parked in the
    # runtime, so naming the agent is no longer enough to identify the text —
    # this is the `AgentProposal.epoch` of the proposal that actually won.
    # Ignored for a fixed line (`utterance` non-empty), which has no generation.
    epoch: int = 0


@dataclass(frozen=True, slots=True)
class StopSpeech:
    """Stop an agent mid-utterance.

    Always an immediate stop. There was once an ``overlap_ms`` here, letting an
    interrupting voice ride briefly over the interrupted one; it only ever
    applied to agent-interrupts-agent, the live runtime never honoured it, and
    both were removed on 21 Sept 2026. When Ricky speaks, agents get out of the
    way immediately — which is the only interrupt this command now serves.
    """

    agent: str
    reason: StopReason
    duck_ms: int = 150


@dataclass(frozen=True, slots=True)
class DuckSpeech:
    """Attenuate a speaking agent without stopping it.

    The fast reflex. Emitted within one audio buffer of VAD onset, *before* we
    know whether the human is interrupting or just backchannelling. A human
    panellist does exactly this: drops their volume when someone says "mm-hm",
    and stops only if the other person keeps going.
    """

    agent: str
    gain_db: float
    ramp_ms: int
    reason: str = "human_speech"


@dataclass(frozen=True, slots=True)
class ResumeSpeech:
    """Restore a ducked agent to full gain — it was only a backchannel."""

    agent: str
    ramp_ms: int


@dataclass(frozen=True, slots=True)
class RequestProposals:
    """Ask agents for fresh candidate turns against the current transcript."""

    agents: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class HandsRaised:
    """Agents want the floor but have no invitation to take it.

    The panel does not get to act on this — Ricky does. It goes to the operator
    console and the video wall so a raised hand is *visible* rather than
    self-served: the audience sees three agents with something to say, and the
    moderator chooses. That visibility is the comprehension infrastructure the
    video wall exists for (FEASIBILITY.md 4.6).
    """

    agents: tuple[tuple[str, float], ...]  # (agent_id, floor_priority), best first


@dataclass(frozen=True, slots=True)
class CueModerator:
    """Signal the operator/video wall that the floor should return to Ricky."""

    reason: str


@dataclass(frozen=True, slots=True)
class StateChanged:
    """Emitted whenever the video wall / operator console needs a repaint."""

    floor_holder: str | None
    speaking: str | None
    turn_id: int
    extra: dict[str, object] = field(default_factory=dict)


Command = (
    StartSpeech
    | StopSpeech
    | DuckSpeech
    | ResumeSpeech
    | RequestProposals
    | HandsRaised
    | CueModerator
    | StateChanged
)
