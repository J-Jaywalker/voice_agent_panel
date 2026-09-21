"""The live runtime — every piece wired to every other piece.

    uv run panel                    # full pipeline, real everything
    uv run panel --no-tts           # floor + STT + brains, printed not spoken
    uv run panel --log recordings/rehearsal.jsonl

This is the adapter layer `panel_core` refuses to be. It owns the clock, the
sockets and the audio devices, and it stamps every event with `time.monotonic()`
on the way in. `panel_core` stays a pure reducer, which is what makes the log
this writes replayable through modified floor logic afterwards.

Shape of it:

    mic ──┬─> Silero VAD ──> HumanSpeechStarted/Ended ─┐
          │                                            ├─> FloorController ──> commands
          └─> Agent STT ─────────> TranscriptUpdated ───┤        (pure)              │
                                  TurnYielded ─────────┘                            │
                                                                                    v
        speakers <── Mixer <── TTS <── StartSpeech ·  DuckSpeech · StopSpeech · ResumeSpeech
                                       ^
                                       └── brains (speculative, during the human's turn)

Two rules the wiring exists to enforce, both from CLAUDE.md:

**Agent speech never enters the STT path.** Only mic audio is fed to
Speechmatics. Agent turns enter conversation state as text, because we generated
them and know them verbatim. Anything else is the feedback loop that ends the
show.

**VAD owns stopping, STT owns understanding.** The barge-in reflex fires off
Silero, never off a transcript. Transcripts only ever *refine* a decision the
VAD already made.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import queue
import re
import time
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
from livekit import rtc
from livekit.agents import vad as lkvad
from livekit.plugins import silero
from panel_core import (
    HUMAN,
    AddressDetected,
    AgentAudioProgress,
    AgentProposal,
    AgentSpeechEnded,
    AgentSpeechStarted,
    CueModerator,
    DuckSpeech,
    FloorConfig,
    FloorController,
    HandsRaised,
    HumanSpeechEnded,
    HumanSpeechStarted,
    PanelCast,
    PanelState,
    RequestProposals,
    ResumeSpeech,
    StartSpeech,
    StateChanged,
    StopSpeech,
    Tick,
    TranscriptUpdated,
    TurnYielded,
)
from panel_core.prompts import AMBIGUOUS_VERDICT
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.text import Text

from .address import AddressClassifier, AddressVerdict
from .brains import (
    BrainConfig,
    ProposalComplete,
    SentenceReady,
    SignalsReady,
    StreamingClaudeBrain,
)
from .config import VAD_SAMPLE_RATE, BargeInConfig
from .mixer import Mixer
from .stt import PanelSTT, STTConfig
from .tts import ElevenLabsTTS, TTSConfig

console = Console()

TICK_INTERVAL_S = 0.1  # drives turn-length deadlines and duration classification

# How often a speaking agent reports that sound is still coming out of it.
#
# Well inside `FloorConfig.agent_audio_stall_timeout_s` (2.0s), because the
# watchdog measures the gap between heartbeats: at this interval a healthy turn
# has four chances to report before the floor is taken off it, so one late
# event loop iteration is not a false positive. Cheap — it is a `put_nowait`
# onto a queue the reducer is already draining, and `_agent_audio_progress`
# emits no commands and does not repaint.
HEARTBEAT_INTERVAL_S = 0.5

# How long a `TurnYielded` may be held back waiting for an address verdict.
#
# Speechmatics' `EndOfTurn` lands within a few milliseconds of the final that
# names an agent, so `TurnYielded` normally beats the verdict. Arbitrating first
# means arbitrating with the floor still closed: Ricky gets cued, the panel says
# nothing, and the audience hears the dead air this project spent a week
# removing. So one event — and only that one — waits.
#
# 0.7s against a measured p50 of 526ms and p95 of 781ms to verdict
# (`tests/bench_address.py`, dev box). Deliberately *inside* the p95 rather than
# outside it, because the hold is not free in either direction: every
# millisecond of it is silence on stage, and the fallback when it expires is the
# regex detector, which is correct for all 153 rows of the regression corpus.
# The trade is "the slowest few per cent of verdicts lose the new capability"
# against "every single turn pays the tail", and the first is much the cheaper.
# Re-measure on the venue rig before trusting either number (CLAUDE.md
# § Deployment) — this dial is the first thing to move if the tail is worse
# there.
ADDRESS_HOLD_TIMEOUT_S = 0.7

# How `AddressVerdict.source` reads on the console. Whether a verdict was
# already decided before Ricky stopped talking is the open question about this
# whole approach — free at a cache hit, ~500ms in series with arbitration at a
# fresh call — and nothing measures it today, so every verdict prints one line.
_ADDRESS_SOURCE_LABELS = {
    "speculative_hit": "cache hit",
    "joined": "joined in-flight",
    "fresh": "fresh call",
    "recomputed": "fresh call, partial revised",
    "unavailable": "unavailable — regex fallback",
    "timeout": "timed out — regex fallback",
}


class Candidate:
    """An agent's turn, being written and possibly already being spoken.

    Bridges two different clocks: the model writes at ~40 tokens/sec, the agent
    speaks at ~2.8 words/sec. Writing therefore stays comfortably ahead, and
    `sentences()` simply yields each chunk as it lands — buffered ones
    immediately, later ones as they arrive.
    """

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._closed = False
        # True while `sentences()` is blocked waiting for the model to write
        # the next chunk. Read by `PanelRuntime._pump_heartbeat`, which counts
        # it as progress: an agent whose audio has caught up with its own
        # generation is not a broken audio path, and the liveness watchdog in
        # `panel_core` must not take the floor off it. Bounding *generation*
        # is a separate job with a separate mechanism — see the note in
        # `_pump_heartbeat`.
        self.awaiting_text = False

    async def add(self, sentence: str) -> None:
        await self._queue.put(sentence)

    def close(self) -> None:
        """Synchronous on purpose: it has to be safe from a cancel handler.

        The queue is unbounded, so putting the sentinel never blocks and there
        is nothing to await. Making that explicit matters because the one
        caller that must not be skipped is `_stream_one`'s `CancelledError`
        path — a stream torn down mid-flight while `speak()` is consuming it
        would otherwise leave the sentinel unsent, and `speak()` would wait on
        a queue nobody is ever going to close, holding the floor for the rest
        of the show.
        """
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(None)

    async def sentences(self):
        while True:
            self.awaiting_text = True
            try:
                sentence = await self._queue.get()
            finally:
                # In a `finally` so a cancelled turn does not leave the flag
                # set: a stuck `awaiting_text` would suppress the liveness
                # watchdog for whatever runs next.
                self.awaiting_text = False
            if sentence is None:
                return
            yield sentence

    @classmethod
    def fixed(cls, agent: str, sentences: list[str]) -> Candidate:
        """Wrap an utterance that is already fully known — an introduction's
        fixed text (`FloorController._grant_introduction`) — as a `Candidate`,
        so `speak()` treats it exactly like a streamed one: same TTS calls,
        same mixer, same completion event. There is nothing left to arrive,
        so every sentence is enqueued up front and the candidate is closed
        immediately rather than waiting on a brain that was never asked.
        """
        candidate = cls(agent)
        for sentence in sentences:
            candidate._queue.put_nowait(sentence)
        candidate.close()
        return candidate


class _AudioProgress:
    """Evidence that a turn is still producing sound, for the heartbeat.

    A counter rather than a flag, so `_pump_heartbeat` can tell "audio arrived
    since I last looked" from "audio arrived at some point during this turn".
    Deliberately *not* a liveness ping on the speaking task: a task blocked
    forever awaiting a queue nobody will close is alive in that sense, and is
    exactly the failure the watchdog exists to catch.
    """

    __slots__ = ("chunks",)

    def __init__(self) -> None:
        self.chunks = 0

    def bump(self) -> None:
        self.chunks += 1


class PanelRuntime:
    def __init__(
        self,
        cast: PanelCast,
        *,
        floor_config: FloorConfig | None = None,
        barge_in: BargeInConfig | None = None,
        block_size: int = 256,
        use_tts: bool = True,
        log_path: Path | None = None,
        address_classifier: AddressClassifier | None = None,
    ) -> None:
        self.cast = cast
        self.fc = FloorController(cast, floor_config or FloorConfig())
        self.state = PanelState.for_agents(cast.ids())
        self.barge_in = barge_in or BargeInConfig()
        self.block_size = block_size
        self.use_tts = use_tts
        self.log_path = log_path

        self.mixer = Mixer(cast.ids(), VAD_SAMPLE_RATE)
        self.brain = StreamingClaudeBrain(BrainConfig())
        self.stt = PanelSTT({"ricky": "human"}, config=STTConfig.from_cast(cast))
        self.tts = ElevenLabsTTS(TTSConfig()) if use_tts else None

        # Built only when `FloorConfig.llm_address_detection` is on: it holds an
        # HTTP client and a model choice, and the regex path must cost nothing
        # at all. Injectable so the runtime tests can drive the deferred-
        # TurnYielded logic with no network call and no API key.
        self._address = address_classifier
        if self._address is None and self.fc.config.llm_address_detection:
            self._address = AddressClassifier(cast)

        self.events: asyncio.Queue = asyncio.Queue()
        self._mic: queue.Queue = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._speaking_task: asyncio.Task | None = None
        self._speaking_turn = None
        self._ricky_final_text = ""
        self._ricky_live: Live | None = None
        # Live candidates filling while the human is still talking. The floor
        # decides *who* speaks; this holds *what* they say.
        #
        # Keyed by `(agent, epoch)`, not by agent. Several generations for one
        # agent are deliberately in flight at once — see `_request_proposals` —
        # so "Wayne's words" is ambiguous and `StartSpeech.epoch` is what
        # resolves it.
        self._candidates: dict[tuple[str, int], Candidate] = {}
        # The brain stream feeding each of those candidates, same key. A
        # generation is identified by the `speculation_epoch` it was started
        # against: `epoch` moves when a final transcript segment lands and
        # changes the text being answered.
        self._proposal_tasks: dict[tuple[str, int], asyncio.Task] = {}
        # The turn each generation was started against, so a grant elsewhere
        # can retire work that is answering a moment nobody can act on any
        # more. Kept separate from the key because `turn_id` is not part of a
        # generation's identity — two generations in the same turn differ by
        # epoch, and epoch alone is what `StartSpeech` can name.
        self._proposal_turn: dict[tuple[str, int], int] = {}
        self._running = True
        self._last_intro_remaining: tuple[str, ...] | None = None
        self._last_intro_done = False
        # Diagnostic-only state for `_show_state_change`: what was last
        # printed, so a repaint that changed nothing stays silent.
        self._last_invitation: tuple[str | None, str | None] = (None, None)
        self._last_address_conflict: tuple[str, ...] = ()
        self._last_awaiting: str | None = None
        # True from the moment a synthetic TurnYielded is queued until its
        # arbitration resolves. Blocks a second proposal from queuing another
        # one in the meantime — without it, two proposals landing back to
        # back before the first grant's AgentSpeechStarted comes back around
        # the queue can each re-open arbitration and award the floor to two
        # different agents at once.
        self._rearbitration_inflight = False
        # The specific synthetic TurnYielded this runtime is waiting on, if
        # any. Cleared unconditionally once *that exact event* has finished
        # being reduced — see `_drain_events`. Two panel_core paths
        # (`_turn_yielded`'s already-speaking guard, `_grant`'s
        # missing-proposal path) resolve an arbitration with `state, []`: no
        # `AgentSpeechStarted`, no `CueModerator`. Clearing only on those two
        # commands left the flag stuck forever whenever one of those paths
        # fired, gating every later proposal out of re-arbitration. Identity
        # on the event, not the command it produced, is what makes this
        # robust to outcomes we cannot enumerate from here.
        self._pending_rearbitration_event: TurnYielded | None = None
        # The address classification running against the most recent human
        # final, and the one `TurnYielded` waiting behind it. Deliberately
        # *runtime* state: the reducer gets no new field for this race, it just
        # sees `AddressDetected` and then `TurnYielded`, in that order.
        self._address_task: asyncio.Task | None = None
        self._held_turn: TurnYielded | None = None
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("")

    # ------------------------------------------------------------ console clock

    def _stamp(self) -> str:
        """`HH:MM:SS.mmm`, wall clock.

        Wall clock so a console line can be lined up against a rehearsal
        recording, the venue's own logs or someone's note of when a thing went
        wrong. Milliseconds because the numbers worth arguing about — dead air
        before an agent takes the floor, barge-in reflex latency — are the
        differences between two of these lines.
        """
        return f"[dim]{datetime.now(UTC).astimezone().strftime('%H:%M:%S.%f')[:-3]}[/]"

    def _print(self, markup: str) -> None:
        """Every console line in the runtime goes through here, stamped."""
        console.print(f"{self._stamp()} {markup}")

    # ------------------------------------------------------------ audio thread

    def _callback(self, indata, outdata, frames, timeinfo, status) -> None:
        """PortAudio callback. Never blocks, never awaits, never allocates much."""
        del timeinfo, status
        mono = indata[:, 0]
        pcm = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)

        # Mic audio goes to VAD and STT. Agent audio goes to neither, ever.
        self._mic.put_nowait(mono.copy())
        self.stt.feed("ricky", pcm.tobytes())

        outdata[:, 0] = self.mixer.render(frames)

    # ------------------------------------------------------------- event path

    def emit(self, event) -> None:
        """The single ordered way into the reducer."""
        self.events.put_nowait(event)

    async def _drain_events(self) -> None:
        while self._running:
            event = await self.events.get()
            self._record(event)
            self.state, commands = self.fc.reduce(self.state, event)
            for command in commands:
                await self._execute(command)
            if isinstance(event, AgentSpeechStarted):
                # The floor is genuinely occupied now — safe to consider a
                # fresh re-arbitration once this agent's turn ends.
                self._rearbitration_inflight = False
            if event is self._pending_rearbitration_event:
                # The synthetic TurnYielded we emitted to trigger this
                # arbitration attempt has now been fully reduced, whatever the
                # outcome — a grant, a CueModerator, or one of the silent
                # `state, []` guards in panel_core that emits neither. The
                # attempt is resolved either way, so the gate comes down
                # unconditionally rather than only on the two commands that
                # happen to fire on the happy path.
                self._rearbitration_inflight = False
                self._pending_rearbitration_event = None
            self._maybe_rearbitrate(event)

    def _maybe_rearbitrate(self, event) -> None:
        """Re-open arbitration once a proposal lands with the floor idle.

        `_proposal` (panel_core's `floor.py`) only *stores* a candidate — it
        never grants the floor itself, because a pure reducer must not
        schedule anything for itself. The floor is only granted from inside
        `_turn_yielded`, which fires solely on `TurnYielded`, and this
        runtime's only source of that event is Speechmatics' `EndOfTurn`
        (`stt.py`). With Ricky silent after handing off multiple turns (e.g.
        an open invitation worth more than one turn), nothing would otherwise
        re-trigger arbitration and the panel would stall with a live
        invitation and idle proposals forever. The introduction round used to
        be the motivating example here, but it no longer goes through this
        path at all — it never asks for proposals in the first place, and
        `_turn_yielded` treats a `TurnYielded` arriving mid-round as a no-op
        rather than something to arbitrate (see `panel_core.floor.
        _turn_yielded` and `_advance_introductions`).

        `panel_sim` has the same requirement and synthesises `TurnYielded`
        once after gathering a batch of proposals (see its `_gather`). Here
        proposals stream in one at a time over the wire, so the check runs
        after each one instead — it is a no-op once someone is already
        speaking or the invitation is spent.
        """
        if not isinstance(event, AgentProposal) or self._rearbitration_inflight:
            return
        state = self.state
        invitation = state.invitation
        if (
            state.speaking is None
            and state.floor_holder is None
            and not state.human_speaking
            and invitation is not None
            and invitation.is_live()
        ):
            self._rearbitration_inflight = True
            turn_yielded = TurnYielded(t=time.monotonic())
            self._pending_rearbitration_event = turn_yielded
            self.emit(turn_yielded)

    def _record(self, event) -> None:
        if not self.log_path:
            return
        payload = {"type": type(event).__name__}
        if is_dataclass(event):
            payload |= {k: _jsonable(v) for k, v in asdict(event).items()}
        with self.log_path.open("a") as fh:
            fh.write(json.dumps(payload) + "\n")

    async def _execute(self, command) -> None:
        match command:
            case RequestProposals():
                self._print(f"  [dim]… gathering proposals ({command.reason})[/]")
                self._request_proposals(command.agents)

            case StartSpeech():
                self._start_speaking(command)

            case StopSpeech():
                # Audible immediately; the provider is told afterwards.
                self.mixer.stop(command.agent)
                task = self._speaking_task
                if self._speaking_turn is not None:
                    self._speaking_turn.cancel()
                if task is not None:
                    task.cancel()
                self._print(
                    f"  [red]⏹ {self.cast[command.agent].name}[/] "
                    f"[dim]({command.reason.value})[/]"
                )
                # `speak()`'s own CancelledError handler emits AgentSpeechEnded
                # with the utterance actually spoken so far. Emitting it again
                # here would double-fire the floor's continuation logic (e.g.
                # popping the same agent out of an introduction round twice).
                # Only fall back to emitting it ourselves when there is no
                # task to cancel — nothing else will ever report the stop.
                if task is None:
                    self.emit(
                        AgentSpeechEnded(t=time.monotonic(), agent=command.agent, completed=False)
                    )

            case DuckSpeech():
                self.mixer.duck(command.agent, command.gain_db, command.ramp_ms)

            case ResumeSpeech():
                self.mixer.resume(command.agent, command.ramp_ms)

            case HandsRaised():
                hands = "  ".join(
                    f"{self.cast[a].name} {s:.2f}" for a, s in command.agents
                )
                self._print(f"  [yellow]✋ wants in:[/] {hands} [dim](not invited)[/]")

            case CueModerator():
                # Arbitration resolved with nobody granted — the floor is
                # still idle, so a later proposal is free to try again.
                self._rearbitration_inflight = False
                # `command.reason` is a `CueReason` — a `str`-mixin `Enum`, so
                # plain interpolation prints "CueReason.NO_PROPOSALS" rather
                # than the value. `.value` is what tells "nobody proposed"
                # apart from "the one agent Ricky named had nothing", which is
                # the entire point of the enum replacing the old undifferen-
                # tiated `no_candidate` string.
                self._print(f"  [magenta]▸ back to Ricky ({command.reason.value})[/]")

            case StateChanged():
                self._show_state_change(command)

            case _:
                pass

    def _show_state_change(self, command: StateChanged) -> None:
        """Surface intro-round progress and invitation/addressee diagnostics.

        `StateChanged` fires on nearly every transition, so this only prints
        on the fields that changed rather than on every paint. The invitation
        fields are the whole reason this rewrite happened: the original
        failure printed `▸ back to Ricky (no_candidate)` twice with no way to
        tell "nobody proposed" from "the invitation was held by the wrong
        agent" — that answer was sitting in `extra["invited"]` all along, just
        never rendered.
        """
        remaining = command.extra.get("intro_remaining")
        if remaining != self._last_intro_remaining:
            self._last_intro_remaining = remaining
            if remaining:
                names = ", ".join(self.cast[a].name for a in remaining)
                self._print(f"  [dim]intros: {len(remaining)} remaining — {names}[/]")

        done = command.extra.get("intro_done")
        if done and not self._last_intro_done:
            self._last_intro_done = True
            self._print("  [dim]intros: all done[/]")

        invited = command.extra.get("invited")
        source = command.extra.get("invitation_source")
        if (invited, source) != self._last_invitation:
            self._last_invitation = (invited, source)
            if source is None:
                self._print("  [dim]floor: closed (no live invitation)[/]")
            else:
                who = self.cast[invited].name if invited else "the panel"
                role = command.extra.get("invitation_role") or "-"
                rule = command.extra.get("invitation_rule") or "-"
                self._print(
                    f"  [dim]floor: invited {who} — {source}/{role} ({rule})[/]"
                )

        awaiting = command.extra.get("awaiting")
        if awaiting != self._last_awaiting:
            self._last_awaiting = awaiting
            if awaiting:
                self._print(
                    f"  [dim]… holding for {self.cast[awaiting].name} "
                    f"({self.fc.config.invited_agent_grace_s:.1f}s)[/]"
                )

        conflict = command.extra.get("address_conflict") or ()
        if conflict != self._last_address_conflict:
            self._last_address_conflict = conflict
            if conflict:
                names = ", ".join(self.cast[a].name for a in conflict)
                self._print(
                    f"  [yellow]✋ ambiguous address:[/] {names} [dim](floor stays closed)[/]"
                )

    # --------------------------------------------------------------- proposals

    def _request_proposals(self, agents: tuple[str, ...]) -> None:
        """Speculate during the human's turn. Generations race; none is discarded.

        The floor re-requests proposals on roughly every 0.8s of partial
        transcript and unconditionally on every final segment, while a brain
        takes 1.7-2.4s to reach `SignalsReady` (`tests/bench_brains.py`). Those
        two facts do not fit together under any cancel-and-restart rule, and
        this method has now been wrong in both directions:

        Cancelling on every *request* meant no agent ever finished at all.
        Cancelling on every *final* — the version this replaces — looked
        careful and was worse, because of where the finals fall. Speechmatics'
        `EndOfTurn` arrives within a few milliseconds of the final that names an
        agent, so the last teardown of a turn always landed at the exact instant
        the work was needed. Arbitration ran against an empty proposal set on
        every invitation and the full cold generation latency sat on the
        critical path, which on stage was about six seconds of Ricky covering
        for a panel that had thrown its answer away.

        So nothing is cancelled here. Every round starts a *new* generation per
        agent and leaves the running ones alone; they all finish; `panel_core`'s
        `_proposal` keeps whichever is newest by epoch rather than whichever
        arrives last. An answer to "Thanks, everybody. Um, so," is a poor answer
        to "where are we on the adoption curve" — but it exists, it is one
        `_stale` check away from being rejected on its own merits, and having it
        is strictly better than having nothing. Cost is not a constraint
        (CLAUDE.md): a discarded proposal is cheap and a silence in front of 400
        people is not.

        *Every* round, and that word is the fix to the second half of the same
        stage failure. `speculation_epoch` used to move only when a final
        landed, so every speculative round inside one human turn asked under
        the same `(agent, epoch)` key — and the still-running check below then
        refused to start a second generation for an agent whose first one had
        not finished. Each agent therefore got at most *one* in-flight
        generation per human turn. The two fast agents finished in ~2.1s, freed
        their key, and were re-asked against a later partial; the slow one
        (3967ms, spanning Ricky's entire question) was not, so its single answer
        was necessarily written against the oldest input of the three. The
        slower the agent, the staler the input behind its winning line — every
        single time, and always the same agent. `_ask_for_proposals` now takes a
        fresh label per round, so a slow generation no longer holds the key
        against its own replacement.

        The cost of that is concurrency: a round opens at most every
        `speculation_interval_s` (0.8s) and a generation lives 2-4s, so expect
        up to ~5 in flight per agent during a long human turn. That is the
        number to watch on the venue rig — not the spend, which is not a
        constraint, but provider concurrency limits and any TTFB degradation
        under it. `speculation_interval_s` is the dial if the rig cannot take
        it; measure before moving it (CLAUDE.md § Deployment).

        Two things are still retired, both after the fact rather than
        pre-emptively, and neither can leave an agent with nothing:

        * `_retire_superseded` drops an agent's older generations once a newer
          one has actually produced a proposal — never before, so the fallback
          is only released when something better is genuinely in hand.
        * A turn boundary (`_retire_stale_turns`) drops work scored against a
          conversational moment a grant has since closed.

        The agent currently on the PA is never touched by either: its stream may
        still be feeding `speak()`'s `Candidate` queue live, and cancelling it
        would cut off audio already playing.

        Each agent streams. Signals arrive first and go straight to the floor
        controller, so arbitration can run while the text is still being
        written. Sentences accumulate in a `Candidate`, ready to be spoken the
        moment that agent is granted the floor.
        """
        snapshot = self.state
        epoch = snapshot.speculation_epoch
        turn_id = snapshot.turn_id
        speaking = snapshot.speaking
        # The transcript timestamp this round's input is frozen at — which
        # question these generations are answers to. `panel_core`'s
        # `_ask_for_proposals` stamped it, alongside the epoch above, in the
        # same `reduce()` call that produced the command being executed here,
        # and `_drain_events` installs the new state before running any command
        # — so `self.state` already carries this round's pair and the two can
        # never be read from different rounds. It rides out on every
        # `AgentProposal` as `input_t`, and `FloorController._stale` measures
        # it. Read anything off `snapshot`, never off `self.state` again below:
        # a grant landing mid-loop would otherwise split the round in two.
        input_t = snapshot.last_proposal_request_t

        self._retire_stale_turns(turn_id, speaking)

        for agent_id in agents:
            if agent_id == speaking:
                continue
            key = (agent_id, epoch)
            existing = self._proposal_tasks.get(key)
            if existing is not None and not existing.done():
                # Unreachable as the code stands, and kept as a guard rather
                # than deleted. `_ask_for_proposals` takes a fresh epoch per
                # round and no single `reduce()` emits two `RequestProposals`,
                # so no key can be asked twice — but this is what enforces "at
                # most one live generation per key", and the key is what
                # `StartSpeech.epoch` uses to find an agent's words. Without
                # it, a second generation on one key would overwrite
                # `_candidates[key]` and orphan the first task with nothing
                # left holding a reference to cancel it. Cheap insurance
                # against the epoch ever stopping being per-round; if it does,
                # this skip is the bug it caused, not the cause.
                continue
            candidate = Candidate(agent_id)
            self._candidates[key] = candidate
            self._proposal_turn[key] = turn_id
            self._proposal_tasks[key] = asyncio.create_task(
                self._stream_one(candidate, snapshot, epoch, input_t),
                name=f"propose-{agent_id}-e{epoch}",
            )

    def _retire_superseded(self, agent_id: str, epoch: int) -> None:
        """Drop `agent_id`'s generations older than `epoch`.

        Called only once `epoch` has produced a proposal. That ordering is the
        whole safety property: an older generation is the fallback answer until
        a better one exists, so it is released when the replacement is in hand
        and not a moment earlier. Doing this at request time instead is exactly
        the bug this design replaces.

        Per-round epochs make this fire on every round rather than only at a
        turn boundary, so the *window* between dropping the old candidates and
        the replacement proposal being reduced now matters. There isn't one:
        `_stream_one` calls this and then `emit`s the replacement with no
        `await` in between, and `_drain_events` only ever suspends on an empty
        queue — so nothing can arbitrate against a dropped candidate, because
        the proposal that replaces it is already queued ahead of any event that
        could. Keep those two statements adjacent.
        """
        if agent_id == self.state.speaking:
            return
        for key in [k for k in self._proposal_tasks if k[0] == agent_id and k[1] < epoch]:
            task = self._proposal_tasks.pop(key)
            if not task.done():
                task.cancel()
            self._proposal_turn.pop(key, None)
            self._candidates.pop(key, None)

    def _retire_stale_turns(self, turn_id: int, speaking: str | None) -> None:
        """Drop work generated for a turn that has since been granted away.

        `turn_id` moves when someone takes the floor, so these proposals were
        scored against a conversational moment nobody can act on any more. This
        is a genuine supersede rather than a guess about freshness, which is
        why it is safe to do pre-emptively where the epoch test was not.
        """
        for key in [k for k in self._proposal_tasks if self._proposal_turn.get(k, turn_id) != turn_id]:
            if key[0] == speaking:
                continue
            task = self._proposal_tasks.pop(key)
            if not task.done():
                task.cancel()
            self._proposal_turn.pop(key, None)
            self._candidates.pop(key, None)

    async def _stream_one(
        self, candidate: Candidate, snapshot: PanelState, epoch: int, input_t: float
    ) -> None:
        """Run one generation and report it.

        Args:
            candidate: Where the streamed sentences accumulate, ready for
                `speak()` if this generation wins the floor.
            snapshot: The conversation state this generation answers. Frozen at
                request time, which is what makes `input_t` meaningful.
            epoch: This round's label, from `PanelState.speculation_epoch`.
            input_t: The transcript timestamp `snapshot` was taken at. Goes out
                on the `AgentProposal` so `FloorController._stale` can judge
                whether this line is an answer to the question that eventually
                opened the floor, rather than to the preamble in front of it.
        """
        persona = self.cast[candidate.agent]
        spoke = False
        try:
            async for event in self.brain.stream(persona, snapshot):
                if isinstance(event, SignalsReady):
                    # The floor can be arbitrated now. The utterance is carried
                    # by the Candidate, not by the event — the reducer decides
                    # who speaks and never needs to know what they will say.
                    self._print(
                        f"  [dim]· {persona.name} ready ({event.elapsed_ms:.0f}ms, "
                        f"e{epoch})[/]"
                    )
                    # Now — and only now — this agent's older generations are
                    # surplus. Retiring them here rather than when the newer
                    # request went out is what guarantees the fallback outlives
                    # its replacement's latency. See `_retire_superseded`.
                    self._retire_superseded(candidate.agent, epoch)
                    self.emit(
                        AgentProposal(
                            t=time.monotonic(),
                            agent=candidate.agent,
                            utterance="",
                            signals=event.signals,
                            epoch=epoch,
                            # Not `t`: the floor has to know how old this
                            # line's *input* was, and a generation that raced
                            # past the question it was written before would
                            # otherwise arrive looking newer than the question.
                            input_t=input_t,
                        )
                    )
                elif isinstance(event, SentenceReady):
                    spoke = True
                    await candidate.add(event.text)
                elif isinstance(event, ProposalComplete):
                    candidate.close()
        except asyncio.CancelledError:
            # Close before re-raising. `speak()` may already be consuming this
            # queue — the agent whose turn is being granted is only protected
            # from `_request_proposals` once `AgentSpeechStarted` has come back
            # around the event queue and set `state.speaking`, and until then a
            # turn-superseded teardown can reach a stream that is feeding live
            # audio. Closing ends that turn with whatever was actually said;
            # leaving it open wedges the floor.
            candidate.close()
            raise
        except Exception as exc:  # noqa: BLE001 — one dead brain must not stop the panel
            self._print(f"  [red]x {candidate.agent} brain failed: {str(exc)[:60]}[/]")
            candidate.close()
        finally:
            if not spoke and self._candidates.get((candidate.agent, epoch)) is candidate:
                # This generation produced no speakable text, so it must not be
                # left parked where a grant can find it. `state.proposals` can
                # still hold an *earlier* generation's hand-raise for the same
                # agent — the proposal and the candidate are separate objects
                # with separate lifetimes, and `_request_proposals` replaces the
                # candidate whenever the previous stream has finished. Dropping
                # it makes `_start_speaking` report an agent with nothing to say
                # instead of printing a name over silence, which is the whole
                # failure this guard exists for. The identity check is what
                # keeps a late teardown from evicting a newer, live candidate.
                del self._candidates[(candidate.agent, epoch)]

    # ------------------------------------------------------------------ speech

    def _start_speaking(self, command: StartSpeech) -> None:
        """Dispatch a grant, honouring `command.lead_in_s` if it has one.

        A non-zero `lead_in_s` (introduction round only) is a silent beat
        before this agent's first word — the instant jump from Ricky's cue
        straight to Dexter's opening line read as a glitch on stage-adjacent
        testing, not a person taking a moment to go first. Scheduled as its
        own task rather than an `await` inline here: `_execute` runs on
        `_drain_events`'s single event loop, and blocking that loop for the
        pause would stall every other event — barge-in included — for its
        duration. Everything else about the grant is unaffected; it just
        starts a beat later.
        """
        if command.lead_in_s:
            asyncio.create_task(
                self._start_speaking_after_delay(command), name=f"lead-in-{command.agent}"
            )
        else:
            self._start_speaking_now(command)

    async def _start_speaking_after_delay(self, command: StartSpeech) -> None:
        await asyncio.sleep(command.lead_in_s)
        self._start_speaking_now(command)

    def _start_speaking_now(self, command: StartSpeech) -> None:
        persona = self.cast[command.agent]
        candidate: Candidate | None = None
        if command.utterance:
            # A fixed line, not a generated one, and it wins unconditionally.
            # An ordinary grant's `Proposal.utterance` is always `""` in this
            # runtime (the real words live in a `Candidate`, filled in by
            # `_stream_one` as the model streams), so a *non-empty*
            # `command.utterance` means the words are already fully known and
            # there was never anything to stream from a model. Today the
            # introduction round is the only source of these
            # (`FloorController._grant_introduction`).
            #
            # This must not defer to a cached candidate, and that is the bug
            # this branch was written wrong for: it used to require
            # `self._candidates.get(...) is None`, which held only in theory.
            # Ricky's opening sentence arrives as a long run of *partial*
            # transcripts, each one re-firing speculation every 0.8s
            # (`scoring.speculation_interval_s`), while the introduction latch
            # in `panel_core.floor` fires only on the *final* transcript. So by
            # the time the round starts, `_request_proposals` has already parked
            # a speculative candidate for every agent, the fixed-text branch was
            # skipped, and each agent read out the model's improvised line
            # instead of the hand-authored `Persona.introduction` — the one
            # piece of the show that was deliberately taken away from the model
            # so it could not come back empty on stage. Any candidate sitting
            # here is speculation about a moment that has passed; it is
            # discarded, not preferred.
            #
            # `_grant_introduction` has already run the text through
            # `sanitise()` — fixed text does not get to bypass that rule just
            # because nobody generated it live (CLAUDE.md) — so it is spoken
            # as-is and must not be sanitised a second time. The sentence split
            # is only for pacing symmetry with a streamed turn: the whole line
            # is already known, so there is no generation latency to save.
            sentences = [s for s in re.split(r"(?<=[.!?])\s+", command.utterance) if s]
            if sentences:
                # Tear down this agent's stale speculation before installing
                # the fixed candidate, so nothing is left running that could
                # write into a queue nobody reads, or evict what we install.
                # The eviction race is real: `_stream_one`'s `finally` does
                # `del self._candidates[agent]` for a generation that produced
                # no speakable text. Two things close it, in this order. First,
                # `_start_speaking` is synchronous and never awaits, so a
                # cancelled task cannot reach its `finally` until we have
                # returned to the event loop — by which point the fixed
                # candidate is already in place. Second, that `finally` is
                # guarded by an identity check against its *own* candidate, so
                # once ours is the one in the dict a late teardown no longer
                # matches and leaves it alone. Cancelling first is therefore
                # safe rather than load-bearing, but it keeps the teardown
                # shape identical to `_request_proposals`. The cancellation
                # also runs `_stream_one`'s `CancelledError` path, which closes
                # the *old* candidate — correct: nothing is consuming it, and
                # an unclosed queue is what wedges the floor.
                #
                # *Every* generation for this agent goes, not just one: several
                # run concurrently now, and a fixed line supersedes all of them
                # unconditionally.
                for key in [k for k in self._proposal_tasks if k[0] == command.agent]:
                    stale = self._proposal_tasks.pop(key)
                    if not stale.done():
                        stale.cancel()
                    self._proposal_turn.pop(key, None)
                    self._candidates.pop(key, None)
                candidate = Candidate.fixed(command.agent, sentences)
                self._candidates[(command.agent, command.epoch)] = candidate
        else:
            # The ordinary streamed grant: the words are still arriving, so the
            # candidate `_request_proposals` parked is the whole point.
            # `command.epoch` names *which* of this agent's parked candidates
            # won arbitration — the floor scored one specific generation's
            # signals, and speaking a different generation's words would air an
            # answer nobody arbitrated.
            candidate = self._candidates.get((command.agent, command.epoch))
        if candidate is None:
            # Either nothing was ever requested for this agent, or the stream
            # that was requested finished without producing a speakable word
            # and `_stream_one` dropped it. Both mean the same thing here and
            # both must be *loud*: the failure this replaces was an agent's
            # name appearing on stage with nothing under it and the turn quietly
            # consumed, which from the console was indistinguishable from an
            # agent choosing to say nothing.
            self._print(f"  [red]x {persona.name} has nothing to say — turn skipped[/]")
            self.emit(
                AgentSpeechEnded(t=time.monotonic(), agent=command.agent, completed=False)
            )
            return

        console.print()
        self._print(f"[bold cyan]{persona.name}[/]")
        self.emit(AgentSpeechStarted(t=time.monotonic(), agent=command.agent))

        progress = _AudioProgress()
        heartbeat = asyncio.create_task(
            self._pump_heartbeat(command.agent, progress, candidate),
            name=f"heartbeat-{command.agent}",
        )

        async def speak() -> None:
            spoken: list[str] = []
            try:
                if self.tts is None:
                    # --no-tts: hold the floor for a plausible speaking duration
                    # so floor behaviour can be exercised without audio.
                    async for sentence in candidate.sentences():
                        spoken.append(sentence)
                        self._print(f"  {sentence}")
                        # Slept in slices rather than one long sleep so the
                        # heartbeat keeps reporting through a long sentence, and
                        # the watchdog is therefore exercised in --no-tts too.
                        # A single `sleep(len/2.8)` is 10s for a 30-word
                        # sentence, well past `agent_audio_stall_timeout_s`, so
                        # the floor would be taken off every printed turn.
                        remaining = len(sentence.split()) / 2.8
                        while remaining > 0:
                            slice_s = min(remaining, HEARTBEAT_INTERVAL_S / 2)
                            await asyncio.sleep(slice_s)
                            remaining -= slice_s
                            progress.bump()
                else:
                    turn = await self.tts.open(voice_id=persona.voice_id)
                    self._speaking_turn = turn
                    self.mixer.clear(command.agent)

                    async def pump_audio() -> None:
                        async for chunk in turn.chunks():
                            self.mixer.feed(command.agent, chunk)
                            # One bump per chunk off the socket: the heartbeat's
                            # primary evidence, and the only one that
                            # distinguishes a live provider from a dead one.
                            progress.bump()

                    audio = asyncio.create_task(pump_audio(), name="tts-audio")
                    async for sentence in candidate.sentences():
                        spoken.append(sentence)
                        # Stamped when the sentence is *pushed*, which runs
                        # ahead of the audio — the model writes faster than
                        # the agent speaks (`Candidate`). Read these gaps as
                        # generation pace, not as what the room hears.
                        self._print(f"  {sentence}")
                        await turn.push(sentence)
                    await turn.finish()
                    await audio
                    self.mixer.finish(command.agent)
                    while not self.mixer.is_drained(command.agent):
                        await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                # Interrupted. What was said still counts — record it.
                self.emit(
                    AgentSpeechEnded(
                        t=time.monotonic(),
                        agent=command.agent,
                        completed=False,
                        utterance=" ".join(spoken),
                    )
                )
                return
            except Exception as exc:  # noqa: BLE001 — see below; this must not be fatal
                # Every raising path in the block above used to kill this task
                # silently: `tts.open()` on a dead socket, `ElevenLabsTTS._guard`
                # refusing a sentence as empty or as markup, `push()` on a socket
                # that died mid-turn. `AgentSpeechEnded` was then never emitted,
                # so `state.speaking` stayed pinned to this agent for the rest of
                # the show — and nobody saw the traceback either, because this
                # task is only ever cancelled, never awaited, so asyncio reported
                # it as "Task exception was never retrieved" at GC time.
                #
                # `_stalled_speaker` in `panel_core` is the backstop and would
                # recover the floor within `agent_audio_stall_timeout_s` even
                # without this arm. Reporting it here is still worth it: the
                # floor recovers immediately rather than two seconds later, the
                # console says which agent broke and why, and the turn is
                # recorded as `completed=False` with the words actually spoken
                # instead of being inferred from silence.
                self._print(
                    f"  [red]x {persona.name} speech failed: {str(exc)[:80]}[/]"
                )
                self.emit(
                    AgentSpeechEnded(
                        t=time.monotonic(),
                        agent=command.agent,
                        completed=False,
                        utterance=" ".join(spoken),
                    )
                )
                return
            finally:
                heartbeat.cancel()
                self._speaking_turn = None
                # The turn is over, so this generation's words are spent. Only
                # this one: any other generation for the same agent is a fresh
                # answer to a later moment and must survive the turn that just
                # ended.
                self._candidates.pop((command.agent, command.epoch), None)

            self.emit(
                AgentSpeechEnded(
                    t=time.monotonic(),
                    agent=command.agent,
                    completed=True,
                    utterance=" ".join(spoken),
                )
            )

        self._speaking_task = asyncio.create_task(speak(), name=f"speak-{command.agent}")

    async def _pump_heartbeat(
        self, agent: str, progress: _AudioProgress, candidate: Candidate
    ) -> None:
        """Report, while `agent` holds the PA, that sound is still coming out.

        Consumed by `FloorController._stalled_speaker`, which takes the floor
        off an agent that stops reporting. So this must only ever fire on real
        evidence, and there are two kinds:

        * `progress.chunks` advanced — audio arrived from the provider (or, in
          --no-tts, the printed turn advanced).
        * the mixer's buffer *shrank* — nothing new arrived, but the room is
          hearing what did. Without this the tail of every turn reads as a
          stall: `turn.finish()` is followed by a drain of whatever is
          buffered, during which no chunk arrives by definition.

        Shrank, not merely non-empty, and that distinction is load-bearing.
        "The buffer has audio in it" is true forever if the audio device has
        faulted and `Mixer.render` is no longer being called from the PortAudio
        callback — which is one of the failures being caught, and is also the
        one that hangs `speak()`'s unbounded `while not is_drained` loop. A
        buffer that is not going down is not being played.

        Neither test is a check that this coroutine, or the speaking task, is
        running. That is the whole design: a `speak()` blocked forever on a
        `Candidate` queue nobody will *close* would pass any liveness ping, and
        is another of the failures being caught.

        There is one deliberate blind spot, `Candidate.awaiting_text`. If the
        agent's audio has caught up with the model still writing its turn, the
        buffer empties and no chunk arrives — and that is not a broken audio
        path, so it counts as progress and the floor is left alone. It does
        mean a brain stream that hangs *mid-turn* is not caught here. That is
        the right split: bounding generation belongs to the generation, and
        `StreamingClaudeBrain.stream` currently has no timeout of its own
        (`BrainConfig.timeout_s` is wired only to the non-streaming
        `ClaudeBrain.propose`), so it is bounded by the Anthropic client's
        600s read timeout. Fixing that is a separate change; conflating the two
        here would only trade a real hang for a false positive on every agent
        whose first sentence is short.

        Cancelled from `speak()`'s `finally`, so it cannot outlive the turn and
        refresh the clock for the next one. `_agent_audio_progress` ignores
        heartbeats for an agent that is not `state.speaking` anyway, which
        covers the window between cancellation and the reducer catching up.
        """
        seen = progress.chunks
        buffered = self.mixer.buffered_seconds(agent)
        while self._running:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            buffered_now = self.mixer.buffered_seconds(agent)
            advanced = (
                progress.chunks != seen
                or buffered_now < buffered
                or candidate.awaiting_text
            )
            seen = progress.chunks
            buffered = buffered_now
            if advanced:
                self.emit(AgentAudioProgress(t=time.monotonic(), agent=agent))

    # -------------------------------------------------------------------- pumps

    async def _run_vad(self) -> None:
        """The barge-in reflex. Never waits on a transcript."""
        detector = silero.VAD.load(
            sample_rate=VAD_SAMPLE_RATE,
            min_speech_duration=self.barge_in.min_speech_duration,
            min_silence_duration=self.barge_in.min_silence_duration,
            activation_threshold=self.barge_in.activation_threshold,
        )
        stream = detector.stream()
        speaking = False

        async def pump() -> None:
            while self._running:
                try:
                    chunk = self._mic.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.002)
                    continue
                pcm = (np.clip(chunk, -1, 1) * 32767).astype(np.int16)
                stream.push_frame(rtc.AudioFrame(pcm.tobytes(), VAD_SAMPLE_RATE, 1, len(pcm)))

        pump_task = asyncio.create_task(pump(), name="vad-pump")
        try:
            async for ev in stream:
                if ev.type == lkvad.VADEventType.INFERENCE_DONE:
                    # Fire ahead of Silero's own debounce: this is the fast path,
                    # and duck-first makes a false positive cheap.
                    if not speaking and ev.probability >= self.barge_in.duck_probability:
                        speaking = True
                        self.emit(HumanSpeechStarted(t=time.monotonic()))
                elif ev.type == lkvad.VADEventType.END_OF_SPEECH:
                    speaking = False
                    self.emit(HumanSpeechEnded(t=time.monotonic()))
        finally:
            pump_task.cancel()

    def _ricky_renderable(self, partial: str = "") -> Text:
        line = Text.from_markup(self._stamp())
        line.append("   ")
        line.append("Ricky: ", style="bold")
        line.append(self._ricky_final_text)
        if partial:
            if self._ricky_final_text:
                line.append(" ")
            line.append(partial, style="dim")
        return line

    def _render_ricky_line(self, partial: str = "") -> None:
        if self._ricky_live is None:
            self._ricky_live = Live(console=console, auto_refresh=False, transient=False)
            self._ricky_live.start()
        self._ricky_live.update(self._ricky_renderable(partial), refresh=True)

    def _close_ricky_line(self) -> None:
        if self._ricky_live is not None:
            # Repainted once more so the stamp left on screen is `TurnYielded`
            # — the moment Ricky stopped, not the moment he started.
            self._ricky_live.update(self._ricky_renderable(), refresh=True)
            self._ricky_live.stop()
            self._ricky_live = None
        self._ricky_final_text = ""

    async def _run_stt(self) -> None:
        """Forward Speechmatics events into the single ordered event path.

        `TranscriptUpdated` is emitted immediately, always. It drives the
        barge-in content check and speculative generation, and delaying it by
        even one classifier round trip would undo both.

        With `--llm-address` on, a human final additionally kicks off address
        classification — non-blocking; this loop must keep pumping or every
        other STT event stalls behind it — and `TurnYielded`, and only
        `TurnYielded`, is held back until that verdict lands. See
        `_defer_turn_yielded`.
        """
        while self._running:
            event = await self.stt.events.get()
            if isinstance(event, TranscriptUpdated):
                if event.is_final:
                    self._ricky_final_text = f"{self._ricky_final_text} {event.text}".strip()
                    self._render_ricky_line()
                else:
                    self._render_ricky_line(event.text)
                self.emit(event)
                self._classify_address_from(event)
                continue
            if isinstance(event, TurnYielded):
                self._close_ricky_line()
                if not self._defer_turn_yielded(event):
                    self._end_of_turn(event)
                continue
            self.emit(event)

    # ----------------------------------------------------------- addressing

    def _classify_address_from(self, event: TranscriptUpdated) -> None:
        """Feed one transcript segment to the address classifier.

        Partials go to `speculate()`, which is fire-and-forget and gated, so
        the answer to the question Ricky is still asking is usually already
        cached by the time he finishes it. Finals go to `classify()`, whose
        verdict becomes an `AddressDetected` event.
        """
        classifier = self._address
        if classifier is None or event.speaker != HUMAN:
            return
        if not event.is_final:
            classifier.speculate(event.text)
            return

        # One classification outstanding at a time. A turn arrives as several
        # finals and only the last of them can be the question; an earlier one
        # is answering text that has since been extended.
        previous = self._address_task
        self._address_task = asyncio.create_task(
            self._classify_address(classifier, event.text, t=event.t),
            name="address-classify",
        )
        if previous is not None and not previous.done():
            # Cancelled *after* the replacement is installed, and that order is
            # load-bearing: the cancelled task's `finally` checks whether it is
            # still the current classification before releasing a held
            # `TurnYielded`, so installing first is what stops it letting go
            # early and arbitrating ahead of the newer verdict.
            previous.cancel()

    async def _classify_address(
        self, classifier: AddressClassifier, text: str, *, t: float
    ) -> None:
        """Classify one human final and emit the verdict as an event.

        Bounded by `ADDRESS_HOLD_TIMEOUT_S` — the same number that bounds the
        hold — so every human final produces exactly one `AddressDetected`, and
        it always arrives *before* arbitration rather than after it. A verdict
        allowed to land late could only install a surprise invitation on top of
        a moderator cue that had already gone out; on stage, predictable beats
        salvaged. On expiry the event carries `verdict=None`, which is the
        reducer's instruction to fall back to the regex.
        """
        current = asyncio.current_task()
        started = time.monotonic()
        try:
            try:
                outcome = await asyncio.wait_for(
                    classifier.classify(text), ADDRESS_HOLD_TIMEOUT_S
                )
            except TimeoutError:
                outcome = AddressVerdict(
                    verdict=None,
                    agent=None,
                    reason=f"no verdict within {ADDRESS_HOLD_TIMEOUT_S * 1000:.0f}ms",
                    latency_ms=(time.monotonic() - started) * 1000.0,
                    source="timeout",
                )
            detected = self._as_detected(outcome, text=text, t=t)
            # Emitted before it is rendered, deliberately. `emit` is a
            # `put_nowait` and nothing is reduced until this task yields, so
            # the console ordering is unaffected — but a console line that
            # somehow threw would otherwise take the verdict with it, and the
            # floor would be released with no invitation and no fallback.
            self.emit(detected)
            self._show_address_verdict(detected)
        finally:
            # Release the held `TurnYielded` unless a newer final has taken over
            # this turn's classification — then the hold is that task's to
            # release, and letting go here would arbitrate before its verdict
            # lands. This runs on cancellation too, on purpose: a held
            # `TurnYielded` that is never released is a panel that never
            # arbitrates again, which is the worst failure this file can cause.
            if self._address_task is current:
                self._release_held_turn()

    def _as_detected(
        self, outcome: AddressVerdict, *, text: str, t: float
    ) -> AddressDetected:
        """Translate a classifier result into the event the reducer reads.

        `t` is the *final's* timestamp, not the verdict's arrival time. The
        invitation has to be dated to the question, or `named_proposal_lookback_
        s` and the invitation TTL would quietly measure something different on
        this path than on the regex one — and the classifier's own cost is
        already carried separately, on `latency_ms`.
        """
        return AddressDetected(
            t=t,
            text=text,
            verdict=outcome.verdict,
            agent=outcome.agent,
            # The verdict token is one word and cannot name who tied. The floor
            # needs a non-empty set to report `AMBIGUOUS_ADDRESS` at all, so the
            # cast stands in for "between these, and we cannot say which".
            conflict=self.cast.ids() if outcome.verdict == AMBIGUOUS_VERDICT else (),
            reason=outcome.reason,
            latency_ms=outcome.latency_ms,
            source=outcome.source,
        )

    def _defer_turn_yielded(self, event: TurnYielded) -> bool:
        """Hold `TurnYielded` back if this final's verdict is still outstanding.

        Speechmatics' `EndOfTurn` lands within a few milliseconds of the final
        that names an agent, so without this the floor is arbitrated before the
        invitation exists: floor closed, Ricky cued, dead air — the exact bug
        removed the week before this was written. Only `TurnYielded` ever waits;
        everything else, `TranscriptUpdated` above all, goes straight through.

        Args:
            event: The end-of-turn event Speechmatics just produced.

        Returns:
            True if this runtime has taken the event over and will emit it
            later, False if the caller should emit it now.
        """
        task = self._address_task
        if task is None or task.done():
            return False
        if self._held_turn is not None:
            # Already holding one. Emitting this now would put it *ahead* of
            # the one already waiting, and holding both would let
            # `_turn_yielded` arbitrate twice for one end of turn — the second
            # run before `AgentSpeechStarted` had set `state.speaking`, which is
            # how two agents end up granted the floor at once. One is enough:
            # a `TurnYielded` carries nothing beyond "the turn ended".
            return True
        self._held_turn = event
        self._print("  [dim]⌛ holding end-of-turn for the address verdict[/]")
        return True

    def _release_held_turn(self) -> None:
        """Emit the `TurnYielded` that was held behind a verdict, if any."""
        held = self._held_turn
        self._held_turn = None
        if held is not None:
            self._end_of_turn(held)

    def _end_of_turn(self, event: TurnYielded) -> None:
        """Emit a `TurnYielded` and close the classifier's books on the turn.

        The reset is not optional. `AddressClassifier`'s growth gate carries the
        previous turn's word count, so without it the next turn's early partials
        — the ones whose head start is worth most — would never be speculated
        on at all.
        """
        self.emit(event)
        if self._address is not None:
            self._address.reset()

    def _show_address_verdict(self, detected: AddressDetected) -> None:
        """One dim line per verdict. This is the cache-hit diagnostic.

        Whether the verdict was already decided before Ricky stopped talking is
        the open question about this whole approach — free at a cache hit,
        ~500ms in series with arbitration at a fresh call — and nothing else
        measures it, on stage or in rehearsal.
        """
        who = detected.verdict or "unavailable"
        persona = self.cast.personas.get(detected.agent or "")
        if persona is not None:
            who = f"{who} → {persona.name}"
        label = _ADDRESS_SOURCE_LABELS.get(detected.source, detected.source)
        line = f"  [dim]⌖ address: {who}  {detected.latency_ms:.0f}ms ({label})[/]"
        if detected.reason:
            # Model output on its way to a rich console: escaped, because a
            # stray "[" in the reason would otherwise be read as markup.
            line += f" [dim]— {escape(detected.reason)}[/]"
        self._print(line)

    async def _run_ticks(self) -> None:
        while self._running:
            await asyncio.sleep(TICK_INTERVAL_S)
            self.emit(Tick(t=time.monotonic()))

    # --------------------------------------------------------------------- run

    async def run(self, *, input_device=None, output_device=None) -> None:
        self._loop = asyncio.get_running_loop()

        if self.tts is not None:
            voices = [p.voice_id for p in self.cast.personas.values()]
            self._print("[dim]pre-warming TTS connections…[/]")
            t0 = time.monotonic()
            await self.tts.prewarm(voices)
            self._print(f"[dim]  {1000 * (time.monotonic() - t0):.0f}ms (paid once)[/]")

        await self.stt.start()

        stream = sd.Stream(
            samplerate=VAD_SAMPLE_RATE,
            blocksize=self.block_size,
            dtype="float32",
            channels=1,
            device=(input_device, output_device),
            callback=self._callback,
        )

        tasks = [
            asyncio.create_task(self._drain_events(), name="events"),
            asyncio.create_task(self._run_vad(), name="vad"),
            asyncio.create_task(self._run_stt(), name="stt"),
            asyncio.create_task(self._run_ticks(), name="ticks"),
        ]

        console.print(
            f"[bold]Panel live.[/] {', '.join(p.name for p in self.cast.personas.values())}\n"
            "[dim]Ask a question to open the floor. A statement invites nobody. "
            "Ctrl-C to stop.[/]\n"
            "[dim]Left columns: seconds since start, +gap since the line above.[/]\n"
        )

        with stream:
            try:
                await asyncio.gather(*tasks)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                self._running = False
                for task in tasks:
                    task.cancel()
                await self.stt.stop()
                if self.tts is not None:
                    await self.tts.aclose()
                if self._address is not None:
                    await self._address.close()


def _jsonable(value):
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser(prog="panel", description=__doc__)
    parser.add_argument("--personas", type=Path, default=Path("personas"))
    parser.add_argument("--block", type=int, default=256)
    parser.add_argument("--no-tts", action="store_true", help="print turns instead of speaking")
    parser.add_argument(
        "--llm-address",
        action="store_true",
        help=(
            "resolve who Ricky addressed with a model instead of the regex "
            "(off by default; needs ANTHROPIC_API_KEY)"
        ),
    )
    parser.add_argument("--input-device", default=None)
    parser.add_argument("--output-device", default=None)
    parser.add_argument("--log", type=Path, default=None, help="event log for replay")
    parser.add_argument("--list-devices", action="store_true")
    args = parser.parse_args()

    if args.list_devices:
        console.print(str(sd.query_devices()))
        return

    cast = PanelCast.from_dir(args.personas)
    runtime = PanelRuntime(
        cast,
        floor_config=FloorConfig(
            # One flag does both halves: the reducer starts reading
            # `AddressDetected`, and `PanelRuntime` builds the classifier that
            # produces it. Off, neither exists.
            llm_address_detection=args.llm_address,
        ),
        block_size=args.block,
        use_tts=not args.no_tts,
        log_path=args.log,
    )

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            runtime.run(input_device=args.input_device, output_device=args.output_device)
        )


if __name__ == "__main__":
    main()
