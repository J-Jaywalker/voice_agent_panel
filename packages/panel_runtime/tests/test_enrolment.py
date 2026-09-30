"""Tests for `panel_runtime.enrolment`: the store, and the two-phase machine.

No network: `websockets.connect` is monkeypatched with a fake connection, the
same plain-`asyncio.run()` style `test_stt.py` uses rather than pytest-asyncio
(not a project dependency here).

The audio caps are driven at small values rather than at their real 30s/60s,
with the defaults themselves asserted separately. A test that actually waited
out a 30-second cap would be a test nobody runs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Self

import pytest
from panel_core import PanelCast
from panel_runtime.enrolment import (
    CAPTURE_LIMIT_S,
    REQUIRED_VERIFIED_SEGMENTS,
    VERIFY_LIMIT_S,
    EnrolledSpeaker,
    SpeakerEnrolment,
    SpeakerStore,
    _dominant_label,
    _LabelTally,
)
from panel_runtime.stt import STTConfig

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"

MODEL = "linden-1"


@pytest.fixture
def cast() -> PanelCast:
    return PanelCast.from_dir(PERSONA_DIR)


# ------------------------------------------------------------------- the store


def test_the_store_round_trips(tmp_path: Path) -> None:
    store = SpeakerStore(tmp_path / "speakers.json")
    speaker = EnrolledSpeaker(
        label="James",
        speaker_identifiers=("opaque-a", "opaque-b"),
        model=MODEL,
        enrolled_at="2026-10-21T09:00:00+00:00",
    )
    store.save(speaker)

    loaded = store.load(model=MODEL)
    assert loaded == speaker


def test_the_store_creates_its_directory(tmp_path: Path) -> None:
    """The default path is `.panel/speakers.json`, which will not exist on the
    venue machine the first time the show is run there."""
    store = SpeakerStore(tmp_path / "nested" / "deeper" / "speakers.json")
    store.save(EnrolledSpeaker(label="James", speaker_identifiers=("x",), model=MODEL))
    assert store.load(model=MODEL) is not None


def test_a_missing_file_is_absent(tmp_path: Path) -> None:
    assert SpeakerStore(tmp_path / "nothing.json").load(model=MODEL) is None


def test_a_model_mismatch_is_treated_as_absent(tmp_path: Path) -> None:
    """The conservative instinct the server's own `Warning` would otherwise
    deliver mid-show.

    Speaker identifiers are bound to the STT model that produced them. A
    stored enrolment from another model is not "probably fine" — it is
    indistinguishable from a good one until nothing James says is recognised
    as his, on stage, with no operator override to recover with.
    """
    store = SpeakerStore(tmp_path / "speakers.json")
    store.save(EnrolledSpeaker(label="James", speaker_identifiers=("x",), model="linden-0"))

    assert store.load(model=MODEL) is None, "must force re-enrolment"
    assert store.load(model="linden-0") is not None, "and is fine for its own model"


@pytest.mark.parametrize(
    "content",
    [
        "not json at all {{{",
        '"a bare string"',
        "[]",
        json.dumps({"model": MODEL}),  # no identifiers
        json.dumps({"model": MODEL, "label": "James", "speaker_identifiers": []}),
        json.dumps({"model": MODEL, "speaker_identifiers": ["x"]}),  # no label
        json.dumps({"label": "James", "speaker_identifiers": ["x"]}),  # no model
    ],
)
def test_an_unusable_file_is_absent(tmp_path: Path, content: str) -> None:
    """Every reason the file cannot be used collapses to one `None`.

    That is the interface: absent, unreadable, malformed and model-mismatched
    all mean "capture again", and no caller has a different response to any of
    them.
    """
    path = tmp_path / "speakers.json"
    path.write_text(content)
    assert SpeakerStore(path).load(model=MODEL) is None


# ------------------------------------------------------- picking the speaker


def _speakers_result(*labels: str) -> dict[str, Any]:
    return {
        "message": "SpeakersResult",
        "speakers": [
            {"label": label, "speaker_identifiers": [f"id-for-{label}"]} for label in labels
        ],
    }


def test_the_dominant_label_is_not_assumed_to_be_s1() -> None:
    """The bug this function exists to avoid.

    Label numbering reflects the order the diariser happened to separate
    voices, so a cough from the back of the room landing first is enough to
    make `S1` somebody else — and enrolling the audience as the moderator is
    the exact inverse of the feature. The speaker who accounted for the most
    audio wins.
    """
    result = _speakers_result("S1", "S2")
    tally = {
        "S1": _LabelTally(seconds=0.8, segments=1),  # a cough, heard first
        "S2": _LabelTally(seconds=24.0, segments=11),  # James
    }
    assert _dominant_label(result, tally) == "S2"


def test_segment_counts_decide_when_no_timings_arrive() -> None:
    """`start_time`/`end_time` are not documented for this endpoint, so the
    ranking falls back to counting segments rather than assuming they exist."""
    result = _speakers_result("S1", "S2")
    tally = {
        "S1": _LabelTally(seconds=0.0, segments=2),
        "S2": _LabelTally(seconds=0.0, segments=9),
    }
    assert _dominant_label(result, tally) == "S2"


def test_a_single_speaker_needs_no_ranking() -> None:
    """One voice in the capture is the ordinary rehearsal case, and taking it
    is not an assumption about numbering."""
    assert _dominant_label(_speakers_result("S1"), {}) == "S1"
    assert _dominant_label(_speakers_result("S7"), {}) == "S7"


def test_the_unattributed_label_is_never_enrolled() -> None:
    """`UU` is the server's "unattributed", not a person."""
    assert _dominant_label(_speakers_result("UU"), {}) is None
    result = _speakers_result("UU", "S2")
    assert _dominant_label(result, {"S2": _LabelTally(seconds=5.0, segments=3)}) == "S2"


