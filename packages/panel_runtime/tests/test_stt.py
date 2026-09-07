"""Tests for `panel_runtime.stt`: vocabulary derivation and the fail-safe
retry when a preview-endpoint `StartRecognition` rejects `additional_vocab`.

No network is involved: `websockets.connect` is monkeypatched with a fake
connection object, matching the plain-`asyncio.run()` style
`test_panel_runtime.py` uses rather than pytest-asyncio (not a project
dependency here).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Self

import pytest
from panel_core import PanelCast
from panel_runtime.stt import (
    PushAudioSource,
    STTConfig,
    _AgentSTTSession,
    vocab_from_cast,
)

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"


@pytest.fixture
def cast() -> PanelCast:
    return PanelCast.from_dir(PERSONA_DIR)


# --------------------------------------------------------- vocab derivation


def test_vocab_from_cast_has_one_entry_per_persona_with_sounds_like(cast: PanelCast) -> None:
    vocab = vocab_from_cast(cast)
    contents = {entry["content"] for entry in vocab}
    assert contents == {"Melia"}, "only melia.yaml declares `sounds_like` today"
    (entry,) = vocab
    assert entry["sounds_like"] == cast["melia"].sounds_like


def test_stt_config_from_cast_populates_additional_vocab(cast: PanelCast) -> None:
    config = STTConfig.from_cast(cast)
    assert config.additional_vocab == vocab_from_cast(cast)
    assert "additional_vocab" in config.to_transcription_config()


def test_from_cast_override_wins_over_derived_vocab(cast: PanelCast) -> None:
    """An explicit `additional_vocab` override is still respected — derived
    vocabulary is a default, not the only path in."""
    config = STTConfig.from_cast(cast, additional_vocab=())
    assert config.additional_vocab == ()


def test_to_transcription_config_omits_vocab_key_when_none_declared() -> None:
    config = STTConfig()
    assert "additional_vocab" not in config.to_transcription_config()


# ------------------------------------------------------------------ fail-safe


class _FakeWebSocket:
    """Stands in for one `websockets.connect(...)` connection.

    `messages` is what the fake server sends, in delivery order — consumed
    by `recv()` (used before `RecognitionStarted`) and by `__anext__` (the
    `async for raw in ws:` loop in `_AgentSTTSession._receive`).
    """

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

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def test_rejected_vocab_falls_back_to_a_working_session(
    monkeypatch: pytest.MonkeyPatch, cast: PanelCast
) -> None:
    """The failure this whole fix is about: a preview endpoint that rejects
    `additional_vocab` must not leave the panel with no transcription. The
    first `StartRecognition` (vocab enabled) is rejected; the session must
    retry once with the vocabulary dropped and reach `RecognitionStarted`.
    """
    first = _FakeWebSocket([{"message": "Error", "reason": "bad config"}])
    second = _FakeWebSocket(
        [{"message": "RecognitionStarted"}, {"message": "EndOfTranscript"}]
    )
    connections = [first, second]

    def fake_connect(*args: object, **kwargs: object) -> _FakeWebSocket:
        del args, kwargs
        return connections.pop(0)

    monkeypatch.setattr("panel_runtime.stt.websockets.connect", fake_connect)

    config = STTConfig.from_cast(cast)
    assert config.additional_vocab, "fixture assumes melia.yaml's vocab entry exists"

    async def body() -> None:
        loop = asyncio.get_running_loop()
        source = PushAudioSource(loop)
        source.close()  # no real mic: `read()` returns b"" immediately

        session = _AgentSTTSession(
            speaker="human",
            source=source,
            config=config,
            api_key="test-key",
            events=asyncio.Queue(),
            name="ricky",
        )
        await session.run()
        return session

    session = asyncio.run(body())

    assert not connections, "both connection attempts should have been consumed"
    assert session._vocab_enabled is False, "vocab must be disabled after the rejection"

    first_sent = json.loads(first.sent[0])
    second_sent = json.loads(second.sent[0])
    assert "additional_vocab" in first_sent["transcription_config"]
    assert "additional_vocab" not in second_sent["transcription_config"]


def test_vocab_rejection_when_no_vocab_was_set_is_a_plain_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `additional_vocab` means no reason to guess the rejection was
    about it — a rejected `StartRecognition` with an empty vocabulary is an
    ordinary session failure, retried with backoff like any other."""
    rejecting = _FakeWebSocket([{"message": "Error", "reason": "bad token"}])
    connections = [rejecting]

    def fake_connect(*args: object, **kwargs: object) -> _FakeWebSocket:
        del args, kwargs
        return connections.pop(0)

    monkeypatch.setattr("panel_runtime.stt.websockets.connect", fake_connect)

    config = STTConfig(reconnect_max_s=0.0)  # no additional_vocab
    assert not config.additional_vocab

    async def body() -> bool:
        loop = asyncio.get_running_loop()
        source = PushAudioSource(loop)
        source.close()

        session = _AgentSTTSession(
            speaker="human",
            source=source,
            config=config,
            api_key="test-key",
            events=asyncio.Queue(),
            name="ricky",
        )
        session.stop()  # run() must not reconnect forever inside this test
        try:
            await session._session()
        except RuntimeError as exc:
            return "additional_vocab" not in str(exc) and "rejected" in str(exc)
        return False

    assert asyncio.run(body())
