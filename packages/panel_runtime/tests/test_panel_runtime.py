"""Runtime-level acceptance tests for the intro round and the proposal stream.

The introduction round no longer calls a model at all: every agent's line is
`Persona.introduction`, fixed at authoring time, and `FloorController.
_grant_introduction` hands it out directly the instant the phrase is detected
and again every time one agent finishes (`_advance_introductions`) — see
`packages/panel_core/tests/test_floor.py` for the reducer-level proof of that.
`StubBrain` below is therefore dead for the two introduction tests in this
file; it is kept only because `PanelRuntime` still needs a `.brain` attribute
to construct, and it remains exactly what the other tests in this file (the
streaming/sanitisation ones, further down) exercise.

What these two tests still prove at the runtime level is that `PanelRuntime`
carries a fixed line all the way to a spoken (well, printed — `use_tts=False`)
turn and back to `AgentSpeechEnded`, with no `RequestProposals` and no brain
call anywhere in between, entirely through `self.events`, `_drain_events` and
`_execute` — nothing stubbed out at that layer. Because the introduction round
no longer waits on anything, the round now runs in genuine wall-clock time (no
`--no-tts` sleeps to skip): roughly 15-20 real seconds per agent, so a few
times that for the whole round — see `_run_until_intro_done`'s timeout.

`test_no_hand_raised_for_an_empty_utterance` sits one layer lower, on
`StreamingClaudeBrain.stream` against a fake client, because that is where the
"granted the floor and then said nothing" failure was actually decided for a
*generated* turn. It is unrelated to the introduction round, which sidesteps
that whole failure class by never generating anything in the first place.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Self

import pytest
from panel_core import (
    HUMAN,
    PanelCast,
    PanelState,
    Signals,
    TranscriptUpdated,
)
from panel_runtime.brains import (
    BrainConfig,
    ProposalComplete,
    SentenceReady,
    SignalsReady,
    StreamingClaudeBrain,
)
from panel_runtime.panel import PanelRuntime

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"


class StubBrain:
    """Replaces `StreamingClaudeBrain`: no network, deterministic output.

    Each `await asyncio.sleep(0)` is a real checkpoint, so proposals from
    different agents genuinely interleave on the event loop the way
    concurrent network calls would — this is what exercises the
    re-arbitration race rather than hiding it behind sequential execution.
    """

    async def stream(self, persona, state):
        del state
        await asyncio.sleep(0)
        yield SignalsReady(agent=persona.id, signals=Signals(relevance=0.8), elapsed_ms=1.0)
        await asyncio.sleep(0)
        yield SentenceReady(agent=persona.id, text=f"Hi, I'm {persona.name}.", index=0, elapsed_ms=2.0)
        await asyncio.sleep(0)
        yield ProposalComplete(agent=persona.id, utterance=f"Hi, I'm {persona.name}.", elapsed_ms=3.0)


@pytest.fixture
def runtime(monkeypatch) -> PanelRuntime:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    cast = PanelCast.from_dir(PERSONA_DIR)
    rt = PanelRuntime(cast, use_tts=False)
    rt.brain = StubBrain()
    return rt


async def _run_until_intro_done(runtime: PanelRuntime, *, timeout: float = 90.0) -> None:
    # 90s of headroom for roughly 50s of real, unstubbed speaking time across
    # the three personas (`--no-tts` paces at ~2.8 words/sec — see `speak()`
    # in panel.py) plus margin for a loaded CI box. This file's whole point is
    # that nothing here is stubbed at the `PanelRuntime` layer any more, so a
    # short timeout tuned for a fake brain's near-instant reply is no longer
    # the right instinct — it would just make the test flaky, not fast.
    drain_task = asyncio.create_task(runtime._drain_events())
    try:
        async with asyncio.timeout(timeout):
            while not runtime.state.intro_done:
                await asyncio.sleep(0.01)
    finally:
        runtime._running = False
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task


def test_introduction_round_completes_with_no_further_human_input(runtime: PanelRuntime):
    """The whole point: once Ricky says the magic words, all three agents get
    a turn with nobody re-prompting the panel in between."""

    async def body():
        runtime.emit(
            TranscriptUpdated(
                t=0.0, speaker=HUMAN, text="Let's do some introductions.", is_final=True
            )
        )
        await _run_until_intro_done(runtime)

    asyncio.run(body())

    assert runtime.state.intro_done
    assert runtime.state.intro_queue is None
    spoken = [u.speaker for u in runtime.state.transcript if u.speaker != HUMAN]
    assert set(spoken) == set(runtime.cast.ids())
    assert len(spoken) == len(runtime.cast.ids()), "each agent must speak exactly once"


def test_introduction_round_never_grants_two_agents_at_once(runtime: PanelRuntime):
    """Regression guard for the re-arbitration race: two proposals landing
    back to back, before the first grant's AgentSpeechStarted comes back
    around the queue, must not both be awarded the floor.

    `_start_speaking` overwrites `self._speaking_task` with the new agent's
    speaking task. If it is called a second time while the previous task is
    still running, two agents were granted the floor concurrently.
    """
    overlaps: list[str] = []
    original_start = runtime._start_speaking

    def tracking_start(command):
        prior = runtime._speaking_task
        if prior is not None and not prior.done():
            overlaps.append(command.agent)
        original_start(command)

    runtime._start_speaking = tracking_start

    async def body():
        runtime.emit(
            TranscriptUpdated(
                t=0.0, speaker=HUMAN, text="Let's do some introductions.", is_final=True
            )
        )
        await _run_until_intro_done(runtime)

    asyncio.run(body())

    assert overlaps == [], "an agent was granted the floor while another was still speaking"


# --------------------------------------------------------------------------
# Wanting the floor is not taking it — and having nothing to say is not
# wanting it either.
# --------------------------------------------------------------------------


class _FakeStream:
    """Replays a JSON response one chunk at a time, like `text_stream` does."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    @property
    def text_stream(self):
        async def gen():
            for chunk in self._chunks:
                await asyncio.sleep(0)
                yield chunk

        return gen()


