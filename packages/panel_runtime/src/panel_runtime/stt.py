"""Speechmatics Agent STT, one WebSocket per human mic.

The preview Agent STT endpoint (`/v2/agent`, model `linden-1`) spoken directly
over raw `websockets` — there is no SDK for it yet, so the protocol is
hand-rolled here the same way `tts.py` hand-rolls ElevenLabs.

Two things this layer is responsible for and the floor controller is not:

**Channel identity.** Agent STT has no multi-channel mode, so each human mic is
its own connection. That is the stronger form of the same property the
multi-channel client gave us: speaker attribution is a fact about the wiring,
not a diarisation result (FEASIBILITY.md 3.3). Diarisation is therefore off —
there is one speaker on the far end of each socket and we already know who.

**End of turn.** `EndOfTurn` from the server becomes `TurnYielded`. This is the
*understanding* path and it is deliberately not in the barge-in path: VAD owns
stopping, STT owns understanding (CLAUDE.md). Nothing here is allowed to be on
the critical path for interrupting an agent — which is why the endpoint's own
`SpeechStarted`/`SpeechEnded` messages are logged and dropped rather than
turned into `HumanSpeechStarted`/`HumanSpeechEnded`.

Agent speech never enters this path. Ever. That is the feedback loop that ends
the show — agent turns enter conversation state as text, because we generated
them and already know them verbatim.

**`additional_vocab` on this endpoint is unverified.** The documented
`content`/`sounds_like` schema (confirmed at
https://docs.speechmatics.com/api-ref/realtime-transcription-websocket, and
corroborated by LiveKit's `speechmatics/linden-1` plugin docs at
https://docs.livekit.io/agents/models/stt/speechmatics/, which describes the
same `content` + optional `sounds_like` shape for this model) is for the
standard `/v2` endpoint and LiveKit's own inference wrapper. Neither
Speechmatics' own preview-mode docs
(https://docs.speechmatics.com/private/preview-mode) nor the realtime API
reference mention the raw `/v2/agent` WebSocket protocol at all, so whether
*this* endpoint accepts the key — or what its `Error` looks like if it
doesn't — is not established. `STTConfig` therefore treats a rejected
`additional_vocab` as recoverable rather than fatal: see `_VocabRejected` and
`_AgentSTTSession.run`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, replace
from typing import Any, Self

import websockets
from panel_core import HUMAN, PanelCast, TranscriptUpdated, TurnYielded

log = logging.getLogger(__name__)

# 16-bit signed LE at 16kHz — the same PCM the VAD and mixer use, so a mic block
# is fed to both without conversion.
STT_SAMPLE_RATE = 16_000

AGENT_STT_WS = "wss://preview.rt.speechmatics.com/v2/agent"

# The only model the agent endpoint serves during preview.
DEFAULT_MODEL = "linden-1"


@dataclass(frozen=True, slots=True)
class STTConfig:
    """Deployment configuration for the transcription session.

    Agent STT drops several RT-API knobs: no multi-channel, no
    `max_delay`/`max_delay_mode`, no translation, no audio filtering, no
    `enable_entities`, no audio events. End-of-turn is the server's own
    `EndOfTurn` decision rather than a silence trigger we tune, so the old
    `end_of_utterance_silence_trigger` has no equivalent here.
    """

    url: str = AGENT_STT_WS
    language: str = "en"
    model: str = DEFAULT_MODEL
    domain: str | None = None
    output_locale: str | None = None
    # One known mic per connection, so identity comes from the wiring.
    diarization: str = "none"
    speaker_diarization_config: dict[str, Any] | None = None
    enable_partials: bool = True
    punctuation_overrides: dict[str, Any] | None = None
    # Pronunciation hints derived from the cast (see `from_cast` /
    # `vocab_from_cast`), not hardcoded here — pronunciation is a fact about
    # a persona, not about a transcription session (CLAUDE.md: personas are
    # data). A tuple, not a list, so the frozen dataclass stays hashable.
    additional_vocab: tuple[dict[str, Any], ...] = ()
    sample_rate: int = STT_SAMPLE_RATE
    chunk_size: int = 1024
    connect_timeout_s: float = 5.0
    # A preview endpoint dropping mid-show must not end the panel. Backoff is
    # capped low: a socket that stays down for seconds is already a failure the
    # operator can see, and retrying fast costs us nothing.
    reconnect_initial_s: float = 0.25
    reconnect_max_s: float = 4.0
    close_timeout_s: float = 5.0

    def to_transcription_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "language": self.language,
            "model": self.model,
            "enable_partials": self.enable_partials,
            "diarization": self.diarization,
        }
        if self.additional_vocab:
            config["additional_vocab"] = list(self.additional_vocab)
        if self.domain is not None:
            config["domain"] = self.domain
        if self.output_locale is not None:
            config["output_locale"] = self.output_locale
        if self.punctuation_overrides is not None:
            config["punctuation_overrides"] = self.punctuation_overrides
        if self.diarization != "none" and self.speaker_diarization_config is not None:
            config["speaker_diarization_config"] = self.speaker_diarization_config
        return config

    def to_start_recognition(self) -> dict[str, Any]:
        return {
            "message": "StartRecognition",
            "audio_format": {
                "type": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": self.sample_rate,
            },
            "transcription_config": self.to_transcription_config(),
        }

    @classmethod
    def from_cast(cls, cast: PanelCast, **overrides: Any) -> STTConfig:
        """Build a deployment config with vocabulary derived from the cast.

        Every other knob (sample rate, timeouts, model, ...) is still
        deployment configuration and comes from `overrides` or this
        dataclass's own defaults — only `additional_vocab` is derived,
        because a persona's pronunciation is a fact about the persona, not
        about the transcription session (CLAUDE.md: personas are data).

        Args:
            cast: The panel's cast, read for each persona's `sounds_like`.
            **overrides: Any other `STTConfig` field to set explicitly.

        Returns:
            A new `STTConfig` with `additional_vocab` populated from `cast`
            unless `additional_vocab` was itself passed in `overrides`.
        """
        overrides.setdefault("additional_vocab", vocab_from_cast(cast))
        return cls(**overrides)


def vocab_from_cast(cast: PanelCast) -> tuple[dict[str, Any], ...]:
    """Build `additional_vocab` entries from persona pronunciation data.

    Reads `sounds_like` off each `Persona` rather than hardcoding names
    here, so a persona's pronunciation stays data (`personas/*.yaml`)
    instead of code. A persona with no `sounds_like` hints contributes no
    entry — vocabulary lists add session-start latency, so this only ever
    biases the personas that actually need it (currently just Melia; see
    `personas/melia.yaml`).

    Args:
        cast: The panel's cast.

    Returns:
        One `additional_vocab` entry per persona that declares
        `sounds_like`, each shaped `{"content": ..., "sounds_like": [...]}`
        per the documented `/v2` schema (see the `additional_vocab`
        verification note in this module's docstring for why that schema
        is not yet confirmed for the preview `/v2/agent` endpoint).
    """
    return tuple(
        {"content": persona.canonical_name, "sounds_like": list(persona.sounds_like)}
        for persona in cast.personas.values()
        if persona.sounds_like
    )


class PushAudioSource:
    """A live mic turned into an awaitable byte stream.

    `feed()` is called from the PortAudio callback thread, so it must never
    block, allocate unboundedly, or touch the event loop directly — hence
    `call_soon_threadsafe`. A blocked audio callback is a glitch on the PA.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, *, max_blocks: int = 64) -> None:
        self._loop = loop
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max_blocks)
        self._buffer = bytearray()
        self._closed = False
        self._eof = False
        self.dropped_blocks = 0

    @property
    def eof(self) -> bool:
        """True once the mic has closed and every buffered block is drained."""
        return self._eof and not self._buffer

    def feed(self, pcm: bytes) -> None:
        """Called from the audio thread. Non-blocking by construction."""
        if self._closed:
            return
        self._loop.call_soon_threadsafe(self._offer, pcm)

    def _offer(self, pcm: bytes) -> None:
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            # Better to drop the oldest audio than to grow without bound or to
            # stall the callback. Counted, because a rising number here means
            # the network is not keeping up and the operator should know.
            self.dropped_blocks += 1
            with contextlib.suppress(asyncio.QueueEmpty, asyncio.QueueFull):
                self._queue.get_nowait()
                self._queue.put_nowait(pcm)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    async def read(self, size: int) -> bytes:
        """Await until `size` bytes are available, or the source closes.

        Returns short — possibly empty — only at end of stream, so a caller can
        treat `b""` as "the mic is gone" rather than "nothing yet".
        """
        while len(self._buffer) < size:
            if self._eof:
                break
            block = await self._queue.get()
            if block is None:
                self._eof = True
                break
            self._buffer.extend(block)
        chunk = bytes(self._buffer[:size])
        del self._buffer[:size]
        return chunk