def test_several_speakers_with_no_attribution_refuses_to_guess() -> None:
    """Nothing here distinguishes the moderator from the room, and guessing is
    the one thing this must not do — a wrong guess enrols the audience."""
    assert _dominant_label(_speakers_result("S1", "S2"), {}) is None


def test_an_empty_result_yields_nobody() -> None:
    assert _dominant_label({"message": "SpeakersResult", "speakers": []}, {}) is None
    assert _dominant_label({"message": "SpeakersResult"}, {}) is None


# --------------------------------------------------------------- the machine


class _FakeWebSocket:
    """One connection's worth of server messages, as an async context manager."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = [json.dumps(m) for m in messages]
        self.sent: list[str] = []

    async def send(self, data: str | bytes) -> None:
        if isinstance(data, str):
            self.sent.append(data)

    async def recv(self) -> str:
        return self._messages.pop(0)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> str:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _install(monkeypatch: pytest.MonkeyPatch, *connections: _FakeWebSocket) -> None:
    """Monkeypatch `websockets.connect` to hand out these, in order.

    Several connections because enrolment is deliberately two sessions: the
    capture pass and the verification pass cannot share one, since the second
    has to be *configured* with what the first returned.
    """
    remaining = list(connections)

    def fake_connect(*args: object, **kwargs: object) -> _FakeWebSocket:
        del args, kwargs
        return remaining.pop(0)

    monkeypatch.setattr("panel_runtime.enrolment.websockets.connect", fake_connect)


def _segment_message(text: str, *, speaker: str, start: float, end: float) -> dict:
    return {
        "message": "AddSegment",
        "segment": {
            "transcript": text,
            "speaker": speaker,
            "start_time": start,
            "end_time": end,
        },
    }


def _enrolment(**overrides: Any) -> SpeakerEnrolment:
    params: dict[str, Any] = {
        "config": STTConfig(model=MODEL, chunk_size=64),
        "api_key": "test-key",
        "capture_limit_s": 0.5,
        "verify_limit_s": 0.5,
        "result_timeout_s": 0.5,
    }
    params.update(overrides)
    return SpeakerEnrolment(**params)


def test_the_real_caps_are_the_documented_ones() -> None:
    """The small values the tests below drive are not the shipped ones."""
    assert CAPTURE_LIMIT_S == 30.0, "the capture cap James is told about"
    assert VERIFY_LIMIT_S == 60.0
    assert REQUIRED_VERIFIED_SEGMENTS == 3
    default = SpeakerEnrolment(config=STTConfig(), api_key="k")
    assert default._capture_limit_s == CAPTURE_LIMIT_S
    assert default._verify_limit_s == VERIFY_LIMIT_S
    assert default._required_segments == REQUIRED_VERIFIED_SEGMENTS


def test_capture_asks_for_identifiers_and_returns_the_dominant_speakers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capture phase end to end, over a faked socket.

    `S1` speaks for under a second and `S2` for twenty-three, so the
    identifiers returned must be `S2`'s.
    """
    socket = _FakeWebSocket(
        [
            {"message": "RecognitionStarted"},
            _segment_message("hm", speaker="S1", start=0.0, end=0.6),
            _segment_message("right, so", speaker="S2", start=1.0, end=12.0),
            _segment_message("and the other thing", speaker="S2", start=12.0, end=24.0),
            _speakers_result("S1", "S2"),
            {"message": "EndOfTranscript"},
        ]
    )
    _install(monkeypatch, socket)
    phases: list[tuple[str, dict]] = []
    enrolment = _enrolment(on_progress=lambda p, d: phases.append((p, d)))

    identifiers = asyncio.run(enrolment.capture())

    assert identifiers == ("id-for-S2",)

    start = json.loads(socket.sent[0])
    config = start["transcription_config"]
    assert config["diarization"] == "speaker"
    assert config["speaker_diarization_config"]["get_speakers"] is True
    assert "speakers" not in config["speaker_diarization_config"], (
        "the capture pass identifies nobody — it is asking who is there"
    )
    assert "additional_vocab" not in config, (
        "persona pronunciation is irrelevant to identifying a voice, and "
        "dropping the key removes the one rejection this endpoint is known "
        "to have produced from the phase that gates the show starting"
    )
    # The documented request form, sent before EndOfStream.
    assert any(json.loads(m).get("message") == "GetSpeakers" for m in socket.sent[1:])
    assert any(json.loads(m).get("message") == "EndOfStream" for m in socket.sent[1:])

    assert [p for p, _ in phases if p == "capture_done"], phases
    done = next(d for p, d in phases if p == "capture_done")
    assert done["source_label"] == "S2"
    assert done["speakers_seen"] == 2