def _fake_client(chunks: list[str]):
    class _Messages:
        def stream(self, **kwargs):
            del kwargs
            return _FakeStream(chunks)

    class _Client:
        messages = _Messages()

    return _Client()


def _chunked(body: str, size: int = 7) -> list[str]:
    return [body[i : i + size] for i in range(0, len(body), size)]


_SIGNALS = (
    '{"relevance": 0.1, "urgency": 0.0, "disagreement": 0.0, "confidence": 1.0, '
    '"expertise": 0.0, "novelty": 0.0, "responding_to": null, "defer_to": null, '
)


def _run_stream(monkeypatch, body: str, *, chunk_size: int = 7):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cast = PanelCast.from_dir(PERSONA_DIR)
    brain = StreamingClaudeBrain(BrainConfig())
    brain.client = _fake_client(_chunked(body, size=chunk_size))

    async def body_coro():
        return [
            event
            async for event in brain.stream(cast["dex"], PanelState.for_agents(cast.ids()))
        ]

    return asyncio.run(body_coro())


def test_no_hand_raised_for_an_empty_utterance(monkeypatch):
    """The live failure, at its source.

    Asked whether it had anything to say while the floor was closed, the real
    model answered by scoring itself low *and* returning `"utterance": ""` —
    measured on every persona, not just one. Releasing the six signals on their
    own raised a hand for an agent with no words, the floor granted it the
    turn, and its name went up on stage over silence with the turn consumed.

    No signals means no `AgentProposal`, which means the floor is never offered
    to an agent that cannot fill it. Silence stays a legitimate outcome; being
    granted the floor and then producing nothing does not.
    """
    events = _run_stream(monkeypatch, _SIGNALS + '"utterance": ""}')

    assert not [e for e in events if isinstance(e, SignalsReady)], (
        "an agent with no utterance raised its hand"
    )
    assert not [e for e in events if isinstance(e, SentenceReady)]
    assert [e.utterance for e in events if isinstance(e, ProposalComplete)] == [""]


