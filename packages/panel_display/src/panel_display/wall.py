"""What the wall is currently showing, as a value.

`panel_core` decides who speaks; this decides what a 12m LED wall says about
it. The split is deliberate and matches the one the rest of the repo uses:
everything here is a pure function of the events and commands already flowing
through `PanelRuntime`, with no sockets, no clock reads and no browser. The
I/O lives next door in `server.py`.

That purity is not decoration. It is the only reason this can be tested at
all — a video wall is otherwise a thing you can only check by looking at it,
in a venue, once, on the night.

Every line in the transcript band — Ricky's and the agents' alike — arrives as
a `TranscriptUpdated`, off a real Speechmatics session. Ricky's comes from his
mic; an agent's comes from a second, display-only session over that agent's own
played audio (`PanelRuntime._run_agent_stt`). So the band is timed by speech in
both cases rather than by an assumed words-per-second, and this file needs no
notion of pace at all. The agent lines used to come from
`AgentUtteranceProgress` instead, which is emitted when a sentence is handed to
the TTS provider and therefore runs ahead of the room by however deep the
mixer's buffer happens to be; that case is gone from here, and the event is
still emitted and still read by the floor for speculation timing — the wall
just no longer listens to it.

Two reasons the wall reads *commands* and not just events:

* `HandsRaised` and `CueModerator` are commands and exist for precisely this
  surface — see their docstrings in `panel_core.events`. A raised hand is
  deliberately not self-served by the panel; it is shown so the room can see
  three agents with something to say and watch Ricky choose.
* `DuckSpeech` / `ResumeSpeech` never touch `PanelState`. They are the
  backchannel reflex, and an agent dropping its level because Ricky said
  "mm-hm" is one of the more legible things this panel does. It would be
  invisible if the wall read state alone.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from panel_core import (
    HUMAN,
    AgentSpeechEnded,
    AgentSpeechStarted,
    CueModerator,
    DuckSpeech,
    HandsRaised,
    HumanSpeechEnded,
    HumanSpeechStarted,
    PanelCast,
    RequestProposals,
    ResumeSpeech,
    StartSpeech,
    StateChanged,
    StopSpeech,
    TranscriptUpdated,
    UnverifiedSpeechDetected,
)

# How many finalised lines the transcript band keeps — the moderator's and
# the agents' together, in the order they were said.
#
# The band shows about five lines before the top fade eats them at current
# type size. Keeping a few more than fit means a browser refreshed mid-show
# repaints a full band instead of an empty one, which is the entire reason
# this is bounded rather than either unbounded or exactly what fits.
TRANSCRIPT_LINES = 14

# Accent slots, assigned by cast order. Three agents, three hues.
#
# Deliberately *not* a persona field. `personas.py` is explicit that "a persona
# field earns its place by being read" — by prompts, the floor, or a brain —
# and a colour is read by none of them. It is a fact about this display, so it
# lives with the display. The cost is that reordering `personas/` recolours the
# panel; the names are on screen underneath, so nobody is misled, and it is
# one line to pin if the show ever wants fixed colours.
#
# Three hues rather than the brand's usual two-accent restraint because this is
# categorical identity, not a chart — the same distinction `--cat-*` draws in
# the playground's tokens.css. Amber is a ring and a bloom here, never a fill
# behind text, so the brand rule that amber never pairs with light text holds.
ACCENTS = ("jade", "cyan", "amber")

# Agent display states, weakest to strongest. Ordered because more than one can
# be true at once — an invited agent is usually also thinking — and the wall
# shows one thing per agent.
IDLE = "idle"
INVITED = "invited"
THINKING = "thinking"
DUCKED = "ducked"
SPEAKING = "speaking"


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """One line in the transcript band, attributed to whoever said it.

    `speaker` is `HUMAN` for the moderator or an agent id. Both share one
    window and one sequence counter — the band is a single conversation, not
    a moderator lane with agent asides — so the client can render them in the
    order they actually happened rather than reconciling two feeds.
    """

    speaker: str
    text: str

    def payload(self) -> dict[str, str]:
        return {"speaker": self.speaker, "text": self.text}


@dataclass
class AgentView:
    """One agent's 3m column."""

    id: str
    name: str
    job_title: str
    employer: str
    accent: str

    state: str = IDLE
    # Floor priority from the last `HandsRaised`, or None. Drives a small
    # "wants in" mark; the panel cannot act on it and neither can the wall.
    hand: float | None = None
    muted: bool = False
    # True while this agent holds the live invitation, whether or not it has
    # started speaking. Separate from `state` because an invited agent that is
    # also speaking needs to show both.
    invited: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.job_title,
            "employer": self.employer,
            "accent": self.accent,
            "state": self.state,
            "hand": self.hand,
            "muted": self.muted,
            "invited": self.invited,
        }


