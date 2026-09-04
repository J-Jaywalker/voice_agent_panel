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
          └─> speechmatics-rt ──> TranscriptUpdated ───┤        (pure)              │
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

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._queue.put(None)

    async def sentences(self):
        while True:
            sentence = await self._queue.get()
            if sentence is None:
                return
            yield sentence


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
        self.stt = PanelSTT({"ricky": "human"}, config=STTConfig())
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
        self._proposal_tasks: list[asyncio.Task] = []
        self._running = True

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
                self._request_proposals(command.agents)

            case StartSpeech():
                self._start_speaking(command)

            case StopSpeech():
                # Audible immediately; the provider is told afterwards.
                self.mixer.stop(command.agent)
                if self._speaking_turn is not None:
                    self._speaking_turn.cancel()
                if self._speaking_task is not None:
                    self._speaking_task.cancel()
                console.print(
                    f"  [red]⏹ {self.cast[command.agent].name}[/] "
                    f"[dim]({command.reason.value})[/]"
                )
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
                console.print(f"  [magenta]▸ back to Ricky ({command.reason})[/]")

    # --------------------------------------------------------------- proposals

    def _request_proposals(self, agents: tuple[str, ...]) -> None:
        """Speculate during the human's turn. Supersedes any in-flight request.

        Each agent streams. Signals arrive first and go straight to the floor
        controller, so arbitration can run while the text is still being
        written. Sentences accumulate in a `Candidate`, ready to be spoken the
        moment that agent is granted the floor.
        """
        for task in self._proposal_tasks:
            if not task.done():
                task.cancel()
        self._proposal_tasks = []

        snapshot = self.state
        for agent_id in agents:
            if agent_id == self.state.speaking:
                continue
            candidate = Candidate(agent_id)
            self._candidates[agent_id] = candidate
            self._proposal_tasks.append(
                asyncio.create_task(
                    self._stream_one(candidate, snapshot), name=f"propose-{agent_id}"
                )
            )

    async def _stream_one(self, candidate: Candidate, snapshot: PanelState) -> None:
        persona = self.cast[candidate.agent]
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
                    await candidate.add(event.text)
                elif isinstance(event, ProposalComplete):
                    await candidate.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one dead brain must not stop the panel
            console.print(f"  [red]x {candidate.agent} brain failed: {str(exc)[:60]}[/]")
            await candidate.close()

    # ------------------------------------------------------------------ speech

    def _start_speaking(self, command: StartSpeech) -> None:
        persona = self.cast[command.agent]
        candidate = self._candidates.get(command.agent)
        if candidate is None:
            console.print(f"  [red]x {persona.name} has no candidate turn[/]")
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
