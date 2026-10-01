"""Speechmatics Agent STT, one WebSocket per voice.

The preview Agent STT endpoint (`/v2/agent`, model `linden-1`) spoken directly
over raw `websockets` — there is no SDK for it yet, so the protocol is
hand-rolled here the same way `tts.py` hand-rolls ElevenLabs.

Two things this layer is responsible for and the floor controller is not:

**Channel identity.** Agent STT has no multi-channel mode, so each voice is its
own connection. That is the stronger form of the same property the
multi-channel client gave us: speaker attribution is a fact about the wiring,
not a diarisation result (FEASIBILITY.md 3.3). One speaker per socket, and we
already know who.

That reasoning still holds exactly as written for the agents' display-only
sessions, which transcribe each agent's own played audio: one voice, known in
advance, nothing to diarise. It does **not** hold for Ricky's mic, and the
difference is not a change of mind. A microphone on a stage is not a channel
with one speaker on it — the audience is in the room and the PA bleeds back
into it — so the wiring cannot answer "is this Ricky?" there, and it never
could; it was simply not being asked. Diarisation on that one socket is
therefore doing the opposite job from the one it was rejected for: not
*guessing* identity where the wiring already knew it, but *establishing*
identity where the wiring cannot. It is configured at exactly one call site
(`PanelSTT.identify`, from `PanelRuntime.run` after enrolment), never by a
changed default, because both families share `STTConfig`.

What it buys is a gate rather than a label. Post-enrolment, a segment not
attributed to Ricky never becomes a `TranscriptUpdated` at all, and his turn
is the only one that may open arbitration — so an audience question is neither
transcribed nor answered. See `_emit_transcript`, `_receive`'s `EndOfTurn`
arm, and `packages/panel_runtime/tests/test_speaker_isolation.py`.

**End of turn.** `EndOfTurn` from the server becomes `TurnYielded`. This is the
*understanding* path and it is deliberately not in the barge-in path: VAD owns
stopping, STT owns understanding (CLAUDE.md). Nothing here is allowed to be on
the critical path for interrupting an agent — which is why the endpoint's own
`SpeechStarted`/`SpeechEnded` messages are logged and dropped rather than
turned into `HumanSpeechStarted`/`HumanSpeechEnded`.

**Agent speech never reaches `panel_core` through this module.** Agent turns
enter conversation state as text, because we generated them and already know
them verbatim — routing a lossy, latent transcription of our own voices into
the reducer is the feedback loop that ends the show.

That is a rule about the *reducer*, not about this class, and the distinction
is now load-bearing: `PanelRuntime` runs a second `PanelSTT` over each agent's
own played audio purely so the video wall's transcript band can be timed off
real speech instead of an assumed words-per-second (see
`PanelRuntime._run_agent_stt`). Nothing in that session is emitted; its
`TranscriptUpdated` events go straight to `panel_display` and its `TurnYielded`
is dropped on the floor. `PanelSTT` itself is, and has always been, generic
over `speaker` — it knows who is on the far end of a socket because the wiring
says so, and it does not care whether that is a person.

**`additional_vocab` on this endpoint is confirmed.** This note used to say the
opposite — that no Speechmatics page documented the raw `/v2/agent` protocol at
all, so whether it accepted the key was unestablished. The agent-specific API
reference now documents it, with exactly the `content` + optional
`sounds_like` schema `vocab_from_cast` already builds (and which LiveKit's
`speechmatics/linden-1` plugin docs describe for the same model). No behaviour
changes on the strength of that: `_VocabRejected` and its one retry with the
key dropped stay exactly as they are. A documented key is not a deployed one,
the preview endpoint has moved under us before, and the machinery costs one
reconnect in the case it is not needed against the whole show's transcription
in the case it is.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Self

import websockets
from panel_core import (
    HUMAN,
    PanelCast,
    TranscriptUpdated,
    TurnYielded,
    UnverifiedSpeechDetected,
)

log = logging.getLogger(__name__)

# 16-bit signed LE at 16kHz — the same PCM the VAD and mixer use, so a mic block
# is fed to both without conversion.
STT_SAMPLE_RATE = 16_000

AGENT_STT_WS = "wss://preview.rt.speechmatics.com/v2/agent"

# The only model the agent endpoint serves during preview.
DEFAULT_MODEL = "linden-1"

# The server's own diarization labels. A known speaker may not be given a label
# in this format — `StartRecognition` rejects it — so `PanelSTT.identify`
# refuses one early, where the error names the cause, rather than letting the
# session fail its handshake and retry a config that can never be accepted.
_INTERNAL_LABEL_RE = re.compile(r"(?:S\d+|UU)", re.IGNORECASE)


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
    # One known voice per connection, so identity comes from the wiring — and
    # the default therefore stays off. Both of this repo's session families
    # inherit it: the agents' display-only pass (`PanelRuntime.agent_stt`)
    # genuinely has one known voice per socket and must never turn this on,
    # and so did Ricky's mic until speaker enrolment arrived.
    #
    # Ricky's mic is now the one exception and it is configured *explicitly*,
    # at one call site, by `PanelSTT.identify()` — never by changing this
    # default, because the two families share this dataclass and a default
    # change would silently diarise the agents' sockets too.
    diarization: str = "none"
    speaker_diarization_config: dict[str, Any] | None = None
    # How readily the engine splits audio into distinct speakers, 0-1, server
    speaker_sensitivity: float = 0.5
    # Known speakers to identify in this session, each
    # `{"label": ..., "speaker_identifiers": [...]}` as returned by a previous
    # session's `SpeakersResult`. A matched segment comes back carrying
    # `segment.speaker == label`; anyone unmatched keeps an `S1`-style label.
    # The server rejects a label in its own internal format (`S1`, `S2`,
    # `UU`), so the labels here are names like "Ricky".
    #
    # Setting this is what turns `_AgentSTTSession` into a gate: see
    # `identified_labels` and `_emit_transcript`. Empty — the default, and what
    # every session in the show ran with before enrolment existed — means no
    # identification and byte-for-byte today's behaviour.
    #
    # A tuple of dicts, matching `additional_vocab` above, so the frozen
    # dataclass is not handed a mutable field.
    speakers: tuple[dict[str, Any], ...] = ()
    # Ask the server to return speaker identifiers for this session. Only the
    # enrolment capture pass sets it; `SpeakersResult` then arrives at end of
    # stream. Mid-session `GetSpeakers` polling is deliberately not used — see
    # `enrolment.py`.
    get_speakers: bool = False
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
        if self.diarization != "none":
            speaker_config: dict[str, Any] = dict(self.speaker_diarization_config or {})
            speaker_config["speaker_sensitivity"] = self.speaker_sensitivity
            if self.get_speakers:
                speaker_config["get_speakers"] = True
            if self.speakers:
                # Copied out, so nothing the server is sent aliases this
                # frozen config's own dicts.
                speaker_config["speakers"] = [dict(entry) for entry in self.speakers]
            if speaker_config:
                config["speaker_diarization_config"] = speaker_config
        return config

    def identified_labels(self) -> frozenset[str]:
        """The speaker labels this session can recognise by name.

        Empty means this session does no identification at all, which is the
        default and is what every session in the show ran with before
        enrolment existed. `_AgentSTTSession` branches on exactly this: empty
        and it behaves as it always has, non-empty and an unmatched segment is
        withheld from the transcript entirely.

        Returns:
            The `label` of every entry in `speakers`, or an empty set when
            diarization is off or no known speakers were supplied.
        """
        if self.diarization == "none":
            return frozenset()
        return frozenset(
            str(entry["label"]) for entry in self.speakers if entry.get("label")
        )

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


# Deliberately not in `personas/*.yaml`, and not a hole in "personas are data"
SHOW_VOCAB: tuple[dict[str, Any], ...] = (
    {"content": "Speechmatics", "sounds_like": ["speech maticks", "speech matticks"]},
    {"content": "LLM", "sounds_like": ["ell ell emm", "elelem"]},
)


def vocab_from_cast(cast: PanelCast) -> tuple[dict[str, Any], ...]:
    """Build `additional_vocab` entries from persona pronunciation data.

    Reads `sounds_like` off each `Persona` rather than hardcoding names
    here, so a persona's pronunciation stays data (`personas/*.yaml`)
    instead of code. A persona with no `sounds_like` hints contributes no
    entry — vocabulary lists add session-start latency, so this only ever
    biases the personas that actually need it (currently just Melia; see
    `personas/melia.yaml`).

    `SHOW_VOCAB` is appended to whatever the cast supplies. It is the one
    part of this list that is not derived from a persona, for the reason
    given above that constant.

    Args:
        cast: The panel's cast.

    Returns:
        One `additional_vocab` entry per persona that declares
        `sounds_like`, plus `SHOW_VOCAB`, each shaped
        `{"content": ..., "sounds_like": [...]}` per the schema the agent
        endpoint's own API reference documents (see this module's docstring).
    """
    return (
        tuple(
            {"content": persona.canonical_name, "sounds_like": list(persona.sounds_like)}
            for persona in cast.personas.values()
            if persona.sounds_like
        )
        + SHOW_VOCAB
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
    still-rejected) config forever. `additional_vocab` is now documented for
    `/v2/agent` (see the module docstring), and this stays anyway: a documented
    key is not a deployed one, and that endpoint's `Error` message still has no
    field naming the offending config key, so this treats *any* rejection
    seen while the key is present as possibly caused by it. That is a
    deliberately broad, safe-by-construction guess: the cost of a wrong
    guess is one wasted reconnect attempt, and the cost of not guessing is
    the show running with no transcription at all.
    """


