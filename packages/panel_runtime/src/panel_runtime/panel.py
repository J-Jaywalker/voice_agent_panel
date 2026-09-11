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
from pathlib import Path

import numpy as np
import sounddevice as sd
from livekit import rtc
from livekit.agents import vad as lkvad
from livekit.plugins import silero
from panel_core import (
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
    InjectDirective,
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
from rich.console import Console
from rich.live import Live
from rich.text import Text

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
            sentence = await self._queue.get()
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

        self.events: asyncio.Queue = asyncio.Queue()
        self._mic: queue.Queue = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._speaking_task: asyncio.Task | None = None
        self._speaking_turn = None
        self._ricky_final_text = ""
        self._ricky_live: Live | None = None
        # Live candidates, one per agent, filling while the human is still
        # talking. The floor decides *who* speaks; this holds *what* they say.
        self._candidates: dict[str, Candidate] = {}
        # One in-flight brain stream per agent, keyed so a re-request can tell
        # "already running, leave it" from "nothing running, start one" — see
        # `_request_proposals`. `_proposal_turn` records the `state.turn_id`
        # each task was started against, which is what makes a genuinely
        # superseded turn distinguishable from an agent simply being asked
        # again mid-turn.
        self._proposal_tasks: dict[str, asyncio.Task] = {}
        self._proposal_turn: dict[str, int] = {}
        self._running = True
        self._last_intro_remaining: tuple[str, ...] | None = None
        self._last_intro_done = False
        # Diagnostic-only state for `_show_state_change`: what was last
        # printed, so a repaint that changed nothing stays silent.
        self._last_invitation: tuple[str | None, str | None] = (None, None)
        self._last_address_conflict: tuple[str, ...] = ()
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

        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("")

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
                console.print(f"  [dim]… gathering proposals ({command.reason})[/]")
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
                console.print(
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

            case InjectDirective():
                # Mid-turn steering needs a regenerate-and-splice that Phase 0
                # does not have. Surfaced so it is visibly unimplemented rather
                # than silently dropped.
                console.print(f"  [yellow]↯ {command.agent}: {command.text}[/] [dim](noop)[/]")

            case HandsRaised():
                hands = "  ".join(
                    f"{self.cast[a].name} {s:.2f}" for a, s in command.agents
                )
                console.print(f"  [yellow]✋ wants in:[/] {hands} [dim](not invited)[/]")

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
                console.print(f"  [magenta]▸ back to Ricky ({command.reason.value})[/]")

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
                console.print(f"  [dim]intros: {len(remaining)} remaining — {names}[/]")

        done = command.extra.get("intro_done")
        if done and not self._last_intro_done:
            self._last_intro_done = True
            console.print("  [dim]intros: all done[/]")

        invited = command.extra.get("invited")
        source = command.extra.get("invitation_source")
        if (invited, source) != self._last_invitation:
            self._last_invitation = (invited, source)
            if source is None:
                console.print("  [dim]floor: closed (no live invitation)[/]")
            else:
                who = self.cast[invited].name if invited else "the panel"
                role = command.extra.get("invitation_role") or "-"
                rule = command.extra.get("invitation_rule") or "-"
                console.print(
                    f"  [dim]floor: invited {who} — {source}/{role} ({rule})[/]"
                )

        conflict = command.extra.get("address_conflict") or ()
        if conflict != self._last_address_conflict:
            self._last_address_conflict = conflict
            if conflict:
                names = ", ".join(self.cast[a].name for a in conflict)
                console.print(
                    f"  [yellow]✋ ambiguous address:[/] {names} [dim](floor stays closed)[/]"
                )

    # --------------------------------------------------------------- proposals

    def _request_proposals(self, agents: tuple[str, ...]) -> None:
        """Speculate during the human's turn. Additive, not cancel-and-restart.

        The floor re-requests proposals on roughly every 0.8s of partial
        transcript, and unconditionally on every final segment, but a brain
        takes 2-3s to reach `SignalsReady`. Cancelling every in-flight stream
        on each request — the previous behaviour — meant no agent ever
        finished before `EndOfTurn` arrived: `state.proposals` was reliably
        empty at the first arbitration, a guaranteed spurious `no_proposals`.

        Only genuinely stale work is torn down: a task started against a turn
        a grant has since superseded (`state.turn_id` has moved on, so its
        `Signals` were scored against a conversational moment nobody can act
        on any more), or an agent no longer in the requested set at all. The
        agent currently on the PA is never touched here regardless of either
        test — its task may still be feeding `speak()`'s `Candidate` queue
        live, and cancelling it would cut off audio already playing.

        Each agent streams. Signals arrive first and go straight to the floor
        controller, so arbitration can run while the text is still being
        written. Sentences accumulate in a `Candidate`, ready to be spoken the
        moment that agent is granted the floor.
        """
        current_turn = self.state.turn_id
        requested = set(agents)
        speaking = self.state.speaking

        for agent_id, task in list(self._proposal_tasks.items()):
            if agent_id == speaking:
                continue
            stale = agent_id not in requested or self._proposal_turn.get(agent_id) != current_turn
            if not stale:
                continue
            if not task.done():
                task.cancel()
            del self._proposal_tasks[agent_id]
            self._proposal_turn.pop(agent_id, None)

        snapshot = self.state
        for agent_id in agents:
            if agent_id == speaking:
                continue
            existing = self._proposal_tasks.get(agent_id)
            if existing is not None and not existing.done():
                continue  # already in flight against this turn — let it run
            candidate = Candidate(agent_id)
            self._candidates[agent_id] = candidate
            self._proposal_turn[agent_id] = current_turn
            self._proposal_tasks[agent_id] = asyncio.create_task(
                self._stream_one(candidate, snapshot), name=f"propose-{agent_id}"
            )

    async def _stream_one(self, candidate: Candidate, snapshot: PanelState) -> None:
        persona = self.cast[candidate.agent]
        spoke = False
        try:
            async for event in self.brain.stream(persona, snapshot):
                if isinstance(event, SignalsReady):
                    # The floor can be arbitrated now. The utterance is carried
                    # by the Candidate, not by the event — the reducer decides
                    # who speaks and never needs to know what they will say.
                    self.emit(
                        AgentProposal(
                            t=time.monotonic(),
                            agent=candidate.agent,
                            utterance="",
                            signals=event.signals,
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
            console.print(f"  [red]x {candidate.agent} brain failed: {str(exc)[:60]}[/]")
            candidate.close()
        finally:
            if not spoke and self._candidates.get(candidate.agent) is candidate:
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
                del self._candidates[candidate.agent]

    # ------------------------------------------------------------------ speech

    def _start_speaking(self, command: StartSpeech) -> None:
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
                stale = self._proposal_tasks.pop(command.agent, None)
                if stale is not None and not stale.done():
                    stale.cancel()
                self._proposal_turn.pop(command.agent, None)
                candidate = Candidate.fixed(command.agent, sentences)
                self._candidates[command.agent] = candidate
        else:
            # The ordinary streamed grant: the words are still arriving, so the
            # candidate `_request_proposals` parked is the whole point.
            candidate = self._candidates.get(command.agent)
        if candidate is None:
            # Either nothing was ever requested for this agent, or the stream
            # that was requested finished without producing a speakable word
            # and `_stream_one` dropped it. Both mean the same thing here and
            # both must be *loud*: the failure this replaces was an agent's
            # name appearing on stage with nothing under it and the turn quietly
            # consumed, which from the console was indistinguishable from an
            # agent choosing to say nothing.
            console.print(f"  [red]x {persona.name} has nothing to say — turn skipped[/]")
            self.emit(
                AgentSpeechEnded(t=time.monotonic(), agent=command.agent, completed=False)
            )
            return

        console.print(f"\n[bold cyan]{persona.name}[/]")
        self.emit(AgentSpeechStarted(t=time.monotonic(), agent=command.agent))

        async def speak() -> None:
            spoken: list[str] = []
            try:
                if self.tts is None:
                    # --no-tts: hold the floor for a plausible speaking duration
                    # so floor behaviour can be exercised without audio.
                    async for sentence in candidate.sentences():
                        spoken.append(sentence)
                        console.print(f"  {sentence}")
                        await asyncio.sleep(len(sentence.split()) / 2.8)
                else:
                    turn = await self.tts.open(voice_id=persona.voice_id)
                    self._speaking_turn = turn
                    self.mixer.clear(command.agent)

                    async def pump_audio() -> None:
                        async for chunk in turn.chunks():
                            self.mixer.feed(command.agent, chunk)

                    audio = asyncio.create_task(pump_audio(), name="tts-audio")
                    async for sentence in candidate.sentences():
                        spoken.append(sentence)
                        console.print(f"  {sentence}")
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
            finally:
                self._speaking_turn = None
                self._candidates.pop(command.agent, None)

            self.emit(
                AgentSpeechEnded(
                    t=time.monotonic(),
                    agent=command.agent,
                    completed=True,
                    utterance=" ".join(spoken),
                )
            )

        self._speaking_task = asyncio.create_task(speak(), name=f"speak-{command.agent}")

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
        line = Text("  ")
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
            self._ricky_live.update(self._ricky_renderable(), refresh=True)
            self._ricky_live.stop()
            self._ricky_live = None
        self._ricky_final_text = ""

    async def _run_stt(self) -> None:
        """Forward Speechmatics events into the single ordered event path."""
        while self._running:
            event = await self.stt.events.get()
            if isinstance(event, TranscriptUpdated):
                if event.is_final:
                    self._ricky_final_text = f"{self._ricky_final_text} {event.text}".strip()
                    self._render_ricky_line()
                else:
                    self._render_ricky_line(event.text)
            elif isinstance(event, TurnYielded):
                self._close_ricky_line()
            self.emit(event)

    async def _run_ticks(self) -> None:
        while self._running:
            await asyncio.sleep(TICK_INTERVAL_S)
            self.emit(Tick(t=time.monotonic()))

    # --------------------------------------------------------------------- run

    async def run(self, *, input_device=None, output_device=None) -> None:
        self._loop = asyncio.get_running_loop()

        if self.tts is not None:
            voices = [p.voice_id for p in self.cast.personas.values()]
            console.print("[dim]pre-warming TTS connections…[/]")
            t0 = time.monotonic()
            await self.tts.prewarm(voices)
            console.print(f"[dim]  {1000 * (time.monotonic() - t0):.0f}ms (paid once)[/]")

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
