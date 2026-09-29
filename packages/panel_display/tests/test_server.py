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
    AgentSpeechEnded,
    AgentSpeechStarted,
    AgentUtteranceProgress,
    PanelCast,
)
from panel_display.server import LEVEL_HZ, SPEAKING_WPS, DisplayServer

PERSONAS = Path(__file__).resolve().parents[3] / "personas"
# Generous against a 30Hz pump: enough frames to have definitely been sent,
# short enough that a hung socket fails the suite rather than stalling it.
WINDOW_S = 6 / LEVEL_HZ

# Sentences for the transcript-pacing tests. Four words each, so at the fast
# rate below every one of them is worth exactly one reveal interval.
SENTENCE_A = "One two three four"
SENTENCE_B = "five six seven eight"
# Twenty times real speaking pace: 0.2s per four-word sentence, which is four
# flush ticks apart and so provably two separate snapshots, while keeping the
# whole test inside a fifth of a second. Same discipline as WINDOW_S — tuned
# off the server's own rates rather than guessed at.
FAST_WPS = SPEAKING_WPS * 20
REVEAL_S = len(SENTENCE_A.split()) / FAST_WPS


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


def test_silence_stops_sending_rather_than_streaming_zeroes(cast: PanelCast):
    async def body():
        server = await _serve(cast)
        try:
            return await _collect(f"http://127.0.0.1:{server.port}/ws", WINDOW_S)
        finally:
            await server.close()

    assert [m for m in asyncio.run(body()) if m["type"] == "levels"] == []


def test_a_turns_sentences_are_revealed_one_at_a_time(cast: PanelCast):
    """The whole point of the pacing: not "both arrive", but "not together".

    `AgentUtteranceProgress` is emitted when a sentence is handed to TTS, so
    a turn's sentences can all land inside one tick of the flush pump. If the
    server forwarded them straight through, the band would get a clump of
    text and then twenty seconds of nothing while the agent caught up. Two
    sentences pushed back to back must therefore be seen in two *different*
    snapshots, with a snapshot showing only the first in between.
    """

    def push(server: DisplayServer) -> None:
        server.on_event(AgentSpeechStarted(t=1.0, agent="dex"))
        for text in (SENTENCE_A, SENTENCE_B):
            server.on_event(AgentUtteranceProgress(t=1.0, agent="dex", text=text))

    async def body():
        server = await _serve(cast, words_per_second=FAST_WPS)
        try:
            return await _collect(
                f"http://127.0.0.1:{server.port}/ws",
                REVEAL_S * 3,
                after=lambda: push(server),
            )
        finally:
            await server.close()

    counts = [
        sum(1 for line in m["lines"] if line["speaker"] == "dex")
        for m in asyncio.run(body())
        if m["type"] == "state"
    ]
    # Not `counts == [0, 1, 2]`: the pump coalesces, so the connect snapshot
    # and repaints in between are free to repeat a count. What may never
    # happen is going straight from no sentences to both of them.
    assert 1 in counts, f"never saw the first sentence alone: {counts}"
    assert counts[-1] == 2, f"the second sentence never arrived: {counts}"
    assert counts.index(1) < counts.index(2)


def test_a_turn_that_ends_drops_what_it_had_not_said_yet(cast: PanelCast):
    """A cut-off turn must not keep trickling onto the band afterwards.

    `AgentSpeechEnded` fires on every real ending — completion, interruption,
    and the exception-recovery arm in `speak()`. Whichever it was, sentences
    the room will now never hear have no business appearing.
    """

    async def body():
        server = await _serve(cast, words_per_second=FAST_WPS)
        try:
            server.on_event(AgentSpeechStarted(t=1.0, agent="dex"))
            for text in (SENTENCE_A, SENTENCE_B):
                server.on_event(AgentUtteranceProgress(t=1.0, agent="dex", text=text))
            # Long enough for the first sentence to be revealed, short enough
            # that the second is still sitting in the queue behind it.
            await asyncio.sleep(REVEAL_S / 2)
            server.on_event(
                AgentSpeechEnded(t=2.0, agent="dex", completed=False, utterance="")
            )
            # Well past when the second sentence would have been due.
            await asyncio.sleep(REVEAL_S * 3)
            return [(line.speaker, line.text) for line in server.wall.lines]
        finally:
            await server.close()

    assert asyncio.run(body()) == [("dex", SENTENCE_A)]


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
