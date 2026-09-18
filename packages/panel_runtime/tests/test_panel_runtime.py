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

`test_a_slow_generation_does_not_block_a_fresh_one_for_the_same_agent`, in the
final section, is the runtime half of the per-round `speculation_epoch` change.
The reducer half is in `packages/panel_core/tests/test_floor.py`; the skip that
used to starve the slowest agent of fresh input lives here, in
`_request_proposals`, so it has to be proven here.

The deferred-`TurnYielded` section covers the logic that `--llm-address`
depends on. That race is pure runtime: `panel_core` gets no new state for it
and simply sees `AddressDetected` and then `TurnYielded`, in that order. Which
means the ordering guarantee has to be proven here, at the layer that owns it,
and it is proven against a stub classifier — nothing in this file touches the
network or needs an API key.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Self

import pytest
from panel_core import (
    HUMAN,
    AddressDetected,
    AgentProposal,
    AgentSpeechEnded,
    FloorConfig,
    PanelCast,
    PanelState,
    Signals,
    StartSpeech,
    TranscriptUpdated,
    TurnYielded,
    sanitise,
)
from panel_core.prompts import NO_VERDICT
from panel_runtime import panel as panel_module
from panel_runtime.address import AddressVerdict
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
        epoch = runtime.state.speculation_epoch
        key = (agent, epoch)
        runtime._candidates[key] = stale
        stale_task = asyncio.create_task(asyncio.sleep(30), name="stale-propose")
        runtime._proposal_tasks[key] = stale_task
        runtime._proposal_turn[key] = runtime.state.turn_id

        runtime._start_speaking(
            StartSpeech(agent=agent, utterance=fixed, turn_id=0, epoch=epoch)
        )
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
    assert not [k for k in runtime._proposal_tasks if k[0] == agent]
    assert not [k for k in runtime._proposal_turn if k[0] == agent]


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


# --------------------------------------------------------------------------
# The ordering race behind `--llm-address`.
#
# Speechmatics' `EndOfTurn` lands within a few milliseconds of the final that
# names an agent, so `TurnYielded` normally beats the address verdict. If
# arbitration runs first the floor is still closed: Ricky gets cued, the panel
# says nothing, and the audience hears the dead air this project spent a week
# removing. So `PanelRuntime` holds that one event — and only that one — until
# the verdict has been emitted.
#
# `panel_core` is given no new state for any of this (its side is covered by
# `packages/panel_core/tests/test_llm_address.py`); it just sees
# `AddressDetected` and then `TurnYielded`, in that order. Proving that order
# is what these tests are for.
# --------------------------------------------------------------------------


class StubAddressClassifier:
    """Stands in for `AddressClassifier`: no network, no API key, no model.

    These tests are about *when* a verdict arrives, not about what it says, so
    the verdict is fixed and `delay` is the only interesting control — it is
    what puts the classification still-in-flight at the moment `TurnYielded`
    turns up, which is the case the whole deferral exists for.
    """

    def __init__(self, outcome: AddressVerdict, *, delay: float = 0.0) -> None:
        self.outcome = outcome
        self.delay = delay
        self.speculated: list[str] = []
        self.classified: list[str] = []
        self.resets = 0

    def speculate(self, partial_text: str) -> None:
        self.speculated.append(partial_text)

    async def classify(self, text: str) -> AddressVerdict:
        self.classified.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.outcome

    def reset(self) -> None:
        self.resets += 1

    async def close(self) -> None:
        return None


def _verdict(token: str | None, agent: str | None = None) -> AddressVerdict:
    return AddressVerdict(
        verdict=token,
        agent=agent,
        reason="stubbed",
        latency_ms=7.0,
        source="fresh",
    )


def _address_runtime(monkeypatch, classifier: StubAddressClassifier) -> PanelRuntime:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    cast = PanelCast.from_dir(PERSONA_DIR)
    rt = PanelRuntime(
        cast,
        floor_config=FloorConfig(llm_address_detection=True),
        use_tts=False,
        address_classifier=classifier,
    )
    rt.brain = StubBrain()
    return rt


def _final(text: str, t: float = 0.0) -> TranscriptUpdated:
    return TranscriptUpdated(t=t, speaker=HUMAN, text=text, is_final=True)


def _partial(text: str, t: float = 0.0) -> TranscriptUpdated:
    return TranscriptUpdated(t=t, speaker=HUMAN, text=text, is_final=False)


