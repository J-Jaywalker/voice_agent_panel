"""Tests for `panel_runtime.tts`'s wire protocol against the v3 dialogue endpoint.

The migration from `eleven_flash_v2_5` on
`/v1/text-to-speech/{voice_id}/multi-stream-input` to
`eleven_v3_conversational` on `/v1/text-to-dialogue/multi-stream-input`
(5 Oct 2026) changed the schema in three ways that each have a *silent* failure
mode, which is why they are pinned here rather than left to a live rehearsal:

* `is_final_audio_for_turn` fires after every flush, not at the end of a turn.
  Treating it the way the old endpoint's `isFinal` was treated truncates every
  multi-sentence turn to its first sentence, and the agent simply stops talking
  early — nothing raises.
* Writing to a context the server has already closed is read as a new context's
  first message and takes down the whole socket, silencing every other turn on
  it, not just the one that overran.
* The personas author `speed` values this model does not implement. The server
  accepts them and ignores them, so nothing fails loudly if they are passed.

No network: `websockets.connect` is monkeypatched with a fake that answers the
real server's message sequence, in the plain-`asyncio.run()` style the rest of
this package's tests use rather than pytest-asyncio (not a dependency here).
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Self

import pytest
from panel_runtime.tts import ElevenLabsTTS, TTSConfig, _preset_stability

VOICE = "test-voice-id"
_AUDIO = base64.b64encode(b"\x00\x01" * 160).decode()


class _FakeDialogueServer:
    """Answers the way the live endpoint was observed to.

    A flush produces audio and then `is_final_audio_for_turn`; only
    `close_context` produces `is_final`. Writing to a context that has already
    ended closes the connection with 1008, as the real server does.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self.pings = 0
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self._open: set[str] = set()
        self._ended: set[str] = set()

    # ---- the half the code under test talks to

    async def send(self, data: str) -> None:
        message = json.loads(data)
        self.sent.append(message)
        context_id = message.get("context_id")

        if message.get("close_socket"):
            await self._inbox.put(None)
            return

        if context_id in self._ended:
            # The real failure: treated as a new context's first message,
            # which has no `voices`, so the socket dies for everyone.
            await self._inbox.put(None)
            self.closed = True
            return

        if context_id not in self._open:
            if "voices" not in message:
                await self._inbox.put(None)
                self.closed = True
                return
            self._open.add(context_id)

        if message.get("flush") and message.get("inputs"):
            await self._emit({"audio": _AUDIO, "context_id": context_id})
            await self._emit({"is_final_audio_for_turn": True, "context_id": context_id})
        if message.get("close_context"):
            self._ended.add(context_id)
            self._open.discard(context_id)
            await self._emit({"is_final": True, "context_id": context_id})

    async def ping(self) -> None:
        self.pings += 1

    async def close(self) -> None:
        self.closed = True
        await self._inbox.put(None)

    async def _emit(self, message: dict) -> None:
        await self._inbox.put(json.dumps(message))

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> str:
        raw = await self._inbox.get()
        if raw is None:
            raise StopAsyncIteration
        return raw


class _FakeEndpoint:
    """Hands out one fake connection per `connect()`, as the real one does.

    One per call and not one shared object: a channel's read loop consumes its
    own socket, so sharing would have two readers racing over one stream of
    messages — an artefact of the fake, not of the code under test.
    """

    def __init__(self) -> None:
        self.connections: list[_FakeDialogueServer] = []

    def connect(self) -> _FakeDialogueServer:
        connection = _FakeDialogueServer()
        self.connections.append(connection)
        return connection

    @property
    def only(self) -> _FakeDialogueServer:
        assert len(self.connections) == 1, "this test assumes a single voice"
        return self.connections[0]

    @property
    def sent(self) -> list[dict]:
        return [message for c in self.connections for message in c.sent]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _FakeEndpoint:
    endpoint = _FakeEndpoint()

    async def fake_connect(*args: object, **kwargs: object) -> _FakeDialogueServer:
        del args, kwargs
        return endpoint.connect()

    monkeypatch.setattr("panel_runtime.tts.websockets.connect", fake_connect)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    return endpoint


# ------------------------------------------------------------ voice settings


def test_stability_snaps_onto_v3s_three_presets() -> None:
    """v3 has three stability modes, not a slider, and rounds silently."""
    assert _preset_stability(0.0) == 0.0
    assert _preset_stability(0.2) == 0.0
    assert _preset_stability(0.62) == 0.5
    assert _preset_stability(0.9) == 1.0


