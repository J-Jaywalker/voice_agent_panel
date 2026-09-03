"""The floor controller.

A pure reducer: ``reduce(state, event) -> (state, commands)``. No I/O, no
awaits, no clock reads. This is the hardest and most-tuned part of the system,
so it is also the part that must be testable in milliseconds and identical on
every run.

The floor is CLOSED by default. Agents propose continuously — that is what
keeps the post-turn gap short — but a proposal is a raised hand, not a turn.
Nothing reaches a speaker until Ricky opens the floor. A panel where agents
self-elect on every remark is a panel that talks over its moderator.

Floor hierarchy, in strict order:

    1. Human moderator          — absolute, immediate, non-negotiable
    2. Explicitly invited agent — Ricky named them and asked them something
    3. Strongest contextual case, but only within an open invitation
    4. Silence — the default, not the failure case
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
    DuckSpeech,
    Event,
    HandsRaised,
    HumanSpeechEnded,
    HumanSpeechStarted,
    InjectDirective,
    OperatorAction,
    OperatorCommand,
    RequestProposals,
    ResumeSpeech,
    StartSpeech,
    StateChanged,
    StopReason,
    StopSpeech,
    Tick,
    TranscriptUpdated,
    TurnYielded,
)
from .personas import PanelCast
from .scoring import FloorConfig, floor_priority, is_backchannel, may_interrupt
from .state import AgentState, Invitation, InvitationSource, PanelState, Proposal, Utterance

WRAP_UP = "You are running long. Land your point in one more sentence."

# Ricky opened the floor if he asked a question, or handed over explicitly.
# Deliberately conservative: a missed invitation costs one beat and the operator
# can open the floor by hand, whereas a false one puts an agent on a PA over the
# moderator in front of 400 people.
_QUESTION_RE = re.compile(r"\?\s*$")
_HANDOVER_RE = re.compile(
    r"\b(?:over to you|take (?:that|this) one|jump in|go ahead|your thoughts"
    r"|thoughts on that|any thoughts|anyone|anybody|what say you"
    r"|tell (?:me|us) about|let's hear)\b",
    re.IGNORECASE,
)


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
                return self._human_ended(state, event)
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
        """The reflex: duck first, classify after.

        We cannot yet know whether this is a barge-in or a backchannel — the
        transcript is ~300ms behind. Waiting for it costs responsiveness;
        assuming "interrupt" means an agent stops dead every time Ricky says
        "mm-hm", and the panel stutters. So we duck within one audio buffer and
        decide once evidence arrives. Responsiveness never trades against
        correctness. See ADR 0001.
        """
        if state.speaking is None:
            state = replace(
                state,
                human_speaking=True,
                floor_holder=HUMAN,
                consecutive_agent_turns=0,
                human_speech_started_at=event.t,
                proposals={},  # a human turn invalidates speculative candidates
                invitation=None,  # ...and revokes the standing invitation
            )
            return state, [self._paint(state)]

        state = replace(
            state,
            human_speaking=True,
            human_speech_started_at=event.t,
            ducked_agent=state.speaking,
        )
        return state, [
            DuckSpeech(
                agent=state.speaking,
                gain_db=self.config.backchannel_duck_db,
                ramp_ms=self.config.duck_ramp_ms,
            )
        ]

    def _human_ended(
        self, state: PanelState, event: HumanSpeechEnded
    ) -> tuple[PanelState, list[Command]]:
        state = replace(state, human_speaking=False)
        if state.ducked_agent is None:
            return state, []

        started = state.human_speech_started_at
        duration = event.t - started if started is not None else 0.0
        if duration >= self.config.backchannel_max_duration_s:
            return self._commit_human_interrupt(state, t=event.t)
        return self._resume_ducked(state)

    def _commit_human_interrupt(
        self, state: PanelState, *, t: float
    ) -> tuple[PanelState, list[Command]]:
        """Classification resolved: a real barge-in. Stop the agent outright."""
        agent = state.ducked_agent or state.speaking
        commands: list[Command] = []
        if agent is not None:
            commands.append(
                StopSpeech(
                    agent=agent,
                    reason=StopReason.HUMAN_INTERRUPT,
                    overlap_ms=0,  # never talk over Ricky
                    duck_ms=self.config.human_duck_ms,
                )
            )
            state = state.with_agent(
                agent, state=AgentState.IDLE, speaking_since=None, wrap_up_sent=False
            )
        state = replace(
            state,
            speaking=None,
            ducked_agent=None,
            floor_holder=HUMAN,
            consecutive_agent_turns=0,
            proposals={},
            invitation=None,  # Ricky is taking the floor back
        )
        commands.append(self._paint(state))
        return state, commands

    def _resume_ducked(self, state: PanelState) -> tuple[PanelState, list[Command]]:
        """It was only an acknowledgement. Bring the agent back to full gain."""
        agent = state.ducked_agent
        state = replace(state, ducked_agent=None, human_speech_started_at=None)
        if agent is None:
            return state, []
        return state, [ResumeSpeech(agent=agent, ramp_ms=self.config.resume_ramp_ms)]

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

        # Content-based classification: substantive words while an agent is
        # speaking are a barge-in, however briefly they were spoken. This also
        # catches a short-but-real interruption already resumed by _human_ended.
        if (
            event.speaker == HUMAN
            and state.speaking is not None
            and not is_backchannel(text, min_words=self.config.interrupt_min_words)
        ):
            state, interrupt_cmds = self._commit_human_interrupt(state, t=event.t)
            commands.extend(interrupt_cmds)

        # Finals only. A partial can match a pattern the completed sentence
        # does not, and a stale invitation is a live mic on the wrong agent.
        if event.speaker == HUMAN and event.is_final:
            invitation = self._detect_invitation(text, t=event.t)
            if invitation is not None:
                state = replace(state, invitation=invitation)

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
        """End of turn confirmed. Arbitrate — but only if the floor is open."""
        state = replace(state, floor_holder=None, human_speaking=False)

        invitation = state.invitation
        if invitation is None or not invitation.is_live():
            # Ricky made a remark, not an invitation. Agents may want the floor;
            # wanting it is not taking it. Their interest goes to the operator
            # console and the video wall, and he decides.
            return state, [
                *self._hands_raised(state, now=event.t),
                CueModerator(reason="no_invitation"),
                self._paint(state),
            ]

        if state.consecutive_agent_turns >= self.config.max_consecutive_agent_turns:
            return state, [CueModerator(reason="agent_turn_limit"), self._paint(state)]

        winner = self._arbitrate(state, invitation=invitation, now=event.t)
        if winner is None:
            # Invited, but nobody had anything worth the airtime. Silence is a
            # legitimate outcome — cue Ricky so the beat does not hang.
            return state, [CueModerator(reason="no_candidate"), self._paint(state)]

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
        # interruption request. Humans are never interrupted, and an agent may
        # only cut in while the panel legitimately holds the floor — an operator
        # override is not an invitation for everyone else to pile in.
        if (
            state.speaking is not None
            and state.speaking != event.agent
            and state.invitation is not None
        ):
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
        if state.ducked_agent == event.agent:
            state = replace(state, ducked_agent=None)

        if event.utterance:
            state = replace(
                state,
                transcript=state.transcript
                + (Utterance(speaker=event.agent, text=event.utterance, t=event.t),),
            )

        commands: list[Command] = [self._paint(state)]

        if not event.completed:
            # Cut off — whoever interrupted is already being granted the floor.
            return state, commands

        # A completed agent turn does NOT reopen the floor. The panel continues
        # only if the invitation had turns left on it; otherwise it goes back to
        # Ricky, which is what stops three agents relaying to each other.
        state = state.cleared_proposals()

        invitation = state.invitation
        if invitation is None or not invitation.is_live():
            state = replace(state, invitation=None)
            commands.append(CueModerator(reason="invitation_spent"))
            return state, commands

        if state.consecutive_agent_turns >= self.config.max_consecutive_agent_turns:
            state = replace(state, invitation=None)
            commands.append(CueModerator(reason="agent_turn_limit"))
            return state, commands

        targets = state.idle_agents()
        if targets:
            state = replace(state, last_proposal_request_t=event.t)
            commands.append(RequestProposals(agents=targets, reason="agent_turn_ended"))
        return state, commands

    def _tick(self, state: PanelState, event: Tick) -> tuple[PanelState, list[Command]]:
        # Duration-based classification: speech this long is a bid for the
        # floor whatever the words turn out to be.
        started = state.human_speech_started_at
        if (
            state.ducked_agent is not None
            and started is not None
            and event.t - started >= self.config.backchannel_max_duration_s
        ):
            return self._commit_human_interrupt(state, t=event.t)

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
                state = replace(
                    state,
                    killed=True,
                    speaking=None,
                    floor_holder=None,
                    proposals={},
                    invitation=None,
                )
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
                    invitation=None,
                )
                return state, cmds + [self._paint(state)]

            case OperatorAction.OPEN_FLOOR:
                # The backstop for a missed invitation. Ricky phrases something
                # as a statement, the panel stays quiet, the operator opens it.
                state = replace(
                    state,
                    invitation=Invitation(
                        agent=event.agent,
                        turns_remaining=max(1, event.turns),
                        source=InvitationSource.OPERATOR,
                        t=event.t,
                    ),
                )
                return state, [self._paint(state)]

            case OperatorAction.CLOSE_FLOOR:
                return replace(state, invitation=None), [self._paint(state)]

            case OperatorAction.ADVANCE_BEAT:
                state = replace(state, beat_index=state.beat_index + 1, proposals={})
                return state, [self._paint(state)]

            case _:
                return state, []

    # ---------------------------------------------------------------- helpers

    def _eligible(self, state: PanelState, invitation: Invitation | None) -> dict[str, Proposal]:
        return {
            a: p
            for a, p in state.proposals.items()
            if not state.agents[a].muted
            and state.agents[a].state is not AgentState.SPEAKING
            and (invitation is None or invitation.admits(a))
        }

    def _score(self, state: PanelState, agent_id: str, proposal: Proposal, *, now: float) -> float:
        return floor_priority(
            proposal.signals,
            self.cast[agent_id],
            now=now,
            last_spoke_at=state.agents[agent_id].last_spoke_at,
            config=self.config,
        )

    def _arbitrate(self, state: PanelState, *, invitation: Invitation, now: float) -> str | None:
        """Pick a winner from within the invitation, or None for silence."""
        candidates = self._eligible(state, invitation)
        if not candidates:
            return None

        # Ricky named them. They answer. The score floor exists so that silence
        # can win an *open* invitation — it has no business overruling a direct
        # question put to a specific panellist.
        if invitation.agent is not None:
            return invitation.agent if invitation.agent in candidates else None

        scored = sorted(
            ((self._score(state, a, p, now=now), a) for a, p in candidates.items()),
            reverse=True,
        )
        best_score, best_agent = scored[0]
        if best_score < self.config.min_floor_priority:
            return None

        # An agent may hand off to a better-placed colleague.
        defer_to = candidates[best_agent].signals.defer_to
        if defer_to and defer_to in candidates:
            return defer_to
        return best_agent

    def _hands_raised(self, state: PanelState, *, now: float) -> list[Command]:
        """Surface interest the panel is not allowed to act on."""
        candidates = self._eligible(state, None)
        if not candidates:
            return []
        scored = sorted(
            ((self._score(state, a, p, now=now), a) for a, p in candidates.items()),
            reverse=True,
        )
        return [HandsRaised(agents=tuple((a, round(sc, 3)) for sc, a in scored))]

    def _detect_invitation(self, text: str, *, t: float) -> Invitation | None:
        """Did Ricky actually open the floor?

        A statement invites nobody, however interesting it is — that is the
        whole rule, and it is one a moderator can hold in his head on stage:
        *ask a question and the panel answers; make a point and they let you
        make it.* Naming an agent narrows the invitation to them.
        """
        if not (_QUESTION_RE.search(text) or _HANDOVER_RE.search(text)):
            return None
        agent = self._detect_address(text)
        if agent is not None:
            return Invitation(
                agent=agent,
                turns_remaining=self.config.address_invitation_turns,
                source=InvitationSource.ADDRESS,
                t=t,
            )
        return Invitation(
            agent=None,
            turns_remaining=self.config.open_invitation_turns,
            source=InvitationSource.OPEN,
            t=t,
        )

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
            consecutive_agent_turns=state.consecutive_agent_turns + 1,
            invitation=state.invitation.spent() if state.invitation else None,
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
                "invited": state.invitation.agent if state.invitation else None,
                "invitation_turns": state.invitation.turns_remaining if state.invitation else 0,
                "consecutive_agent_turns": state.consecutive_agent_turns,
                "killed": state.killed,
            },
        )