def test_hand_raised_before_the_utterance_finishes(monkeypatch):
    """...and the streaming advantage survives the guard.

    The signals must still be released while the text is being written — that
    ordering is what lets arbitration start before generation ends
    (FEASIBILITY.md 3.6). The guard costs the gap between the last signal and
    the utterance's first character, measured at 0-409ms, not the whole turn.
    """
    body = _SIGNALS + (
        '"utterance": "Right, but historically that is the part people skip. '
        'Adoption is real and uneven at once."}'
    )
    events = _run_stream(monkeypatch, body)

    kinds = [type(e).__name__ for e in events]
    assert kinds[0] == "SignalsReady", f"signals must lead, got {kinds}"
    assert kinds.count("SignalsReady") == 1
    assert "SentenceReady" in kinds, "the utterance must still stream in sentences"
    assert kinds[-1] == "ProposalComplete"


# --------------------------------------------------------------------------
# sanitise() is not prefix-monotonic, and diffing two independently
# sanitised strings used to leak or corrupt text across a chunk boundary.
# `stable_prefix()` (panel_core.prompts) is the fix; these are its
# regression tests at the point the corruption actually reached TTS.
# --------------------------------------------------------------------------

# Each case puts one of the non-monotonic constructs from `stable_prefix`'s
# docstring across a streaming chunk boundary: an unclosed tag, an unclosed
# bracket, an unclosed stage-direction paren, and — Wayne's and Dexter's
# em-dash speech tic, both "... —" in their persona YAML — a `—` escape
# that `json.dumps` below always renders as the same `\uXXXX` form the real
# API streams, so splitting it across single-character chunks genuinely
# exercises the partial-escape path in `_escaped_stable_prefix`, not just the
# tag/bracket/paren path in `stable_prefix` itself.
_NON_MONOTONIC_REGRESSION_CASES = [
    "Look at this, it is fine, but <em>slow</em>, and that is the whole point.",
    "Come on, [note to self] adoption is real, and it is uneven, honestly.",
    "She said, quite calmly, that it works (laughs) and then went on a while.",
    "Right, but — historically that is the part people skip, honestly.",
]


def _spoken_text(events: list) -> str:
    """Every `SentenceReady` chunk, concatenated in emitted order — the
    words that actually reached TTS over the course of the turn, as opposed
    to `ProposalComplete.utterance`, which is always computed fresh from the
    complete text and was never at risk from this bug."""
    return "".join(e.text for e in events if isinstance(e, SentenceReady))


@pytest.mark.parametrize("utterance", _NON_MONOTONIC_REGRESSION_CASES)
@pytest.mark.parametrize("chunk_size", [1, 3, 7])
def test_streamed_sentences_never_corrupt_across_chunk_boundaries(
    monkeypatch, utterance: str, chunk_size: int
):
    """Whatever size the network happens to deliver text in, the
    concatenation of every `SentenceReady` chunk must reconstruct exactly
    the same text as the fully-resolved `ProposalComplete.utterance` — never
    more, never less, and never reordered. Before `stable_prefix()`, this
    failed for all four cases at `chunk_size=1`: a raw "<em", "[note to
    self]" or "(laughs" reached TTS verbatim and then vanished from
    underneath already-emitted text once the construct closed, and a split
    `\\u2014` briefly emitted a literal "\\u20"-shaped fragment instead of
    the em-dash it was about to become.
    """
    body = _SIGNALS + f'"utterance": {json.dumps(utterance)}}}'
    events = _run_stream(monkeypatch, body, chunk_size=chunk_size)

    complete = [e for e in events if isinstance(e, ProposalComplete)]
    assert len(complete) == 1
    assert _spoken_text(events) == complete[0].utterance
