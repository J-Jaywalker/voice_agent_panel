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
from dataclasses import dataclass, replace
from enum import Enum

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
from .prompts import sanitise
from .scoring import FloorConfig, floor_priority, is_backchannel, may_interrupt
from .state import AgentState, Invitation, InvitationSource, PanelState, Proposal, Utterance


class CueReason(str, Enum):
    """Why the floor is handing back to the moderator.

    These strings are the operator console's and the video wall's vocabulary,
    so they are an enum rather than scattered literals. The `no_*` family used
    to be a single `no_candidate`, which told a rehearsal that nobody spoke but
    not why — "nobody proposed" and "the one agent Ricky named had nothing" are
    completely different problems and want completely different fixes.
    """

    NO_INVITATION = "no_invitation"  # Ricky made a remark, not a request
    AMBIGUOUS_ADDRESS = "ambiguous_address"  # two agents addressed; we do not guess
    NO_PROPOSALS = "no_proposals"  # the panel had nothing queued at all
    INVITED_AGENT_SILENT = "invited_agent_silent"  # the named agent had nothing
    BELOW_FLOOR = "below_floor"  # proposals existed but lost to silence
    INVITATION_SPENT = "invitation_spent"
    INVITATION_EXPIRED = "invitation_expired"  # TTL reaped one nobody acted on
    AGENT_TURN_LIMIT = "agent_turn_limit"
    INTRODUCTIONS_COMPLETE = "introductions_complete"


class AddressRole(str, Enum):
    """The grammatical role a name occurrence holds in a clause.

    Position is not role. "Sorry, Dexter, can I just interrupt? Uh, Melia, can
    you continue?" names Dexter first, but Dexter is being *dismissed* — the
    leftmost name is the one agent who must not get the floor. So we classify
    each occurrence and take the strongest role, never the earliest.
    """

    SUBJECT_OF_REQUEST = "subject_of_request"  # "can Melia speak", "over to Melia"
    VOCATIVE = "vocative"  # "Melia, can you continue?"
    OBLIQUE = "oblique"  # "sorry for interrupting Dexter" — never an addressee


# How strongly a clause resolves. Combined across clauses and across transcript
# segments by *precedence*, never by recency: an addressee named in the first
# half of a turn is not undone by a vaguer question in the second half.
_STRENGTH_SUBJECT = 3
_STRENGTH_VOCATIVE = 2
_STRENGTH_OPEN = 1

_ROLE_STRENGTH: dict[AddressRole, int] = {
    AddressRole.SUBJECT_OF_REQUEST: _STRENGTH_SUBJECT,
    AddressRole.VOCATIVE: _STRENGTH_VOCATIVE,
    AddressRole.OBLIQUE: 0,
}

# Ricky opened the floor if he asked the panel something, or handed over
# explicitly. Deliberately conservative: a missed invitation costs one beat and
# the operator can open the floor by hand, whereas a false one puts an agent on
# a PA over the moderator in front of 400 people.
_HANDOVER_RE = re.compile(
    r"\b(?:over to you|take (?:that|this) one|jump in|go ahead|your thoughts"
    r"|thoughts on that|any thoughts|anyone|anybody|what say you"
    r"|tell (?:me|us) about|let's hear)\b",
    re.IGNORECASE,
)

# Continuation cues. "Melia, can you continue to elaborate on that" is a
# handover even though none of the phrases above appear — the original bug only
# minted an invitation at all because that sentence happened to end in "?".
# Gated on `_FIRST_PERSON_RE` so "we continue to invest in this" and "let me
# elaborate on that" stay statements, which is the whole point of a closed
# floor.
_CONTINUATION_RE = re.compile(
    r"\b(?:continue|elaborate|carry on|go on|say more|said more|expand on"
    r"|finish your point|keep going|pick (?:that|it) up|take (?:that|it) further"
    r"|more on that)\b",
    re.IGNORECASE,
)
_FIRST_PERSON_RE = re.compile(
    r"\b(?:i|i'm|i'll|i've|we|we'll|we've|my|our)\b|\blet (?:me|us)\b",
    re.IGNORECASE,
)

# A question aimed at the panel, as opposed to at the moderator's own place in
# the conversation. A bare "?" is not enough: "is that okay?", "does that
# work?", "do you mind?" are courtesy tags on the end of Ricky's own sentence
# and used to open the floor to whoever happened to score best.
_CONTENT_QUESTION_RE = re.compile(
    r"\b(?:who|what|which|how|why|where|when|whose|anyone|anybody|everyone"
    r"|thought|thoughts|reaction|reactions|views?)\b",
    re.IGNORECASE,
)
_COURTESY_TAG_RE = re.compile(
    r"""^
    (?:(?:and|so|but|or|well|okay|ok|uh|um|er|erm|right|now)[\s,]+)*
    (?:
        (?:is|does|was|would|will|are|isn't|doesn't)\s+(?:that|this|it)\s+
            (?:okay|ok|alright|all\s+right|fine|good|clear|work|works|
               make\s+sense|sound\s+(?:okay|ok|good|right))
      | if\s+(?:that|this)(?:'s|\s+is)?\s+(?:okay|ok|alright|all\s+right|fine|good)
      | (?:do|would)\s+you\s+mind
      | (?:is|are)\s+(?:we|you)\s+(?:okay|ok|good|alright|all\s+right|happy)
      | (?:right|okay|ok|yeah|yes|no|sorry|sure|hm+|mm+)
    )
    [\s,]* [.?!]* \s* $""",
    re.IGNORECASE | re.VERBOSE,
)

