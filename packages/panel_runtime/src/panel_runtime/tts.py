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

So we use a `multi-stream-input` endpoint, where one socket carries many
independent *contexts*. A turn is a context; interrupting a turn closes that
context and leaves the connection up. Sockets are opened during the pre-show,
which is the right time to spend 200ms, and kept alive from then on.

**The endpoint is text-to-dialogue, not text-to-speech** (migrated 5 Oct 2026,
with the move to `eleven_v3_conversational`). v3 models are rejected outright by
`/v1/text-to-speech/{voice_id}/multi-stream-input`; they live only on
`/v1/text-to-dialogue/multi-stream-input`, which is a different wire protocol
wearing a similar name:

* The voice is **not** in the URL. Each context registers its own voice, as
  `{"voices": [voice_id]}` on that context's first message. One voice per
  context is a hard limit of `eleven_v3_conversational`, which is exactly the
  shape this module already had — one channel, one voice.
* Text arrives as `{"inputs": [{"text": ..., "voice_id": ...}]}`, an array of
  per-voice objects, not a bare `text` field.
* **Finality is two messages, not a flag on the audio.**
  `is_final_audio_for_turn` fires after *every* flush — i.e. after every
  sentence of a streamed turn — so it must never end a stream. Only `is_final`
  does, and it arrives only once the context is actually closed. Verified
  against the live socket, not inferred: a read loop that ended on
  `is_final_audio_for_turn` would truncate every turn to its first sentence.

Two live-verified protocol hazards shape the code below; both kill the whole
connection, not just one turn, because this endpoint answers a protocol
violation with a 1008 close of the socket:

* `{"keep_alive": true}` **cannot** be used as a socket keepalive here. Without
  a `context_id` the server rejects it (`missing_context_id`) and closes. A
  WebSocket ping is what holds an idle socket open — measured surviving 120s of
  silence with no contexts registered.
* A context left idle auto-closes after ~20s. Pushing to a context that has
  already sent `is_final` is read as a *new* context's first message, fails the
  `voices` requirement, and takes the socket down. So an ended context is
  remembered and further pushes to it are dropped.

**Accent is injected here, not written by the model** (migrated 5 Oct 2026,
alongside the move to v3). `_VoiceChannel.push` prepends a persona's standing
accent tag — e.g. `[Yorkshire accent]`, `[irish accent]` — to *every*
push, not just a context's first:
this endpoint buffers and flushes roughly every 40 characters/8 words
(above), each flush is its own generation, and a tag asserted only at the
top of a turn was observed to drift back towards the voice's default within
a few sentences. See `panel_core.personas.ACCENT_TAGS` for why this is not a
prompt instruction. `_guard`'s allowlist check still applies to whatever the
model wrote; the accent tag is prepended after that check, never subject to
it.

(A `pace`/`[rapid-fire]` version of this same mechanism was tried for Wayne
the same day, to compensate for v3 dropping `speed` entirely — measured as
working, ~15% shorter audio for the same text, but it read as shouting
rather than brisk on the real voice and was reverted. If this is revisited,
it is a text/delivery problem, not a TTS-settings one.)

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
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Protocol

import websockets
from panel_core.personas import AUDIO_TAGS

# ElevenLabs streams raw little-endian 16-bit PCM for any pcm_* format. 16kHz
# matches the VAD and mixer rate, so agent audio is never resampled.
#
# A higher rate (pcm_24000) was tried for fidelity and reverted: it required
# splitting the mic and speakers into two independent PortAudio streams
# instead of one duplex stream, and opening the same physical device twice at
# two different rates produced audible crackling on the dev box — the two
# streams fighting the driver, not a code bug in either one. Revisit only
# alongside a real two-device setup (separate physical input/output
# hardware), verified on the target rig — see CLAUDE.md § Deployment.
PCM_SAMPLE_RATE = 16_000
_BYTES_PER_SAMPLE = 2

_ELEVENLABS_WS = "wss://api.elevenlabs.io/v1/text-to-dialogue/multi-stream-input"

# v3 conversational. Chosen on a measured A/B against eleven_flash_v2_5 across
# all three cast voices: median TTFB ~119ms vs ~110ms, which is inside the noise
# of a venue uplink. TTFB is the architectural constraint and it survived, so
# the delivery is what decided it.
DEFAULT_MODEL = "eleven_v3_conversational"

# v3 takes `stability` as three presets, not a slider: 0.0 Creative, 0.5
# Natural, 1.0 Robust. Anything else is rounded to the nearest of these by the
# server, silently — it accepts the float and does not error, which is why the
# rounding is done here instead, where it can be read in a diff.
_V3_STABILITY_PRESETS = (0.0, 0.5, 1.0)


