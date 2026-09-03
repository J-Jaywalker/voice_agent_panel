"""The floor controller.

A pure reducer: ``reduce(state, event) -> (state, commands)``. No I/O, no
awaits, no clock reads. This is the hardest and most-tuned part of the system,
so it is also the part that must be testable in milliseconds and identical on
every run.

Floor hierarchy, in strict order:

    1. Human moderator          — absolute, immediate, non-negotiable
    2. Explicitly invited agent — Ricky named them
    3. Strongest contextual case
    4. Everyone else
"""

from __future__ import annotations

import re
from dataclasses import replace

from .events import (
    HUMAN,
    AgentProposal,
    AgentSpeechEnded,
    AgentSpeechStarted,
    Command,
    CueModerator,
    Event,
    HumanSpeechEnded,
    HumanSpeechStarted,
    InjectDirective,
    OperatorAction,
    OperatorCommand,
    RequestProposals,
    StartSpeech,
    StateChanged,
    StopReason,
    StopSpeech,
    Tick,
    TranscriptUpdated,
    TurnYielded,
)
from .personas import PanelCast
from .scoring import FloorConfig, floor_priority, may_interrupt
from .state import AgentState, PanelState, Proposal, Utterance

WRAP_UP = "You are running long. Land your point in one more sentence."


