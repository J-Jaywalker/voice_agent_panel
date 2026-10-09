"""`--mute-while-agents-speak`: the last-resort feedback guard.

Two properties, both asserted directly rather than through the console/CLI
layer: the mode forces the mic ungated regardless of what `speaker_lock` was
passed (its whole premise is that enrolment cannot be trusted here), and it
gates the mic in `_callback` for as long as an agent is on the floor, driving
`_callback` directly the same way `--aec`'s tests do (see `test_aec.py`).

The gate is read off `PanelState.speaking` — the reducer's own answer to "is
anyone on the PA" — and not off the `AgentSpeechStarted`/`AgentSpeechEnded`
event types it used to count. The two agree until the floor changes hands
without passing through idle, which is exactly what the console's force key
does, and `test_an_interrupting_grant_leaves_the_mic_shut` is that case.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
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
    """Just enough reducer to answer "is anyone on the PA".

    The gate is derived from `PanelState.speaking`, so a stub that returned
    the state untouched would leave the test asserting against a field nothing
    ever moved. These are the two rules `_agent_started`/`_agent_ended` apply,
    and the second one is the point: an end from someone who is not the
    current speaker changes nothing.
    """

    def reduce(self, state, event):
        if isinstance(event, AgentSpeechStarted):
            return replace(state, speaking=event.agent), []
        if isinstance(event, AgentSpeechEnded) and state.speaking == event.agent:
            return replace(state, speaking=None), []
        return state, []


def test_an_interrupting_grant_leaves_the_mic_shut(
    runtime: PanelRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The force key hands the floor straight from one agent to the next.

    `FloorController._force` stops whoever is speaking and grants the
    replacement inside a single `reduce()`, so the runtime emits the new
    agent's `AgentSpeechStarted` before the cut-off agent's task has run its
    cancellation handler and reported `AgentSpeechEnded`. Counted by event
    type, that trailing end opens the mic while the new agent is on the PA —
    in the one mode where nothing downstream can tell his voice from Ricky's,
    which is the whole reason the mode exists.
    """
    fed: list[bytes] = []
    monkeypatch.setattr(runtime.stt, "feed", lambda _speaker, pcm: fed.append(pcm))
    runtime.events = asyncio.Queue()
    runtime.fc = _StubFloorController()

    async def drain(n: int):
        task = asyncio.create_task(runtime._drain_events())
        for _ in range(n):
            await asyncio.sleep(0)
        task.cancel()

    runtime.emit(AgentSpeechStarted(t=0.0, agent="wayne"))
    # The force lands: Melia is granted, and only then does Wayne's cancelled
    # speaking task report the turn it was cut off in the middle of.
    runtime.emit(AgentSpeechStarted(t=0.1, agent="melia"))
    runtime.emit(AgentSpeechEnded(t=0.2, agent="wayne", utterance="half a", completed=False))
    asyncio.run(drain(6))

    assert runtime._agent_on_floor is True, "Melia is on the PA; the mic must stay shut"

    rng = np.random.default_rng(2)
    indata = (rng.standard_normal((BLOCK, 1)).astype(np.float32) * 0.2).clip(-1, 1)
    outdata = np.zeros((BLOCK, 1), dtype=np.float32)
    runtime._callback(indata, outdata, BLOCK, None, None)
    assert fed == [], "the PA is live — its bleed must not reach STT as Ricky"