def _preset_stability(value: float) -> float:
    """Snap a persona's continuous stability onto v3's three presets.

    **This used to flatten the whole cast.** The personas originally authored
    0.30 (Dexter), 0.35 (Wayne) and 0.62 (Melia) against the old Flash scale,
    and all three were nearer 0.5 than to either end — so every voice came out
    Natural and the deliberate contrast between them (Melia steadier, the
    other two looser) did not reach the wire. That was not this function
    being wrong: it is what the server does with those numbers either way,
    made visible.

    Dexter is since re-authored straight to 0.0 (Creative) — not for that
    contrast, but because he now carries a standing `accent` tag
    (`panel_core.personas.ACCENT_TAGS`), and the vendor's own guidance is that
    Robust "suppresses audio tag responsiveness" and Natural is a real step in
    that direction. The same was tried for Melia's accent and for Wayne's
    pace (a `[rapid-fire]` tag, to compensate for v3 dropping `speed`
    entirely) and both were reverted 5 Oct 2026 — Melia's tag did not move
    the voice, and Wayne's measurably worked but read as shouting rather than
    brisk. Wayne is back to his originally-authored value and is the one
    persona this function's flattening still applies to; Melia's accent was
    re-added 6 Oct 2026 with stability dropped to 0.0 again — see
    `personas/melia.yaml` for the trade-off and the fallback if it still
    doesn't render.
    """
    return min(_V3_STABILITY_PRESETS, key=lambda preset: abs(preset - value))


