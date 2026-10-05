"""The agents' own transcription must never reach the floor.

`PanelRuntime` runs a second `PanelSTT` over each agent's played audio so the
video wall's transcript band can be timed off real speech instead of an assumed
words-per-second. CLAUDE.md's invariant — agent speech never reaches
`panel_core` through STT — is untouched by that, but *only* because
`_run_agent_stt` never calls `emit()`. There is no type, no config and no
reducer guard standing behind it: one line of restraint in one loop is the
whole safety property, and one careless `self.emit(event)` there would put a
lossy, latent transcription of the panel's own voices into the reducer that
already has the verbatim text. That is the feedback loop that ends the show.

So this file guards that omission directly. It is the most important test in
`panel_runtime`, and it is deliberately written against the reducer's own
front door — `PanelRuntime.events`, `fc.reduce` and `state` — rather than
against `_run_agent_stt`'s internals, because a refactor that reroutes the
loop must still fail here.

Nothing here touches the network: both `PanelSTT` instances are constructed
(they only need an API key to exist) but never started, and their event queues
are driven by hand.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from panel_core import (
    HUMAN,
    PanelCast,
    Tick,
    TranscriptUpdated,
    TurnYielded,
)
from panel_runtime.panel import PanelRuntime

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"


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


@pytest.fixture
def cast() -> PanelCast:
    return PanelCast.from_dir(PERSONA_DIR)


@pytest.fixture(autouse=True)
def keys(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")


def _runtime(cast: PanelCast, display: FakeDisplay | None) -> PanelRuntime:
    return PanelRuntime(cast, use_tts=False, display=display)


def _counting_reduce(runtime: PanelRuntime) -> list[object]:
    """Replace the reducer with a spy that records every event it is given."""
    seen: list[object] = []
    original = runtime.fc.reduce

    def reduce(state, event):
        seen.append(event)
        return original(state, event)

    runtime.fc.reduce = reduce  # type: ignore[method-assign]
    return seen


async def _pump(runtime: PanelRuntime, *tasks_for) -> None:
    """Run the named loops long enough for a queued event to be handled."""
    tasks = [asyncio.create_task(coro()) for coro in tasks_for]
    # Several passes: an event has to come off one queue, be handed on, and —
    # if anything were emitting — come back around the reducer's queue too.
    for _ in range(20):
        await asyncio.sleep(0)
    runtime._running = False
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ------------------------------------------------------------------ the wiring


def test_no_display_means_no_agent_transcription_at_all(cast: PanelCast):
    """`uv run panel` without `--display` opens no extra sockets and pays
    nothing in the audio callback."""
    runtime = _runtime(cast, None)
    assert runtime.agent_stt is None
    assert runtime.mixer.on_played is None


def test_a_display_wires_one_channel_per_agent_to_the_mixer_tap(cast: PanelCast):
    runtime = _runtime(cast, FakeDisplay())
    assert runtime.agent_stt is not None
    # Each agent id maps to itself, so `TranscriptUpdated.speaker` is the id
    # the transcript band attributes the line to.
    assert runtime.agent_stt.channels == {a: a for a in cast.ids()}
    # Tapped at `Mixer.render` — the playback clock — not where chunks arrive
    # from the TTS provider, which is the generation clock.
    assert runtime.mixer.on_played == runtime.agent_stt.feed
    # And it is a genuinely separate session from the one feeding the floor.
    assert runtime.agent_stt is not runtime.stt
    assert runtime.agent_stt.events is not runtime.stt.events


# ------------------------------------------------------------- the isolation


def test_an_agent_transcript_never_reaches_the_reducer(cast: PanelCast):
    """The guard. A `TranscriptUpdated` naming an agent, arriving on the
    display-only session, must reach the wall and nothing else."""
    display = FakeDisplay()
    runtime = _runtime(cast, display)
    assert runtime.agent_stt is not None
    reduced = _counting_reduce(runtime)
    before = runtime.state

    transcript = TranscriptUpdated(t=1.0, speaker="wayne", text="Look —", is_final=True)

    async def body():
        runtime.agent_stt.events.put_nowait(transcript)
        # `_drain_events` runs too, deliberately: this proves the event does
        # not arrive at the reducer by *any* route, not merely that
        # `_run_agent_stt` does not call `reduce` itself.
        await _pump(runtime, runtime._run_agent_stt, runtime._drain_events)

    asyncio.run(body())

    assert display.events == [transcript]
    assert reduced == []
    assert runtime.events.qsize() == 0
    assert runtime.state is before


def test_the_agents_own_end_of_turn_is_dropped_entirely(cast: PanelCast):
    """`EndOfTurn` on an agent's own voice is an agent pausing, and the floor
    already knows the turn's shape from `AgentSpeechEnded` with the verbatim
    text on it. Forwarded, it would let an agent end its own turn by taking a
    breath, and would race a real end-of-turn off Ricky's mic."""
    display = FakeDisplay()
    runtime = _runtime(cast, display)
    assert runtime.agent_stt is not None
    reduced = _counting_reduce(runtime)

    async def body():
        runtime.agent_stt.events.put_nowait(TurnYielded(t=1.0))
        await _pump(runtime, runtime._run_agent_stt, runtime._drain_events)

    asyncio.run(body())

    # Not forwarded to the wall either — it is not a floor signal on this
    # session, so it is not any kind of signal on this session.
    assert display.events == []
    assert reduced == []
    assert runtime.events.qsize() == 0


def test_nothing_on_the_display_session_is_written_to_the_rehearsal_log(
    cast: PanelCast, tmp_path: Path
):
    """The log is a record of what the floor controller saw, so a replay
    reproduces the show. An agent's own transcript was never seen by the floor
    and must not appear in it."""
    display = FakeDisplay()
    log_path = tmp_path / "rehearsal.jsonl"
    runtime = PanelRuntime(cast, use_tts=False, display=display, log_path=log_path)
    assert runtime.agent_stt is not None

    async def body():
        runtime.agent_stt.events.put_nowait(
            TranscriptUpdated(t=1.0, speaker="dex", text="Fleet scale.", is_final=True)
        )
        await _pump(runtime, runtime._run_agent_stt, runtime._drain_events)

    asyncio.run(body())

    assert log_path.read_text() == ""


# --------------------------------------------------------- the test's own teeth


def test_the_human_path_still_reaches_the_reducer(cast: PanelCast):
    """Proof the assertions above are not vacuous.

    Same fixtures, same pump, an event emitted the ordinary way — it must land
    on the reducer. Without this, a `_pump` that silently ran nothing at all
    would make every isolation test above pass for the wrong reason.
    """
    display = FakeDisplay()
    runtime = _runtime(cast, display)
    reduced = _counting_reduce(runtime)

    human = TranscriptUpdated(t=1.0, speaker=HUMAN, text="So, Wayne?", is_final=True)

    async def body():
        runtime.emit(human)
        runtime.emit(Tick(t=1.1))
        await _pump(runtime, runtime._drain_events)

    asyncio.run(body())

    assert human in reduced
    assert human in display.events
