"""What the wall is currently showing, as a value.

`panel_core` decides who speaks; this decides what a 12m LED wall says about
it. The split is deliberate and matches the one the rest of the repo uses:
everything here is a pure function of the events and commands already flowing
through `PanelRuntime`, with no sockets, no clock reads and no browser. The
I/O lives next door in `server.py`.

That purity is not decoration. It is the only reason this can be tested at
all — a video wall is otherwise a thing you can only check by looking at it,
in a venue, once, on the night.

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
)

# How many finalised moderator lines the transcript lane keeps.
#
# The lane is 3m wide and roughly 3.4m of it is usable type; at the current
# size that is about eight lines before the top fade eats them. Keeping a few
# more than fit means a browser refreshed mid-show repaints a full lane
# instead of an empty one, which is the entire reason this is bounded rather
# than either unbounded or exactly what fits.
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
    partial: str = ""
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=TRANSCRIPT_LINES))
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

            case TranscriptUpdated(speaker=speaker) if speaker == HUMAN:
                if event.is_final:
                    text = event.text.strip()
                    if text:
                        self.lines.append(text)
                        self.line_seq += 1
                    self.partial = ""
                else:
                    self.partial = event.text

            case AgentSpeechStarted(agent=agent):
                self.cue = None
                view = self.agents.get(agent)
                if view is not None:
                    view.state = SPEAKING
                    view.hand = None
                # Only one agent is ever on the PA. Anyone else still showing
                # as speaking is a dropped `AgentSpeechEnded`, and on a wall
                # that reads as two agents talking at once.
                for other_id, other in self.agents.items():
                    if other_id != agent and other.state in (SPEAKING, DUCKED):
                        other.state = IDLE

            case AgentSpeechEnded(agent=agent):
                view = self.agents.get(agent)
                if view is not None:
                    view.state = IDLE

            case _:
                pass
        return self._bump(before)

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

        invited = command.extra.get("invited")
        live = command.extra.get("invitation_source") is not None
        # `invited is None` with a live invitation means the floor is open to
        # the whole panel — a real and distinct third state, not "nobody".
        self.open_floor = live and invited is None
        for agent_id, view in self.agents.items():
            view.invited = live and (invited == agent_id or invited is None)

        awaiting = command.extra.get("awaiting")
        if awaiting:
            view = self.agents.get(awaiting)
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
            "partial": self.partial,
            "lines": list(self.lines),
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
            self.partial,
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