@dataclass(frozen=True, slots=True)
class TTSConfig:
    """Deployment configuration. Nothing here is measured on a dev box.

    The field list is what `eleven_v3_conversational` actually acts on. v3
    ignores `speed`, `similarity_boost` and `style` — the dialogue endpoint
    accepts them in `voice_settings` without complaint and then does nothing
    with them, which is worse than rejecting them, so they are not carried
    here. `auto_mode` is gone for a different reason: it is a text-to-speech
    query parameter and does not exist on this endpoint at all.
    """

    model_id: str = DEFAULT_MODEL
    sample_rate: int = PCM_SAMPLE_RATE
    stability: float = 0.5
    connect_timeout_s: float = 5.0
    # A show is ~20 minutes; connections must outlive quiet stretches between
    # an agent's turns. A ping every 15s holds an idle socket open — see the
    # module docstring on why this is a ping and not a `keep_alive` message.
    keepalive_interval_s: float = 15.0

    @property
    def output_format(self) -> str:
        return f"pcm_{self.sample_rate}"

    @property
    def voice_settings(self) -> dict[str, float]:
        """The only `voice_settings` payload v3 reads."""
        return {"stability": _preset_stability(self.stability)}


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

    def __init__(
        self,
        voice_id: str,
        api_key: str,
        config: TTSConfig,
        *,
        accent_tag: str | None = None,
    ) -> None:
        self.voice_id = voice_id
        self._api_key = api_key
        self._config = config
        # A standing characteristic of this voice, not a per-turn choice — see
        # `panel_core.personas.ACCENT_TAGS`. Applied in `push()`, on every
        # push, never by the model.
        self._accent_tag = accent_tag
        self._ws: websockets.ClientConnection | None = None
        self._streams: dict[str, _ContextStream] = {}
        # Contexts the server has said `is_final` for. Pushing to one of these
        # takes the whole socket down (module docstring), so they are
        # remembered rather than merely forgotten.
        self._ended: set[str] = set()
        self._reader: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._counter = itertools.count(1)

    # ------------------------------------------------------------- connection

    def _url(self) -> str:
        # The voice is not in the path on this endpoint — it is registered
        # per-context, in `open_context`.
        params = {
            "model_id": self._config.model_id,
            "output_format": self._config.output_format,
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return _ELEVENLABS_WS + "?" + query

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
        self._ended.clear()

    # ------------------------------------------------------------------ pumps

    async def _read_loop(self) -> None:
        """Fan inbound audio out to whichever turn asked for it."""
        try:
            # A dead socket must never kill the show, so a transport fault ends
            # the loop instead of propagating out of the task.
            with contextlib.suppress(Exception):
                await self._pump()
        finally:
            # The connection ended, cleanly or otherwise. Wake every waiting
            # turn rather than leaving an agent silent mid-sentence with no one
            # noticing. In a `finally` rather than an exception handler because
            # a server-side close is a *clean* end to the iterator, and that is
            # exactly how this endpoint reports a protocol violation.
            for stream in list(self._streams.values()):
                stream.queue.put_nowait(_END)

    async def _pump(self) -> None:
        """Dispatch one message at a time until the socket ends."""
        assert self._ws is not None
        async for raw in self._ws:
            message = json.loads(raw)
            if message.get("error"):
                # The server answers a protocol violation by closing the
                # socket, so this is terminal for every turn on it. Nothing to
                # route; the close that follows wakes them.
                continue
            context_id = message.get("context_id")
            stream = self._resolve(context_id)
            if stream is None:
                continue
            audio = message.get("audio")
            if audio and not stream.cancelled:
                stream.queue.put_nowait(base64.b64decode(audio))
            # `is_final_audio_for_turn` is deliberately not handled: it fires
            # after every flush, so after every sentence of a streamed turn.
            # `is_final` is the one that means the context is closed and no
            # more audio is coming.
            if message.get("is_final"):
                if context_id is not None:
                    self._ended.add(context_id)
                stream.queue.put_nowait(_END)

    def _resolve(self, context_id: str | None) -> _ContextStream | None:
        if context_id is not None:
            return self._streams.get(context_id)
        # Server did not tag the message. This endpoint tags every audio and
        # finality message, so in practice only an error reaches here — but it
        # is only unambiguous with one live turn anyway, which is the normal
        # case: an agent speaks one turn at a time.
        if len(self._streams) == 1:
            return next(iter(self._streams.values()))
        return None

    async def _keepalive_loop(self) -> None:
        """Hold the socket open through quiet stretches between an agent's turns.

        A WebSocket ping, never the endpoint's own `{"keep_alive": true}`: that
        message requires a `context_id`, and between turns there is no context
        to name. Sent without one it is a protocol violation and the server
        closes the connection (verified live — see the module docstring).
        """
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
            # A context's first message registers its voice and settings, and
            # may carry no text at all — the server accepts it and waits. That
            # is what lets a turn be opened before it has been written.
            await self._ws.send(
                json.dumps(
                    {
                        "context_id": context_id,
                        "voices": [self.voice_id],
                        "voice_settings": self._config.voice_settings,
                    }
                )
            )
        except Exception:
            self._streams.pop(context_id, None)
            raise
        return stream

    def _writable(self, context_id: str) -> bool:
        """Is this context still safe to send text to?

        An ended context is not merely useless to write to — the write is read
        as a new context's first message, fails the `voices` requirement, and
        closes the socket out from under every other turn on it.
        """
        return (
            self._ws is not None and context_id in self._streams and context_id not in self._ended
        )

    async def push(self, context_id: str, text: str, *, flush: bool = True) -> None:
        """Add a sentence to a turn already in progress.

        This is the mechanism that decouples first audio from last token: the
        agent starts speaking its opening clause while the model is still
        writing the rest of the turn.
        """
        if not self._writable(context_id):
            return
        assert self._ws is not None
        if self._accent_tag is not None:
            # On every push, not just the context's first: each flushed push is
            # its own generation (this endpoint buffers ~40 characters/8 words
            # before generating at all — the comment two lines down), and an
            # accent asserted once at the top of a turn was observed to drift
            # back towards the voice's default by the turn's second or third
            # sentence. Measured as free: unlike a performance tag (`[sighs]`),
            # an accent tag does not add performed audio length, so restating
            # it costs nothing extra per sentence.
            text = f"[{self._accent_tag}] {text}"
        # Text chunks must end with a space, per the API. `flush` matters more
        # here than it did on the old endpoint: this one buffers until roughly
        # 40 characters and 8 words before it generates anything, and a short
        # opening clause is routinely under that.
        message: dict[str, object] = {
            "context_id": context_id,
            "inputs": [{"text": text.rstrip() + " ", "voice_id": self.voice_id}],
        }
        if flush:
            message["flush"] = True
        await self._ws.send(json.dumps(message))

    async def finish(self, context_id: str) -> None:
        """No more text is coming; let the context complete and close itself."""
        if not self._writable(context_id):
            return
        assert self._ws is not None
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
        """Tell the provider to stop generating. Fire and forget, by design.

        `close_context` flushes whatever is already in flight before the
        context closes, so this is not an abrupt kill and never was — the
        audible stop is the mixer's gain envelope. This only stops us paying
        for audio nobody will hear.
        """
        if not self._writable(context_id):
            return
        ws = self._ws
        assert ws is not None

        async def _send() -> None:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"context_id": context_id, "close_context": True}))

        task = asyncio.create_task(_send(), name=f"tts-close-{context_id}")
        # Hold a reference so the task is not garbage collected mid-flight.
        task.add_done_callback(lambda _: None)

    def release(self, context_id: str) -> None:
        self._streams.pop(context_id, None)
        # The id is never reused, so once nothing is listening for it there is
        # nothing left to protect and the set should not grow for the length of
        # a show.
        self._ended.discard(context_id)


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

    def __init__(
        self,
        config: TTSConfig | None = None,
        *,
        api_key: str | None = None,
        voice_overrides: dict[str, dict[str, float]] | None = None,
        accent_tags: dict[str, str] | None = None,
    ) -> None:
        self.config = config or TTSConfig()
        key = api_key or os.environ.get("ELEVENLABS_API_KEY")
        if not key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set")
        self._api_key = key
        # Per-voice `TTSConfig` field overrides (e.g. a persona's
        # `voice_settings`), keyed by voice_id. A voice with no entry gets
        # `self.config` untouched.
        self._voice_overrides = voice_overrides or {}
        # Per-voice standing accent (`panel_core.personas.Persona.accent`),
        # keyed by voice_id. Not a `TTSConfig`/`voice_overrides` field: it is
        # text content prepended in `_VoiceChannel.push`, not an ElevenLabs
        # `voice_settings` parameter, and `_apply_overrides` below keeps
        # `TTSConfig` honest about only holding things that reach the wire as
        # settings — see its docstring.
        self._accent_tags = accent_tags or {}
        self._channels: dict[str, _VoiceChannel] = {}

    def _channel(self, voice_id: str) -> _VoiceChannel:
        channel = self._channels.get(voice_id)
        if channel is None:
            config = self._apply_overrides(self._voice_overrides.get(voice_id))
            channel = _VoiceChannel(
                voice_id,
                self._api_key,
                config,
                accent_tag=self._accent_tags.get(voice_id),
            )
            self._channels[voice_id] = channel
        return channel

    def _apply_overrides(self, overrides: dict[str, float] | None) -> TTSConfig:
        """Merge a persona's `voice_settings` over the engine defaults.

        Personas are data and name ElevenLabs' settings, not ours, so they can
        legitimately carry keys this model has no use for — every persona in
        the cast still authors a `speed`, which `eleven_v3_conversational` does
        not implement. Dropping those here rather than widening `TTSConfig` to
        hold them keeps the config honest about what reaches the wire: a field
        on `TTSConfig` is a thing the model acts on.
        """
        if not overrides:
            return self.config
        known = {k: v for k, v in overrides.items() if k in TTSConfig.__dataclass_fields__}
        return replace(self.config, **known) if known else self.config

    # Short, and a real sentence rather than a token: the warm-up has to make
    # the model generate in this voice, which is the cost being paid down.
    _WARMUP_TEXT = "Sound check, one two."

    async def prewarm(self, voice_ids: list[str]) -> None:
        """Pay every per-voice cost before the audience is in the room.

        Two distinct costs, done as two steps on purpose so a slow pre-show can
        be attributed to one or the other:

        1. The handshake — ~200ms of TCP and TLS per socket.
        2. A **per-voice warm-up inside the model**, which the handshake does
           not cover. Measured on `eleven_v3_conversational`: the first
           utterance in a voice that had not yet been used on its socket cost
           ~300-350ms TTFB against a ~119ms median for every one after it. That
           is a cost that would otherwise land on an agent's first live line,
           which is the single worst place on a 20-minute show to spend 200ms.

        The warm-up audio is generated, fully drained and thrown away — it
        never reaches the mixer, because `prewarm` returns no stream and
        nothing is subscribed to one.
        """
        await asyncio.gather(*(self._channel(v).connect() for v in voice_ids))
        await asyncio.gather(*(self._warm_voice(v) for v in voice_ids))

    async def _warm_voice(self, voice_id: str) -> None:
        """Generate and discard one utterance, so the next one is not the first."""
        # A failed warm-up is not a reason to refuse to start a show: the cost
        # it exists to pay down is ~200ms on one line, and the socket is
        # already up. Same posture as the read loop — see its comment.
        with contextlib.suppress(Exception):
            stream = await self._channel(voice_id).speak(self._WARMUP_TEXT)
            async for _chunk in stream.chunks():
                pass

    @staticmethod
    def _guard(text: str) -> str:
        text = text.strip()
        if not text:
            raise ValueError("refusing to synthesise empty text")
        if "<" in text and ">" in text:
            # Defence in depth. sanitise() should already have caught this.
            raise ValueError(f"refusing to synthesise markup: {text[:60]!r}")
        # Brackets stopped being inert when the model started performing them.
        # An allowlisted tag is expected here; anything else means sanitise()
        # did not run, and on v3 that is a tag the model may *act on* — an
        # `[applause]` or an accent — not merely a word read aloud.
        unknown = [t for t in re.findall(r"\[([^\]]*)\]", text) if t not in AUDIO_TAGS]
        if unknown:
            raise ValueError(f"refusing to synthesise unallowed audio tag: {unknown!r}")
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
