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


@dataclass(frozen=True, slots=True)
class Invitation:
    """Permission for an agent — or the panel — to take the floor.

    The floor is closed by default. This is the object that opens it, and it is
    spent as it is used. Without one, ``TurnYielded`` returns the floor to the
    moderator and the panel stays quiet however much it wants to speak.
    """

    agent: str | None  # None = open to the whole panel
    turns_remaining: int
    source: InvitationSource
    t: float

    def spent(self) -> Invitation:
        return replace(self, turns_remaining=max(0, self.turns_remaining - 1))

    def is_live(self) -> bool:
        return self.turns_remaining > 0

    def admits(self, agent_id: str) -> bool:
        return self.agent is None or self.agent == agent_id


@dataclass(frozen=True, slots=True)
class Proposal:
    agent: str
    utterance: str
    signals: Signals
    t: float


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    id: str
    state: AgentState = AgentState.IDLE
    last_spoke_at: float | None = None
    speaking_since: float | None = None
    wrap_up_sent: bool = False
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
    # The floor is closed unless this is set. See FEASIBILITY.md 3.8.
    invitation: Invitation | None = None
    human_speaking: bool = False

    transcript: tuple[Utterance, ...] = ()
    partial: str = ""

    # Set while an agent is ducked pending backchannel classification.
    ducked_agent: str | None = None
    human_speech_started_at: float | None = None

    turn_id: int = 0
    consecutive_agent_turns: int = 0
    beat_index: int = 0

    last_proposal_request_t: float = -999.0
    killed: bool = False

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

    def recent_text(self, limit: int = 12) -> str:
        lines = [f"{u.speaker}: {u.text}" for u in self.transcript[-limit:]]
        if self.partial:
            lines.append(f"{HUMAN} (speaking): {self.partial}")
        return "\n".join(lines)

    def idle_agents(self) -> tuple[str, ...]:
        return tuple(
            a.id for a in self.agents.values() if not a.muted and a.state != AgentState.SPEAKING
        )
