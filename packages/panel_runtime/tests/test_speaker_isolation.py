"""An unenrolled voice on Ricky's mic must never become words anywhere.

Ricky's microphone is on a stage. The audience is in the room and the PA bleeds
back into it, so audio arriving on that socket has never meant "Ricky said
this". After enrolment, `panel_runtime.stt` asks the question per segment: a
segment attributed to the enrolled moderator becomes a `TranscriptUpdated`
exactly as before, and a segment attributed to anyone else becomes
`UnverifiedSpeechDetected` — an event with no `text` field on it at all.

Two properties, and this file is the whole guard on both:

**A stranger's words never exist as text.** Not on the console, not on the
video wall, not in `PanelState.transcript`, not in the rehearsal log. The
mechanism is that the text is never written down: `_emit_transcript` does not
construct a `TranscriptUpdated` for such a segment, so there is nothing for any
of the four sinks that subscribe to those events to leak. Filtering further
upstream would have left the sentence sitting in an object several consumers
already receive, and "nobody renders it" is a much weaker claim than "it was
never written down".

**A stranger cannot make the panel answer them.** Dropping the text is not
enough on its own. `TurnYielded` carries no speaker, and arbitration does not
ask whose words opened the floor — so an audience question landing under a
standing invitation would have been answered by an agent, with nothing in the
transcript to show why. `_AgentSTTSession` therefore withholds `TurnYielded`
for any turn in which no segment was identified as Ricky.

Written against the real path, deliberately: raw Speechmatics messages go into
`_AgentSTTSession._receive`, and what comes out is pumped through the actual
`_run_stt` and `_drain_events` into the actual reducer, the actual
`PanelState`, and the actual log file. A refactor that reroutes any of that
must still fail here. The one thing faked is the socket — nothing in this file
touches the network.

The final test in this file is the teeth: the identical setup with Ricky's own
label on the segment must reach the reducer. Without it, a pump that silently
ran nothing would make every assertion above pass for the wrong reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, Self

import pytest
from panel_core import (
    HUMAN,
    AgentSpeechStarted,
    HumanSpeechEnded,
    HumanSpeechStarted,
    PanelCast,
    StopSpeech,
    Tick,
    TranscriptUpdated,
    TurnYielded,
    UnverifiedSpeechDetected,
)
from panel_runtime.panel import PanelRuntime
from panel_runtime.stt import PushAudioSource, STTConfig, _AgentSTTSession

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"

LABEL = "Ricky"
IDENTIFIERS = ("opaque-model-bound-identifier",)


@pytest.fixture
def cast() -> PanelCast:
    return PanelCast.from_dir(PERSONA_DIR)


@pytest.fixture(autouse=True)
def keys(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")


class FakeDisplay:
    """Stands in for `DisplayServer`. Records, answers nothing."""

    def __init__(self) -> None:
        self.events: list[object] = []
        self.commands: list[object] = []

    def on_event(self, event) -> None:
        self.events.append(event)

    def on_command(self, command) -> None:
        self.commands.append(command)

    def set_levels(self, levels) -> None:
        del levels


class _FakeWebSocket:
    """One `websockets.connect(...)` connection's worth of server messages."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = [json.dumps(m) for m in messages]
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        return self._messages.pop(0)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> str:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


def _segment(text: str, *, speaker: str | None, final: bool = True) -> dict[str, Any]:
    """One `AddSegment`/`AddPartialSegment` as the server sends it."""
    segment: dict[str, Any] = {"transcript": text, "start_time": 0.0, "end_time": 1.0}
    if speaker is not None:
        segment["speaker"] = speaker
    return {
        "message": "AddSegment" if final else "AddPartialSegment",
        "segment": segment,
    }


def _identified_runtime(
    cast: PanelCast, *, display: FakeDisplay | None = None, log_path: Path | None = None
) -> PanelRuntime:
    """A runtime whose human mic is configured exactly as the show configures it."""
    runtime = PanelRuntime(cast, use_tts=False, display=display, log_path=log_path)
    runtime.stt.identify(label=LABEL, speaker_identifiers=IDENTIFIERS)
    assert runtime.stt.config.identified_labels() == {LABEL}
    return runtime