async def _drive_stt(runtime: PanelRuntime, events: list, *, timeout: float = 5.0) -> list:
    """Push Speechmatics events through `_run_stt` and collect what it emitted.

    Only `_run_stt` runs — no `_drain_events` — because the reducer is not what
    is under test here. The emitted events are read straight off the queue, in
    the order the runtime put them there, which is the entire assertion.

    Waits for quiescence rather than for a fixed sleep: the STT queue drained,
    no classification in flight, and nothing still being held back. A test that
    slept instead would pass for the wrong reason the day the hold broke.
    """
    task = asyncio.create_task(runtime._run_stt(), name="stt-under-test")
    try:
        for event in events:
            runtime.stt.events.put_nowait(event)
        async with asyncio.timeout(timeout):
            while True:
                await asyncio.sleep(0.005)
                pending = runtime._address_task
                if (
                    runtime.stt.events.empty()
                    and runtime._held_turn is None
                    and (pending is None or pending.done())
                ):
                    break
    finally:
        runtime._running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    emitted = []
    while not runtime.events.empty():
        emitted.append(runtime.events.get_nowait())
    return emitted


def test_turn_yielded_waits_for_the_address_verdict(monkeypatch):
    """The whole point: the invitation exists before the floor is arbitrated."""
    classifier = StubAddressClassifier(_verdict("MELIA", "melia"), delay=0.05)
    runtime = _address_runtime(monkeypatch, classifier)
    final = _final("Melia, carry on.")

    emitted = asyncio.run(_drive_stt(runtime, [final, TurnYielded(t=0.01)]))

    assert [type(e).__name__ for e in emitted] == [
        "TranscriptUpdated",
        "AddressDetected",
        "TurnYielded",
    ]
    detected = emitted[1]
    assert detected.verdict == "MELIA"
    assert detected.agent == "melia"
    assert detected.text == final.text
    # Dated to the question, not to the verdict's arrival — otherwise
    # `named_proposal_lookback_s` and the invitation TTL would measure
    # something different on this path than on the regex one.
    assert detected.t == final.t
    assert classifier.classified == [final.text]


def test_the_transcript_is_never_delayed_by_the_classifier(monkeypatch):
    """`TranscriptUpdated` drives barge-in and speculative generation.

    Holding it back for a classifier round trip would delay the content-based
    interrupt check and every agent's head start on the answer — the two things
    the short post-turn gap is made of. Only `TurnYielded` ever waits.
    """
    classifier = StubAddressClassifier(_verdict(NO_VERDICT), delay=0.5)
    runtime = _address_runtime(monkeypatch, classifier)
    final = _final("Adoption is uneven, honestly.")

    async def body():
        task = asyncio.create_task(runtime._run_stt(), name="stt-under-test")
        try:
            runtime.stt.events.put_nowait(final)
            async with asyncio.timeout(2.0):
                while runtime._address_task is None:
                    await asyncio.sleep(0.001)
            assert not runtime._address_task.done(), "the stub answered too fast to prove anything"
            return [runtime.events.get_nowait() for _ in range(runtime.events.qsize())]
        finally:
            runtime._running = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    early = asyncio.run(body())
    assert [type(e).__name__ for e in early] == ["TranscriptUpdated"]


def test_a_late_verdict_releases_the_turn_and_falls_back_to_the_regex(monkeypatch):
    """The hold is bounded, and expiry is not a stall.

    On timeout the runtime emits `AddressDetected(verdict=None)` — the
    reducer's instruction to run the regex on the same final — and only then
    releases the turn. A held `TurnYielded` that is never released is a panel
    that never arbitrates again, which is the worst failure this path can
    produce.
    """
    monkeypatch.setattr(panel_module, "ADDRESS_HOLD_TIMEOUT_S", 0.02)
    classifier = StubAddressClassifier(_verdict("MELIA", "melia"), delay=5.0)
    runtime = _address_runtime(monkeypatch, classifier)
    final = _final("Melia, carry on.")

    emitted = asyncio.run(_drive_stt(runtime, [final, TurnYielded(t=0.01)]))

    assert [type(e).__name__ for e in emitted] == [
        "TranscriptUpdated",
        "AddressDetected",
        "TurnYielded",
    ]
    detected = emitted[1]
    assert detected.verdict is None, "an expired hold must not guess a verdict"
    assert detected.source == "timeout"
    assert detected.text == final.text


