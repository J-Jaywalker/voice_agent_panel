"""`AddressClassifier`, against a fake stream. No network, no API key.

The accuracy of the verdicts is measured elsewhere — `bench_address.py` scores
the prompt against the regex's acceptance corpus, and that needs a real model.
What is testable offline is everything this module was written for, which is
the *latency and correctness machinery* around the call:

- the verdict resolves at the first content delta, not at the end of the
  message, and `classify` returns there while the reason is still arriving;
- a speculative verdict for the same text is reused at zero cost, and costs one
  API call rather than two;
- a speculative verdict for a *prefix* of the final is never reused. This is
  the correctness footgun, not a performance nicety: a partial of "Wayne" looks
  like a vocative, and the completed sentence can name somebody else entirely;
- a response the model got wrong is treated as unavailable — fall back to the
  regex — rather than guessed at;
- the speculation gates actually gate.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Self

import pytest
from panel_core import PanelCast
from panel_runtime.address import AddressClassifier

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"


class _FakeStream:
    """Replays one response a chunk at a time, like `text_stream` does."""

    def __init__(self, chunks: list[str], stall: float) -> None:
        self._chunks = chunks
        self._stall = stall

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    @property
    def text_stream(self):
        async def gen():
            for index, chunk in enumerate(self._chunks):
                # Everything after the verdict token is deliberately slow, so a
                # test that passes only because the whole message arrived at
                # once cannot pass at all.
                await asyncio.sleep(self._stall if index else 0)
                yield chunk

        return gen()


class _FakeClient:
    """Minimal stand-in for `anthropic.AsyncAnthropic`.

    Records the kwargs of every call so the request shape can be asserted —
    `temperature` in particular must never appear, because the SDK dropped it
    from `messages.stream()` at 1.x and sending it raises `TypeError`.
    """

    def __init__(self, responses: list[list[str]], *, stall: float = 0.0) -> None:
        self._responses = responses
        self._stall = stall
        self.calls: list[dict] = []
        outer = self

        class _Messages:
            def stream(self, **kwargs):
                outer.calls.append(kwargs)
                index = min(len(outer.calls) - 1, len(outer._responses) - 1)
                return _FakeStream(outer._responses[index], outer._stall)

        self.messages = _Messages()

    async def close(self) -> None:
        return None


class _ExplodingMessages:
    def stream(self, **kwargs):
        del kwargs
        raise RuntimeError("connection reset")


class _ExplodingClient:
    """A client whose every call fails, the way a dropped socket does."""

    messages = _ExplodingMessages()

    async def close(self) -> None:
        return None


@pytest.fixture
def cast() -> PanelCast:
    return PanelCast.from_dir(PERSONA_DIR)


def _classifier(
    cast: PanelCast, responses: list[list[str]], *, stall: float = 0.0
) -> tuple[AddressClassifier, _FakeClient]:
    classifier = AddressClassifier(cast, api_key="test-key")
    client = _FakeClient(responses, stall=stall)
    classifier._client = client
    return classifier, client


async def _settle(classifier: AddressClassifier, *, timeout: float = 2.0) -> None:
    """Wait for any in-flight speculation to finish."""
    inflight = classifier._inflight
    if inflight is None:
        return
    async with asyncio.timeout(timeout):
        await asyncio.wait({inflight})


def test_the_verdict_resolves_at_the_first_delta(cast):
    """Time to verdict is time to first token, which is the whole argument.

    The reason is stalled for five seconds behind the token. `classify` has to
    come back with the verdict anyway — if it ever waits for the completed
    message, the classifier costs a full generation instead of a first token
    and there is no latency case for doing this at all.
    """
    classifier, client = _classifier(
        cast, [["MELIA", " - named as the subject of the request."]], stall=5.0
    )

    async def body():
        try:
            return await asyncio.wait_for(classifier.classify("Melia, carry on."), 1.0)
        finally:
            await classifier.close()

    outcome = asyncio.run(body())

    assert outcome.verdict == "MELIA"
    assert outcome.agent == "melia"
    assert outcome.source == "fresh"
    assert outcome.reason == "", "the floor must not have waited on prose"
    assert len(client.calls) == 1


def test_the_request_shape_is_the_one_the_sdk_accepts(cast):
    """Verified against the SDK, not remembered.

    `temperature` is not a parameter of `messages.stream()` at anthropic 1.x
    (this repo pins 1.2.0) and `thinking`/`output_config`/`effort` are all
    rejected by pre-4.6 models. `wake.py` sends `temperature=0.0` because it is
    on the 0.x line; this is the guard against that line being copied across.
    The system block must also be cached, or the prompt is re-prefilled on
    every single turn.
    """
    classifier, client = _classifier(cast, [["NONE", " - a statement."]])

    asyncio.run(classifier.classify("Adoption is uneven."))

    (kwargs,) = client.calls
    assert "temperature" not in kwargs
    assert "thinking" not in kwargs
    assert "output_config" not in kwargs
    assert "effort" not in kwargs
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert 'RICKY SAID: "Adoption is uneven."' in kwargs["messages"][0]["content"]


def test_a_speculative_verdict_for_the_same_text_is_free(cast):
    """The common case: the answer exists before the question is finished."""
    classifier, client = _classifier(cast, [["MELIA", " - vocative."]])

    async def body():
        classifier.speculate("Melia, carry on.")
        await _settle(classifier)
        return await classifier.classify("Melia, carry on.")

    outcome = asyncio.run(body())

    assert outcome.verdict == "MELIA"
    assert outcome.source == "speculative_hit"
    assert outcome.latency_ms == 0.0
    assert len(client.calls) == 1, "a cache hit must not cost a second call"
    # The reason finished streaming behind the verdict, so by the time the
    # final lands the operator console can have it.
    assert outcome.reason == "vocative."


def test_a_speculative_verdict_is_never_reused_for_a_longer_final(cast):
    """The correctness footgun, in one test.

    A partial naming Wayne looks like a vocative and resolves to WAYNE. The
    completed sentence turns out to be about Wayne and addressed to Melia —
    Wayne is the one agent who must not get the floor. The cache is keyed on
    the whole normalised text precisely so that prefix reuse is impossible;
    never rewrite that lookup as a `startswith`.
    """
    classifier, client = _classifier(
        cast, [["WAYNE", " - vocative."], ["MELIA", " - asked directly."]]
    )

    async def body():
        classifier.speculate("So Wayne")
        await _settle(classifier)
        return await classifier.classify(
            "So Wayne's point earlier was wrong, Melia, what do you think?"
        )

    outcome = asyncio.run(body())

    assert outcome.verdict == "MELIA"
    assert outcome.agent == "melia"
    assert outcome.source == "recomputed", "speculation ran, against text since revised"
    assert len(client.calls) == 2


def test_an_undecodable_response_is_unavailable_not_a_guess(cast):
    """A hallucinated verdict hands the question back to the regex.

    `verdict=None` is the reducer's instruction to run `_apply_detection` on the
    same final, which is a correct answer for every row of the acceptance
    corpus. Guessing which token the model meant is the one thing that must not
    happen — a wrong verdict puts the wrong panellist on a PA over Ricky.
    """
    classifier, _ = _classifier(cast, [["The answer is probably Melia, I think."]])

    outcome = asyncio.run(classifier.classify("Melia, carry on."))

    assert outcome.verdict is None
    assert outcome.agent is None
    assert outcome.source == "unavailable"


def test_a_failed_call_is_unavailable_not_an_exception(cast):
    """One dead call must never take the panel down.

    `classify` sits on the critical path of every human turn, so it reports
    failure as a fallback verdict rather than raising into the STT pump.
    """

    classifier = AddressClassifier(cast, api_key="test-key")
    classifier._client = _ExplodingClient()

    outcome = asyncio.run(classifier.classify("Melia, carry on."))

    assert outcome.verdict is None
    assert outcome.source == "unavailable"


def test_empty_text_is_never_sent(cast):
    classifier, client = _classifier(cast, [["NONE", " - nothing."]])

    outcome = asyncio.run(classifier.classify("   "))

    assert outcome.verdict is None
    assert client.calls == []


def test_the_speculation_gates_gate(cast):
    """Speechmatics emits interims far faster than the model can answer them.

    Two gates, both needed: the partial must have grown by a couple of words
    since the last call, and calls are throttled on the wall clock however fast
    interims arrive. Without them a single human turn is dozens of requests
    whose answers are all superseded before they land.
    """
    classifier, client = _classifier(cast, [["OPEN", " - open question."]])

    async def body():
        # One word: under the growth floor, so not worth asking about.
        classifier.speculate("So,")
        assert client.calls == []

        classifier.speculate("So, what does everyone")
        await _settle(classifier)
        assert len(client.calls) == 1

        # Grown enough, but inside the wall-clock throttle.
        classifier.speculate("So, what does everyone think about that")
        await _settle(classifier)
        assert len(client.calls) == 1, "the throttle did not hold"

        # A turn boundary clears the gates, or the next turn's early partials —
        # the ones whose head start is worth most — would never be asked about.
        classifier.reset()
        classifier.speculate("Right, Melia, over to you")
        await _settle(classifier)
        assert len(client.calls) == 2

    asyncio.run(body())


def test_reset_clears_the_cache(cast):
    """Per-turn state. A verdict for last turn's sentence is not an answer."""
    classifier, client = _classifier(cast, [["MELIA", " - vocative."]])

    async def body():
        classifier.speculate("Melia, carry on.")
        await _settle(classifier)
        classifier.reset()
        return await classifier.classify("Melia, carry on.")

    outcome = asyncio.run(body())

    assert outcome.source == "fresh"
    assert len(client.calls) == 2