async def _feed_server_messages(runtime: PanelRuntime, messages: list[dict]) -> None:
    """Run the real receive path over a faked socket, then the real event path.

    `_AgentSTTSession._receive` does the gating; `_run_stt` and `_drain_events`
    carry whatever survived it to the reducer. Both halves are the shipping
    code.
    """
    loop = asyncio.get_running_loop()
    source = PushAudioSource(loop)
    source.close()
    session = _AgentSTTSession(
        speaker=HUMAN,
        source=source,
        config=runtime.stt.config,
        api_key="test-key",
        events=runtime.stt.events,
        name="ricky",
    )

    tasks = [
        asyncio.create_task(runtime._drain_events(), name="events"),
        asyncio.create_task(runtime._run_stt(), name="stt"),
    ]
    try:
        await session._receive(_FakeWebSocket(messages))
        # Several passes: off the STT queue, through `_run_stt`, onto the
        # reducer's queue, and through `_drain_events`.
        for _ in range(50):
            await asyncio.sleep(0)
    finally:
        runtime._running = False
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _texts(events: list[object]) -> list[str]:
    return [e.text for e in events if isinstance(e, TranscriptUpdated)]


# ----------------------------------------------------------------- the wiring


def test_the_agents_own_sessions_are_never_diarized(cast: PanelCast):
    """The split that CLAUDE.md's "identity is a fact about the wiring" line
    still applies to. Configuring Ricky's mic must not reach the agents'
    display-only sessions, which have one known voice per socket."""
    runtime = _identified_runtime(cast, display=FakeDisplay())
    assert runtime.agent_stt is not None
    assert runtime.agent_stt.config.diarization == "none"
    assert runtime.agent_stt.config.speakers == ()
    assert runtime.agent_stt.config.identified_labels() == frozenset()
    # ...while Ricky's mic is gated.
    assert runtime.stt.config.diarization == "speaker"


def test_identification_is_off_until_it_is_asked_for(cast: PanelCast):
    """Nothing about the default changed. A runtime that never enrols behaves
    exactly as it did before this feature existed."""
    runtime = PanelRuntime(cast, use_tts=False)
    assert runtime.stt.config.diarization == "none"
    assert runtime.stt.config.identified_labels() == frozenset()
    assert "speaker_diarization_config" not in runtime.stt.config.to_transcription_config()


def test_the_identification_target_is_sent_on_start_recognition(cast: PanelCast):
    runtime = _identified_runtime(cast)
    config = runtime.stt.config.to_transcription_config()
    assert config["diarization"] == "speaker"
    assert config["speaker_diarization_config"]["speakers"] == [
        {"label": LABEL, "speaker_identifiers": list(IDENTIFIERS)}
    ]


def test_an_internal_server_label_is_refused(cast: PanelCast):
    """`S1`/`S2`/`UU` are the server's own labels and `StartRecognition`
    rejects them. Caught here, where the error names the cause, rather than as
    a handshake failure that retries a config which can never be accepted."""
    runtime = PanelRuntime(cast, use_tts=False)
    for label in ("S1", "s2", "UU"):
        with pytest.raises(ValueError, match="label format"):
            runtime.stt.identify(label=label, speaker_identifiers=IDENTIFIERS)


# --------------------------------------------------------- the words vanish


