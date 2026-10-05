"""Speaker enrolment for the moderator's mic.

Ricky's microphone is on a stage. The audience is in the room and the PA
bleeds back into it, so "audio arrived on this socket" has never meant "Ricky
said this" — it was simply never being asked. This module asks it once, before
the show starts, and hands `panel_runtime.stt` the identifiers that let it
answer per segment for the rest of the night.

Two phases, in one continuous mic feed:

**Capture.** A session with `diarization: "speaker"` and
`speaker_diarization_config.get_speakers: true`. Ricky talks for up to 30
seconds; the stream is ended and the server replies with `SpeakersResult`,
one entry per diarised speaker, each carrying opaque `speaker_identifiers`.

**Verification.** A *second* session, configured with those identifiers as a
known speaker labelled "Ricky". His matched segments come back carrying
`segment.speaker == "Ricky"`; three finalised ones and enrolment succeeds.

That second session is what "get the same speaker back three times" means
here, and the shape is forced by the API rather than chosen. There is no
embedding, no vector and no similarity score anywhere in this protocol —
`speaker_identifiers` is an opaque, model-bound string — so there is nothing
to compare and no threshold to tune. The only way to ask "does this voice
match that enrolment?" is to hand the identifiers back to the server and see
what it labels. Verification is therefore a rehearsal of the exact mechanism
the show will run on, which makes it a better check than a distance metric
would have been: it fails if the identifiers are unusable for *any* reason,
including ones we would not have thought to measure.

**Identifiers are bound to the STT model.** A model change invalidates them,
and the server says so with a `Warning` on the session that tries to use
them. `SpeakerStore` therefore records the model alongside the identifiers and
treats a stored file from a different model as absent — re-enrolling costs 30
seconds of rehearsal, while trusting a stale identifier costs a show in which
nothing Ricky says is recognised as his.

**Mid-session `GetSpeakers` polling is deliberately not used.** `final: false`
is documented only for the standard `/v2` endpoint, against an
`AddTranscript` message `/v2/agent` does not emit, so its behaviour here is
unverified. Both sessions here are *terminating*: they send `EndOfStream` and
read the result at end of stream, which is the documented path. That is also
why they do not reuse `_AgentSTTSession` — see `_EnrolmentSession`.

Nothing here prints. The console is the CLI's job, exactly as
`address.AddressClassifier` stays silent and `panel.py` renders around it;
progress arrives through `on_progress` so the same state machine can be driven
by a test that asserts on phases rather than on scraped output.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import websockets

from .stt import PushAudioSource, STTConfig

log = logging.getLogger(__name__)

# Where the identifiers live between runs. Plain JSON, gitignored, not
# encrypted: an opaque model-bound speaker identifier is not a credential and
# not audio, and the file sits on the venue machine for one night.
DEFAULT_STORE_PATH = Path(".panel/speakers.json")

# The label matched segments carry, and the one string `panel_runtime.stt`
# compares against. Not `S1`/`S2`/`UU` — the server rejects its own internal
# label format on `StartRecognition`.
MODERATOR_LABEL = "Ricky"

# Hard cap on the capture phase. A cap rather than a target: if nobody ever
# says anything usable the phase must still end, because the show is waiting
# behind it. 30s is the longest the docs' own enrolment guidance asks for.
CAPTURE_LIMIT_S = 30.0

# Hard cap on verification. Generous, because the failure it guards against is
# nobody talking rather than anything slow: three finalised segments take a
# sentence or two, and a rig where they never arrive at all is a rig where
# waiting longer will not help.
VERIFY_LIMIT_S = 60.0

# How many finalised segments must come back labelled as the enrolled speaker.
# Three, not one: a single match can be luck on a short segment, and the point
# of the phase is to find out on a laptop rather than on stage.
REQUIRED_VERIFIED_SEGMENTS = 3

# How long to wait after `EndOfStream` for `SpeakersResult` and
# `EndOfTranscript`. Longer than `STTConfig.close_timeout_s` (5s, sized for
# draining a transcript) because this is the one message the capture phase
# exists to collect and the server computes it at end of stream. Re-measure on
# the venue rig (CLAUDE.md § Deployment) — nothing here has been timed against
# the real endpoint.
RESULT_TIMEOUT_S = 15.0


@dataclass(frozen=True, slots=True)
class EnrolledSpeaker:
    """One enrolled voice, and the model the identifiers are only valid for.

    Attributes:
        label: The label matched segments will carry, e.g. "Ricky".
        speaker_identifiers: Opaque, model-bound identifiers from
            `SpeakersResult`. Not embeddings — there is nothing to compare
            them with; they are handed back to the server verbatim.
        model: The STT model that produced them. The reason this dataclass
            exists rather than a bare tuple: identifiers outlive the process
            but not a model change, and a stale one is indistinguishable from
            a good one until nothing Ricky says is recognised.
        enrolled_at: ISO 8601, for the operator's benefit. Never compared —
            identifiers do not expire with time, only with the model.
    """

    label: str
    speaker_identifiers: tuple[str, ...]
    model: str
    enrolled_at: str = ""

    def to_json(self) -> dict[str, Any]:
        """The on-disk shape."""
        return {
            "label": self.label,
            "speaker_identifiers": list(self.speaker_identifiers),
            "model": self.model,
            "enrolled_at": self.enrolled_at,
        }


class SpeakerStore:
    """The enrolment file, read and written defensively.

    `load()` collapses every reason the stored enrolment cannot be used into a
    single `None`, and that is the interface: absent, unreadable, malformed and
    model-mismatched all mean "capture again", and no caller has a different
    response to any of them. Distinguishing them would only invite a caller to
    try to recover from one.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_STORE_PATH

    def load(self, *, model: str) -> EnrolledSpeaker | None:
        """Read the stored enrolment, if there is a usable one for `model`.

        Args:
            model: The STT model the show will run on. A stored enrolment from
                any other model is treated as absent — identifiers are bound
                to the model that made them.

        Returns:
            The stored speaker, or None if there is nothing usable on disk.
        """
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("enrolment: %s is unreadable (%s) — re-enrolling", self.path, exc)
            return None

        if not isinstance(raw, dict):
            log.warning("enrolment: %s is not an object — re-enrolling", self.path)
            return None

        stored_model = raw.get("model")
        if stored_model != model:
            # The `Warning` the server would otherwise send mid-show, caught
            # before the show instead.
            log.warning(
                "enrolment: %s was made with model %r but this session runs "
                "%r — identifiers are model-bound, so re-enrolling",
                self.path,
                stored_model,
                model,
            )
            return None

        identifiers = raw.get("speaker_identifiers")
        label = raw.get("label")
        if not isinstance(identifiers, list) or not identifiers or not isinstance(label, str):
            log.warning("enrolment: %s has no usable identifiers — re-enrolling", self.path)
            return None

        return EnrolledSpeaker(
            label=label,
            speaker_identifiers=tuple(str(i) for i in identifiers),
            model=str(stored_model),
            enrolled_at=str(raw.get("enrolled_at") or ""),
        )

    def save(self, speaker: EnrolledSpeaker) -> None:
        """Write the enrolment, creating the directory if need be.

        Args:
            speaker: The enrolment to persist.

        Raises:
            OSError: If the file cannot be written. Deliberately not swallowed:
                the caller can still run the show on the in-memory identifiers
                and is better placed to decide whether a store it cannot write
                is worth a console line.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(speaker.to_json(), indent=2) + "\n")


@dataclass
class _LabelTally:
    """How much of the capture one diarised label accounted for."""

    seconds: float = 0.0
    segments: int = 0


class _EnrolmentSession:
    """One terminating recognition session, for enrolment only.

    Deliberately not `_AgentSTTSession`, and the difference is lifecycle
    rather than protocol. That class reconnects for as long as the show is
    running, which is exactly right for the show and exactly wrong here: these
    two sessions must send `EndOfStream` and read what comes back. Teaching it
    to stop would have put a "stop after one session" path inside the class the
    show's transcription depends on, to save about forty lines — a poor trade
    against a live stage. The show's session keeps its single behaviour, and
    this keeps its own.

    It also runs without `additional_vocab`. Persona pronunciation is
    irrelevant to identifying a voice, and dropping the key removes the one
    documented way `StartRecognition` is known to have been rejected on this
    endpoint (`stt._VocabRejected`) from the phase that gates the show
    starting at all.
    """

    def __init__(
        self,
        *,
        config: STTConfig,
        api_key: str,
        source: PushAudioSource,
        on_segment: Callable[[dict[str, Any], bool], None],
        limit_s: float,
        result_timeout_s: float = RESULT_TIMEOUT_S,
    ) -> None:
        self._config = config
        self._api_key = api_key
        self._source = source
        self._on_segment = on_segment
        self._limit_s = limit_s
        self._result_timeout_s = result_timeout_s
        self._finish = asyncio.Event()
        self.speakers_result: dict[str, Any] | None = None
        self.warnings: list[dict[str, Any]] = []

    def finish(self) -> None:
        """End the stream at the next opportunity.

        Called from `on_segment` once the phase has what it came for, so
        verification stops at the third match instead of running the clock out.
        """
        self._finish.set()

    async def run(self) -> None:
        """Connect, stream audio, end the stream, read what comes back.

        Raises:
            RuntimeError: If `StartRecognition` is rejected or the server
                reports an error mid-session.
        """
        async with websockets.connect(
            self._config.url,
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            open_timeout=self._config.connect_timeout_s,
        ) as ws:
            await ws.send(json.dumps(self._config.to_start_recognition()))
            await self._await_started(ws)

            receiver = asyncio.create_task(self._receive(ws), name="enrolment-recv")
            seq_no = 0
            try:
                seq_no = await self._send_audio(ws)
            finally:
                with contextlib.suppress(Exception):
                    if self._config.get_speakers:
                        # The documented request form. `final: true` resolves
                        # at end of stream, which is where this is going
                        # anyway; sent alongside `get_speakers: true` because
                        # either route delivering `SpeakersResult` is a result,
                        # and a duplicate is harmless.
                        await ws.send(json.dumps({"message": "GetSpeakers", "final": True}))
                    await ws.send(json.dumps({"message": "EndOfStream", "last_seq_no": seq_no}))
                try:
                    await asyncio.wait_for(receiver, timeout=self._result_timeout_s)
                except TimeoutError:
                    log.warning(
                        "enrolment: no EndOfTranscript within %.0fs of EndOfStream",
                        self._result_timeout_s,
                    )
                finally:
                    receiver.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await receiver

    async def _await_started(self, ws: websockets.ClientConnection) -> None:
        while True:
            message = json.loads(await ws.recv())
            kind = message.get("message")
            if kind == "RecognitionStarted":
                return
            if kind == "Error":
                raise RuntimeError(f"StartRecognition rejected: {message}")
            log.debug("enrolment: %s before RecognitionStarted", kind)

    async def _send_audio(self, ws: websockets.ClientConnection) -> int:
        """Pump the mic until the cap, the mic closing, or `finish()`.

        Each read is bounded by the time left on the cap rather than the cap
        being re-checked between reads. That distinction is the whole
        robustness of the phase: `PushAudioSource.read()` waits for a full
        block, so on a mic that has gone quiet — a muted channel, a device
        that opened but produces nothing — a loop that only checked the clock
        between reads would never reach the check, and the cap would be
        enforced by nothing but the caller's outer timeout. The show is
        waiting behind this phase, so it has to end on its own.

        Cancelling a half-filled read discards at most one block, and only
        ever at the end of a phase that is finishing anyway.

        Returns:
            The number of audio frames sent, for `EndOfStream.last_seq_no`.
        """
        seq_no = 0
        deadline = time.monotonic() + self._limit_s
        while not self._finish.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(
                    self._source.read(self._config.chunk_size), timeout=remaining
                )
            except TimeoutError:
                break  # the cap
            if not frame:
                break  # the mic closed
            await ws.send(frame)
            seq_no += 1
        return seq_no

    async def _receive(self, ws: websockets.ClientConnection) -> None:
        async for raw in ws:
            message = json.loads(raw)
            match message.get("message"):
                case "AddSegment":
                    self._on_segment(message.get("segment") or {}, True)
                case "AddPartialSegment":
                    self._on_segment(message.get("segment") or {}, False)
                case "SpeakersResult":
                    self.speakers_result = message
                case "Warning":
                    # Where a model/identifier mismatch would announce itself.
                    # Collected as well as logged so the caller can fail the
                    # phase on it rather than discovering it mid-show.
                    self.warnings.append(message)
                    log.warning("enrolment: %s", message)
                case "Error":
                    raise RuntimeError(f"server error: {message}")
                case "EndOfTranscript":
                    return
                case other:
                    log.debug("enrolment: %s", other)


class SpeakerEnrolment:
    """The capture-then-verify state machine.

    Holds no console and no floor: it is handed mic audio through `feed()` and
    reports phases through `on_progress`. `panel.py` owns everything the
    operator sees.

    Typical use, and the only one:

        enrolment = SpeakerEnrolment(config=stt_config, on_progress=render)
        runtime_feeds_audio_to(enrolment.feed)
        speaker = await enrolment.run()
    """

    def __init__(
        self,
        *,
        config: STTConfig,
        api_key: str | None = None,
        label: str = MODERATOR_LABEL,
        capture_limit_s: float = CAPTURE_LIMIT_S,
        verify_limit_s: float = VERIFY_LIMIT_S,
        required_segments: int = REQUIRED_VERIFIED_SEGMENTS,
        result_timeout_s: float = RESULT_TIMEOUT_S,
        on_progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        """
        Args:
            config: The show's STT deployment config. Only the transport and
                model settings are used; diarization, `get_speakers`,
                `speakers` and `additional_vocab` are all set per phase here.
            api_key: Speechmatics key, or None to read
                `SPEECHMATICS_API_KEY`.
            label: What to call the enrolled speaker.
            capture_limit_s: Hard cap on the capture phase.
            verify_limit_s: Hard cap on the verification phase.
            required_segments: Finalised matched segments needed to pass.
            result_timeout_s: How long to wait for `SpeakersResult` after
                `EndOfStream`.
            on_progress: Called with `(phase, detail)` as the machine
                advances. Never used for control flow — a caller that passes
                nothing gets the same behaviour.

        Raises:
            RuntimeError: If no API key is available.
        """
        key = api_key or os.environ.get("SPEECHMATICS_API_KEY")
        if not key:
            raise RuntimeError("SPEECHMATICS_API_KEY is not set")
        self._api_key = key
        self._config = config
        self.label = label
        self._capture_limit_s = capture_limit_s
        self._verify_limit_s = verify_limit_s
        self._required_segments = required_segments
        self._result_timeout_s = result_timeout_s
        self._on_progress = on_progress
        # The live phase's audio sink, or None between phases. `feed()` is
        # called from the PortAudio callback and must never care which phase is
        # running, so this is one attribute read and one method call.
        self._source: PushAudioSource | None = None

    # ------------------------------------------------------------ audio in

    def feed(self, pcm: bytes) -> None:
        """Push one block of mic audio. Safe from the PortAudio callback.

        A no-op between phases, which is what lets the audio callback hold a
        single reference to this object for the whole enrolment rather than
        branching on which session is up.
        """
        source = self._source
        if source is not None:
            source.feed(pcm)

    def _progress(self, phase: str, **detail: Any) -> None:
        if self._on_progress is not None:
            self._on_progress(phase, detail)

    # ------------------------------------------------------------- phases

    async def run(self) -> EnrolledSpeaker | None:
        """Capture, then verify, then report.

        Returns:
            The enrolled speaker, or None if either phase failed. None is not
            an error to raise on: the caller decides what an unenrollable
            moderator means for the show, and that decision is not this
            module's to make.
        """
        identifiers = await self.capture()
        if not identifiers:
            return None
        if not await self.verify(identifiers):
            return None
        speaker = EnrolledSpeaker(
            label=self.label,
            speaker_identifiers=identifiers,
            model=self._config.model,
            enrolled_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self._progress("enrolled", label=self.label, model=speaker.model)
        return speaker

    async def capture(self) -> tuple[str, ...]:
        """Phase one: diarise up to `capture_limit_s` of mic audio.

        Returns:
            The `speaker_identifiers` of the speaker who accounted for most of
            the capture, or an empty tuple if the phase produced nothing
            usable.
        """
        config = replace(
            self._config,
            diarization="speaker",
            get_speakers=True,
            speakers=(),
            additional_vocab=(),
        )
        tally: dict[str, _LabelTally] = {}

        def on_segment(segment: dict[str, Any], is_final: bool) -> None:
            if not is_final:
                return
            speaker = segment.get("speaker")
            if not speaker:
                return
            entry = tally.setdefault(str(speaker), _LabelTally())
            entry.segments += 1
            entry.seconds += _segment_seconds(segment)
            self._progress(
                "capture_segment",
                speaker=str(speaker),
                segments=entry.segments,
                seconds=entry.seconds,
            )

        self._progress("capture_started", limit_s=self._capture_limit_s)
        session = await self._run_session(
            config, on_segment=on_segment, limit_s=self._capture_limit_s
        )
        if session is None:
            return ()

        result = session.speakers_result
        if not result:
            self._progress("capture_failed", reason="no SpeakersResult")
            return ()

        label = _dominant_label(result, tally)
        if label is None:
            self._progress("capture_failed", reason="no speaker could be identified")
            return ()

        identifiers = _identifiers_for(result, label)
        if not identifiers:
            self._progress("capture_failed", reason=f"no identifiers for {label}")
            return ()

        self._progress(
            "capture_done",
            source_label=label,
            seconds=tally.get(label, _LabelTally()).seconds,
            segments=tally.get(label, _LabelTally()).segments,
            speakers_seen=len(result.get("speakers") or ()),
        )
        return identifiers

    async def verify(self, identifiers: tuple[str, ...]) -> bool:
        """Phase two: hand the identifiers back and see if they stick.

        Args:
            identifiers: What `capture()` returned.

        Returns:
            True once `required_segments` finalised segments have come back
            labelled `self.label`.
        """
        config = replace(
            self._config,
            diarization="speaker",
            get_speakers=False,
            speakers=({"label": self.label, "speaker_identifiers": list(identifiers)},),
            additional_vocab=(),
        )
        matched = 0
        session: _EnrolmentSession | None = None

        def on_segment(segment: dict[str, Any], is_final: bool) -> None:
            nonlocal matched
            # Finalised segments only. A partial can be revised, and
            # `segment.speaker` on `AddPartialSegment` is not documented for
            # this endpoint either way — counting one would be counting
            # something that may not be there and may not survive.
            if not is_final or segment.get("speaker") != self.label:
                return
            matched += 1
            self._progress("verify_segment", matched=matched, needed=self._required_segments)
            if matched >= self._required_segments and session is not None:
                session.finish()

        self._progress(
            "verify_started", needed=self._required_segments, limit_s=self._verify_limit_s
        )
        session = _EnrolmentSession(
            config=config,
            api_key=self._api_key,
            source=self._open_source(),
            on_segment=on_segment,
            limit_s=self._verify_limit_s,
            result_timeout_s=self._result_timeout_s,
        )
        ok = await self._drive(session, limit_s=self._verify_limit_s)
        if not ok:
            return False
        if session.warnings:
            # A `Warning` on the session that is *using* the identifiers is the
            # documented signal that they are not valid for this model. Failing
            # here means re-enrolling now rather than finding out on stage.
            self._progress("verify_failed", reason="server warning", matched=matched)
            return False
        if matched < self._required_segments:
            self._progress("verify_failed", reason="not enough matches", matched=matched)
            return False
        self._progress("verify_done", matched=matched)
        return True

    # ------------------------------------------------------------- plumbing

    def _open_source(self) -> PushAudioSource:
        source = PushAudioSource(asyncio.get_running_loop())
        self._source = source
        return source

    async def _run_session(
        self,
        config: STTConfig,
        *,
        on_segment: Callable[[dict[str, Any], bool], None],
        limit_s: float,
    ) -> _EnrolmentSession | None:
        session = _EnrolmentSession(
            config=config,
            api_key=self._api_key,
            source=self._open_source(),
            on_segment=on_segment,
            limit_s=limit_s,
            result_timeout_s=self._result_timeout_s,
        )
        if not await self._drive(session, limit_s=limit_s):
            return None
        return session

    async def _drive(self, session: _EnrolmentSession, *, limit_s: float) -> bool:
        """Run one session to completion, bounded and never raising.

        The outer bound is belt to `_send_audio`'s braces: that loop's deadline
        assumes `PushAudioSource.read()` keeps returning, which assumes the mic
        keeps producing. A dead input device would otherwise park the show
        here forever, which is the one outcome worse than an ungated mic.

        Returns:
            True if the session ran to completion, False if it failed or timed
            out. Both are reported through `on_progress`; neither raises,
            because a failed enrolment is a decision for the caller and not an
            exception to unwind the show with.
        """
        budget = limit_s + self._result_timeout_s + self._config.connect_timeout_s + 5.0
        try:
            await asyncio.wait_for(session.run(), timeout=budget)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._progress("session_failed", reason=f"timed out after {budget:.0f}s")
            log.warning("enrolment: session timed out after %.0fs", budget)
            return False
        except Exception as exc:  # noqa: BLE001 — a failed enrolment is a result
            self._progress("session_failed", reason=str(exc))
            log.warning("enrolment: session failed: %s", exc)
            return False
        finally:
            source = self._source
            self._source = None
            if source is not None:
                source.close()
        return True


# ------------------------------------------------------------------- helpers


def _segment_seconds(segment: dict[str, Any]) -> float:
    """How much audio a segment covered, or 0.0 if it does not say.

    `start_time`/`end_time` on an `/v2/agent` segment are not documented, so
    this reads them if they are there and contributes nothing if they are not —
    in which case `_dominant_label` falls back to counting segments. Written
    this way rather than assuming either, in the same spirit as the rest of
    this endpoint's unverified corners.
    """
    try:
        start = float(segment.get("start_time", 0.0))
        end = float(segment.get("end_time", 0.0))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, end - start)


def _dominant_label(result: dict[str, Any], tally: dict[str, _LabelTally]) -> str | None:
    """Which diarised label was the enrolling speaker?

    Explicitly *not* "assume `S1`". Label numbering reflects the order the
    diariser happened to separate voices, so a cough from the back of the room
    landing first is enough to make `S1` somebody else — and enrolling the
    audience as the moderator is the exact inverse of what this feature is
    for. So the speaker who accounted for the most audio wins, on the
    reasonable assumption that the person asked to talk for thirty seconds did
    most of the talking.

    Ranked on attributed seconds first and segment count second, because
    seconds are the better measure where available and `start_time`/`end_time`
    are not documented for this endpoint. Where no timing arrived at all, the
    count alone decides.

    The one case with no ranking to do is a result naming exactly one
    speaker: that is not an assumption, and a capture with a single voice in it
    is the ordinary rehearsal case.

    Args:
        result: The `SpeakersResult` message.
        tally: Per-label audio accounting from the capture's finalised
            segments.

    Returns:
        The winning label, or None if the result named nobody usable or gave
        no way to choose between several.
    """
    labels = [
        str(entry["label"])
        for entry in (result.get("speakers") or ())
        if isinstance(entry, dict)
        and entry.get("label")
        # `UU` is the server's "unattributed", not a person.
        and str(entry["label"]).upper() != "UU"
    ]
    if not labels:
        return None
    if len(labels) == 1:
        return labels[0]

    ranked = sorted(
        labels,
        key=lambda label: (
            tally.get(label, _LabelTally()).seconds,
            tally.get(label, _LabelTally()).segments,
        ),
        reverse=True,
    )
    best = ranked[0]
    if tally.get(best, _LabelTally()).segments == 0:
        # Several speakers and no attribution for any of them: nothing here
        # distinguishes the moderator from the room, and guessing is the one
        # thing this function exists not to do.
        log.warning(
            "enrolment: %d speakers returned and no segment attribution — "
            "cannot tell which is the moderator",
            len(labels),
        )
        return None
    return best


def _identifiers_for(result: dict[str, Any], label: str) -> tuple[str, ...]:
    """Pull one speaker's identifiers out of a `SpeakersResult`."""
    for entry in result.get("speakers") or ():
        if isinstance(entry, dict) and str(entry.get("label")) == label:
            identifiers = entry.get("speaker_identifiers") or ()
            return tuple(str(i) for i in identifiers)
    return ()