def test_capture_with_no_speakers_result_yields_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capture the server never answered is a failed capture, not an
    identifier-shaped guess."""
    _install(
        monkeypatch,
        _FakeWebSocket([{"message": "RecognitionStarted"}, {"message": "EndOfTranscript"}]),
    )
    phases: list[tuple[str, dict]] = []
    enrolment = _enrolment(on_progress=lambda p, d: phases.append((p, d)))

    assert asyncio.run(enrolment.capture()) == ()
    assert any(p == "capture_failed" for p, _ in phases)


def test_capture_stops_at_its_cap_even_with_a_mic_that_never_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hard cap. Nobody says anything usable and audio keeps arriving; the
    phase must still end, because the show is waiting behind it.

    Driven at 0.2s rather than the shipped 30s — the behaviour under test is
    that the deadline is enforced at all, not its value, which
    `test_the_real_caps_are_the_documented_ones` pins separately.
    """
    _install(
        monkeypatch,
        _FakeWebSocket([{"message": "RecognitionStarted"}]),
    )
    enrolment = _enrolment(capture_limit_s=0.2)

    async def body() -> tuple[tuple[str, ...], float]:
        loop = asyncio.get_running_loop()
        feeding = True

        async def feed_forever() -> None:
            while feeding:
                enrolment.feed(b"\x00" * 64)
                await asyncio.sleep(0.001)

        pump = asyncio.create_task(feed_forever())
        started = loop.time()
        try:
            identifiers = await enrolment.capture()
        finally:
            feeding = False
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
        return identifiers, loop.time() - started

    identifiers, elapsed = asyncio.run(body())

    assert identifiers == ()
    assert elapsed < 5.0, f"the cap did not end the phase ({elapsed:.1f}s)"


@pytest.mark.parametrize("matched", [0, 1, 2])
def test_verification_fails_below_three_matched_segments(
    monkeypatch: pytest.MonkeyPatch, matched: int
) -> None:
    """Three, not one and not two.

    There is no embedding to compare and no similarity score anywhere in this
    API, so "the same speaker three times" is exactly this: hand the
    identifiers back and count how often the server agrees. One match can be
    luck on a short segment, and the point of the phase is to find that out on
    a laptop rather than on stage.
    """
    messages: list[dict] = [{"message": "RecognitionStarted"}]
    messages += [
        _segment_message(f"sentence {i}", speaker="James", start=float(i), end=float(i) + 1)
        for i in range(matched)
    ]
    messages.append({"message": "EndOfTranscript"})
    _install(monkeypatch, _FakeWebSocket(messages))
    phases: list[tuple[str, dict]] = []
    enrolment = _enrolment(on_progress=lambda p, d: phases.append((p, d)))

    assert asyncio.run(enrolment.verify(("id-for-S2",))) is False
    failure = next(d for p, d in phases if p == "verify_failed")
    assert failure["matched"] == matched


def test_verification_succeeds_at_three_matched_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = _FakeWebSocket(
        [
            {"message": "RecognitionStarted"},
            _segment_message("one", speaker="James", start=0.0, end=1.0),
            _segment_message("two", speaker="James", start=1.0, end=2.0),
            _segment_message("three", speaker="James", start=2.0, end=3.0),
            {"message": "EndOfTranscript"},
        ]
    )
    _install(monkeypatch, socket)
    enrolment = _enrolment()

    assert asyncio.run(enrolment.verify(("id-for-S2",))) is True

    # The verification session is the one that *uses* the identifiers, which
    # is the only way this API can be asked whether a voice matches.
    config = json.loads(socket.sent[0])["transcription_config"]
    assert config["diarization"] == "speaker"
    assert config["speaker_diarization_config"]["speakers"] == [
        {"label": "James", "speaker_identifiers": ["id-for-S2"]}
    ]
    assert "get_speakers" not in config["speaker_diarization_config"]