@dataclass
class WallState:
    """The whole wall, as a value. Feed it events and commands; read snapshots."""

    agents: dict[str, AgentView]
    order: tuple[str, ...]

    human_speaking: bool = False
    # In-progress lines, keyed by speaker. Per-speaker rather than the single
    # global string this used to be, because there are now four transcription
    # sessions feeding this class instead of one, and two of them are
    # genuinely live at once in the beat the show is built around: Ricky
    # barging in over an agent. His first partials land while the agent's
    # socket is still returning partials for audio the room has already heard
    # — a few hundred milliseconds of network, unavoidable — so one string
    # would flicker between two voices at exactly the moment the band matters
    # most. Keyed, both are shown, and the wall reads the way the room sounds.
    partials: dict[str, str] = field(default_factory=dict)
    lines: deque[TranscriptLine] = field(default_factory=lambda: deque(maxlen=TRANSCRIPT_LINES))
    # Total lines ever finalised, not the length of the window above.
    #
    # The window slides, so "the same four lines" and "four new lines that
    # happen to be similar" are indistinguishable from the client's side of
    # the socket. This makes them distinguishable, which is what lets the
    # client *append* rather than rebuild — and a rebuild re-runs the entrance
    # animation on every visible line every time anything on the wall changes.
    line_seq: int = 0
    # Set when the floor comes back to Ricky and cleared the moment anyone
    # speaks again. The audience's cue that the panel is done, not stuck.
    cue: str | None = None
    open_floor: bool = False
    killed: bool = False
    turn_id: int = 0
    # Monotonically increasing, sent with every snapshot. A client that has
    # reconnected can tell a stale frame from a fresh one without a clock.
    revision: int = 0
    # The last agent handed the PA, or None before the first turn. This is the
    # attribution guard for agent transcripts and it replaces the old
    # `state in (SPEAKING, DUCKED)` test, which cannot be used here: an
    # agent's STT session finalises its last sentence *after* the audio has
    # finished, so `AgentSpeechEnded` always beats the final that carries the
    # end of the turn, and a state test would drop the closing line of every
    # single turn. What actually has to be prevented is *misattribution* — a
    # straggler landing under a later speaker's lines — and the agent last put
    # on air is exactly that test, with no clock and no timeout: the moment
    # another agent starts, the previous one's stragglers stop counting.
    # Never painted on its own, so deliberately absent from `_fingerprint`.
    last_on_air: str | None = None

    @classmethod
    def for_cast(cls, cast: PanelCast) -> WallState:
        order = cast.ids()
        agents = {
            agent_id: AgentView(
                id=agent_id,
                name=cast[agent_id].name,
                job_title=cast[agent_id].job_title,
                employer=cast[agent_id].employer,
                accent=ACCENTS[index % len(ACCENTS)],
            )
            for index, agent_id in enumerate(order)
        }
        return cls(agents=agents, order=order)

    # ------------------------------------------------------------------ events

    def apply_event(self, event: Any) -> bool:
        """Fold one inbound event in. Returns True if the wall changed."""
        before = self._fingerprint()
        match event:
            case HumanSpeechStarted():
                self.human_speaking = True
                self.cue = None

            case HumanSpeechEnded():
                self.human_speaking = False

            case TranscriptUpdated(speaker=speaker):
                self._transcript(speaker, event.text, is_final=event.is_final)

            case UnverifiedSpeechDetected():
                # A segment on Ricky's mic that `panel_runtime/stt.py` could
                # not attribute to him — the audience, or the PA bleeding back
                # into the room. It carries no text and never may, so there is
                # nothing here to paint.
                #
                # What it *does* settle is the fate of whatever of Ricky's is
                # currently in progress: his socket has moved on to someone
                # else's segment, so the partial on the band is never going to
                # be finalised. Left alone it would sit under the band for the
                # rest of the show — the same leak `AgentSpeechStarted` closes
                # for the agents, which until now Ricky had no equivalent of,
                # because nothing else on this hook marks the end of a line of
                # his that does not end in a final.
                self.partials.pop(HUMAN, None)

            case AgentSpeechStarted(agent=agent):
                self.cue = None
                view = self.agents.get(agent)
                if view is not None:
                    view.state = SPEAKING
                    view.hand = None
                    # Whoever was last on air is no longer, so their in-flight
                    # words stop counting from here: see `last_on_air`.
                    self.last_on_air = agent
                # Only one agent is ever on the PA. Anyone else still showing
                # as speaking is a dropped `AgentSpeechEnded`, and on a wall
                # that reads as two agents talking at once.
                for other_id, other in self.agents.items():
                    if other_id != agent and other.state in (SPEAKING, DUCKED):
                        other.state = IDLE
                    if other_id != agent:
                        # And a partial the previous speaker never finalised —
                        # a socket that dropped mid-sentence — would otherwise
                        # sit under the band for the rest of the show. The
                        # handover is the natural place to bound its life:
                        # nothing that agent still owes the band can be
                        # accepted after this point anyway.
                        self.partials.pop(other_id, None)

            case AgentSpeechEnded(agent=agent):
                view = self.agents.get(agent)
                if view is not None:
                    view.state = IDLE

            case _:
                pass
        return self._bump(before)

    def _transcript(self, speaker: str, text: str, *, is_final: bool) -> None:
        """Fold one transcript segment into the band, whoever said it.

        One path for all four voices, because they are all now real STT. The
        only difference is the attribution guard: Ricky's mic is authoritative
        about Ricky unconditionally, whereas an agent's session is only
        believed for the agent last put on air (see `last_on_air`). A
        `TranscriptUpdated` naming an agent that has never held the floor is
        not a real line — there is no audio it could have come from — and is
        dropped rather than painted.

        Args:
            speaker: `HUMAN`, or the agent id the session is wired to.
            text: The segment. Partials replace, finals append.
            is_final: Whether Speechmatics called this segment done.
        """
        if speaker != HUMAN and speaker != self.last_on_air:
            return
        if speaker != HUMAN and speaker not in self.agents:
            return
        if is_final:
            stripped = text.strip()
            if stripped:
                self.lines.append(TranscriptLine(speaker, stripped))
                self.line_seq += 1
            self.partials.pop(speaker, None)
        elif text.strip():
            self.partials[speaker] = text
        else:
            self.partials.pop(speaker, None)

    def drop_partial(self, speaker: str) -> bool:
        """Forget one voice's in-progress line. Returns True if there was one.

        For the case no event can close, which is the one the venue will
        actually hit: `panel_runtime/stt.py` drops a segment *silently* when
        diarisation attributed nothing at all — deliberately, because
        "attributed nothing" and "this is not Ricky" are different facts and
        conflating them would let a run of unattributed segments block his
        interrupt. The consequence here is that a partial can simply stop being
        updated, with no final, no `UnverifiedSpeechDetected` and nothing else
        to hang a clear on.

        So staleness is the only signal left, and staleness is a fact about
        arrival time — which this module, having no clock, cannot know. The
        sweep therefore lives in `DisplayServer._expire_partials`, which owns
        the pump and the clock, and this is the one verb it needs. Keeping the
        decision there rather than taking a timestamp here is what stops a
        clock read appearing in a file whose testability is the reason the
        wall can be checked anywhere but a venue.
        """
        return self.partials.pop(speaker, None) is not None

    def _partials(self) -> list[dict[str, str]]:
        """In-progress lines, agents in stage order and Ricky last.

        Ordered rather than a map so the client can append without sorting,
        and Ricky last because the band's finals land at its bottom edge —
        putting the moderator's in-progress words closest to them keeps his
        line reading as the newest thing on the wall, which during a barge-in
        is exactly what it is.
        """
        rows = [
            {"speaker": agent_id, "text": self.partials[agent_id]}
            for agent_id in self.order
            if self.partials.get(agent_id)
        ]
        if self.partials.get(HUMAN):
            rows.append({"speaker": HUMAN, "text": self.partials[HUMAN]})
        return rows

    # ---------------------------------------------------------------- commands

    def apply_command(self, command: Any) -> bool:
        """Fold one outbound command in. Returns True if the wall changed."""
        before = self._fingerprint()
        match command:
            case RequestProposals(agents=agents):
                for agent_id in agents:
                    view = self.agents.get(agent_id)
                    # Thinking never overrides being audible. An agent already
                    # holding the floor is asked for proposals routinely, and
                    # its orb must not drop out of its speaking state to show
                    # that a *future* turn is being written.
                    if view is not None and view.state not in (SPEAKING, DUCKED):
                        view.state = THINKING

            case StartSpeech(agent=agent):
                # The grant, not the audio. `AgentSpeechStarted` is what turns
                # the orb on; this only clears the stale marks, because
                # `StartSpeech` may carry a `lead_in_s` silent beat and an orb
                # that lights up during it is lighting up before the voice.
                view = self.agents.get(agent)
                if view is not None:
                    view.hand = None

            case StopSpeech(agent=agent):
                view = self.agents.get(agent)
                if view is not None:
                    view.state = IDLE

            case DuckSpeech(agent=agent):
                view = self.agents.get(agent)
                if view is not None and view.state == SPEAKING:
                    view.state = DUCKED

            case ResumeSpeech(agent=agent):
                view = self.agents.get(agent)
                if view is not None and view.state == DUCKED:
                    view.state = SPEAKING

            case HandsRaised(agents=hands):
                raised = dict(hands)
                for agent_id, view in self.agents.items():
                    view.hand = raised.get(agent_id)

            case CueModerator(reason=reason):
                self.cue = getattr(reason, "value", str(reason))
                for view in self.agents.values():
                    view.hand = None
                    if view.state == THINKING:
                        view.state = IDLE

            case StateChanged():
                self._apply_state_changed(command)

            case _:
                pass
        return self._bump(before)

    def _apply_state_changed(self, command: StateChanged) -> None:
        self.turn_id = command.turn_id
        self.killed = bool(command.extra.get("killed"))

        live = command.extra.get("invitation_source") is not None
        # `invited_agents` is the authoritative set; `invited` is the single
        # addressee and is None for both an open floor and a named pair, so
        # reading it alone would light the whole panel when Ricky named two of
        # them. Falling back to it keeps older rehearsal logs, written before
        # the set existed, replaying correctly.
        invited = tuple(command.extra.get("invited_agents") or ())
        if not invited and command.extra.get("invited"):
            invited = (command.extra["invited"],)
        # An empty set with a live invitation means the floor is open to the
        # whole panel — a real and distinct third state, not "nobody".
        self.open_floor = live and not invited
        for agent_id, view in self.agents.items():
            view.invited = live and (not invited or agent_id in invited)

        awaiting = tuple(command.extra.get("awaiting_agents") or ())
        if not awaiting and command.extra.get("awaiting"):
            awaiting = (command.extra["awaiting"],)
        for agent_id in awaiting:
            view = self.agents.get(agent_id)
            if view is not None and view.state not in (SPEAKING, DUCKED):
                view.state = THINKING

    # --------------------------------------------------------------- the wire

    # Audio levels are deliberately absent from this class. They arrive 30
    # times a second, they never change what the wall *is*, and folding them
    # in would mark the snapshot dirty on every frame — turning a 2KB
    # state message into a continuous stream of mostly unchanged fields.
    # `DisplayServer` sends them on their own channel.

    def snapshot(self) -> dict[str, Any]:
        """Everything a freshly-connected browser needs to paint correctly.

        Full state every time, not a diff. Someone will refresh this browser
        during the show — or plug the wall in halfway through the first beat —
        and a client that can only apply deltas comes back blank. The payload
        is a couple of kilobytes on localhost.
        """
        return {
            "type": "state",
            "revision": self.revision,
            "turn_id": self.turn_id,
            "killed": self.killed,
            "open_floor": self.open_floor,
            "cue": self.cue,
            "human_speaking": self.human_speaking,
            "partials": self._partials(),
            "lines": [line.payload() for line in self.lines],
            "line_seq": self.line_seq,
            "agents": [self.agents[agent_id].payload() for agent_id in self.order],
        }

    # ------------------------------------------------------------------ change

    def _fingerprint(self) -> tuple:
        """Everything a repaint depends on, cheaply comparable.

        `StateChanged` fires on nearly every transition and most of them change
        nothing the wall shows. Comparing before and after means the socket
        stays quiet unless the picture actually moved.
        """
        return (
            self.human_speaking,
            tuple((row["speaker"], row["text"]) for row in self._partials()),
            self.line_seq,
            self.cue,
            self.open_floor,
            self.killed,
            self.turn_id,
            tuple(
                (v.state, v.hand, v.muted, v.invited)
                for v in (self.agents[a] for a in self.order)
            ),
        )

    def _bump(self, before: tuple) -> bool:
        if self._fingerprint() == before:
            return False
        self.revision += 1
        return True