def test_a_newer_final_takes_over_the_held_turn(monkeypatch):
    """A turn arrives as several finals; only the last one can be the question.

    The superseded classification must not release the hold on its way out —
    that would arbitrate ahead of the verdict that actually matters — so the
    hold transfers to the newer classification and is released by it.
    """
    classifier = StubAddressClassifier(_verdict("WAYNE", "wayne"), delay=0.05)
    runtime = _address_runtime(monkeypatch, classifier)
    first = _final("So, Wayne,", t=0.0)
    second = _final("what does the financial side make of that?", t=0.02)

    emitted = asyncio.run(_drive_stt(runtime, [first, TurnYielded(t=0.01), second]))

    kinds = [type(e).__name__ for e in emitted]
    assert kinds.count("AddressDetected") == 1, "the superseded verdict must not be emitted"
    assert kinds[-1] == "TurnYielded"
    assert kinds.index("AddressDetected") < kinds.index("TurnYielded")
    detected = next(e for e in emitted if isinstance(e, AddressDetected))
    assert detected.text == second.text


def test_partials_speculate_and_finals_classify(monkeypatch):
    """Speculation on partials is what makes the verdict free at finalisation.

    A partial must never be `classify()`-ed (nothing waits on it, and a partial
    verdict is not an answer to the completed sentence) and a final must never
    be merely speculated on (its verdict is the one the floor reads).
    """
    classifier = StubAddressClassifier(_verdict("MELIA", "melia"))
    runtime = _address_runtime(monkeypatch, classifier)
    final = _final("So, Melia, what do you think?", t=0.3)

    asyncio.run(
        _drive_stt(
            runtime,
            [_partial("So,"), _partial("So, Melia,", t=0.1), final, TurnYielded(t=0.31)],
        )
    )

    assert classifier.speculated == ["So,", "So, Melia,"]
    assert classifier.classified == [final.text]


def test_the_turn_boundary_resets_the_classifier(monkeypatch):
    """Its speculation gates are per-turn state.

    `AddressClassifier`'s growth gate carries the previous turn's word count,
    so a missing reset would silently block every early speculation of the next
    turn — the partials whose head start is worth most.
    """
    classifier = StubAddressClassifier(_verdict("MELIA", "melia"))
    runtime = _address_runtime(monkeypatch, classifier)

    asyncio.run(_drive_stt(runtime, [_final("Melia, carry on."), TurnYielded(t=0.01)]))

    assert classifier.resets == 1


def test_turn_yielded_is_never_held_with_the_flag_off(runtime: PanelRuntime):
    """No classifier, no classification, no hold, and no extra event.

    Off is the shipped path, so the STT pump has to behave exactly as it did:
    one `TranscriptUpdated`, one `TurnYielded`, nothing else and nothing
    deferred.
    """
    assert runtime._address is None
    final = _final("Melia, carry on.")

    emitted = asyncio.run(_drive_stt(runtime, [final, TurnYielded(t=0.01)]))

    assert [type(e).__name__ for e in emitted] == ["TranscriptUpdated", "TurnYielded"]
    assert runtime._held_turn is None
    assert runtime._address_task is None


def test_a_verdict_carries_all_the_way_to_a_granted_turn(monkeypatch):
    """The whole wire, once: STT final -> verdict -> invitation -> the floor.

    Every other test in this section checks one link. This one checks that they
    are actually joined up — `_run_stt` and `_drain_events` both running, the
    reducer reading `AddressDetected` for real, and the named agent ending up
    with the turn. "What does the financial side make of that?" names nobody,
    so the regex resolves it to nobody: this turn only happens at all because
    the classifier answered, which is the entire reason for the feature.
    """
    classifier = StubAddressClassifier(_verdict("WAYNE", "wayne"))
    runtime = _address_runtime(monkeypatch, classifier)
    now = 1000.0
    final = _final("What does the financial side make of that?", t=now)

    async def body():
        tasks = [
            asyncio.create_task(runtime._drain_events(), name="events"),
            asyncio.create_task(runtime._run_stt(), name="stt"),
        ]
        try:
            runtime.stt.events.put_nowait(final)
            runtime.stt.events.put_nowait(TurnYielded(t=now + 0.01))
            async with asyncio.timeout(20.0):
                while runtime.state.turn_id == 0:
                    await asyncio.sleep(0.01)
        finally:
            runtime._running = False
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    asyncio.run(body())

    assert runtime.state.turn_id == 1, "exactly one turn was granted"
    assert runtime.state.floor_holder == "wayne" or runtime.state.speaking == "wayne"


