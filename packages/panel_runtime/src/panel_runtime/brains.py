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
    # S0.7 bake-off (docs/spike-phase0.md): haiku-4.5 is both the cheapest and
    # the fastest of the three — 2582ms total vs 4110-4129ms for sonnet-5 and
    # 6207-6218ms for opus-5 — because the cost here is generation time, not
    # reasoning, and no model/effort setting changes that. Haiku rejects the
    # `effort` param outright (400), so it is unconfigurable rather than merely
    # fast: `effort` is omitted from the request whenever the model is a haiku.
    model: str = "claude-haiku-4-5-20251001"
    effort: str = "medium"
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
        # A trailing half-escape. Drop the last character and retry once.
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(f'"{raw[:-1]}"')
        return raw


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

                if not signals_sent:
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

                match = _UTTERANCE_OPEN.search(buffer)
                if match:
                    # sanitise() before chunking: markup must never reach TTS,
                    # and a stray tag would also corrupt sentence boundaries.
                    full = sanitise(_unescape(match.group(1)))
                    fresh = full[len(emitted_utterance) :]
                    if fresh:
                        emitted_utterance = full
                        for sentence in chunker.push(fresh):
                            index += 1
                            event = SentenceReady(
                                agent=persona.id, text=sentence, index=index, elapsed_ms=ms()
                            )
                            if on_event:
                                on_event(event)
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