def test_segments_attributed_to_someone_else_do_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three voices in the room are not three confirmations of one voice."""
    _install(
        monkeypatch,
        _FakeWebSocket(
            [
                {"message": "RecognitionStarted"},
                _segment_message("a", speaker="James", start=0.0, end=1.0),
                _segment_message("b", speaker="S2", start=1.0, end=2.0),
                _segment_message("c", speaker="S3", start=2.0, end=3.0),
                {"message": "EndOfTranscript"},
            ]
        ),
    )
    assert asyncio.run(_enrolment().verify(("id",))) is False


def test_partials_do_not_count_towards_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalised segments only.

    A partial can be revised, and whether `segment.speaker` is populated on
    `AddPartialSegment` at all is undocumented for this endpoint — counting
    one would be counting something that may not be there and may not survive.
    """
    partial = {
        "message": "AddPartialSegment",
        "segment": {"transcript": "hello", "speaker": "James"},
    }
    _install(
        monkeypatch,
        _FakeWebSocket(
            [{"message": "RecognitionStarted"}, partial, partial, partial,
             {"message": "EndOfTranscript"}]
        ),
    )
    assert asyncio.run(_enrolment().verify(("id",))) is False


def test_a_server_warning_fails_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `Warning` on the session using the identifiers is the documented
    signal that they are not valid for this model. Failing here means
    re-enrolling now rather than finding out on stage."""
    _install(
        monkeypatch,
        _FakeWebSocket(
            [
                {"message": "RecognitionStarted"},
                _segment_message("one", speaker="James", start=0.0, end=1.0),
                _segment_message("two", speaker="James", start=1.0, end=2.0),
                _segment_message("three", speaker="James", start=2.0, end=3.0),
                {"message": "Warning", "reason": "speaker identifiers are for another model"},
                {"message": "EndOfTranscript"},
            ]
        ),
    )
    phases: list[tuple[str, dict]] = []
    enrolment = _enrolment(on_progress=lambda p, d: phases.append((p, d)))

    assert asyncio.run(enrolment.verify(("id",))) is False
    assert any(d.get("reason") == "server warning" for p, d in phases if p == "verify_failed")


def test_a_rejected_start_recognition_is_a_failed_phase_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed enrolment is a result the caller decides about, not an
    exception that unwinds the show. `panel.py` runs ungated rather than
    refusing to start."""
    _install(monkeypatch, _FakeWebSocket([{"message": "Error", "reason": "bad token"}]))
    phases: list[tuple[str, dict]] = []
    enrolment = _enrolment(on_progress=lambda p, d: phases.append((p, d)))

    assert asyncio.run(enrolment.capture()) == ()
    assert any(p == "session_failed" for p, _ in phases)


def test_run_does_capture_then_verify_and_records_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both phases, in order, over two separate sockets — which is the shape
    the API forces: there is nothing to compare identifiers with, so the only
    way to check them is a second session configured to use them."""
    capture = _FakeWebSocket(
        [
            {"message": "RecognitionStarted"},
            _segment_message("talking", speaker="S2", start=0.0, end=20.0),
            _speakers_result("S2"),
            {"message": "EndOfTranscript"},
        ]
    )
    verify = _FakeWebSocket(
        [
            {"message": "RecognitionStarted"},
            _segment_message("one", speaker="James", start=0.0, end=1.0),
            _segment_message("two", speaker="James", start=1.0, end=2.0),
            _segment_message("three", speaker="James", start=2.0, end=3.0),
            {"message": "EndOfTranscript"},
        ]
    )
    _install(monkeypatch, capture, verify)
    phases: list[str] = []
    enrolment = _enrolment(on_progress=lambda p, _d: phases.append(p))

    speaker = asyncio.run(enrolment.run())

    assert speaker is not None
    assert speaker.label == "James"
    assert speaker.speaker_identifiers == ("id-for-S2",)
    assert speaker.model == MODEL, "the model is recorded so a later run can reject it"
    assert speaker.enrolled_at, "stamped for the operator's benefit"
    assert phases.index("capture_done") < phases.index("verify_started")
    assert "enrolled" in phases


def test_run_stops_after_a_failed_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """No identifiers means nothing to verify — the second socket is never
    opened."""
    _install(
        monkeypatch,
        _FakeWebSocket([{"message": "RecognitionStarted"}, {"message": "EndOfTranscript"}]),
    )
    phases: list[str] = []
    enrolment = _enrolment(on_progress=lambda p, _d: phases.append(p))

    assert asyncio.run(enrolment.run()) is None
    assert "verify_started" not in phases


def test_feed_between_phases_is_a_no_op() -> None:
    """The audio callback holds one reference for the whole enrolment and must
    not have to know which session is up — or whether any is."""
    enrolment = _enrolment()
    enrolment.feed(b"\x00" * 64)  # must not raise
    assert enrolment._source is None