def test_a_stranger_s_words_never_become_text_anywhere(cast: PanelCast, tmp_path: Path):
    """The headline property, asserted at every sink at once.

    An audience question arrives, finalised and attributed to `S2`. It must
    produce no `TranscriptUpdated`, nothing in `PanelState.transcript`, no
    partial, nothing on the video wall, and no line in the rehearsal log
    carrying the words.
    """
    display = FakeDisplay()
    log_path = tmp_path / "rehearsal.jsonl"
    runtime = _identified_runtime(cast, display=display, log_path=log_path)
    stranger = "will this thing take my job then"

    asyncio.run(
        _feed_server_messages(
            runtime,
            [
                _segment(stranger, speaker="S2", final=False),
                _segment(stranger, speaker="S2", final=True),
            ],
        )
    )

    # No transcript event was ever constructed...
    assert _texts(display.events) == []
    assert not any(isinstance(e, TranscriptUpdated) for e in display.events)
    # ...so nothing reached conversation state...
    assert runtime.state.transcript == ()
    assert runtime.state.partial == ""
    # ...and the words appear nowhere in the log, which records whole events.
    log = log_path.read_text()
    assert stranger not in log
    for word in stranger.split():
        assert word not in log

    # What the floor *did* learn is the content-free event, and only that.
    assert any(isinstance(e, UnverifiedSpeechDetected) for e in display.events)
    unverified = [e for e in display.events if isinstance(e, UnverifiedSpeechDetected)]
    assert len(unverified) == 2, "one per segment, partial and final"
    assert not any(hasattr(e, "text") or hasattr(e, "speaker") for e in unverified), (
        "there must be no field on this event a stranger's words could ride in"
    )


def test_an_unattributed_segment_is_dropped_without_being_blamed_on_anyone(
    cast: PanelCast, tmp_path: Path
):
    """No `speaker` on the segment is "no evidence", not "not Ricky".

    Conflating the two would let a run of unattributed segments disarm Ricky's
    own interrupt, so an unattributed segment emits nothing at all: no
    transcript (its words are not known to be his) and no
    `UnverifiedSpeechDetected` (nor known not to be).
    """
    display = FakeDisplay()
    log_path = tmp_path / "rehearsal.jsonl"
    runtime = _identified_runtime(cast, display=display, log_path=log_path)

    asyncio.run(
        _feed_server_messages(runtime, [_segment("diarisation attributed nothing", speaker=None)])
    )

    assert _texts(display.events) == []
    assert not any(isinstance(e, UnverifiedSpeechDetected) for e in display.events)
    assert runtime.state.transcript == ()
    assert "attributed" not in log_path.read_text()


# ------------------------------------------------- the panel does not answer


def test_a_stranger_s_turn_never_opens_arbitration(cast: PanelCast):
    """The second half of the property, and the one the text-dropping misses.

    A whole audience turn — `StartOfTurn`, segments, `EndOfTurn` — must not
    produce a `TurnYielded`, because arbitration does not care whose words
    opened the floor and a standing invitation would have an agent answer
    them.
    """
    display = FakeDisplay()
    runtime = _identified_runtime(cast, display=display)

    asyncio.run(
        _feed_server_messages(
            runtime,
            [
                {"message": "StartOfTurn"},
                _segment("so what about jobs", speaker="S2", final=True),
                {"message": "EndOfTurn"},
            ],
        )
    )

    assert not any(isinstance(e, TurnYielded) for e in display.events), (
        "an unidentified turn must not reach the floor at all"
    )
    assert runtime.state.turn_id == 0
    assert runtime.state.floor_holder is None
    assert runtime.state.invitation is None