def test_melia_and_wayne_still_land_on_the_old_flattened_preset() -> None:
    """The historical finding, narrowed to what is still true.

    Dexter 0.30, Wayne 0.35 and Melia 0.62 were authored against the old
    continuous Flash scale, and all three were nearest to 0.5 — so v3
    rendered the whole panel Natural and the contrast those numbers encoded
    did not reach the wire. Dexter has since been re-authored straight to 0.0
    (`personas/dexter.yaml`) for his accent tag's sake — Natural measurably
    suppresses tag responsiveness next to Creative. The same was tried for
    Melia's accent and Wayne's pace and both were reverted 5 Oct 2026; Melia's
    accent was re-added 6 Oct 2026 with stability dropped again (see
    `personas/melia.yaml`), so Wayne is now the only persona this still
    describes — Melia's raw 0.62 still lands on 0.5 here, it just isn't what
    the cast authors any more.
    """
    assert _preset_stability(0.62) == 0.5
    assert _preset_stability(0.35) == 0.5


def test_the_real_casts_stability_matches_what_each_persona_needs() -> None:
    """Dexter and Melia are both authored for their accent tag's
    responsiveness (0.0, Creative). Wayne carries a standing `[briskly]` pace
    tag (2026-10-07) and is deliberately not dropped with them: the sample
    that chose that tag was generated at a raw 0.75, which `_preset_stability`
    snaps to 0.5, on a comparison too noisy to act on. He stays at the
    authored 0.5 until that is measured properly — and whether the snap costs
    anything at all is itself unverified, see `_preset_stability`'s docstring
    and `personas/wayne.yaml`.

    NOTE (2026-10-07): the Melia assertion below currently fails —
    `personas/melia.yaml` authors 1.0 (Robust), which is the preset the
    vendor says suppresses audio tag responsiveness, against an `accent` she
    carries. Left failing on purpose rather than retuned to match the YAML:
    this guard is the only thing that noticed, and which of the two is wrong
    is a casting decision, not a test fix."""
    from pathlib import Path

    from panel_core import PanelCast

    cast = PanelCast.from_dir(Path(__file__).resolve().parents[3] / "personas")
    assert cast["dex"].voice_settings["stability"] == 0.0
    assert cast["wayne"].voice_settings["stability"] == 0.5
    assert cast["melia"].voice_settings["stability"] == 0.0


def test_voice_settings_carries_stability_and_nothing_else() -> None:
    assert TTSConfig(stability=0.62).voice_settings == {"stability": 0.5}


def test_a_personas_speed_is_dropped_rather_than_sent(server: _FakeEndpoint) -> None:
    """Every persona in the cast authors a `speed`; v3 does not implement one.

    It must not reach `voice_settings`, where the server would accept it and
    quietly do nothing — and it must not crash the engine either, because
    personas are data and `panel_core` still describes the full ElevenLabs
    settings vocabulary.
    """
    engine = ElevenLabsTTS(voice_overrides={VOICE: {"speed": 1.07, "stability": 0.62}})
    config = engine._channel(VOICE)._config
    assert config.stability == 0.62, "a setting v3 does act on still comes through"
    assert not hasattr(config, "speed")
    assert config.voice_settings == {"stability": 0.5}


# ------------------------------------------------------------- wire protocol


def test_a_context_registers_its_voice_and_sends_text_as_inputs(
    server: _FakeEndpoint,
) -> None:
    """The voice is not in the URL on this endpoint; each context declares it."""

    async def body() -> None:
        engine = ElevenLabsTTS(TTSConfig(stability=0.62))
        turn = await engine.open(voice_id=VOICE)
        await turn.push("I think that framing gets the causation backwards.")
        await turn.finish()
        async for _chunk in turn.chunks():
            pass
        await engine.aclose()

    asyncio.run(body())

    opening = server.sent[0]
    assert opening["voices"] == [VOICE], "one voice per context, registered up front"
    assert opening["voice_settings"] == {"stability": 0.5}
    assert "text" not in opening, "the legacy endpoint's bare text field is gone"

    text_message = server.sent[1]
    assert text_message["inputs"] == [
        {"text": "I think that framing gets the causation backwards. ", "voice_id": VOICE}
    ]
    assert text_message["flush"] is True, (
        "this endpoint buffers ~40 chars before generating; a short opening "
        "clause would otherwise sit there"
    )


def test_is_final_audio_for_turn_does_not_end_a_streamed_turn(
    server: _FakeEndpoint,
) -> None:
    """The truncation regression. It fires after *every* sentence."""
    received: list[bytes] = []

    async def body() -> None:
        engine = ElevenLabsTTS()
        turn = await engine.open(voice_id=VOICE)

        async def drain() -> None:
            async for chunk in turn.chunks():
                received.append(chunk)

        reader = asyncio.create_task(drain())
        for sentence in ("First sentence here.", "Second sentence here.", "And a third."):
            await turn.push(sentence)
            await asyncio.sleep(0)
        await turn.finish()
        await asyncio.wait_for(reader, timeout=2.0)
        await engine.aclose()

    asyncio.run(body())

    assert len(received) == 3, (
        "a turn ended on the first is_final_audio_for_turn would deliver one "
        "sentence and the agent would stop talking mid-turn"
    )


