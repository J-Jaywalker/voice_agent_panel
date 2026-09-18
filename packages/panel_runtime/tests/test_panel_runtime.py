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
`--no-tts` sleeps to skip): the personas' own word counts set the floor, so
the timeout is derived from them rather than guessed — see
`_intro_round_seconds` and `_run_until_intro_done`.

`test_fixed_utterance_beats_a_speculative_candidate` covers the other half of
that guarantee: carrying the fixed line is not enough if `_start_speaking` can
be talked out of using it by speculation that was already in flight.

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
    AgentSpeechEnded,
    PanelCast,
    PanelState,
    Signals,
    StartSpeech,
    TranscriptUpdated,
    sanitise,
)
from panel_runtime.brains import (
    BrainConfig,
    ProposalComplete,
    SentenceReady,
    SignalsReady,
    StreamingClaudeBrain,
)
from panel_runtime.panel import Candidate, PanelRuntime

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"

# Mirrors the `--no-tts` pacing divisor in `speak()` (panel.py) — the rate is
# hardcoded there, so this is a deliberate duplicate and the two must move
# together. Only `_intro_round_seconds` reads it; if the runtime ever makes
# the rate injectable, this constant should go away in favour of that.
_NO_TTS_WORDS_PER_SECOND = 2.8


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


def _intro_round_seconds(runtime: PanelRuntime) -> float:
    """How long the intro round must take, derived from the cast itself.

    The round speaks every agent's `Persona.introduction` in full, and
    `--no-tts` paces each sentence by a real `asyncio.sleep` — so the floor
    on this test's runtime is fixed by how many words the personas actually
    contain, not by anything the test controls.

    Derived rather than written down because a literal cannot survive the
    personas being reworded. The previous literal (90s, justified in a
    comment as "roughly 50s of real speaking time") was already wrong: the
    three introductions total 258 words, which is 92.1s of mandatory sleep,
    so both intro-round tests timed out by construction and no amount of
    re-running would have gone green. An estimate that drifts silently out
    of date is worse than no estimate — this one cannot.
    """
    words = sum(
        len(sanitise(runtime.cast[agent_id].introduction).split())
        for agent_id in runtime.cast.ids()
    )
    return words / _NO_TTS_WORDS_PER_SECOND


async def _run_until_intro_done(runtime: PanelRuntime, *, timeout: float | None = None) -> None:
    # Nothing at the `PanelRuntime` layer is stubbed here, so the round runs
    # in genuine wall-clock time and the timeout has to clear the personas'
    # real length (`_intro_round_seconds`) with room for a loaded CI box. A
    # short timeout tuned for a fake brain's near-instant reply would only
    # make this flaky, not fast. The margin is generous on purpose: it is
    # only ever paid when the test is already failing, since a passing run
    # finishes as soon as `intro_done` latches.
    if timeout is None:
        timeout = _intro_round_seconds(runtime) * 1.5 + 30.0
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


def test_fixed_utterance_beats_a_speculative_candidate(runtime: PanelRuntime):
    """The introduction the panel actually rehearsed must be the one it says.

    Ricky's opening line arrives as a long run of *partial* transcripts, each
    re-firing speculation (`scoring.speculation_interval_s`), while the
    introduction latch in `panel_core.floor` fires only on the *final*
    transcript. So every agent already has a speculative `Candidate` parked by
    the time `_grant_introduction` hands out its fixed, pre-sanitised line.
    `_start_speaking` used to build the fixed candidate only when no cached one
    existed, which on stage meant never: the agents improvised over the top of
    the one part of the show deliberately taken away from the model.

    A non-empty `StartSpeech.utterance` therefore wins unconditionally, and the
    stale speculation behind it is torn down rather than spoken.
    """
    agent = runtime.cast.ids()[0]
    improvised = "I reckon adoption is uneven."
    fixed = "Fixed line."

    async def body():
        # The speculative candidate a run of partials would have left behind,
        # plus the in-flight stream that produced it — both must be discarded.
        stale = Candidate(agent)
        await stale.add(improvised)
        runtime._candidates[agent] = stale
        stale_task = asyncio.create_task(asyncio.sleep(30), name="stale-propose")
        runtime._proposal_tasks[agent] = stale_task
        runtime._proposal_stamp[agent] = (
            runtime.state.turn_id,
            runtime.state.speculation_epoch,
        )

        runtime._start_speaking(StartSpeech(agent=agent, utterance=fixed, turn_id=0))
        await runtime._speaking_task
        # No `_drain_events` here on purpose: the reducer is not under test,
        # and this turn was never arbitrated. The emitted events are read
        # straight off the queue.
        emitted = []
        while not runtime.events.empty():
            emitted.append(runtime.events.get_nowait())
        return stale_task, emitted

    stale_task, emitted = asyncio.run(body())

    ended = [e for e in emitted if isinstance(e, AgentSpeechEnded)]
    assert len(ended) == 1, "the fixed turn must complete exactly once"
    assert ended[0].completed
    assert ended[0].utterance == fixed
    assert improvised not in ended[0].utterance

    assert stale_task.cancelled(), "stale speculation was left running"
    assert agent not in runtime._proposal_tasks
    assert agent not in runtime._proposal_stamp


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
