"""Speechmatics real-time STT, one channel per human mic.

`speechmatics-rt` direct — not `speechmatics-voice`, not the LiveKit STT plugin,
not Flow. See ADR 0001 and CLAUDE.md.

Two things this layer is responsible for and the floor controller is not:

**Channel identity.** One `AsyncMultiChannelClient` carries every human mic on a
single WebSocket, each tagged with a channel id. Speaker attribution is then a
property of the wiring rather than of diarisation, which is what makes it
reliable enough to drive floor decisions (FEASIBILITY.md 3.3).

**End of turn.** `ConversationConfig.end_of_utterance_silence_trigger` gives us
`EndOfUtterance` from the server, which becomes `TurnYielded`. This is the
*understanding* path and it is deliberately not in the barge-in path: VAD owns
stopping, STT owns understanding (CLAUDE.md). Nothing here is allowed to be on
the critical path for interrupting an agent.

Agent speech never enters this path. Ever. That is the feedback loop that ends
the show — agent turns enter conversation state as text, because we generated
them and already know them verbatim.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Self

from panel_core import HUMAN, TranscriptUpdated, TurnYielded
from speechmatics.rt import (
    AsyncMultiChannelClient,
    AudioEncoding,
    AudioFormat,
    ConversationConfig,
    OperatingPoint,
    ServerMessageType,
    TranscriptionConfig,
    TranscriptResult,
    Model,
)

# 16-bit signed LE at 16kHz — the same PCM the VAD and mixer use, so a mic block
# is fed to both without conversion.
STT_SAMPLE_RATE = 16_000


@dataclass(frozen=True, slots=True)
class STTConfig:
    """Deployment configuration for the transcription session."""

    language: str = "en"
    # The server emits a deprecation Warning for operating_point, pointing at a
    # `model` property — but speechmatics-rt 1.1.1 has no such field, so the
    # SDK is behind the API. Left as-is deliberately rather than guessing at an
    # undocumented parameter; revisit on the next SDK release.
    model: Model = Model.ENHANCED
    max_delay: float = 0.7
    enable_partials: bool = True
    # End-of-turn tuning (spike S0.6). Too short and Ricky gets cut off
    # mid-thought; too long and the panel feels sluggish. Re-measure on the
    # venue rig with the real mics — room tone changes this.
    end_of_utterance_silence_trigger: float = 0.6
    sample_rate: int = STT_SAMPLE_RATE
    chunk_size: int = 1024

    def to_transcription_config(self, channels: tuple[str, ...]) -> TranscriptionConfig:
        return TranscriptionConfig(
            language=self.language,
            model=self.model,
            max_delay=self.max_delay,
            enable_partials=self.enable_partials,
            conversation_config=ConversationConfig(
                end_of_utterance_silence_trigger=self.end_of_utterance_silence_trigger
            ),
            channel_diarization_labels=list(channels),
        )

    def to_audio_format(self) -> AudioFormat:
        return AudioFormat(
            encoding=AudioEncoding.PCM_S16LE,
            sample_rate=self.sample_rate,
            chunk_size=self.chunk_size,
        )


class PushAudioSource:
    """A live mic dressed up as the file-like object the client wants.

    `speechmatics-rt` streams from anything with a `read`, and awaits it if it
    is a coroutine (`_audio_sources._make_iter`). That is the hook that lets a
    real-time source work without a thread or a temp file.

    `feed()` is called from the PortAudio callback thread, so it must never
    block, allocate unboundedly, or touch the event loop directly — hence
    `call_soon_threadsafe`. A blocked audio callback is a glitch on the PA.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, *, max_blocks: int = 64) -> None:
        self._loop = loop
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max_blocks)
        self._buffer = bytearray()
        self._closed = False
        self.dropped_blocks = 0

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
        """Await until `size` bytes are available, or the source closes."""
        while len(self._buffer) < size:
            block = await self._queue.get()
            if block is None:
                break
            self._buffer.extend(block)
        chunk = bytes(self._buffer[:size])
        del self._buffer[:size]
        return chunk


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
        """
        if not channels:
            raise ValueError("at least one channel is required")
        self.channels = channels
        self.config = config or STTConfig()
        self._api_key = api_key
        self.events: asyncio.Queue = asyncio.Queue()
        self.sources: dict[str, PushAudioSource] = {}
        self._client: AsyncMultiChannelClient | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ wiring

    def _speaker_for(self, channel: str | None) -> str:
        if channel is None:
            # Single-channel deployments do not tag results.
            return next(iter(self.channels.values()))
        return self.channels.get(channel, HUMAN)

    def _register(self, client: AsyncMultiChannelClient) -> None:
        def on_transcript(message: dict) -> None:
            self._emit_transcript(message, is_final=True)

        def on_partial(message: dict) -> None:
            self._emit_transcript(message, is_final=False)

        def on_end_of_utterance(message: dict) -> None:
            # End of turn — the floor may now be arbitrated. Note that this is
            # the *understanding* path; the barge-in reflex already fired on VAD
            # hundreds of milliseconds ago.
            del message
            self.events.put_nowait(TurnYielded(t=time.monotonic()))

        client.on(ServerMessageType.ADD_TRANSCRIPT, on_transcript)
        client.on(ServerMessageType.ADD_PARTIAL_TRANSCRIPT, on_partial)
        client.on(ServerMessageType.END_OF_UTTERANCE, on_end_of_utterance)

    def _emit_transcript(self, message: dict, *, is_final: bool) -> None:
        result = TranscriptResult.from_message(message)
        text = result.metadata.transcript.strip()
        if not text:
            return
        channel = None
        if result.results:
            channel = getattr(result.results[0], "channel", None) or message.get("channel")
        self.events.put_nowait(
            TranscriptUpdated(
                # The runtime clock, not the audio timeline: the reducer reasons
                # about when it *learned* something, not when it was uttered.
                t=time.monotonic(),
                speaker=self._speaker_for(channel),
                text=text,
                is_final=is_final,
            )
        )

    # ------------------------------------------------------------------ session

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.sources = {c: PushAudioSource(loop) for c in self.channels}

        client = AsyncMultiChannelClient(api_key=self._api_key)
        self._register(client)
        self._client = client

        self._task = asyncio.create_task(
            client.transcribe(
                dict(self.sources),
                transcription_config=self.config.to_transcription_config(
                    tuple(self.channels)
                ),
                audio_format=self.config.to_audio_format(),
            ),
            name="speechmatics-rt",
        )

    def feed(self, channel: str, pcm: bytes) -> None:
        """Push one mic block. Safe to call from the PortAudio callback."""
        source = self.sources.get(channel)
        if source is not None:
            source.feed(pcm)

    async def stop(self) -> None:
        for source in self.sources.values():
            source.close()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        if self._client is not None:
            await self._client.close()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()