# --------------------------------------------------------------------------
# A slow generation must not block a fresher one for the same agent.
#
# `_request_proposals` keys generations by `(agent, epoch)` and skips an agent
# whose task for that key is still running. `speculation_epoch` used to move
# only when a *final* landed, so every speculative round inside one human turn
# shared a key and each agent got at most one in-flight generation for the
# whole turn. On stage that meant: Dexter and Melia finished in ~2.1s, freed
# their key and were re-asked against a later partial; Wayne took 3967ms,
# spanning Ricky's entire question, so no fresher request could start and his
# one answer was necessarily written against the turn's oldest input. The
# slower the agent, the staler the input behind its winning line — structurally,
# and always the same agent.
#
# `_ask_for_proposals` now takes a fresh label per round. This is the proof
# that the skip can no longer starve anyone.
# --------------------------------------------------------------------------


class SlowBrain:
    """One agent takes longer than the whole human turn; the rest are quick.

    That asymmetry *is* the bug, so it is the fixture. `delay` is long enough
    that the slow generation is unambiguously still in flight when the second
    round opens — the case the old key could not represent.
    """

    def __init__(self, slow_agent: str, *, delay: float = 30.0) -> None:
        self.slow_agent = slow_agent
        self.delay = delay
        self.started: list[str] = []

    async def stream(self, persona, state):
        del state
        self.started.append(persona.id)
        if persona.id == self.slow_agent:
            await asyncio.sleep(self.delay)
        await asyncio.sleep(0)
        yield SignalsReady(agent=persona.id, signals=Signals(relevance=0.8), elapsed_ms=1.0)
        await asyncio.sleep(0)
        yield SentenceReady(agent=persona.id, text="Words.", index=0, elapsed_ms=2.0)
        await asyncio.sleep(0)
        yield ProposalComplete(agent=persona.id, utterance="Words.", elapsed_ms=3.0)


def test_a_slow_generation_does_not_block_a_fresh_one_for_the_same_agent(runtime):
    """Two speculative rounds in one human turn, and the slow agent gets both.

    Shaped like the live run: the first round is allowed to finish for the two
    quick agents before the second partial lands, so round two is exactly the
    case where only the slow agent still has a generation outstanding. It must
    be asked again anyway, and the fresh round must carry the *later* partial's
    timestamp as `input_t` — a fresher label is only worth anything if fresher
    input comes with it, since `input_t` is what `FloorController._stale`
    measures.

    Nothing is cancelled to make room: the slow generation stays in flight, as
    the fallback answer, until something better is actually in hand
    (`_retire_superseded`).
    """
    slow, fast = runtime.cast.ids()[0], runtime.cast.ids()[1]
    brain = SlowBrain(slow)
    runtime.brain = brain

    emitted: list = []
    plain_emit = runtime.emit

    def recording_emit(event) -> None:
        emitted.append(event)
        plain_emit(event)

    runtime.emit = recording_emit

    first_t = 10.0
    second_t = first_t + runtime.fc.config.speculation_interval_s + 0.01

    def proposals(agent: str) -> list:
        return [e for e in emitted if isinstance(e, AgentProposal) and e.agent == agent]

    async def body():
        drain = asyncio.create_task(runtime._drain_events(), name="events")
        try:
            runtime.emit(_partial("So where are we actually on the adoption curve", t=first_t))
            # Let the quick agents finish round one, as Dexter and Melia did.
            async with asyncio.timeout(10.0):
                while len([e for e in emitted if isinstance(e, AgentProposal)]) < 2:
                    await asyncio.sleep(0.01)
            assert brain.started.count(slow) == 1, "the slow generation should still be running"

            runtime.emit(
                _partial(
                    "So where are we actually on the adoption curve these days", t=second_t
                )
            )
            # A bounded settle rather than a condition wait: a regression
            # should fail on the assertions below, with the real numbers in the
            # output, not on an opaque timeout.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if brain.started.count(slow) >= 2 and len(proposals(fast)) >= 2:
                    break
                await asyncio.sleep(0.01)

            live = {
                key[1]: not task.done()
                for key, task in runtime._proposal_tasks.items()
                if key[0] == slow
            }
            return live
        finally:
            runtime._running = False
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain
            pending = list(runtime._proposal_tasks.values())
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    live = asyncio.run(body())

    assert brain.started.count(slow) == 2, (
        "the slow agent was skipped — its in-flight generation held its key"
    )
    assert sorted(live) == [1, 2], f"expected one generation per round, got {live}"
    assert all(live.values()), "neither generation may be cancelled to make room for the other"

    # The fresher label carried fresher input, which is the entire payoff.
    assert sorted((e.epoch, e.input_t) for e in proposals(fast)[:2]) == [
        (1, first_t),
        (2, second_t),
    ]