def test_a_stranger_cannot_stop_a_speaking_agent(cast: PanelCast):
    """End to end, through the real reducer: the audience never stops an agent.

    Endpointing fires on any voice and knows nothing about identity, but it
    moves nothing on the PA by itself (`panel_core.floor`); only a
    `TranscriptUpdated` stops an agent, and `stt.py` builds one only for a
    segment attributed to Ricky. So a segment attributed to someone else
    leaves the agent completely undisturbed.
    """
    display = FakeDisplay()
    runtime = _identified_runtime(cast, display=display)

    async def body():
        tasks = [asyncio.create_task(runtime._drain_events(), name="events")]
        loop = asyncio.get_running_loop()
        source = PushAudioSource(loop)
        source.close()
        session = _AgentSTTSession(
            speaker=HUMAN,
            source=source,
            config=runtime.stt.config,
            api_key="test-key",
            events=runtime.stt.events,
            name="ricky",
        )
        stt = asyncio.create_task(runtime._run_stt(), name="stt")
        tasks.append(stt)
        try:
            # An agent is on the PA, and the VAD hears a voice.
            runtime.emit(AgentSpeechStarted(t=1.0, agent="wayne"))
            runtime.emit(HumanSpeechStarted(t=2.0))
            for _ in range(20):
                await asyncio.sleep(0)
            # The transcript says it was not Ricky.
            await session._receive(
                _FakeWebSocket([_segment("is it going to replace us", speaker="S3")])
            )
            for _ in range(50):
                await asyncio.sleep(0)
            # Well past the duration threshold that would otherwise stop him.
            runtime.emit(Tick(t=3.0))
            for _ in range(50):
                await asyncio.sleep(0)
        finally:
            runtime._running = False
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    asyncio.run(body())

    assert not any(isinstance(c, StopSpeech) for c in display.commands), (
        "the audience must not be able to stop an agent"
    )
    assert runtime.state.speaking == "wayne", "the agent kept the floor"


# --------------------------------------------------------- the test's teeth


def test_ricky_own_words_still_travel_the_whole_path(cast: PanelCast, tmp_path: Path):
    """Proof the assertions above are not vacuous.

    Identical setup, identical pump, one field different — the segment carries
    Ricky's label. His words must reach the console path, the wall, the
    reducer, `PanelState.transcript` and the log, and his `EndOfTurn` must open
    the floor. If this ever fails, every test above is passing for the wrong
    reason.
    """
    display = FakeDisplay()
    log_path = tmp_path / "rehearsal.jsonl"
    runtime = _identified_runtime(cast, display=display, log_path=log_path)

    asyncio.run(
        _feed_server_messages(
            runtime,
            [
                {"message": "StartOfTurn"},
                _segment("Wayne, what holds it back?", speaker=LABEL, final=True),
                {"message": "EndOfTurn"},
            ],
        )
    )

    assert _texts(display.events) == ["Wayne, what holds it back?"]
    assert any(isinstance(e, TurnYielded) for e in display.events), (
        "Ricky's end of turn must still open arbitration"
    )
    assert [u.text for u in runtime.state.transcript] == ["Wayne, what holds it back?"]
    assert [u.speaker for u in runtime.state.transcript] == [HUMAN]
    assert "holds it back" in log_path.read_text()
    assert not any(isinstance(e, UnverifiedSpeechDetected) for e in display.events)


def test_ricky_words_in_a_turn_also_carrying_room_noise_still_yield(cast: PanelCast):
    """A turn is Ricky's if any segment in it was his.

    On a live stage his question and an audience murmur land in the same turn
    routinely. Requiring every segment to be his would have withheld the
    `TurnYielded` for a real question, which is the failure that leaves the
    panel silent — much the worse of the two errors available here.
    """
    display = FakeDisplay()
    runtime = _identified_runtime(cast, display=display)

    asyncio.run(
        _feed_server_messages(
            runtime,
            [
                {"message": "StartOfTurn"},
                _segment("murmuring", speaker="S4", final=True),
                _segment("Melia, is that fair?", speaker=LABEL, final=True),
                _segment("more murmuring", speaker="S4", final=True),
                {"message": "EndOfTurn"},
            ],
        )
    )

    assert _texts(display.events) == ["Melia, is that fair?"], "only his words"
    assert any(isinstance(e, TurnYielded) for e in display.events)