class FloorController:
    """Holds the cast and config; the state itself is passed in and out."""

    def __init__(self, cast: PanelCast, config: FloorConfig | None = None) -> None:
        self.cast = cast
        self.config = config or FloorConfig()
        self._address_patterns = {
            agent_id: re.compile(
                r"\b(" + "|".join(re.escape(a) for a in persona.aliases()) + r")\b",
                re.IGNORECASE,
            )
            for agent_id, persona in cast.personas.items()
        }

    # ------------------------------------------------------------------ entry

    def reduce(self, state: PanelState, event: Event) -> tuple[PanelState, list[Command]]:
        if state.killed and not (
            isinstance(event, OperatorCommand) and event.action is OperatorAction.RELEASE_KILL
        ):
            return state, []

        match event:
            case HumanSpeechStarted():
                return self._human_started(state, event)
            case HumanSpeechEnded():
                return replace(state, human_speaking=False), []
            case TranscriptUpdated():
                return self._transcript(state, event)
            case TurnYielded():
                return self._turn_yielded(state, event)
            case AgentProposal():
                return self._proposal(state, event)
            case AgentSpeechStarted():
                return self._agent_started(state, event)
            case AgentSpeechEnded():
                return self._agent_ended(state, event)
            case Tick():
                return self._tick(state, event)
            case OperatorCommand():
                return self._operator(state, event)
            case _:
                return state, []

    # --------------------------------------------------------------- handlers

    def _human_started(
        self, state: PanelState, event: HumanSpeechStarted
    ) -> tuple[PanelState, list[Command]]:
        """The barge-in reflex. Nothing outranks this."""
        commands: list[Command] = []
        if state.speaking is not None:
            commands.append(
                StopSpeech(
                    agent=state.speaking,
                    reason=StopReason.HUMAN_INTERRUPT,
                    overlap_ms=0,  # never talk over Ricky
                    duck_ms=self.config.human_duck_ms,
                )
            )
            state = state.with_agent(
                state.speaking, state=AgentState.IDLE, speaking_since=None, wrap_up_sent=False
            )

        state = replace(
            state,
            human_speaking=True,
            speaking=None,
            floor_holder=HUMAN,
            consecutive_agent_turns=0,
            # A human turn invalidates every speculative candidate.
            proposals={},
        )
        commands.append(self._paint(state))
        return state, commands

    def _transcript(
        self, state: PanelState, event: TranscriptUpdated
    ) -> tuple[PanelState, list[Command]]:
        commands: list[Command] = []

        if event.is_final:
            state = replace(
                state,
                transcript=state.transcript
                + (Utterance(speaker=event.speaker, text=event.text, t=event.t),),
                partial="",
            )
            text = event.text
        else:
            state = replace(state, partial=event.text)
            text = event.text

        if event.speaker == HUMAN:
            addressed = self._detect_address(text)
            if addressed:
                state = replace(state, addressed_agent=addressed)

        # Speculate during the human's turn so the gap after end-of-turn is
        # TTS latency only (FEASIBILITY.md 3.6). Debounced here rather than in
        # the runtime so the behaviour stays pure and testable.
        #
        # The debounce throttles *partials* only. A final segment must always
        # ask, or a turn that lands inside the debounce window gets no
        # candidates at all and the panel falls silent.
        due = event.t - state.last_proposal_request_t >= self.config.speculation_interval_s
        if event.is_final or due:
            targets = state.idle_agents()
            if targets:
                state = replace(state, last_proposal_request_t=event.t)
                commands.append(
                    RequestProposals(
                        agents=targets, reason="final" if event.is_final else "speculation"
                    )
                )

        return state, commands

    def _turn_yielded(
        self, state: PanelState, event: TurnYielded
    ) -> tuple[PanelState, list[Command]]:
        """End of turn confirmed: arbitrate the open floor."""
        state = replace(state, floor_holder=None, human_speaking=False)

        if state.consecutive_agent_turns >= self.config.max_consecutive_agent_turns:
            return state, [CueModerator(reason="agent_turn_limit"), self._paint(state)]

        winner = self._arbitrate(state, now=event.t)
        if winner is None:
            # Silence is a legitimate outcome. The moderator carries on.
            return state, [self._paint(state)]

        return self._grant(state, winner, now=event.t)

    def _proposal(
        self, state: PanelState, event: AgentProposal
    ) -> tuple[PanelState, list[Command]]:
        agent = state.agents[event.agent]
        if agent.muted or agent.state is AgentState.SPEAKING:
            return state, []

        proposal = Proposal(
            agent=event.agent, utterance=event.utterance, signals=event.signals, t=event.t
        )
        state = state.with_proposal(proposal)
        state = state.with_agent(event.agent, state=AgentState.WANTS_FLOOR)

        # A proposal arriving while another *agent* is speaking is an
        # interruption request. Humans are never interrupted.
        if state.speaking is not None and state.speaking != event.agent:
            speaker = state.agents[state.speaking]
            if may_interrupt(
                signals=event.signals,
                persona=self.cast[event.agent],
                now=event.t,
                speaker_started_at=speaker.speaking_since,
                challenger_last_spoke_at=agent.last_spoke_at,
                config=self.config,
            ):
                commands: list[Command] = [
                    StopSpeech(
                        agent=state.speaking,
                        reason=StopReason.AGENT_INTERRUPT,
                        overlap_ms=self.config.interrupt_overlap_ms,
                    )
                ]
                state = state.with_agent(
                    state.speaking, state=AgentState.IDLE, speaking_since=None, wrap_up_sent=False
                )
                state = replace(state, speaking=None)
                granted_state, grant_cmds = self._grant(state, event.agent, now=event.t)
                return granted_state, commands + grant_cmds

        return state, []

    def _agent_started(
        self, state: PanelState, event: AgentSpeechStarted
    ) -> tuple[PanelState, list[Command]]:
        state = state.with_agent(
            event.agent,
            state=AgentState.SPEAKING,
            speaking_since=event.t,
            wrap_up_sent=False,
        )
        state = replace(state, speaking=event.agent, floor_holder=event.agent)
        return state, [self._paint(state)]

    def _agent_ended(
        self, state: PanelState, event: AgentSpeechEnded
    ) -> tuple[PanelState, list[Command]]:
        state = state.with_agent(
            event.agent,
            state=AgentState.IDLE,
            speaking_since=None,
            last_spoke_at=event.t,
            wrap_up_sent=False,
        )
        if state.speaking == event.agent:
            state = replace(state, speaking=None, floor_holder=None)

        commands: list[Command] = [self._paint(state)]

        if not event.completed:
            # Cut off — whoever interrupted is already being granted the floor.
            return state, commands

        # A completed agent turn opens the floor again. Ask for fresh proposals
        # rather than reusing stale ones, so agents respond to what was just said.
        state = state.cleared_proposals()
        if state.consecutive_agent_turns >= self.config.max_consecutive_agent_turns:
            commands.append(CueModerator(reason="agent_turn_limit"))
            return state, commands

        targets = state.idle_agents()
        if targets:
            state = replace(state, last_proposal_request_t=event.t)
            commands.append(RequestProposals(agents=targets, reason="agent_turn_ended"))
        return state, commands

    def _tick(self, state: PanelState, event: Tick) -> tuple[PanelState, list[Command]]:
        if state.speaking is None:
            return state, []

        agent = state.agents[state.speaking]
        persona = self.cast[state.speaking]
        if agent.speaking_since is None:
            return state, []

        elapsed = event.t - agent.speaking_since

        if elapsed >= persona.max_turn_seconds:
            state = state.with_agent(
                state.speaking, state=AgentState.IDLE, speaking_since=None, wrap_up_sent=False
            )
            stopped = state.speaking
            state = replace(state, speaking=None, floor_holder=None)
            return state, [
                StopSpeech(agent=stopped, reason=StopReason.TURN_LIMIT),  # type: ignore[arg-type]
                self._paint(state),
            ]

        if elapsed >= persona.target_turn_seconds and not agent.wrap_up_sent:
            state = state.with_agent(state.speaking, wrap_up_sent=True)
            return state, [InjectDirective(agent=state.speaking, text=WRAP_UP)]  # type: ignore[arg-type]

        return state, []

    def _operator(
        self, state: PanelState, event: OperatorCommand
    ) -> tuple[PanelState, list[Command]]:
        match event.action:
            case OperatorAction.KILL_ALL:
                commands: list[Command] = []
                if state.speaking:
                    commands.append(StopSpeech(agent=state.speaking, reason=StopReason.KILL))
                    state = state.with_agent(
                        state.speaking, state=AgentState.IDLE, speaking_since=None
                    )
                state = replace(state, killed=True, speaking=None, floor_holder=None, proposals={})
                return state, commands + [self._paint(state)]

            case OperatorAction.RELEASE_KILL:
                return replace(state, killed=False), [self._paint(state)]

            case OperatorAction.MUTE_AGENT if event.agent:
                state = state.with_agent(event.agent, muted=True, state=AgentState.MUTED)
                return state.without_proposal(event.agent), [self._paint(state)]

            case OperatorAction.UNMUTE_AGENT if event.agent:
                state = state.with_agent(event.agent, muted=False, state=AgentState.IDLE)
                return state, [self._paint(state)]

            case OperatorAction.FORCE_AGENT if event.agent:
                if state.speaking:
                    state = state.with_agent(
                        state.speaking, state=AgentState.IDLE, speaking_since=None
                    )
                    state = replace(state, speaking=None)
                return self._grant(state, event.agent, now=event.t, forced=True)

            case OperatorAction.HAND_TO_MODERATOR:
                cmds: list[Command] = []
                if state.speaking:
                    cmds.append(StopSpeech(agent=state.speaking, reason=StopReason.OPERATOR))
                    state = state.with_agent(
                        state.speaking, state=AgentState.IDLE, speaking_since=None
                    )
                state = replace(
                    state,
                    speaking=None,
                    floor_holder=HUMAN,
                    consecutive_agent_turns=0,
                    proposals={},
                )
                return state, cmds + [self._paint(state)]

            case OperatorAction.ADVANCE_BEAT:
                state = replace(state, beat_index=state.beat_index + 1, proposals={})
                return state, [self._paint(state)]

            case _:
                return state, []

    # ---------------------------------------------------------------- helpers

    def _arbitrate(self, state: PanelState, *, now: float) -> str | None:
        """Pick a winner from the current proposals, or None for silence."""
        candidates = {
            a: p
            for a, p in state.proposals.items()
            if not state.agents[a].muted and state.agents[a].state is not AgentState.SPEAKING
        }
        if not candidates:
            return None

        # 2. An explicitly invited agent outranks scoring entirely.
        if state.addressed_agent and state.addressed_agent in candidates:
            return state.addressed_agent

        # An agent may hand off to a better-placed colleague.
        scored: list[tuple[float, str]] = []
        for agent_id, proposal in candidates.items():
            persona = self.cast[agent_id]
            score = floor_priority(
                proposal.signals,
                persona,
                now=now,
                last_spoke_at=state.agents[agent_id].last_spoke_at,
                config=self.config,
            )
            scored.append((score, agent_id))

        scored.sort(reverse=True)
        best_score, best_agent = scored[0]
        if best_score < self.config.min_floor_priority:
            return None

        defer_to = candidates[best_agent].signals.defer_to
        if defer_to and defer_to in candidates:
            return defer_to
        return best_agent

    def _grant(
        self, state: PanelState, agent_id: str, *, now: float, forced: bool = False
    ) -> tuple[PanelState, list[Command]]:
        proposal = state.proposals.get(agent_id)
        if proposal is None:
            if not forced:
                return state, []
            # Operator forced an agent with nothing queued — ask for a turn.
            return state, [RequestProposals(agents=(agent_id,), reason="operator_forced")]

        state = state.without_proposal(agent_id)
        state = replace(
            state,
            turn_id=state.turn_id + 1,
            addressed_agent=None,
            consecutive_agent_turns=state.consecutive_agent_turns + 1,
        )
        return state, [
            StartSpeech(agent=agent_id, utterance=proposal.utterance, turn_id=state.turn_id)
        ]

    def _detect_address(self, text: str) -> str | None:
        """Did the human name an agent? Deterministic, no model call."""
        best: tuple[int, str] | None = None
        for agent_id, pattern in self._address_patterns.items():
            match = pattern.search(text)
            if match and (best is None or match.start() < best[0]):
                best = (match.start(), agent_id)
        return best[1] if best else None

    def _paint(self, state: PanelState) -> StateChanged:
        return StateChanged(
            floor_holder=state.floor_holder,
            speaking=state.speaking,
            turn_id=state.turn_id,
            extra={
                "addressed": state.addressed_agent,
                "consecutive_agent_turns": state.consecutive_agent_turns,
                "killed": state.killed,
            },
        )
