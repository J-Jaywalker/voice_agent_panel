"""Text-to-speech, streamed and cancellable.

The one thing this module exists to get right is **stopping**. An agent that
cannot be silenced inside the barge-in budget is unusable on a live stage, and
that property is easy to assume and hard to retrofit.

Cancellation is *local*. We do not ask the provider to stop and then wait for it
to comply — we stop reading, and the mixer's gain envelope takes the audio down
within one output buffer (`gain.GainEnvelope`). Telling the provider is cleanup
that happens afterwards, off the latency path:

    StopSpeech -> envelope.ramp_to(-inf, ~20ms)   <- the audible stop
               -> stream.cancel()                 <- local flag, returns in µs
               -> {"close_context": true}         <- teardown, fire and forget

That is what makes the 150ms hard limit a property of our own code rather than
of somebody else's network.

**One connection per voice, held open for the whole show.** Provider choice
turns on time to first byte, because speculative generation (FEASIBILITY.md 3.6)
collapses the post-turn gap to TTS TTFB and nothing else. Measured on the
single-stream endpoint, a fresh socket per utterance cost ~200ms of a ~440ms
median TTFB — half the gap was handshake, paid again on every turn.

So we use `multi-stream-input`, where one socket carries many independent
*contexts*. A turn is a context; interrupting a turn closes that context and
leaves the connection up. Sockets are opened during the pre-show, which is the
right time to spend 200ms, and kept alive from then on.

`sanitise()` is applied by the caller in `panel_sim.brains`; this layer will not
second-guess it, but it does refuse obviously unsafe input as a last line of
defence — a leaked tag read over a PA is the worst-case failure (CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

import websockets

# ElevenLabs streams raw little-endian 16-bit PCM for any pcm_* format. 16kHz
# matches the VAD and mixer rate, so agent audio is never resampled.
PCM_SAMPLE_RATE = 16_000
_BYTES_PER_SAMPLE = 2

_ELEVENLABS_WS = "wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/multi-stream-input"

# Flash is the low-latency model. Voice quality is a casting decision; TTFB is
# an architectural constraint, so it is what the default optimises for.
DEFAULT_MODEL = "eleven_flash_v2_5"


@dataclass(frozen=True, slots=True)
class TTSConfig:
    """Deployment configuration. Nothing here is measured on a dev box."""

    model_id: str = DEFAULT_MODEL
    sample_rate: int = PCM_SAMPLE_RATE
    stability: float = 0.5
    similarity_boost: float = 0.75
    speed: float = 1.0
    # auto_mode disables the character-buffering schedule for lowest latency on
    # complete sentences. We always send a whole turn, so this is right for us.
    auto_mode: bool = True
    connect_timeout_s: float = 5.0
    # A show is ~20 minutes; connections must outlive quiet stretches between
    # an agent's turns. The keepalive refreshes well inside the server timeout.
    keepalive_interval_s: float = 15.0

    @property
    def output_format(self) -> str:
        return f"pcm_{self.sample_rate}"


@dataclass(slots=True)
class SpeechMetrics:
    """What a rehearsal needs to know about one synthesised turn."""

    requested_at: float
    first_audio_at: float | None = None
    cancelled_at: float | None = None
    completed_at: float | None = None
    bytes_received: int = 0

    @property
    def ttfb_ms(self) -> float | None:
        """Request to first audio byte. The number that decides panel feel."""
        if self.first_audio_at is None:
            return None
        return 1000.0 * (self.first_audio_at - self.requested_at)

    @property
    def audio_seconds(self) -> float:
        return self.bytes_received / (_BYTES_PER_SAMPLE * PCM_SAMPLE_RATE)


class SpeechStream(Protocol):
    """One synthesised utterance, consumed as it arrives."""

    metrics: SpeechMetrics

    def chunks(self) -> AsyncIterator[bytes]: ...

    def cancel(self) -> None:
        """Stop delivering audio. Must return immediately — see module docstring."""
        ...


class TTSEngine(Protocol):
    async def prewarm(self, voice_ids: list[str]) -> None: ...

    async def synthesise(self, text: str, *, voice_id: str) -> SpeechStream: ...


# --------------------------------------------------------------------------
# ElevenLabs, multi-context
# --------------------------------------------------------------------------

_END = object()  # sentinel: this context produced its last chunk


@dataclass
class _ContextStream:
    """One turn's audio, arriving on a shared connection."""

    context_id: str
    channel: _VoiceChannel
    metrics: SpeechMetrics = field(default_factory=lambda: SpeechMetrics(time.monotonic()))
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    _cancelled: bool = False

    def cancel(self) -> None:
        """Local, synchronous, and safe to call from anywhere.

        Sets a flag and hands the provider-side teardown to a background task.
        It never awaits the socket and cannot block the caller — the audible
        stop is the mixer's job and has already happened by the time this runs.
        """
        if self._cancelled:
            return
        self._cancelled = True
        self.metrics.cancelled_at = time.monotonic()
        self.channel.close_context_soon(self.context_id)

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            while True:
                item = await self.queue.get()
                if item is _END or self._cancelled:
                    return
                assert isinstance(item, bytes)
                if self.metrics.first_audio_at is None:
                    self.metrics.first_audio_at = time.monotonic()
                self.metrics.bytes_received += len(item)
                yield item
        finally:
            self.metrics.completed_at = time.monotonic()
            self.channel.release(self.context_id)


