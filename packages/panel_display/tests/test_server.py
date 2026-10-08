"""The socket, end to end, against a real client.

`test_wall.py` covers what the wall *shows*; this covers whether a browser
actually receives it. Both halves matter and only one of them is pure: the
snapshot-on-connect behaviour is the thing a mid-show refresh depends on, and
it lives entirely in the server.

Async tests follow this repo's existing shape — `asyncio.run(body())` inside a
sync test, no pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
from panel_core import (
    HUMAN,
    AgentSpeechEnded,
    AgentSpeechStarted,
    PanelCast,
    TranscriptUpdated,
)
from panel_display import server as server_module
from panel_display.server import LEVEL_HZ, DisplayServer

PERSONAS = Path(__file__).resolve().parents[3] / "personas"
# Generous against a 30Hz pump: enough frames to have definitely been sent,
# short enough that a hung socket fails the suite rather than stalling it.
WINDOW_S = 6 / LEVEL_HZ

SENTENCE_A = "One two three four"
SENTENCE_B = "five six seven eight"


@pytest.fixture(scope="module")
def cast() -> PanelCast:
    return PanelCast.from_dir(PERSONAS)


async def _serve(cast: PanelCast, **kwargs) -> DisplayServer:
    """A server on an ephemeral port, so a wall left running never collides."""
    server = DisplayServer(cast, port=0, host="127.0.0.1", **kwargs)
    assert await server.start() is not None
    return server


async def _collect(url: str, seconds: float, before=None, after=None) -> list[dict]:
    """Connect, optionally poke the server, and return everything received."""
    messages: list[dict] = []
    async with aiohttp.ClientSession() as session, session.ws_connect(url) as socket:
        if before is not None:
            before()
        # The snapshot is pushed before anything else, so this first read is
        # the one that proves a refreshed browser repaints correctly.
        messages.append(json.loads((await socket.receive(timeout=2.0)).data))
        if after is not None:
            after()
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                message = await socket.receive(timeout=max(0.01, remaining))
            except TimeoutError:
                break
            if message.type is not aiohttp.WSMsgType.TEXT:
                break
            messages.append(json.loads(message.data))
    return messages


def test_a_new_client_is_painted_before_it_is_told_anything(cast: PanelCast):
    """The mid-show refresh. A delta-only client would come back blank."""

    async def body():
        server = await _serve(cast)
        try:
            # Something happened *before* this browser existed.
            server.on_event(AgentSpeechStarted(t=1.0, agent="wayne"))
            messages = await _collect(f"http://127.0.0.1:{server.port}/ws", 0.0)
        finally:
            await server.close()
        return messages

    first = asyncio.run(body())[0]
    assert first["type"] == "state"
    assert [a["id"] for a in first["agents"]] == list(cast.ids())
    speaking = next(a for a in first["agents"] if a["id"] == "wayne")
    assert speaking["state"] == "speaking"


def test_a_change_reaches_a_connected_client(cast: PanelCast):
    async def body():
        server = await _serve(cast)
        try:
            return await _collect(
                f"http://127.0.0.1:{server.port}/ws",
                WINDOW_S,
                after=lambda: server.on_event(AgentSpeechStarted(t=1.0, agent="dex")),
            )
        finally:
            await server.close()

    states = [m for m in asyncio.run(body()) if m["type"] == "state"]
    assert len(states) >= 2
    latest = next(a for a in states[-1]["agents"] if a["id"] == "dex")
    assert latest["state"] == "speaking"


def test_levels_are_a_separate_channel_from_state(cast: PanelCast):
    """Envelopes must not drag a full snapshot along 30 times a second."""

    async def body():
        server = await _serve(cast)
        try:
            return await _collect(
                f"http://127.0.0.1:{server.port}/ws",
                WINDOW_S,
                after=lambda: server.set_levels({"dex": 0.3, "melia": 0.0, "wayne": 0.0}),
            )
        finally:
            await server.close()

    messages = asyncio.run(body())
    levels = [m for m in messages if m["type"] == "levels"]
    assert levels, "no level frames arrived"
    assert levels[-1]["v"]["dex"] == 0.3
    # One state message (the snapshot on connect) and no repaints: nothing
    # about the wall changed, only how loud someone was.
    assert sum(1 for m in messages if m["type"] == "state") == 1


def test_a_spectrum_rides_the_level_frame(cast: PanelCast):
    """The orbs' coronas, on the same frame as their envelopes.

    One message rather than a third channel: they are two measurements of the
    same block of audio taken in the same pass of `PanelRuntime._pump_levels`,
    and splitting them would let a corona and its own loudness arrive a frame
    apart on a wall where both are drawn in the same `requestAnimationFrame`.
    """

    async def body():
        server = await _serve(cast)
        try:
            return await _collect(
                f"http://127.0.0.1:{server.port}/ws",
                WINDOW_S,
                after=lambda: server.set_levels(
                    {"dex": 0.3, "melia": 0.0, "wayne": 0.0},
                    {"dex": [0.1, 0.2, 0.05]},
                ),
            )
        finally:
            await server.close()

    levels = [m for m in asyncio.run(body()) if m["type"] == "levels"]
    assert levels, "no level frames arrived"
    assert levels[-1]["b"]["dex"] == [0.1, 0.2, 0.05]


def test_a_level_frame_without_a_spectrum_omits_it(cast: PanelCast):
    """Anything driving this server with no spectrum to give leaves it out.

    The key is absent rather than empty, and the client treats absent as "keep
    the corona you have" rather than "go flat" — so a caller that has nothing
    to say about frequency costs neither bytes nor a flicker.
    """

    async def body():
        server = await _serve(cast)
        try:
            return await _collect(
                f"http://127.0.0.1:{server.port}/ws",
                WINDOW_S,
                after=lambda: server.set_levels({"dex": 0.3, "melia": 0.0, "wayne": 0.0}),
            )
        finally:
            await server.close()

    levels = [m for m in asyncio.run(body()) if m["type"] == "levels"]
    assert levels, "no level frames arrived"
    assert all("b" not in m for m in levels)


def test_silence_stops_sending_rather_than_streaming_zeroes(cast: PanelCast):
    async def body():
        server = await _serve(cast)
        try:
            return await _collect(f"http://127.0.0.1:{server.port}/ws", WINDOW_S)
        finally:
            await server.close()

    assert [m for m in asyncio.run(body()) if m["type"] == "levels"] == []


def test_an_agents_transcript_reaches_the_client_tagged_to_that_agent(cast: PanelCast):
    """The band's agent lines, end to end over the socket.

    This is the event `PanelRuntime._run_agent_stt` hands to `on_event` and to
    nothing else, and it has to arrive at the browser attributed to the agent
    whose socket produced it — the client reads `speaker` to pick the name and
    the accent.
    """

    def push(server: DisplayServer) -> None:
        server.on_event(AgentSpeechStarted(t=1.0, agent="dex"))
        server.on_event(
            TranscriptUpdated(t=1.1, speaker="dex", text=SENTENCE_A, is_final=False)
        )
        server.on_event(
            TranscriptUpdated(t=1.4, speaker="dex", text=SENTENCE_A, is_final=True)
        )

    async def body():
        server = await _serve(cast)
        try:
            return await _collect(
                f"http://127.0.0.1:{server.port}/ws",
                WINDOW_S,
                after=lambda: push(server),
            )
        finally:
            await server.close()

    states = [m for m in asyncio.run(body()) if m["type"] == "state"]
    assert states[-1]["lines"] == [{"speaker": "dex", "text": SENTENCE_A}]
    assert states[-1]["partials"] == []


def test_transcripts_are_not_held_back_by_the_server(cast: PanelCast):
    """The pacing hack is gone, and its absence is the fix.

    The server used to queue agent sentences and pay them out at an assumed
    2.8 words/sec, because `AgentUtteranceProgress` arrives when a sentence is
    handed to TTS rather than when it is heard. Agent lines now arrive already
    paced by the audio they were transcribed from, so two finals landing back
    to back must both be on the wall immediately — anything that delays one of
    them is re-introducing an estimate on top of a measurement.
    """

    async def body():
        server = await _serve(cast)
        try:
            server.on_event(AgentSpeechStarted(t=1.0, agent="dex"))
            for text in (SENTENCE_A, SENTENCE_B):
                server.on_event(
                    TranscriptUpdated(t=1.0, speaker="dex", text=text, is_final=True)
                )
            # No sleep at all: `on_event` is synchronous through to `WallState`.
            return [(line.speaker, line.text) for line in server.wall.lines]
        finally:
            await server.close()

    assert asyncio.run(body()) == [("dex", SENTENCE_A), ("dex", SENTENCE_B)]


def test_a_partial_that_stops_arriving_leaves_the_band(
    cast: PanelCast, monkeypatch: pytest.MonkeyPatch
):
    """The undiarized case, which no event can close.

    `panel_runtime/stt.py` drops a segment silently when diarisation
    attributed nothing at all, so Ricky's in-progress line can simply stop
    being updated: no final, no empty update, no `UnverifiedSpeechDetected`.
    Staleness is the only signal left and it is a fact about arrival time, so
    the sweep lives here rather than in the pure half.

    The TTL is shortened rather than waited out; six real seconds in a unit
    test is the kind of thing that gets deleted later.
    """
    monkeypatch.setattr(server_module, "PARTIAL_TTL_S", 2 / LEVEL_HZ)

    async def body():
        server = await _serve(cast)
        try:
            server.on_event(
                TranscriptUpdated(t=1.0, speaker=HUMAN, text=SENTENCE_A, is_final=False)
            )
            assert server.wall.partials == {HUMAN: SENTENCE_A}
            await asyncio.sleep(WINDOW_S)
            return dict(server.wall.partials)
        finally:
            await server.close()

    assert asyncio.run(body()) == {}


def test_a_partial_still_arriving_is_left_alone(
    cast: PanelCast, monkeypatch: pytest.MonkeyPatch
):
    """The guard on the test above. A line that is still growing must survive
    the sweep however short the TTL is, or the band loses words mid-sentence —
    which is a worse display than a stale one, not a better."""
    monkeypatch.setattr(server_module, "PARTIAL_TTL_S", 2 / LEVEL_HZ)

    async def body():
        server = await _serve(cast)
        try:
            words = SENTENCE_A.split()
            for index in range(1, len(words) + 1):
                server.on_event(
                    TranscriptUpdated(
                        t=1.0, speaker=HUMAN, text=" ".join(words[:index]), is_final=False
                    )
                )
                await asyncio.sleep(WINDOW_S / len(words))
            return dict(server.wall.partials)
        finally:
            await server.close()

    assert asyncio.run(body()) == {HUMAN: SENTENCE_A}


def test_a_straggler_after_the_next_speaker_still_reaches_the_band(cast: PanelCast):
    """A cut-off turn's tail still lands, even under the agent that replaced it.

    `AgentSpeechEnded` fires on every real ending — completion, interruption,
    and the exception-recovery arm in `speak()` — but a transcription session
    finalises a few hundred milliseconds behind the audio either way, so
    "somebody else is on air now" is the routine case, not the exception. Each
    agent has its own dedicated session, so a late segment is still
    unambiguously that agent's own words and belongs on the band.
    """

    async def body():
        server = await _serve(cast)
        try:
            server.on_event(AgentSpeechStarted(t=1.0, agent="dex"))
            server.on_event(
                TranscriptUpdated(t=1.1, speaker="dex", text=SENTENCE_A, is_final=True)
            )
            server.on_event(
                AgentSpeechEnded(t=2.0, agent="dex", completed=False, utterance="")
            )
            server.on_event(AgentSpeechStarted(t=2.1, agent="melia"))
            server.on_event(
                TranscriptUpdated(t=2.2, speaker="dex", text=SENTENCE_B, is_final=True)
            )
            return [(line.speaker, line.text) for line in server.wall.lines]
        finally:
            await server.close()

    assert asyncio.run(body()) == [("dex", SENTENCE_A), ("dex", SENTENCE_B)]


def test_the_page_and_its_assets_are_served(cast: PanelCast):
    """No build step anywhere: the venue machine gets files and a browser."""

    async def body():
        server = await _serve(cast)
        base = f"http://127.0.0.1:{server.port}"
        try:
            async with aiohttp.ClientSession() as session:
                found = {}
                for path in (
                    "/",
                    "/static/css/tokens.css",
                    "/static/css/wall.css",
                    "/static/css/radix/jade-dark.css",
                    "/static/js/wall.js",
                    "/static/js/orb.js",
                    "/static/fonts/PPTelegraf-VariableUpright.woff2",
                ):
                    async with session.get(base + path) as response:
                        found[path] = response.status
                return found
        finally:
            await server.close()

    assert set(asyncio.run(body()).values()) == {200}


def test_a_port_already_in_use_does_not_raise(cast: PanelCast):
    """A wall that cannot bind is worth a warning, never a cancelled show."""

    async def body():
        first = await _serve(cast)
        second = DisplayServer(cast, port=first.port, host="127.0.0.1")
        try:
            return await second.start()
        finally:
            await second.close()
            await first.close()

    assert asyncio.run(body()) is None