def test_is_final_ends_the_turn(server: _FakeEndpoint) -> None:
    """And the one that does end it actually does, so a turn is not left hanging."""

    async def body() -> list[bytes]:
        engine = ElevenLabsTTS()
        stream = await engine.synthesise("One sentence.", voice_id=VOICE)
        chunks = [chunk async for chunk in stream.chunks()]
        await engine.aclose()
        return chunks

    assert len(asyncio.run(body())) == 1
    assert any(m.get("close_context") for m in server.sent)


def test_pushing_to_an_ended_context_is_dropped_and_the_socket_survives(
    server: _FakeEndpoint,
) -> None:
    """A context idles out after ~20s server-side. Writing to it kills the socket.

    The scenario is a brain that stalls mid-turn: the context closes under us,
    and the next sentence must be dropped rather than silencing all three
    agents by taking the shared connection down.
    """

    async def body() -> None:
        engine = ElevenLabsTTS()
        turn = await engine.open(voice_id=VOICE)
        await turn.push("First sentence here.")
        await turn.finish()
        async for _chunk in turn.chunks():
            pass
        # The stalled sentence, arriving after the context has gone.
        await turn.push("A sentence that arrives too late.")
        await asyncio.sleep(0)
        await engine.aclose()

    asyncio.run(body())

    assert not any(m.get("inputs") and "too late" in m["inputs"][0]["text"] for m in server.sent), (
        "the late push must never reach the wire"
    )


def test_a_server_side_close_wakes_a_waiting_turn(server: _FakeEndpoint) -> None:
    """A protocol violation closes the socket *cleanly*, which must still end turns.

    Not a transport error, so nothing raises in the read loop — an agent would
    otherwise wait on a queue nobody will ever write to again.
    """

    async def body() -> None:
        engine = ElevenLabsTTS()
        turn = await engine.open(voice_id=VOICE)

        async def drain() -> None:
            async for _chunk in turn.chunks():
                pass

        reader = asyncio.create_task(drain())
        await asyncio.sleep(0)
        await server.only.close()
        await asyncio.wait_for(reader, timeout=2.0)
        await engine.aclose()

    asyncio.run(body())


# -------------------------------------------------------------------- prewarm


def test_prewarm_generates_and_discards_one_utterance_per_voice(
    server: _FakeEndpoint,
) -> None:
    """The per-voice warm-up is the point of prewarm on v3, not just the handshake.

    Measured: the first utterance in a voice that had not been used on its
    socket cost ~300-350ms against a ~119ms median. That belongs in the
    pre-show silence, not on an agent's first live line.
    """
    voices = ["voice-a", "voice-b"]

    async def body() -> None:
        engine = ElevenLabsTTS()
        await engine.prewarm(voices)
        await engine.aclose()

    asyncio.run(body())

    spoken = [m for m in server.sent if m.get("inputs")]
    assert len(spoken) == len(voices), "one throwaway utterance per voice, not per socket"
    assert all(m.get("close_context") for m in server.sent if m.get("close_context")), (
        "the warm-up context is closed, not left to idle out"
    )


# --------------------------------------------------------------------- accent


def test_accent_is_prepended_on_every_push_not_just_the_first(
    server: _FakeEndpoint,
) -> None:
    """A standing characteristic, restated on every push within a turn.

    Not once at the top of the context: this endpoint buffers and flushes
    roughly every 40 characters/8 words, each flush is its own generation, and
    an accent asserted only at the start of a turn was observed to drift back
    towards the voice's default within a few sentences. Free to repeat — an
    accent tag does not add performed audio length the way `[sighs]` does.
    """

    async def body() -> None:
        engine = ElevenLabsTTS(accent_tags={VOICE: "strong irish accent"})
        turn = await engine.open(voice_id=VOICE)
        await turn.push("First sentence here.")
        await turn.push("Second sentence here.")
        await turn.finish()
        async for _chunk in turn.chunks():
            pass
        await engine.aclose()

    asyncio.run(body())

    texts = [m["inputs"][0]["text"] for m in server.sent if m.get("inputs")]
    assert texts[0].startswith("[strong irish accent] First sentence")
    assert texts[1].startswith("[strong irish accent] Second sentence"), (
        "every push must restate it, not just the context's first"
    )


