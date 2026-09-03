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
class TurnYielded:
    """End-of-turn confirmed. The floor is open for arbitration."""

    t: float


@dataclass(frozen=True, slots=True)
class AgentProposal:
    """A speculative candidate turn from one agent.

    Produced during the human's turn (see FEASIBILITY.md 3.6) so the floor
    controller already holds scored candidates when TurnYielded fires.
    """

    t: float
    agent: str
    utterance: str
    signals: Signals


@dataclass(frozen=True, slots=True)
class AgentSpeechStarted:
    """An agent's audio has begun."""

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
    | TurnYielded
    | AgentProposal
    | AgentSpeechStarted
    | AgentSpeechEnded
    | Tick
    | OperatorCommand
)


# --------------------------------------------------------------------------
# Commands (outbound)
# --------------------------------------------------------------------------


class StopReason(str, Enum):
    HUMAN_INTERRUPT = "human_interrupt"
    AGENT_INTERRUPT = "agent_interrupt"
    TURN_LIMIT = "turn_limit"
    OPERATOR = "operator"
    KILL = "kill"


@dataclass(frozen=True, slots=True)
class StartSpeech:
    agent: str
    utterance: str
    turn_id: int


@dataclass(frozen=True, slots=True)
class StopSpeech:
    """Stop an agent mid-utterance.

    ``overlap_ms`` lets an interrupting voice ride over the interrupted one
    briefly before it ducks — that overlap is what reads as a real argument
    rather than a queue (FEASIBILITY.md 3.7). Zero for a human interrupt: when
    Ricky speaks, agents get out of the way immediately.
    """

    agent: str
    reason: StopReason
    overlap_ms: int = 0
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
class InjectDirective:
    """Mid-turn steer to a speaking agent, e.g. 'wrap up in one sentence'."""

    agent: str
    text: str


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
    | InjectDirective
    | HandsRaised
    | CueModerator
    | StateChanged
)