class _AgentSTTSession:
    """One voice, one socket, reconnected for as long as the show is running."""

    def __init__(
        self,
        *,
        speaker: str,
        source: PushAudioSource,
        config: STTConfig,
        api_key: str,
        events: asyncio.Queue,
        name: str,
        on_speakers: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._speaker = speaker
        self._source = source
        self._config = config
        self._api_key = api_key
        self._events = events
        self._name = name
        self._on_speakers = on_speakers
        self._running = True
        self._started = False
        # Non-empty only on Ricky's mic, post-enrolment. This is the whole
        # switch between "transcribe whatever arrives", which is what every
        # session did before enrolment existed and what the agents'
        # display-only sessions still do, and "transcribe only the enrolled
        # moderator" — see `_emit_transcript`.
        self._identified = config.identified_labels()
        # Whether any segment in the turn now in progress was identified as an
        # enrolled speaker. Reset at both turn boundaries and on every fresh
        # socket; read once, at `EndOfTurn`, to decide whether this turn is
        # allowed to open arbitration. Turn-level protocol state, which is
        # already this class's job.
        self._turn_had_identified = False
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
            # A reconnect mid-turn leaves the old turn's evidence behind with
            # the old socket. Starting clean is the conservative direction: the
            # new socket's first `EndOfTurn` must be earned by a segment this
            # connection actually identified.
            self._turn_had_identified = False
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
                case "StartOfTurn":
                    # Only bookkeeping, and only for the identity gate: a new
                    # turn's evidence starts empty. Not forwarded to the floor
                    # — the reducer has no start-of-turn event and does not
                    # want one, because `HumanSpeechStarted` off the local VAD
                    # already says this hundreds of milliseconds sooner.
                    self._turn_had_identified = False
                case "EndOfTurn":
                    # End of turn — the floor may now be arbitrated. This is
                    # the *understanding* path; the barge-in reflex already
                    # fired on VAD hundreds of milliseconds ago.
                    #
                    # Withheld entirely when this session identifies speakers
                    # and nothing in the turn was the enrolled moderator.
                    # Dropping a stranger's transcript text is not enough on
                    # its own: `TurnYielded` carries no speaker, arbitration
                    # does not ask whose words opened the floor, and a standing
                    # invitation would therefore let an audience question be
                    # answered by an agent. The words are already gone; this is
                    # what stops the *turn* existing too.
                    if self._identified and not self._turn_had_identified:
                        log.info(
                            "stt[%s]: EndOfTurn withheld — no segment in the "
                            "turn was identified as %s",
                            self._name,
                            "/".join(sorted(self._identified)),
                        )
                    else:
                        self._events.put_nowait(TurnYielded(t=time.monotonic()))
                    self._turn_had_identified = False
                case "SpeakersResult":
                    # Enrolment's answer, and only enrolment ever asks: the
                    # show's sessions never set `get_speakers`, so one arriving
                    # here is worth a line rather than a silent debug. The
                    # enrolment module runs its own session (see
                    # `enrolment.py`) and does not come through this class, so
                    # the hook is a courtesy for anything that later wants to.
                    if self._on_speakers is not None:
                        self._on_speakers(message)
                    else:
                        log.warning(
                            "stt[%s]: unsolicited SpeakersResult (%d speakers)",
                            self._name,
                            len(message.get("speakers") or ()),
                        )
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
        """Turn one segment into an event — or into no text at all.

        The identity gate, and the lowest layer it can live at. A segment that
        was not attributed to an enrolled speaker never becomes a
        `TranscriptUpdated`, so the stranger's words do not exist as an event
        with content anywhere in the process: no consumer — console, video
        wall, rehearsal log, reducer, `PanelState.transcript` — can leak what
        it was never handed. Filtering a `TranscriptUpdated` further upstream
        would leave the text sitting in an object that four sinks already
        subscribe to, and the requirement is not "nobody renders it" but "it
        was never written down".

        Three cases when this session identifies speakers, and the third is
        the one that matters:

        * **matched** — the enrolled moderator. Emitted exactly as before,
          unchanged in every field.
        * **attributed to someone else** — `UnverifiedSpeechDetected`, which
          has no text field at all. The floor uses it to keep a duck
          recoverable rather than promoting it to a stop.
        * **no attribution at all** — dropped silently, and deliberately *not*
          reported as a stranger. "Diarization attributed nothing" and
          "diarization says this is not Ricky" are different facts, and
          conflating them would let a run of unattributed segments block
          Ricky's own interrupt. Absence of confirmation is not confirmation
          of absence; `panel_core` sees no evidence either way and behaves
          exactly as it does today.

        **Unverified on this endpoint** (venue-rig check, CLAUDE.md
        § Deployment): whether `segment.speaker` is populated on
        `AddPartialSegment` as reliably as on `AddSegment` is documented
        nowhere for `/v2/agent`, in either direction — the same standing
        uncertainty as `additional_vocab`'s schema was. If partials turn out to
        carry no `speaker`, they take the third branch above: Ricky's live
        partial text stops reaching the console and the wall, his finals still
        land, and content-based barge-in falls back to finals only. His
        *reflex* barge-in is unaffected either way, because that fires on the
        local VAD and never on a transcript (CLAUDE.md). Degraded display, not
        a broken interrupt — which is why this direction is the safe one to
        guess.
        """
        segment = message.get("segment") or {}
        if self._identified:
            speaker = segment.get("speaker")
            if not speaker:
                return  # no evidence either way — say nothing at all
            if speaker not in self._identified:
                self._events.put_nowait(
                    UnverifiedSpeechDetected(t=time.monotonic(), is_final=is_final)
                )
                return
            # Counted from partials as well as finals, and on the match rather
            # than on the text. The failure to avoid is withholding *Ricky's*
            # `EndOfTurn`, which would leave a real question unarbitrated and
            # the panel silent — much the worse of the two errors, since a
            # stranger's turn getting through opens arbitration against a
            # transcript their words never entered.
            self._turn_had_identified = True

        text = segment.get("transcript", "").strip()
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
    """Transcription for a set of channels, as `panel_core` event objects.

    Events are pushed onto a queue rather than dispatched directly, so the
    consumer decides what they mean. For the human mics that consumer is
    `PanelRuntime._run_stt`, which emits them into the reducer; for the
    display-only pass over the agents' own audio it is
    `PanelRuntime._run_agent_stt`, which emits nothing at all. Same class,
    same protocol, two different sinks — the isolation is the *caller's*
    property, not this class's (see the module docstring).
    """

    def __init__(
        self,
        channels: dict[str, str],
        *,
        config: STTConfig | None = None,
        api_key: str | None = None,
    ) -> None:
        """`channels` maps a channel id to the speaker id used on events.

        For the human mics that is `{"ricky": HUMAN}` today, and a second
        entry the day an audience mic is added — which is a wiring change, not
        a code one. The display's agent pass maps each agent id to itself.
        Each entry is its own connection.

        A channel id is a wiring name — it names a socket, appears in logs and
        task names, and is never compared against a diarization label (see
        `identify`). `"ricky"` and the enrolled label `"Ricky"` are unrelated
        strings.
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

    # ----------------------------------------------------------- identification

    def identify(self, *, label: str, speaker_identifiers: tuple[str, ...]) -> None:
        """Configure this instance to recognise one enrolled speaker.

        The single call site that turns diarization on anywhere in the show
        (`PanelRuntime.run`, once enrolment has produced identifiers for
        Ricky). Deliberately a method rather than a changed `STTConfig`
        default: both session families share that dataclass, and the agents'
        display-only pass over their own played audio must stay undiarized —
        one voice per socket, identity already a fact about the wiring. A moved
        default would have diarised those three sockets too, silently.

        Must be called before `start()`. Each `_AgentSTTSession` reads the
        config once, when it is constructed there, so a later change would
        apply to nothing and look as though it had.

        Args:
            label: The label matched segments will carry, e.g. "Ricky". Must
                not be in the server's internal format (`S1`, `S2`, `UU`) —
                those are rejected on `StartRecognition`.
            speaker_identifiers: Opaque identifiers from a previous session's
                `SpeakersResult`. Bound to the STT model that produced them;
                `enrolment.SpeakerStore` is what keeps the two together.

        Raises:
            RuntimeError: If the sessions are already running.
            ValueError: If `label` looks like an internal server label, or no
                identifiers were supplied.
        """
        if self._sessions or self._tasks:
            raise RuntimeError("identify() must be called before start()")
        if not speaker_identifiers:
            raise ValueError("speaker_identifiers must not be empty")
        if _INTERNAL_LABEL_RE.fullmatch(label):
            raise ValueError(
                f"{label!r} is the server's own label format and is rejected "
                "on StartRecognition — use a name"
            )
        self.config = replace(
            self.config,
            diarization="speaker",
            speakers=({"label": label, "speaker_identifiers": list(speaker_identifiers)},),
        )

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
        """Push one block of audio. Safe to call from the PortAudio callback."""
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