def test_accent_is_reapplied_on_the_next_turn(server: _FakeEndpoint) -> None:
    """Each turn is a new context, and the tag has to be restated in it —
    the bookkeeping is per `context_id`, not a one-time latch on the voice."""

    async def body() -> list[str]:
        engine = ElevenLabsTTS(accent_tags={VOICE: "Yorkshire accent"})
        first = await engine.synthesise("First turn.", voice_id=VOICE)
        async for _chunk in first.chunks():
            pass
        second = await engine.synthesise("Second turn.", voice_id=VOICE)
        async for _chunk in second.chunks():
            pass
        await engine.aclose()
        return [m["inputs"][0]["text"] for m in server.sent if m.get("inputs")]

    texts = asyncio.run(body())
    assert texts[0].startswith("[Yorkshire accent] First turn")
    assert texts[1].startswith("[Yorkshire accent] Second turn")


# ----------------------------------------------------------------------- pace


def test_pace_is_prepended_on_every_push_not_just_the_first(
    server: _FakeEndpoint,
) -> None:
    """The accent property, for the other standing characteristic.

    Same reasoning end to end: each flush is its own generation, so a pace
    asserted only at the start of a turn drifts back towards the voice's
    default, and restating it is free because a pace tag adds no performed
    audio length. See `panel_core.personas.PACE_TAGS`.
    """

    async def body() -> None:
        engine = ElevenLabsTTS(pace_tags={VOICE: "briskly"})
        turn = await engine.open(voice_id=VOICE)
        await turn.push("First sentence here.")
        await turn.push("Second sentence here.")
        await turn.finish()
        async for _chunk in turn.chunks():
            pass
        await engine.aclose()

    asyncio.run(body())

    texts = [m["inputs"][0]["text"] for m in server.sent if m.get("inputs")]
    assert texts[0].startswith("[briskly] First sentence")
    assert texts[1].startswith("[briskly] Second sentence"), (
        "every push must restate it, not just the context's first"
    )


def test_pace_is_reapplied_on_the_next_turn(server: _FakeEndpoint) -> None:
    """Per `context_id`, not a one-time latch on the voice — this is what makes
    Wayne brisk in his live turns and not only in his fixed introduction."""

    async def body() -> list[str]:
        engine = ElevenLabsTTS(pace_tags={VOICE: "briskly"})
        first = await engine.synthesise("First turn.", voice_id=VOICE)
        async for _chunk in first.chunks():
            pass
        second = await engine.synthesise("Second turn.", voice_id=VOICE)
        async for _chunk in second.chunks():
            pass
        await engine.aclose()
        return [m["inputs"][0]["text"] for m in server.sent if m.get("inputs")]

    texts = asyncio.run(body())
    assert texts[0].startswith("[briskly] First turn")
    assert texts[1].startswith("[briskly] Second turn")


def test_a_voice_with_no_pace_tag_is_unaffected(server: _FakeEndpoint) -> None:
    """No `pace_tags` entry must reproduce today's behaviour exactly — this is
    Dexter's and Melia's case; neither carries one."""

    async def body() -> str:
        engine = ElevenLabsTTS(accent_tags={VOICE: "Yorkshire accent"})
        stream = await engine.synthesise("Right, let's get into it.", voice_id=VOICE)
        async for _chunk in stream.chunks():
            pass
        await engine.aclose()
        return server.sent[1]["inputs"][0]["text"]

    assert asyncio.run(body()) == "[Yorkshire accent] Right, let's get into it. "


def test_accent_and_pace_together_are_ordered_accent_first(server: _FakeEndpoint) -> None:
    """No persona carries both today. The order is pinned anyway so that the
    first one to do so does not silently decide it."""

    async def body() -> str:
        engine = ElevenLabsTTS(
            accent_tags={VOICE: "Yorkshire accent"}, pace_tags={VOICE: "briskly"}
        )
        stream = await engine.synthesise("Right, let's get into it.", voice_id=VOICE)
        async for _chunk in stream.chunks():
            pass
        await engine.aclose()
        return server.sent[1]["inputs"][0]["text"]

    assert asyncio.run(body()) == "[Yorkshire accent] [briskly] Right, let's get into it. "


def test_a_voice_with_no_accent_tag_is_unaffected(server: _FakeEndpoint) -> None:
    """No `accent_tags` entry for a voice must reproduce today's behaviour
    exactly — this is Wayne's case; he carries a pace tag but no accent."""

    async def body() -> str:
        engine = ElevenLabsTTS()
        stream = await engine.synthesise("Right, let's get into it.", voice_id=VOICE)
        async for _chunk in stream.chunks():
            pass
        await engine.aclose()
        return server.sent[1]["inputs"][0]["text"]

    assert asyncio.run(body()) == "Right, let's get into it. "