class _VoiceChannel:
    """One persistent WebSocket for one voice, demultiplexed by context id."""

    def __init__(self, voice_id: str, api_key: str, config: TTSConfig) -> None:
        self.voice_id = voice_id
        self._api_key = api_key
        self._config = config
        self._ws: websockets.ClientConnection | None = None
        self._streams: dict[str, _ContextStream] = {}
        self._reader: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._counter = itertools.count(1)

    # ------------------------------------------------------------- connection

    def _url(self) -> str:
        params = {
            "model_id": self._config.model_id,
            "output_format": self._config.output_format,
            "auto_mode": "true" if self._config.auto_mode else "false",
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return _ELEVENLABS_WS.format(voice_id=self.voice_id) + "?" + query

    async def connect(self) -> None:
        """Open the socket. Called during the pre-show, not mid-panel."""
        async with self._lock:
            if self._ws is not None:
                return
            self._ws = await websockets.connect(
                self._url(),
                additional_headers={"xi-api-key": self._api_key},
                open_timeout=self._config.connect_timeout_s,
            )
            self._reader = asyncio.create_task(self._read_loop(), name=f"tts-read-{self.voice_id}")
            self._keepalive = asyncio.create_task(
                self._keepalive_loop(), name=f"tts-ka-{self.voice_id}"
            )

    async def _reconnect(self) -> None:
        await self.close()
        await self.connect()

    async def close(self) -> None:
        for task in (self._reader, self._keepalive):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader = self._keepalive = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"close_socket": True}))
                await self._ws.close()
            self._ws = None
        for stream in self._streams.values():
            stream.queue.put_nowait(_END)
        self._streams.clear()

    # ------------------------------------------------------------------ pumps

    async def _read_loop(self) -> None:
        """Fan inbound audio out to whichever turn asked for it."""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                context_id = message.get("contextId") or message.get("context_id")
                stream = self._resolve(context_id)
                if stream is None:
                    continue
                audio = message.get("audio")
                if audio and not stream.cancelled:
                    stream.queue.put_nowait(base64.b64decode(audio))
                if message.get("isFinal"):
                    stream.queue.put_nowait(_END)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a dead socket must never kill the show
            # The connection died. Wake every waiting turn rather than leaving
            # an agent silent mid-sentence with no one noticing.
            for stream in list(self._streams.values()):
                stream.queue.put_nowait(_END)

    def _resolve(self, context_id: str | None) -> _ContextStream | None:
        if context_id is not None:
            return self._streams.get(context_id)
        # Server did not tag the message. Only unambiguous with one live turn,
        # which is the normal case: an agent speaks one turn at a time.
        if len(self._streams) == 1:
            return next(iter(self._streams.values()))
        return None

    async def _keepalive_loop(self) -> None:
        """Hold the socket open through quiet stretches between an agent's turns."""
        while True:
            await asyncio.sleep(self._config.keepalive_interval_s)
            if self._ws is None or self._streams:
                continue
            try:
                await self._ws.ping()
            except Exception:  # noqa: BLE001 — reconnect on any transport fault
                with contextlib.suppress(Exception):
                    await self._reconnect()
                return

    # ------------------------------------------------------------------ turns

    async def open_context(self) -> _ContextStream:
        """Start a turn without knowing yet what it will say."""
        if self._ws is None:
            await self.connect()
        assert self._ws is not None

        context_id = f"{self.voice_id}-{next(self._counter)}"
        stream = _ContextStream(context_id=context_id, channel=self)
        self._streams[context_id] = stream

        try:
            await self._ws.send(
                json.dumps(
                    {
                        "text": " ",
                        "context_id": context_id,
                        "voice_settings": {
                            "stability": self._config.stability,
                            "similarity_boost": self._config.similarity_boost,
                            "speed": self._config.speed,
                        },
                    }
                )
            )
        except Exception:
            self._streams.pop(context_id, None)
            raise
        return stream

    async def push(self, context_id: str, text: str, *, flush: bool = True) -> None:
        """Add a sentence to a turn already in progress.

        This is the mechanism that decouples first audio from last token: the
        agent starts speaking its opening clause while the model is still
        writing the rest of the turn.
        """
        if self._ws is None or context_id not in self._streams:
            return
        # Text chunks must end with a space, per the API.
        await self._ws.send(json.dumps({"text": text.rstrip() + " ", "context_id": context_id}))
        if flush:
            await self._ws.send(json.dumps({"context_id": context_id, "flush": True}))

    async def finish(self, context_id: str) -> None:
        """No more text is coming; let the context complete and close itself."""
        if self._ws is None or context_id not in self._streams:
            return
        with contextlib.suppress(Exception):
            await self._ws.send(json.dumps({"context_id": context_id, "close_context": True}))

    async def speak(self, text: str) -> _ContextStream:
        """One-shot: the whole turn is already written."""
        stream = await self.open_context()
        try:
            await self.push(stream.context_id, text)
            await self.finish(stream.context_id)
        except Exception:
            self._streams.pop(stream.context_id, None)
            raise
        return stream

    def close_context_soon(self, context_id: str) -> None:
        """Tell the provider to stop generating. Fire and forget, by design."""
        if self._ws is None or context_id not in self._streams:
            return
        ws = self._ws

        async def _send() -> None:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"context_id": context_id, "close_context": True}))

        task = asyncio.create_task(_send(), name=f"tts-close-{context_id}")
        # Hold a reference so the task is not garbage collected mid-flight.
        task.add_done_callback(lambda _: None)

    def release(self, context_id: str) -> None:
        self._streams.pop(context_id, None)