def test_the_next_turn_does_not_inherit_the_last_turn_s_confirmation(cast: PanelCast):
    """`StartOfTurn` resets the evidence, so a stranger's turn following one of
    Ricky's is still withheld."""
    display = FakeDisplay()
    runtime = _identified_runtime(cast, display=display)

    asyncio.run(
        _feed_server_messages(
            runtime,
            [
                {"message": "StartOfTurn"},
                _segment("Dexter, go on.", speaker=LABEL, final=True),
                {"message": "EndOfTurn"},
                {"message": "StartOfTurn"},
                _segment("can I ask something", speaker="S5", final=True),
                {"message": "EndOfTurn"},
            ],
        )
    )

    yielded = [e for e in display.events if isinstance(e, TurnYielded)]
    assert len(yielded) == 1, "Ricky's turn yielded; the stranger's did not"
    assert _texts(display.events) == ["Dexter, go on."]


def test_an_undiarized_session_is_byte_for_byte_unchanged(cast: PanelCast):
    """The agents' display-only sessions, and every session before enrolment
    existed: no identification configured means no gate, and a segment with no
    `speaker` on it transcribes exactly as it always has."""
    events: asyncio.Queue = asyncio.Queue()

    async def body() -> list:
        loop = asyncio.get_running_loop()
        source = PushAudioSource(loop)
        source.close()
        session = _AgentSTTSession(
            speaker="wayne",
            source=source,
            config=STTConfig.from_cast(cast),  # diarization="none"
            api_key="test-key",
            events=events,
            name="wayne",
        )
        await session._receive(
            _FakeWebSocket(
                [
                    _segment("Fleet scale is the thing.", speaker=None, final=True),
                    {"message": "EndOfTurn"},
                ]
            )
        )
        drained = []
        while not events.empty():
            drained.append(events.get_nowait())
        return drained

    drained = asyncio.run(body())

    assert _texts(drained) == ["Fleet scale is the thing."]
    assert any(isinstance(e, TurnYielded) for e in drained)
    assert not any(isinstance(e, UnverifiedSpeechDetected) for e in drained)


def test_a_short_utterance_from_ricky_still_stops_an_agent(cast: PanelCast):
    """The counterweight to the stranger test above, on the shortest input there is.

    "mm-hm" used to duck and resume; it now stops, like every other word of
    his over a live agent. What this file is actually guarding is unchanged
    either way — that routing Ricky's own words through the identity gate has
    not changed which of them reach the reducer — and a one-token partial is
    the hardest case for that to survive.

    Three of them, because the floor now wants a partial attribution confirmed
    `FloorConfig.interrupt_confirm_partials` times before it acts on it. That
    is a rule about how firmly a segment is his, not about whether it reached
    the reducer, and it is the latter this file exists to pin — so the test
    feeds enough partials to clear it and still asserts the stop.
    """
    display = FakeDisplay()
    runtime = _identified_runtime(cast, display=display)

    async def body():
        tasks = [
            asyncio.create_task(runtime._drain_events(), name="events"),
            asyncio.create_task(runtime._run_stt(), name="stt"),
        ]
        loop = asyncio.get_running_loop()
        source = PushAudioSource(loop)
        source.close()
        session = _AgentSTTSession(
            speaker=HUMAN,
            source=source,
            config=runtime.stt.config,
            api_key="test-key",
            events=runtime.stt.events,
            name="ricky",
        )
        try:
            runtime.emit(AgentSpeechStarted(t=1.0, agent="wayne"))
            runtime.emit(HumanSpeechStarted(t=2.0))
            for _ in range(20):
                await asyncio.sleep(0)
            await session._receive(
                _FakeWebSocket(
                    [_segment("mm-hm", speaker=LABEL, final=False)]
                    * runtime.fc.config.interrupt_confirm_partials
                )
            )
            for _ in range(50):
                await asyncio.sleep(0)
            runtime.emit(HumanSpeechEnded(t=2.2))
            for _ in range(50):
                await asyncio.sleep(0)
        finally:
            runtime._running = False
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    asyncio.run(body())

    # His "mm-hm" reached the reducer as his — which is what the gate had to
    # not break — and took the floor off the agent.
    assert any(isinstance(c, StopSpeech) for c in display.commands)
    assert runtime.state.speaking is None
    assert runtime.state.floor_holder == HUMAN
    assert [u.text for u in runtime.state.transcript] == [], "a partial is not the record"
