"""Agent brains for the live runtime.

Async, and concurrent across the cast: three agents are asked what they would
say at the same moment, during the human's turn, so that by the time end-of-turn
fires the floor controller already holds scored candidates. That is speculative
generation (FEASIBILITY.md 3.6) and it is what collapses the post-turn gap to
TTS TTFB.

The prompt, the schema and `sanitise()` come from `panel_core.prompts` — the
same ones the text harness uses, so a persona tuned in `panel-sim` behaves
identically on stage.

Cost is not a constraint here (CLAUDE.md): we deliberately generate candidate
turns that will mostly be thrown away, because a discarded proposal is cheap and
a two-second silence in front of 400 people is not.

**Streaming is not an optimisation here, it is the design.** Measured, a
proposal is ~200 output tokens at roughly 40 tokens/sec — 4 to 6 seconds, and
disabling extended thinking does not change it, because the time is generation
rather than reasoning. Waiting for the whole proposal before speaking is
therefore unusable, and no model choice fixes it.

Two things fall out of the stream at different times, and both are used the
moment they arrive:

1. **Signals**, complete about halfway through. `PROPOSAL_SCHEMA` orders the six
   signal fields before `utterance` precisely so the floor can be arbitrated
   while the text is still being written.
2. **Sentences**, as the chunker finds boundaries. Each goes straight to TTS, so
   first audio depends on the first clause instead of the last token.

**A proposal without words is not a proposal.** The model answers the "you will
almost certainly not be speaking" branch of `build_turn_prompt` by scoring
itself low *and* returning `"utterance": ""` — measured at 9/9 and 8/9 of runs
before the prompt was tightened, on every persona, not just one. Releasing the
signals the instant the six numbers land therefore raised a hand on behalf of
an agent that had nothing to say: the floor granted the turn, `speak()` drained
an already-closed empty queue, and an agent's name went up on stage over
silence. So `SignalsReady` waits for the numbers *and* for the first real
character of the utterance — 0-409ms later, measured, and 0ms in half of runs
because the opening quote arrives in the same chunk as `novelty`. That is the
same rule the non-streaming `ClaudeBrain.propose` has always applied with
`if not utterance: return None`; the streaming path simply lost it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol

from panel_core import (
    PROPOSAL_SCHEMA,
    PanelState,
    Persona,
    Signals,
    build_system_prompt,
    build_turn_prompt,
    sanitise,
    stable_prefix,
)

from .chunking import SentenceChunker

SIGNAL_FIELDS = (
    "relevance",
    "urgency",
    "disagreement",
    "confidence",
    "expertise",
    "novelty",
)

# Matches a completed numeric field in the partial JSON. Cheaper and far more
# predictable than running a tolerant JSON parser on every token.
_FIELD_DONE = re.compile(r'"(\w+)"\s*:\s*(-?[\d.]+)\s*,')
# The utterance value, as it streams. Group 1 is whatever has arrived so far.
_UTTERANCE_OPEN = re.compile(r'"utterance"\s*:\s*"((?:[^"\\]|\\.)*)')


@dataclass(frozen=True, slots=True)
class BrainConfig:
    # Opus 5 at `low`, decided 7 Sept 2026 (FEASIBILITY 10.3), and chosen
    # against the S0.7 bake-off rather than because of it: that measured
    # haiku-4.5 fastest at 2582ms total, sonnet-5 at 4110-4129ms and opus-5 at
    # 6207-6218ms, since the cost here is generation time rather than
    # reasoning. What buys the difference back is a capability neither of the
    # others has — Opus supports mid-conversation `role: "system"` messages
    # that do not invalidate the prompt cache, which is the only way
    # `InjectDirective` and the operator console's "wrap up" control can ever
    # work (FEASIBILITY 6). `low` is the mitigation for the latency, and the
    # bake-off did not measure that combination: re-measure it before relying
    # on the figure. Speculation and sentence-level streaming are what make
    # the remaining gap survivable.
    #
    # Haiku rejects `effort` outright (400), so it is unconfigurable rather
    # than merely fast: `effort` is omitted whenever the model is a haiku,
    # which keeps the bake-off re-runnable from this same config.
    model: str = "claude-opus-5"
    effort: str = "low"
    max_tokens: int = 1200
    # A proposal that arrives after the floor has been decided is worthless, so
    # it is better to abandon it than to let it hold up the panel.
    #
    # 4.0 was the original guess and it was wrong: measured generation is 4-6s,
    # so it timed out more often than not and the panel simply had no
    # candidates. Streaming is the real answer — this bound now only catches a
    # genuinely stuck request.
    timeout_s: float = 12.0

    def output_config(self) -> dict:
        config: dict = {"format": {"type": "json_schema", "schema": PROPOSAL_SCHEMA}}
        if not self.model.startswith("claude-haiku"):
            config["effort"] = self.effort
        return config


class AsyncBrain(Protocol):
    async def propose(
        self, persona: Persona, state: PanelState
    ) -> tuple[str, Signals] | None: ...


class ClaudeBrain:
    """One request per proposal, returning signals and utterance together."""

    def __init__(self, config: BrainConfig | None = None) -> None:
        import anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self.config = config or BrainConfig()
        self.client = anthropic.AsyncAnthropic()
        self.last_latency_ms: dict[str, float] = {}

    async def propose(self, persona: Persona, state: PanelState) -> tuple[str, Signals] | None:
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self.client.messages.create(
                    model=self.config.model,
                    max_tokens=self.config.max_tokens,
                    output_config=self.config.output_config(),
                    system=[
                        {
                            "type": "text",
                            "text": build_system_prompt(persona),
                            # The stable prefix. Caching it is what makes asking
                            # every agent on every partial transcript viable.
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": build_turn_prompt(state, persona)}],
                ),
                timeout=self.config.timeout_s,
            )
        except TimeoutError:
            return None

        self.last_latency_ms[persona.id] = 1000.0 * (time.monotonic() - started)

        if response.stop_reason == "refusal":
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            return None

        data = json.loads(text)
        utterance = sanitise(data.pop("utterance", ""))
        if not utterance:
            return None
        return utterance, Signals(**data)


async def gather_proposals(
    brain: AsyncBrain,
    personas: list[Persona],
    state: PanelState,
) -> dict[str, tuple[str, Signals]]:
    """Ask the whole cast at once.

    A brain that fails, refuses or times out simply does not propose. One dead
    agent must never take the panel down — the other two carry the turn, and the
    audience cannot tell the difference.
    """
    results = await asyncio.gather(
        *(brain.propose(p, state) for p in personas), return_exceptions=True
    )
    proposals: dict[str, tuple[str, Signals]] = {}
    for persona, result in zip(personas, results, strict=True):
        if isinstance(result, BaseException) or result is None:
            continue
        proposals[persona.id] = result
    return proposals


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignalsReady:
    """Every floor signal has arrived. Arbitration can run now."""

    agent: str
    signals: Signals
    elapsed_ms: float


@dataclass(frozen=True, slots=True)
class SentenceReady:
    """One speakable chunk, ready for TTS."""

    agent: str
    text: str
    index: int
    elapsed_ms: float


@dataclass(frozen=True, slots=True)
class ProposalComplete:
    agent: str
    utterance: str
    elapsed_ms: float


StreamEvent = SignalsReady | SentenceReady | ProposalComplete


def _unescape(raw: str) -> str:
    """Decode a partially-streamed JSON string value."""
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        # A trailing half-escape that isn't the well-understood partial
        # `\uXXXX` case `_escaped_stable_prefix` already trims before this is
        # ever called — genuinely malformed input, in other words. Drop the
        # last character and retry once on the chance that helps; if not,
        # return the raw text rather than raise, since a brain that cannot
        # decode its own output must not be the reason the panel goes down.
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(f'"{raw[:-1]}"')
        return raw


# A `\u` escape needs four hex digits before it means anything at all; fewer
# than that at the very *end* of the buffer is one still arriving mid-stream,
# not a malformed one — the next token will bring the rest. Left alone,
# `_unescape` decodes the partial escape as best it can (or gives up and
# returns it as literal backslash-u-digits text), and either way that result
# is not a prefix of what the same position decodes to once the escape
# finishes arriving: two personas have an em-dash speech tic
# (`personas/wayne.yaml`, `personas/dexter.yaml`, both "... —"), and "\u2014"
# streams in a few bytes at a time exactly like anything else the model
# writes. Trimming the partial escape back to its opening backslash keeps
# `_unescape` from ever being asked to decode a value the next token could
# still change out from under it — the matching argument for tags, brackets,
# parens and labels lives in `stable_prefix()` in `panel_core.prompts`; this
# one case is resolved here instead because it is a property of the raw,
# still-JSON-escaped text, not the decoded utterance `stable_prefix` sees.
_TRAILING_PARTIAL_UNICODE_ESCAPE = re.compile(r"\\u[0-9a-fA-F]{0,3}\Z")


def _escaped_stable_prefix(raw: str) -> str:
    """Trim a `\\uXXXX` escape off the end of `raw` if it has not finished
    arriving yet. `raw` is still in JSON-escaped form — whatever
    `_UTTERANCE_OPEN` captured — not yet decoded."""
    match = _TRAILING_PARTIAL_UNICODE_ESCAPE.search(raw)
    return raw[: match.start()] if match else raw


class StreamingClaudeBrain:
    """Emits signals and sentences as they arrive, not when the turn is done."""

    def __init__(self, config: BrainConfig | None = None) -> None:
        import anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self.config = config or BrainConfig()
        self.client = anthropic.AsyncAnthropic()

    async def stream(
        self,
        persona: Persona,
        state: PanelState,
        *,
        on_event: Callable[[StreamEvent], None] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        started = time.monotonic()
        chunker = SentenceChunker()
        buffer = ""
        signals_sent = False
        emitted_utterance = ""
        index = 0

        def ms() -> float:
            return 1000.0 * (time.monotonic() - started)

        async def sentences(fresh: str):
            # Shared by the in-stream path below and the end-of-stream
            # resolution after the loop, so there is exactly one place that
            # turns a fresh slice of sanitised text into `SentenceReady`
            # events — two independent copies of this would be two
            # independent places for them to drift apart.
            nonlocal index
            for sentence in chunker.push(fresh):
                index += 1
                event = SentenceReady(
                    agent=persona.id, text=sentence, index=index, elapsed_ms=ms()
                )
                if on_event:
                    on_event(event)
                yield event

        async with self.client.messages.stream(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            output_config=self.config.output_config(),
            system=[
                {
                    "type": "text",
                    "text": build_system_prompt(persona),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": build_turn_prompt(state, persona)}],
        ) as response:
            async for text in response.text_stream:
                buffer += text

                # The utterance is read *before* the signals are released, even
                # though it arrives after them in the stream. Whether this agent
                # has any words at all is part of whether it has a proposal at
                # all, so it has to be known by the time the hand goes up.
                fresh = ""
                match = _UTTERANCE_OPEN.search(buffer)
                if match:
                    # sanitise() before chunking: markup must never reach TTS,
                    # and a stray tag would also corrupt sentence boundaries.
                    #
                    # sanitise() is not prefix-monotonic, though: an unclosed
                    # tag, bracket, stage direction or leading label can all
                    # still change shape once more text arrives, and this
                    # used to diff two independently sanitised strings
                    # against that risk — which is exactly how a leaked
                    # "<em" or a chopped "Wayne" happened. `stable_prefix()`
                    # is what makes the diff safe: it withholds everything
                    # from the first still-open construct onward, so `full`
                    # is only ever computed from the part of the utterance
                    # nothing left in the stream can rewrite, and the assert
                    # below is the structural check that this promise holds
                    # rather than a comment asking the next edit to be
                    # careful. `_escaped_stable_prefix` does the same job one
                    # layer down, for a `\uXXXX` escape still arriving.
                    escaped = _escaped_stable_prefix(match.group(1))
                    full = sanitise(stable_prefix(_unescape(escaped)))
                    assert full.startswith(emitted_utterance), (
                        "stable_prefix() broke its own invariant: a later, "
                        "larger sanitised prefix must always extend the text "
                        "already emitted to TTS, never rewrite it. See "
                        "stable_prefix()'s docstring in panel_core.prompts."
                    )
                    fresh = full[len(emitted_utterance) :]
                    if fresh:
                        emitted_utterance = full

                if not signals_sent and emitted_utterance:
                    # `emitted_utterance` is the gate, not merely a value we
                    # happen to have: an empty utterance, or one that sanitises
                    # away to nothing, means this agent has nothing to say and
                    # must not be offered the floor. Silence is a legitimate
                    # outcome; an agent granted the floor and then saying
                    # nothing never is.
                    found = {m.group(1): m.group(2) for m in _FIELD_DONE.finditer(buffer)}
                    if all(f in found for f in SIGNAL_FIELDS):
                        signals_sent = True
                        event = SignalsReady(
                            agent=persona.id,
                            signals=Signals(**{f: float(found[f]) for f in SIGNAL_FIELDS}),
                            elapsed_ms=ms(),
                        )
                        if on_event:
                            on_event(event)
                        yield event

                if fresh:
                    async for event in sentences(fresh):
                        yield event

        # The stream is over: nothing further can arrive to retroactively
        # rewrite an unclosed tag, bracket, parenthetical or leading label,
        # so whatever `stable_prefix()` was withholding is resolved by
        # definition and safe to release in full now — this is the mirror
        # image of `chunker.flush()` below, one layer up, and skipping it
        # would silently swallow a turn's last few words any time it ended
        # mid-construct. That would trade the corruption bug for a dropped-
        # audio bug, which is not a trade this fix is allowed to make.
        match = _UTTERANCE_OPEN.search(buffer)
        if match:
            full = sanitise(_unescape(match.group(1)))
            assert full.startswith(emitted_utterance), (
                "final sanitise() did not extend the streamed prefix — see "
                "stable_prefix()'s docstring in panel_core.prompts."
            )
            fresh = full[len(emitted_utterance) :]
            if fresh:
                emitted_utterance = full
                async for event in sentences(fresh):
                    yield event

        tail = chunker.flush()
        if tail:
            index += 1
            event = SentenceReady(agent=persona.id, text=tail, index=index, elapsed_ms=ms())
            if on_event:
                on_event(event)
            yield event

        yield ProposalComplete(
            agent=persona.id, utterance=emitted_utterance.strip(), elapsed_ms=ms()
        )