@dataclass
class StreamingTurn:
    """A turn being spoken while it is still being written.

    Audio starts after the first sentence rather than the last, which is the
    difference between an agent that answers and one that visibly thinks.
    """

    stream: _ContextStream
    _channel: _VoiceChannel
    _guard: object

    @property
    def metrics(self) -> SpeechMetrics:
        return self.stream.metrics

    def cancel(self) -> None:
        self.stream.cancel()

    def chunks(self) -> AsyncIterator[bytes]:
        return self.stream.chunks()

    async def push(self, sentence: str) -> None:
        await self._channel.push(self.stream.context_id, self._guard(sentence))  # type: ignore[operator]

    async def finish(self) -> None:
        await self._channel.finish(self.stream.context_id)


class ElevenLabsTTS:
    """Streaming TTS with one held-open connection per voice.

    Raw `websockets` rather than the vendor SDK: one fewer dependency, and the
    cancellation and connection-lifetime semantics above are the whole point of
    this module — they should not be mediated by a client library whose teardown
    behaviour we do not control.
    """

    def __init__(self, config: TTSConfig | None = None, *, api_key: str | None = None) -> None:
        self.config = config or TTSConfig()
        key = api_key or os.environ.get("ELEVENLABS_API_KEY")
        if not key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set")
        self._api_key = key
        self._channels: dict[str, _VoiceChannel] = {}

    def _channel(self, voice_id: str) -> _VoiceChannel:
        channel = self._channels.get(voice_id)
        if channel is None:
            channel = _VoiceChannel(voice_id, self._api_key, self.config)
            self._channels[voice_id] = channel
        return channel

    async def prewarm(self, voice_ids: list[str]) -> None:
        """Pay every handshake before the audience is in the room."""
        await asyncio.gather(*(self._channel(v).connect() for v in voice_ids))

    @staticmethod
    def _guard(text: str) -> str:
        text = text.strip()
        if not text:
            raise ValueError("refusing to synthesise empty text")
        if "<" in text and ">" in text:
            # Defence in depth. sanitise() should already have caught this.
            raise ValueError(f"refusing to synthesise markup: {text[:60]!r}")
        return text

    async def synthesise(self, text: str, *, voice_id: str) -> _ContextStream:
        """One-shot: use when the whole turn is already written."""
        return await self._channel(voice_id).speak(self._guard(text))

    async def open(self, *, voice_id: str) -> StreamingTurn:
        """Incremental: start speaking before the turn is finished being written."""
        channel = self._channel(voice_id)
        return StreamingTurn(await channel.open_context(), channel, self._guard)

    async def aclose(self) -> None:
        await asyncio.gather(*(c.close() for c in self._channels.values()))
        self._channels.clear()