# A request whose subject is the moderator himself. "Sorry, Dexter, can I just
# interrupt?" is a question, and Dexter is in vocative position, but nobody is
# being invited to speak — Ricky is asking permission to keep the floor. Blocks
# the VOCATIVE and OPEN paths only: "can I hear from Melia?" is still a request
# for Melia.
_SELF_DIRECTED_RE = re.compile(
    r"\b(?:can|could|may|might|shall|should)\s+(?:i|we)\b"
    r"|\blet\s+(?:me|us)\b"
    r"|\bi'?(?:m| am)\s+going to\b"
    r"|\bi\s+(?:just\s+)?(?:want|need|have)\s+to\b",
    re.IGNORECASE,
)

# A request addressed to "you". This is what binds a vocative to the clause:
# "Sorry, Dexter, can you wrap up?" resolves to Dexter, while "Sorry, Dexter,
# can Melia speak?" does not.
_SECOND_PERSON_REQUEST_RE = re.compile(
    r"\b(?:can|could|would|will|do|did|are|have|shall|should)\s+you\b"
    r"|\byou'?(?:d|ll|re)\b"
    r"|\byour\s+(?:thoughts?|view|views|take|turn|point|reaction)\b"
    r"|\bwhat\s+do\s+you\s+think\b"
    r"|\b(?:carry on|go on|continue|elaborate|say more|expand on|keep going"
    r"|wrap up|finish your point|go ahead|jump in|over to you)\b",
    re.IGNORECASE,
)

# --- context tests, applied to the text either side of a name occurrence ---

# Never an addressee: the object of an apology, an interruption, or a
# comparison. "Sorry for interrupting Dexter" and "do you agree with Dexter"
# both name Dexter in a role that cannot receive the floor.
_OBLIQUE_LEFT_RE = re.compile(
    r"\b(?:interrupting|interrupt|interrupted|cutting off|cutting|cut off"
    r"|talking over|talk over|spoke over|butting in on|jumping in on"
    r"|after|before|with|alongside|unlike|than|agree with|disagree with"
    r"|compared to|instead of|rather than|as well as|about what)\s*$",
    re.IGNORECASE,
)

# An apology or a thank-you immediately before a name is a *dismissal* of that
# agent, not an invitation to them — unless the same clause also carries a
# second-person request, which is what makes "Sorry, Dexter, can you wrap up?"
# different from "Sorry, Dexter, can Melia speak?".
_DISMISSAL_LEFT_RE = re.compile(
    r"\b(?:sorry|apologies|apologise|apologize|excuse me|pardon|forgive me"
    r"|thanks|thank you|thankyou|hold on|hang on|one moment)\b[\s,]*$",
    re.IGNORECASE,
)

# The strongest role: the name is what is being requested.
_SUBJECT_LEFT_RE = re.compile(
    r"(?:"
    r"\b(?:can|could|would|will|shall|should|might|may)\s+"
    r"|\b(?:let'?s\s+)?hear\s+from\s+"
    r"|\blet\s+"
    r"|\bi'?d\s+(?:like|love)\s+(?:to\s+hear\s+from\s+)?"
    r"|\bi\s+want\s+(?:to\s+hear\s+from\s+)?"
    r"|\bwhat\s+(?:does|do|would|did|will)\s+"
    r"|\b(?:what|how)\s+about\s+"
    r"|\b(?:over|back|straight|across|round)\s+to\s+"
    r"|\b(?:ask|asking)\s+"
    r"|\b(?:bring|bringing|get|getting)\s+(?:in\s+)?"
    r"|\bstart(?:ing)?\s+with\s+"
    r"|\bstraight\s+in\s+with\s+"
    r")$",
    re.IGNORECASE,
)
_SUBJECT_RIGHT_RE = re.compile(
    r"^(?:'|’)?s?\s*"
    r"(?:thought|thoughts|take|view|views|turn|perspective|reaction|reactions"
    r"|point of view|go\b)",
    re.IGNORECASE,
)

# Vocative position: the name stands alone as an address, either at the head of
# the clause (possibly behind filler — "so", "uh", "okay") or trailing after a
# comma ("what do you think, Wayne?").
_VOCATIVE_LEFT_RE = re.compile(
    r"^(?:(?:so|and|but|or|now|then|okay|ok|right|well|alright|uh|um|er|erm"
    r"|hey|look|listen|sorry|apologies|thanks|thank you|first|firstly|finally"
    r"|maybe|perhaps|actually|please|come on)[\s,]+)*$",
    re.IGNORECASE,
)
_TRAILING_VOCATIVE_LEFT_RE = re.compile(r",[\s]*$")
_VOCATIVE_RIGHT_RE = re.compile(r"^[\s]*(?:,|$|[.?!])")
_TRAILING_VOCATIVE_RIGHT_RE = re.compile(
    r"^[\s,]*(?:please|thanks|thank you)?[\s,]*[.?!]*$", re.IGNORECASE
)

# Sentence, then clause. Both matter: the vocative test needs to know where a
# clause begins, and a turn is routinely two sentences with two different
# addressees in it.
_COORDINATOR_GAP_RE = re.compile(r"^[\s,]*(?:and|or|&|plus|along with)?[\s,]*$", re.IGNORECASE)