class _VocabRejected(RuntimeError):
    """`StartRecognition` was rejected while `additional_vocab` was set.

    Raised instead of a bare `RuntimeError` so `run()` can retry once with
    the vocabulary key dropped, rather than treating the rejection as an
    ordinary transient fault and retrying the identical (and presumably
    still-rejected) config forever. Whether `additional_vocab` is actually
    supported on the preview `/v2/agent` endpoint is unverified — see the
    module docstring — and that endpoint's `Error` message has no documented
    field naming the offending config key, so this treats *any* rejection
    seen while the key is present as possibly caused by it. That is a
    deliberately broad, safe-by-construction guess: the cost of a wrong
    guess is one wasted reconnect attempt, and the cost of not guessing is
    the show running with no transcription at all.
    """


class _AgentSTTSession:
    """One mic, one socket, reconnected for as long as the show is running."""

    def __init__(
        self,
        *,
        speaker: str,
        source: PushAudioSource,
        config: STTConfig,
        api_key: str,
        events: asyncio.Queue,
        name: str,
    ) -> None:
        self._speaker = speaker
        self._source = source
        self._config = config
        self._api_key = api_key
        self._events = events
        self._name = name
        self._running = True
        self._started = False
        # Dropped for the rest of the show on the first rejection — see
        # `_VocabRejected`. A fact about this session's history, not the
        # deployment config, so it lives here rather than on `STTConfig`.
        self._vocab_enabled = bool(config.additional_vocab)

    async def run(self) -> None:
        backoff = self._config.reconnect_initial_s
        while self._running:
            self._started = False
            try:
                await self._session()
            except _VocabRejected as exc:
                log.warning(
                    "stt[%s]: StartRecognition rejected additional_vocab "
                    "(%s) — disabling vocabulary bias and reconnecting "
                    "immediately so transcription still starts. Persona "
                    "name recognition may be degraded for the rest of the "
                    "show.",
                    self._name,
                    exc,
                )
                self._vocab_enabled = False
                continue  # config change, not a transient fault — no backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a dead socket must not end the show
                log.warning("stt[%s]: session failed: %s", self._name, exc)
            if not self._running or self._source.eof:
                return
            # A socket that carried transcripts and then broke is a transient
            # fault, not a bad config — retry it at full speed.
            backoff = (
                self._config.reconnect_initial_s
                if self._started
                else min(backoff * 2, self._config.reconnect_max_s)
            )
            await asyncio.sleep(backoff)

    async def _session(self) -> None:
        # `additional_vocab` only: every other knob (chunk size, timeouts,
        # sample rate, ...) is unaffected by a prior rejection and keeps
        # coming from `self._config` directly.
        start_config = (
            self._config
            if self._vocab_enabled
            else replace(self._config, additional_vocab=())
        )
        async with websockets.connect(
            self._config.url,
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            open_timeout=self._config.connect_timeout_s,
        ) as ws:
            await ws.send(json.dumps(start_config.to_start_recognition()))
            await self._await_started(ws)
            self._started = True
            log.info("stt[%s]: recognition started", self._name)

            seq_no = 0

            async def send_audio() -> None:
                nonlocal seq_no
                while True:
                    frame = await self._source.read(self._config.chunk_size)
                    if not frame:
                        return
                    await ws.send(frame)
                    seq_no += 1

            receiver = asyncio.create_task(self._receive(ws), name=f"agent-stt-recv-{self._name}")
            sender = asyncio.create_task(send_audio(), name=f"agent-stt-send-{self._name}")
            try:
                done, _ = await asyncio.wait(
                    {receiver, sender}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    task.result()
            finally:
                sender.cancel()
                with contextlib.suppress(Exception):
                    await ws.send(json.dumps({"message": "EndOfStream", "last_seq_no": seq_no}))
                    await asyncio.wait_for(receiver, timeout=self._config.close_timeout_s)
                receiver.cancel()
                await asyncio.gather(receiver, sender, return_exceptions=True)

    async def _await_started(self, ws: websockets.ClientConnection) -> None:
        while True:
            message = json.loads(await ws.recv())
            kind = message.get("message")
            if kind == "RecognitionStarted":
                return
            if kind == "Error":
                if self._vocab_enabled:
                    raise _VocabRejected(message)
                raise RuntimeError(f"StartRecognition rejected: {message}")
            log.debug("stt[%s]: %s before RecognitionStarted", self._name, kind)

    async def _receive(self, ws: websockets.ClientConnection) -> None:
        async for raw in ws:
            message = json.loads(raw)
            match message.get("message"):
                case "AddSegment":
                    self._emit_transcript(message, is_final=True)
                case "AddPartialSegment":
                    self._emit_transcript(message, is_final=False)
                case "EndOfTurn":
                    # End of turn — the floor may now be arbitrated. This is
                    # the *understanding* path; the barge-in reflex already
                    # fired on VAD hundreds of milliseconds ago.
                    self._events.put_nowait(TurnYielded(t=time.monotonic()))
                case "SpeechStarted" | "SpeechEnded":
                    # Deliberately inert. Endpointing for barge-in belongs to
                    # the local VAD; routing these into the floor would put a
                    # network round-trip in the interrupt path (CLAUDE.md).
                    pass
                case "Warning":
                    log.warning("stt[%s]: %s", self._name, message)
                case "Error":
                    raise RuntimeError(f"server error: {message}")
                case "EndOfTranscript":
                    return
                case _:
                    log.debug("stt[%s]: %s", self._name, message.get("message"))

    def _emit_transcript(self, message: dict[str, Any], *, is_final: bool) -> None:
        text = (message.get("segment") or {}).get("transcript", "").strip()
        if not text:
            return
        self._events.put_nowait(
            TranscriptUpdated(
                # The runtime clock, not the audio timeline: the reducer reasons
                # about when it *learned* something, not when it was uttered.
                t=time.monotonic(),
                speaker=self._speaker,
                text=text,
                is_final=is_final,
            )
        )

    def stop(self) -> None:
        self._running = False


class PanelSTT:
    """Transcription for every human mic, as `panel_core` events.

    Events are pushed onto a queue rather than dispatched directly, so the
    runtime keeps a single ordered path into the reducer and a rehearsal log
    records exactly what the floor controller saw.
    """

    def __init__(
        self,
        channels: dict[str, str],
        *,
        config: STTConfig | None = None,
        api_key: str | None = None,
    ) -> None:
        """`channels` maps a mic channel id to the speaker id used on events.

        For Boost Camp that is `{"ricky": HUMAN}` today, and a second entry the
        day an audience mic is added — which is a wiring change, not a code one.
        Each entry is its own connection.
        """
        if not channels:
            raise ValueError("at least one channel is required")
        self.channels = channels
        self.config = config or STTConfig()
        key = api_key or os.environ.get("SPEECHMATICS_API_KEY")
        if not key:
            raise RuntimeError("SPEECHMATICS_API_KEY is not set")
        self._api_key = key
        self.events: asyncio.Queue = asyncio.Queue()
        self.sources: dict[str, PushAudioSource] = {}
        self._sessions: dict[str, _AgentSTTSession] = {}
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ session

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.sources = {c: PushAudioSource(loop) for c in self.channels}

        for channel, speaker in self.channels.items():
            session = _AgentSTTSession(
                speaker=speaker or HUMAN,
                source=self.sources[channel],
                config=self.config,
                api_key=self._api_key,
                events=self.events,
                name=channel,
            )
            self._sessions[channel] = session
            self._tasks.append(
                asyncio.create_task(session.run(), name=f"agent-stt-{channel}")
            )

    def feed(self, channel: str, pcm: bytes) -> None:
        """Push one mic block. Safe to call from the PortAudio callback."""
        source = self.sources.get(channel)
        if source is not None:
            source.feed(pcm)

    async def stop(self) -> None:
        for session in self._sessions.values():
            session.stop()
        for source in self.sources.values():
            source.close()
        if self._tasks:
            done = asyncio.gather(*self._tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(done, timeout=self.config.close_timeout_s)
            except (TimeoutError, asyncio.CancelledError):
                for task in self._tasks:
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._sessions = {}

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()
