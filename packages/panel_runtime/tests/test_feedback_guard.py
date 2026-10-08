"""`--mute-while-agents-speak`: the last-resort feedback guard.

Two properties, both asserted directly rather than through the console/CLI
layer: the mode forces the mic ungated regardless of what `speaker_lock` was
passed (its whole premise is that enrolment cannot be trusted here), and it
gates the mic in `_callback` for as long as an agent is on the floor, tracked
off `AgentSpeechStarted`/`AgentSpeechEnded` the same way `--aec`'s tests drive
`_callback` directly (see `test_aec.py`).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
from panel_core import AgentSpeechEnded, AgentSpeechStarted, PanelCast
from panel_runtime.panel import PanelRuntime

BLOCK = 256
PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"


@pytest.fixture
def runtime(monkeypatch) -> PanelRuntime:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    cast = PanelCast.from_dir(PERSONA_DIR)
    return PanelRuntime(
        cast,
        use_tts=False,
        block_size=BLOCK,
        speaker_lock=True,
        mute_while_agents_speak=True,
    )


def test_it_forces_the_mic_ungated_regardless_of_speaker_lock(runtime: PanelRuntime) -> None:
    assert runtime._speaker_lock is False


def test_the_mic_feeds_normally_before_any_agent_has_the_floor(
    runtime: PanelRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    fed: list[bytes] = []
    monkeypatch.setattr(runtime.stt, "feed", lambda _speaker, pcm: fed.append(pcm))
    rng = np.random.default_rng(0)
    indata = (rng.standard_normal((BLOCK, 1)).astype(np.float32) * 0.2).clip(-1, 1)
    outdata = np.zeros((BLOCK, 1), dtype=np.float32)
    runtime._callback(indata, outdata, BLOCK, None, None)
    assert len(fed) == 1


def test_the_mic_is_gated_for_as_long_as_an_agent_is_on_the_floor(
    runtime: PanelRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    fed: list[bytes] = []
    monkeypatch.setattr(runtime.stt, "feed", lambda _speaker, pcm: fed.append(pcm))
    runtime.events = asyncio.Queue()
    runtime.fc = _StubFloorController()

    async def drain_one():
        task = asyncio.create_task(runtime._drain_events())
        await asyncio.sleep(0)
        task.cancel()

    runtime.emit(AgentSpeechStarted(t=0.0, agent="wayne"))
    asyncio.run(drain_one())
    assert runtime._agent_on_floor is True

    rng = np.random.default_rng(1)
    indata = (rng.standard_normal((BLOCK, 1)).astype(np.float32) * 0.2).clip(-1, 1)
    outdata = np.zeros((BLOCK, 1), dtype=np.float32)
    runtime._callback(indata, outdata, BLOCK, None, None)
    assert fed == [], "an agent is speaking — the mic must not reach STT"

    runtime.emit(AgentSpeechEnded(t=0.1, agent="wayne", utterance="hi", completed=True))
    asyncio.run(drain_one())
    assert runtime._agent_on_floor is False

    runtime._callback(indata, outdata, BLOCK, None, None)
    assert len(fed) == 1, "the floor is clear again — the mic must feed normally"


class _StubFloorController:
    """`reduce` is irrelevant here — only the event-type tracking is under test."""

    def reduce(self, state, event):
        del event
        return state, []