_SENTENCE_RE = re.compile(r"[^.?!;]+[.?!;]?")
_CLAUSE_SPLIT_RE = re.compile(
    r",\s*(?=(?:and|but|so|then|now|uh|um|er|erm|okay|ok|right|well|alright"
    r"|also|meanwhile|instead)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _RoleHit:
    """One name occurrence, classified. Internal to detection."""

    agent: str
    role: AddressRole
    rule: str

    @property
    def strength(self) -> int:
        return _ROLE_STRENGTH[self.role]


@dataclass(frozen=True, slots=True)
class _Detection:
    """What an utterance invites, and on what grounds.

    ``conflict`` is non-empty when two different agents tied at the top role.
    That is a first-class outcome, not a failure: the floor stays closed and
    the operator is shown the tie.
    """

    strength: int = 0
    agent: str | None = None
    role: str = ""
    rule: str = ""
    conflict: tuple[str, ...] = ()

# The one-shot introduction round. Deliberately not folded into _HANDOVER_RE:
# this is usually a statement, not a question, and it must guarantee every
# agent a turn rather than let the strongest score win repeatedly.
#
# Cast wide on purpose: "introduce yourselves", "quick introductions",
# "give us an intro", "let's do intros" all count. The one-shot latch is what
# makes that safe — a stray hit costs one round, not a recurring hazard, and
# missing the real cue on stage is the worse failure. Matches "intro",
# "intros", and anything sharing the "introduc-" stem (introduce/
# introduces/introducing/introduction/introductions/introduced), but not
# "introvert"/"introspective" — the optional suffix must reach a word
# boundary, so a bare "intro" stem followed by more letters does not count.
_INTRODUCTION_RE = re.compile(r"\bintro(?:duc\w*|s)?\b", re.IGNORECASE)


def _clauses(text: str) -> list[tuple[str, bool]]:
    """Split a final transcript into ``(clause, is_question)`` pairs.

    Sentences first, then comma-plus-conjunction boundaries inside them, so
    "Thanks Dexter, and Melia, what do you think?" separates the agent being
    thanked off from the agent being asked. ``is_question`` is a property of
    the sentence, inherited by its clauses.
    """
    out: list[tuple[str, bool]] = []
    for sentence in _SENTENCE_RE.findall(text):
        chunk = sentence.strip()
        if not chunk:
            continue
        is_question = chunk.endswith("?")
        for clause in _CLAUSE_SPLIT_RE.split(chunk):
            trimmed = clause.strip().strip(",").strip()
            if trimmed:
                out.append((trimmed, is_question))
    return out


def _coordination_groups(
    clause: str, occurrences: list[tuple[int, int, str]]
) -> list[list[tuple[int, int, str]]]:
    """Group name occurrences joined by nothing but a coordinator.

    "Melia and Wayne, can you take that?" addresses a group, and the group is
    what has a grammatical role — classifying the two names separately gets
    neither of them, and picking one is exactly the guess this rewrite exists
    to stop. Names separated by any real words ("Melia, do you agree with
    Dexter?") are separate groups and get separate roles.
    """
    groups: list[list[tuple[int, int, str]]] = []
    for occurrence in occurrences:
        if groups:
            previous = groups[-1][-1]
            gap = clause[previous[1] : occurrence[0]]
            if _COORDINATOR_GAP_RE.match(gap):
                groups[-1].append(occurrence)
                continue
        groups.append([occurrence])
    return groups


def _is_handover(clause: str) -> bool:
    """Does this clause hand the floor over, question mark or not?"""
    if _HANDOVER_RE.search(clause):
        return True
    return bool(_CONTINUATION_RE.search(clause)) and not _FIRST_PERSON_RE.search(clause)


def _opens_to_panel(clause: str, *, is_question: bool) -> bool:
    """Is this a question put to the panel, rather than a courtesy tag?

    The old rule was "ends in a question mark", which let "is that okay?" open
    the floor. An open invitation now needs an explicit handover or an actual
    content question.
    """
    if _COURTESY_TAG_RE.match(clause):
        return False
    if _SELF_DIRECTED_RE.search(clause):
        return False
    if _is_handover(clause):
        return True
    return is_question and bool(_CONTENT_QUESTION_RE.search(clause))


class FloorController:
    """Holds the cast and config; the state itself is passed in and out."""

    def __init__(self, cast: PanelCast, config: FloorConfig | None = None) -> None:
        self.cast = cast
        self.config = config or FloorConfig()
        # Sorted, longest alias first. Sorting is the part that matters:
        # `Persona.aliases()` returns a set, whose iteration order is not
        # stable across processes, and the role tests read the *end* of a
        # match as right-hand context — so an unstable alternation order
        # would make address detection vary run to run. Length ordering on
        # top of that costs nothing and settles which alias wins when one is
        # a prefix of another separated by punctuation, the only case where
        # alternation order changes how much text a match consumes.
        self._address_patterns = {
            agent_id: re.compile(
                r"\b("
                + "|".join(
                    re.escape(a) for a in sorted(persona.aliases(), key=lambda a: (-len(a), a))
                )
                + r")\b",
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
                address_conflict=(),  # ...and any unresolved tie with it
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
            state = state.with_agent(agent, state=AgentState.IDLE, speaking_since=None)
        state = replace(
            state,
            speaking=None,
            ducked_agent=None,
            floor_holder=HUMAN,
            consecutive_agent_turns=0,
            proposals={},
            invitation=None,  # Ricky is taking the floor back
            address_conflict=(),
            # An incomplete introduction round is abandoned, not spent — it
            # has not "been done", so the safety latch does not engage and
            # the phrase can be said again to restart it cleanly.
            intro_queue=None,
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
                # The text a proposal could have been generated against has
                # changed for good. A stream started against "So, Wayne, uh"
                # is now answering a question that no longer exists, and the
                # runtime tears it down on this. Partials deliberately do not
                # bump it: a turn arrives as several finals and cancelling on
                # every 0.8s partial is what used to leave `proposals` empty
                # at every first arbitration.
                speculation_epoch=state.speculation_epoch + 1,
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
            if (
                not state.intro_done
                and state.intro_queue is None
                and _INTRODUCTION_RE.search(text)
            ):
                state, intro_cmds = self._start_introductions(state, t=event.t)
                commands.extend(intro_cmds)
            else:
                state, detect_cmds = self._apply_detection(state, text, t=event.t)
                commands.extend(detect_cmds)

        # Speculate during the human's turn so the gap after end-of-turn is
        # TTS latency only (FEASIBILITY.md 3.6). Debounced here rather than in
        # the runtime so the behaviour stays pure and testable.
        #
        # The debounce throttles *partials* only. A final segment must always
        # ask, or a turn that lands inside the debounce window gets no
        # candidates at all and the panel falls silent.
        #
        # None of this applies while an introduction round is live. Every
        # line in that round is `Persona.introduction` — fixed at authoring
        # time, never generated — so a speculative candidate would be work
        # nobody ever reads: exactly the model round trip (4-6s per agent)
        # that produced a silent agent on stage and that fixed text exists to
        # remove. See `_start_introductions` and `_grant_introduction`.
        if state.intro_queue is None:
            due = (
                event.t - state.last_proposal_request_t >= self.config.speculation_interval_s
                # Finals land on pauses, not on complete thoughts, so a turn's
                # first partials are routinely "So," / "So, Wayne, uh". Asking
                # there produces an answer to nothing, and `speculation_epoch`
                # cannot undo it — the runtime only tears that stream down when
                # the *next* final arrives, which may be the one that names an
                # agent. Cheaper to never ask. See `speculation_min_words`.
                and len(state.partial.split()) >= self.config.speculation_min_words
            )
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
        if state.speaking is not None:
            # A stray or duplicate TurnYielded that outran the state it was
            # based on (e.g. the runtime re-opening arbitration after a
            # proposal, racing a grant that already landed). TurnYielded means
            # "the human's turn just ended" — nonsensical while an agent
            # already holds the floor, so it is a no-op rather than a second
            # concurrent grant.
            return state, []
        state = replace(state, floor_holder=None, human_speaking=False)

        invitation = state.invitation
        if invitation is not None and invitation.source is InvitationSource.INTRODUCTION:
            # The introduction round grants itself: `_start_introductions`
            # and `_agent_ended` (via `_advance_introductions`) hand out each
            # fixed line directly, the instant the previous one ends, with no
            # arbitration in between because there is nothing left to score —
            # a fixed line has no signals. A genuine `TurnYielded` can still
            # land mid-round (Speechmatics' `EndOfTurn` and this runtime's TTS
            # pipeline are two different clocks, so it can arrive before the
            # currently-granted agent's `AgentSpeechStarted` has come back
            # around the event queue) and reducing it as an ordinary end of
            # turn would either re-grant a turn already in flight or send the
            # round back to Ricky mid-introduction. Absorbing it here is a
            # no-op, not a lost decision — nothing about this event was ever
            # needed to advance a round that paces itself.
            return state, []

        if invitation is None or not invitation.is_live():
            # Ricky made a remark, not an invitation — or he addressed two
            # agents at once and we refused to guess between them. Agents may
            # want the floor; wanting it is not taking it. Their interest goes
            # to the operator console and the video wall, and he decides.
            reason = (
                CueReason.AMBIGUOUS_ADDRESS if state.address_conflict else CueReason.NO_INVITATION
            )
            return state, [
                *self._hands_raised(state, now=event.t),
                CueModerator(reason=reason),
                self._paint(state),
            ]

        if state.consecutive_agent_turns >= self.config.max_consecutive_agent_turns:
            return state, [CueModerator(reason=CueReason.AGENT_TURN_LIMIT), self._paint(state)]

        winner, reason = self._arbitrate(state, invitation=invitation, now=event.t)
        if winner is None:
            # Invited, but nobody had anything worth the airtime. Silence is a
            # legitimate outcome — cue Ricky so the beat does not hang, and say
            # which rule produced the silence. The invitation deliberately
            # survives: an empty proposal set for two or three seconds is
            # normal, and only the TTL reaps one nobody ever acts on.
            commands: list[Command] = [CueModerator(reason=reason or CueReason.NO_PROPOSALS)]
            # ...and ask again, against the completed turn. Until now recovery
            # depended on a stream that happened to still be in flight: if the
            # only generation for this turn had already finished (against a
            # partial, or before the question was asked) nothing re-drove
            # arbitration and the live invitation sat there until the TTL. The
            # debounce is what stops a refused proposal and a fresh request
            # chasing each other.
            targets = state.idle_agents()
            if targets and (
                event.t - state.last_proposal_request_t >= self.config.speculation_interval_s
            ):
                state = replace(state, last_proposal_request_t=event.t)
                commands.append(RequestProposals(agents=targets, reason="post_turn"))
            commands.append(self._paint(state))
            return state, commands

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
                state = state.with_agent(state.speaking, state=AgentState.IDLE, speaking_since=None)
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

        if not event.completed and state.speaking is not None:
            # Cut off by an agent interrupt — the challenger was already
            # granted the floor synchronously, in the same reduce() call that
            # issued the StopSpeech. `state.speaking` is that challenger, not
            # this agent (an interrupted turn always clears its own
            # `speaking` a few lines up), so there is nothing left to
            # arbitrate here.
            #
            # A human interrupt or a turn-limit hard stop also arrives with
            # `completed=False`, but both clear `state.speaking` to None
            # themselves before this event lands — so they fall through to
            # the same continuation logic as a normal completion. That is
            # deliberate: nobody else has taken the floor, so the panel (or
            # the introduction round) must still be given its next turn
            # rather than stalling silently until Ricky speaks again.
            return state, commands

        # A completed agent turn does NOT reopen the floor. The panel continues
        # only if the invitation had turns left on it; otherwise it goes back to
        # Ricky, which is what stops three agents relaying to each other.
        state = state.cleared_proposals()

        if state.intro_queue is not None and event.agent in state.intro_queue:
            # Pop the agent who just finished and hand the round straight to
            # `_advance_introductions`, which either grants the next fixed
            # line directly or, if nobody is left, latches the round shut.
            # There is no `RequestProposals` here any more: the old version
            # of this branch asked the model for the *next* agent's line on
            # every turn, which is the 4-6s round trip fixed text exists to
            # remove, and it is also the reason the round used to be able to
            # stall — nothing else was left to re-drive arbitration once a
            # proposal never arrived.
            remaining = tuple(a for a in state.intro_queue if a != event.agent)
            state = replace(state, intro_queue=remaining)
            state, advance_cmds = self._advance_introductions(state, now=event.t)
            commands.extend(advance_cmds)
            return state, commands

        invitation = state.invitation
        if invitation is None or not invitation.is_live():
            state = replace(state, invitation=None, address_conflict=())
            commands.append(CueModerator(reason=CueReason.INVITATION_SPENT))
            return state, commands

        if state.consecutive_agent_turns >= self.config.max_consecutive_agent_turns:
            state = replace(state, invitation=None)
            commands.append(CueModerator(reason=CueReason.AGENT_TURN_LIMIT))
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
            # Only reap a stale invitation when nothing is on the PA: an
            # invitation being acted on right now is live conversation, not a
            # leftover, whatever its clock says.
            return self._expire_invitation(state, now=event.t)

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
                    address_conflict=(),
                    intro_queue=None,  # abandoned, not spent — safe to retry later
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
                    address_conflict=(),
                    intro_queue=None,  # abandoned, not spent — safe to retry later
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
                        role="operator",
                        rule="operator_open_floor",
                    ),
                    # The operator opening the floor by hand *is* the answer to
                    # an ambiguous address.
                    address_conflict=(),
                )
                return state, [self._paint(state)]

            case OperatorAction.CLOSE_FLOOR:
                state = replace(state, invitation=None, intro_queue=None, address_conflict=())
                return state, [self._paint(state)]

            case OperatorAction.ADVANCE_BEAT:
                state = replace(state, beat_index=state.beat_index + 1, proposals={})
                return state, [self._paint(state)]

            case _:
                return state, []

    # ---------------------------------------------------------------- helpers

    def _apply_detection(
        self, state: PanelState, text: str, *, t: float
    ) -> tuple[PanelState, list[Command]]:
        """Fold one final transcript segment into the standing invitation.

        Precedence, not recency. A moderator's act of moderation routinely
        arrives as two or three transcript segments — "Sorry Dexter, can Melia
        speak?" then "Sorry for interrupting Dexter, is that okay?" — and the
        trailing courtesy must not downgrade the invitation from "Melia" to
        "whoever scores best". A fresh ADDRESS always supersedes; a fresh OPEN
        only displaces a live ADDRESS once
        `FloorConfig.invitation_supersede_window_s` has passed.
        """
        invitation, conflict = self._detect_invitation(text, t=t)

        if conflict:
            # Ambiguity is an outcome. Stay closed, revoke nothing that was
            # already specific, and put the tie in front of the operator.
            state = replace(state, address_conflict=conflict)
            return state, [self._paint(state)]

        if invitation is None:
            return state, []

        standing = state.invitation
        if standing is not None and standing.is_live():
            fresh = t - standing.t < self.config.invitation_supersede_window_s
            if fresh and invitation.precedence() < standing.precedence():
                return state, []

        state = replace(state, invitation=invitation, address_conflict=())
        return state, [self._paint(state)]

    def _eligible(self, state: PanelState, invitation: Invitation | None) -> dict[str, Proposal]:
        # Never called with an introduction invitation: `_turn_yielded` routes
        # that round to `_advance_introductions` before arbitration is ever
        # reached, because a fixed line has no proposal to be eligible with in
        # the first place. See `_grant_introduction`.
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

    def _stale(self, proposal: Proposal, invitation: Invitation) -> bool:
        """Was this line written too long before the question to be its answer?

        See `FloorConfig.named_proposal_lookback_s`. A proposal *newer* than
        the invitation is never stale, which is the ordinary case for anything
        generated by the `RequestProposals` the invitation's own final emitted.
        """
        return invitation.t - proposal.t > self.config.named_proposal_lookback_s

    def _arbitrate(
        self, state: PanelState, *, invitation: Invitation, now: float
    ) -> tuple[str | None, CueReason | None]:
        """Pick a winner from within the invitation, or None for silence.

        Returns the winner and, when there is none, *why* — "nobody proposed"
        and "the one agent Ricky named had nothing to say" are different
        problems, and a rehearsal that cannot tell them apart cannot be tuned.

        Never called with an introduction invitation — `_turn_yielded` routes
        that round to `_advance_introductions` instead, since scoring exists
        to choose between competing proposals and a fixed line never has one.
        """
        candidates = self._eligible(state, invitation)
        if not candidates:
            if invitation.agent is not None:
                return None, CueReason.INVITED_AGENT_SILENT
            return None, CueReason.NO_PROPOSALS

        # Ricky named them. They answer. The score floor exists so that silence
        # can win an *open* invitation — it has no business overruling a direct
        # question put to a specific panellist.
        if invitation.agent is not None:
            if invitation.agent not in candidates:
                return None, CueReason.INVITED_AGENT_SILENT
            # ...but it has to be an answer to *this* question. Speculation runs
            # ahead of the final that opens the floor, so a proposal is normally
            # a second or two older than the invitation and that is exactly what
            # keeps the post-turn gap short. One written well before it is
            # answering a different moment — Ricky's preamble, or the previous
            # turn, whose proposals survive in `state.proposals` whenever that
            # turn ended without a grant. Silence plus a cue is recoverable;
            # an unrelated line on the PA in answer to a direct question is not.
            # `_turn_yielded` asks for a fresh one, so this costs a beat, not
            # the answer.
            #
            # Measured from the invitation, not from `now`: the gap between the
            # question and `EndOfTurn` is detector latency, not conversation,
            # and charging it against the proposal would refuse good candidates
            # whenever Ricky trails off slowly. ADDRESS only — an OPERATOR
            # invitation is a human deciding, at the console, that this agent
            # should speak with whatever it has; that is the backstop for a
            # missed cue and second-guessing its freshness would break it.
            if invitation.source is InvitationSource.ADDRESS and self._stale(
                candidates[invitation.agent], invitation
            ):
                return None, CueReason.INVITED_AGENT_SILENT
            # ...but a handoff is still honoured. "Melia's the one to follow
            # here" is information the panel generated about itself, and
            # throwing it away is how a named grant ends up answering a
            # question its own agent just said it was the wrong one for. The
            # score floor stays bypassed: a direct question deserves an answer.
            deferred = candidates[invitation.agent].signals.defer_to
            # Evaluated against the *unrestricted* eligible set: a named
            # invitation admits only the named agent, so the handoff target is
            # by construction outside `candidates`.
            unrestricted = self._eligible(state, None)
            if (
                deferred
                and deferred != invitation.agent
                and deferred in unrestricted
                # The handoff target gets the same freshness test as the agent
                # who named it: a direct question is still being answered, and
                # the score floor is still bypassed.
                and not self._stale(unrestricted[deferred], invitation)
            ):
                return deferred, None
            return invitation.agent, None

        scored = sorted(
            ((self._score(state, a, p, now=now), a) for a, p in candidates.items()),
            reverse=True,
        )
        best_score, best_agent = scored[0]

        if best_score < self.config.min_floor_priority:
            return None, CueReason.BELOW_FLOOR

        # An agent may hand off to a better-placed colleague.
        defer_to = candidates[best_agent].signals.defer_to
        if defer_to and defer_to in candidates:
            return defer_to, None
        return best_agent, None

    def _expire_invitation(
        self, state: PanelState, *, now: float
    ) -> tuple[PanelState, list[Command]]:
        """Reap an invitation nobody ever acted on.

        `no_candidate` deliberately leaves the invitation standing — the
        proposal set is legitimately empty for a couple of seconds after a
        human turn, and clearing it there would make the panel unanswerable.
        The cost of that is an invitation that outlives its moment, and a
        *mis-addressed* one would otherwise stand for the rest of the show.
        This is the only thing that clears it.
        """
        invitation = state.invitation
        if invitation is None or state.human_speaking:
            return state, []
        deadline = invitation.expires_at(self.config.invitation_ttl_s)
        if deadline is None or now < deadline:
            return state, []
        state = replace(state, invitation=None, address_conflict=())
        return state, [CueModerator(reason=CueReason.INVITATION_EXPIRED), self._paint(state)]

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

    def _detect_invitation(
        self, text: str, *, t: float
    ) -> tuple[Invitation | None, tuple[str, ...]]:
        """Did Ricky actually open the floor, and to whom?

        A statement invites nobody, however interesting it is — that is the
        whole rule, and it is one a moderator can hold in his head on stage:
        *ask a question and the panel answers; make a point and they let you
        make it.* Naming an agent narrows the invitation to them.

        Returns the invitation (or None) and any set of agents that tied for
        addressee. A tie yields no invitation at all: see `_detect`.
        """
        detection = self._detect(text)

        if detection.conflict:
            return None, detection.conflict

        if detection.agent is not None:
            return (
                Invitation(
                    agent=detection.agent,
                    turns_remaining=self.config.address_invitation_turns,
                    source=InvitationSource.ADDRESS,
                    t=t,
                    role=detection.role,
                    rule=detection.rule,
                ),
                (),
            )

        if detection.strength >= _STRENGTH_OPEN:
            return (
                Invitation(
                    agent=None,
                    turns_remaining=self.config.open_invitation_turns,
                    source=InvitationSource.OPEN,
                    t=t,
                    role="open",
                    rule=detection.rule,
                ),
                (),
            )
        return None, ()

    # ------------------------------------------------------------- detection

    def _detect(self, text: str) -> _Detection:
        """Resolve a whole final transcript to at most one addressee.

        Clause by clause, then combined by *precedence* rather than recency: an
        agent named as the subject of a request outranks one merely in vocative
        position, which outranks an open question, and an agent named in an
        oblique role ("sorry for interrupting Dexter") outranks nothing at all
        because it can never be an addressee.

        Two different agents at the same top strength is a genuine ambiguity
        and is reported as one. Guessing between them is the failure mode this
        whole function exists to remove.
        """
        hits: list[_RoleHit] = []
        open_rule = ""
        for clause, is_question in _clauses(text):
            hits.extend(self._classify_clause(clause, is_question=is_question))
            if not open_rule and _opens_to_panel(clause, is_question=is_question):
                open_rule = "handover" if _is_handover(clause) else "content_question"

        addressed = [h for h in hits if h.strength > 0]
        if addressed:
            top = max(h.strength for h in addressed)
            winners = {h.agent: h for h in addressed if h.strength == top}
            if len(winners) > 1:
                # Ambiguity, not a coin toss. The floor stays closed.
                first = next(iter(winners.values()))
                return _Detection(
                    strength=top,
                    agent=None,
                    role=first.role.value,
                    rule="ambiguous_" + first.rule,
                    conflict=tuple(sorted(winners)),
                )
            only = next(iter(winners.values()))
            return _Detection(
                strength=top, agent=only.agent, role=only.role.value, rule=only.rule
            )

        if open_rule:
            return _Detection(strength=_STRENGTH_OPEN, agent=None, role="open", rule=open_rule)
        return _Detection()

    def _classify_clause(self, clause: str, *, is_question: bool) -> list[_RoleHit]:
        """Classify every name occurrence in one clause by grammatical role.

        Coordinated names ("can Melia and Wayne take that?", "Melia, Dexter,
        thoughts?") are classified as one group and share the resulting role.
        That is what makes them come out *ambiguous* rather than resolving to
        whichever one the regexes happened to reach first.
        """
        if _COURTESY_TAG_RE.match(clause):
            # "Is that okay?", "does that work?" — Ricky checking in on his own
            # sentence. Nobody is addressed and nobody is invited, even if a
            # name happens to trail off the end of it.
            return []

        second_person = bool(_SECOND_PERSON_REQUEST_RE.search(clause))
        self_directed = bool(_SELF_DIRECTED_RE.search(clause))
        requested = second_person or is_question or _is_handover(clause)

        occurrences = [
            (match.start(), match.end(), agent_id)
            for agent_id, pattern in self._address_patterns.items()
            for match in pattern.finditer(clause)
        ]
        occurrences.sort()

        hits: list[_RoleHit] = []
        for group in _coordination_groups(clause, occurrences):
            role_rule = self._classify_span(
                left=clause[: group[0][0]],
                right=clause[group[-1][1] :],
                second_person=second_person,
                self_directed=self_directed,
                requested=requested,
            )
            if role_rule is None:
                continue
            role, rule = role_rule
            for _, _, agent_id in group:
                hits.append(_RoleHit(agent_id, role, rule))
        return hits

    def _classify_span(
        self,
        *,
        left: str,
        right: str,
        second_person: bool,
        self_directed: bool,
        requested: bool,
    ) -> tuple[AddressRole, str] | None:
        """One name (or coordinated group), in the context it appeared in."""
        # Oblique first. An occurrence that cannot be an addressee must not be
        # rescued by also sitting somewhere that looks vocative.
        if _OBLIQUE_LEFT_RE.search(left):
            return AddressRole.OBLIQUE, "oblique_object"
        if _DISMISSAL_LEFT_RE.search(left) and not second_person:
            # "Sorry Dexter, can Melia speak?" — Dexter is being stood down.
            # With a second-person request in the clause it is the opposite:
            # "Sorry, Dexter, can you wrap up?" is addressed to Dexter.
            return AddressRole.OBLIQUE, "dismissal_object"

        if _SUBJECT_LEFT_RE.search(left):
            return AddressRole.SUBJECT_OF_REQUEST, "subject_left"
        if _SUBJECT_RIGHT_RE.match(right):
            return AddressRole.SUBJECT_OF_REQUEST, "subject_possessive"

        if not requested or self_directed:
            # A name in a statement, or in Ricky's own permission request, is
            # mentioned rather than addressed.
            return None

        if _VOCATIVE_LEFT_RE.match(left) and _VOCATIVE_RIGHT_RE.match(right):
            return AddressRole.VOCATIVE, "vocative_leading"
        if _TRAILING_VOCATIVE_LEFT_RE.search(left) and _TRAILING_VOCATIVE_RIGHT_RE.match(right):
            return AddressRole.VOCATIVE, "vocative_trailing"
        return None

    def _start_introductions(
        self, state: PanelState, *, t: float
    ) -> tuple[PanelState, list[Command]]:
        """Invite the whole panel to introduce itself, one fixed turn each.

        Every agent's line is `Persona.introduction` — fixed at authoring
        time, never generated — so there are no candidate proposals for
        scoring to choose between, and this guarantees every agent a turn
        directly rather than by scoring the strongest case each time: order
        is `tuple(state.agents.keys())`, which is the cast's own order
        (`PanelCast.from_dir`'s alphabetical directory listing, carried
        through unchanged by `PanelState.for_agents`), fixed and identical on
        every run. A fixed opening that ran in a different order each
        rehearsal would only be half of what "fixed" was for. Deliberately
        not derived from any prior turn's content either: `Persona.
        introduction` never references another panellist by name, precisely
        so this ordering decision is free to be simple.

        Callers must already have checked `intro_done`: this is the one-shot
        round, and there is no event that resets `intro_done` once it
        latches.
        """
        agents = tuple(state.agents.keys())
        state = replace(
            state,
            invitation=Invitation(
                agent=None,
                turns_remaining=len(agents),
                source=InvitationSource.INTRODUCTION,
                t=t,
                role="introduction",
                rule="introduction_round",
            ),
            intro_queue=agents,
            address_conflict=(),
        )
        return self._advance_introductions(state, now=t)

    def _advance_introductions(
        self, state: PanelState, *, now: float
    ) -> tuple[PanelState, list[Command]]:
        """Grant the next fixed introduction turn, or close the round out.

        This *is* the introduction round's entire arbitration. There is
        nothing to score because there is nothing to generate — every line is
        `Persona.introduction` — so progressing the round is a direct,
        synchronous grant rather than a request that waits on a model
        response and a later proposal to re-open arbitration. That round trip
        is exactly what left an agent holding the floor over dead air on
        stage-adjacent testing. Called once when the round starts
        (`_start_introductions`) and again every time one agent's turn ends
        (`_agent_ended`), so from Ricky's "introduce yourselves" to the last
        agent's last word, nothing here ever waits on anything.
        """
        winner = self._next_introduction(state)
        if winner is None:
            if state.intro_queue:
                # Every agent still owed a turn is muted. The round cannot
                # finish itself — an unmute, or Ricky abandoning and
                # re-triggering it, is what resumes it — so it is left
                # standing rather than quietly marked done. `expires_at`
                # exempts this source from the TTL for exactly this reason.
                return state, [CueModerator(reason=CueReason.NO_PROPOSALS), self._paint(state)]
            # Everyone has introduced themselves. Latch it shut — permanently,
            # by design — and hand back to the moderator.
            state = replace(state, intro_queue=None, intro_done=True, invitation=None)
            return state, [
                CueModerator(reason=CueReason.INTRODUCTIONS_COMPLETE),
                self._paint(state),
            ]
        return self._grant_introduction(state, winner, now=now)

    def _next_introduction(self, state: PanelState) -> str | None:
        """The next agent still owed an introduction, skipping anyone muted.

        Order is `intro_queue`'s own order, fixed once at
        `_start_introductions` and never re-derived. A muted agent is
        skipped, not granted: `OperatorAction.MUTE_AGENT` is an absolute veto
        everywhere else in this file (`_proposal` drops a muted agent's
        candidate before it is ever stored, and `_eligible` excludes muted
        agents from every other kind of arbitration), and the introduction
        round granting fixed text directly, with no proposal to drop, must
        not become the one path that quietly overrides it.
        """
        for agent_id in state.intro_queue or ():
            if not state.agents[agent_id].muted:
                return agent_id
        return None

    def _grant_introduction(
        self, state: PanelState, agent_id: str, *, now: float
    ) -> tuple[PanelState, list[Command]]:
        """Grant one turn in the introduction round.

        The only `StartSpeech` in this whole file that is not built from a
        `Proposal`: there is nothing in `state.proposals` for this agent, and
        there was never meant to be — introductions never ask the model in
        the first place (see `_advance_introductions`). The utterance comes
        straight from `Persona.introduction`, run through `sanitise()` the
        same as any other text on its way to audio (CLAUDE.md: fixed text
        does not get to bypass that rule just because nobody generated it
        live).
        """
        persona = self.cast[agent_id]
        state = replace(
            state,
            turn_id=state.turn_id + 1,
            consecutive_agent_turns=state.consecutive_agent_turns + 1,
            invitation=state.invitation.spent(t=now) if state.invitation else None,
            address_conflict=(),
        )
        return state, [
            StartSpeech(
                agent=agent_id, utterance=sanitise(persona.introduction), turn_id=state.turn_id
            )
        ]

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
            # `now` refreshes the TTL clock: an invitation producing turns is
            # live conversation and must not age out mid-exchange.
            invitation=state.invitation.spent(t=now) if state.invitation else None,
            address_conflict=(),
        )
        return state, [
            StartSpeech(agent=agent_id, utterance=proposal.utterance, turn_id=state.turn_id)
        ]

    def _paint(self, state: PanelState) -> StateChanged:
        invitation = state.invitation
        return StateChanged(
            floor_holder=state.floor_holder,
            speaking=state.speaking,
            turn_id=state.turn_id,
            extra={
                "invited": invitation.agent if invitation else None,
                "invitation_turns": invitation.turns_remaining if invitation else 0,
                # Provenance for the operator console and the rehearsal log: a
                # rehearsal has to be able to see *why* the floor opened where
                # it did, not just that it did.
                "invitation_source": invitation.source.value if invitation else None,
                "invitation_role": invitation.role if invitation else None,
                "invitation_rule": invitation.rule if invitation else None,
                "address_conflict": state.address_conflict,
                "consecutive_agent_turns": state.consecutive_agent_turns,
                "killed": state.killed,
                "intro_remaining": state.intro_queue,
                "intro_done": state.intro_done,
            },
        )
