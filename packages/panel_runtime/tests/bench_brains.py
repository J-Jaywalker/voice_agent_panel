"""S0.7 — model bake-off for the proposal call.

    uv run python packages/panel_runtime/tests/bench_brains.py
    uv run python packages/panel_runtime/tests/bench_brains.py --models claude-opus-5,claude-sonnet-5

Three numbers per model, and the middle one is the one that matters.

**TTFT** — time to first token. Interesting only as a floor.

**Time to signals** — when the last floor signal has arrived. This is the real
deadline. `PROPOSAL_SCHEMA` puts the six signal fields *before* `utterance`
deliberately, and structured output is generated in order, so the floor
controller can score a proposal while its text is still being written. Whether
that ordering actually buys anything is exactly what this bench answers.

**Total** — the whole proposal. Matters because the utterance must exist before
TTS can start; measured end-to-end it sits in series with the ~200ms TTS TTFB.

The budget: agents are asked repeatedly *during* the human's turn (§3.6), so a
proposal has roughly as long as Ricky's sentence. Anything under ~1.5s is
comfortably speculative. Above that, the last request before end-of-turn will
not have landed and the panel falls back on a staler candidate.

Effort matters more than model here — check both before choosing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import time
from pathlib import Path

from panel_core import PROPOSAL_SCHEMA, PanelCast, PanelState, Utterance
from panel_core.prompts import build_system_prompt, build_turn_prompt

SIGNAL_FIELDS = (
    "relevance",
    "urgency",
    "disagreement",
    "confidence",
    "expertise",
    "novelty",
)

# A realistic mid-panel moment: a live invitation, some history, a point to
# disagree with. Measuring on an empty transcript would flatter the result.
HISTORY = [
    ("human", "So let us start with the obvious one. What is actually holding this back?"),
    ("dex", "Trust, mostly. Every organisation I have watched stall, stalled on accountability."),
    ("human", "Melia, is that your read too, or is that letting the technology off the hook?"),
]


def scenario(cast: PanelCast) -> PanelState:
    state = PanelState.for_agents(cast.ids())
    from dataclasses import replace

    from panel_core.state import Invitation, InvitationSource

    return replace(
        state,
        transcript=tuple(
            Utterance(speaker=s, text=t, t=float(i)) for i, (s, t) in enumerate(HISTORY)
        ),
        invitation=Invitation(
            agent="melia", turns_remaining=1, source=InvitationSource.ADDRESS, t=3.0
        ),
    )


async def measure(client, model: str, effort: str, persona, state) -> dict[str, float]:
    """Stream one proposal, timing the points that matter."""
    started = time.monotonic()
    ttft = signals_at = None
    buffer = ""
    seen: set[str] = set()

    async with client.messages.stream(
        model=model,
        max_tokens=1200,
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": PROPOSAL_SCHEMA},
        },
        system=[
            {
                "type": "text",
                "text": build_system_prompt(persona),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": build_turn_prompt(state, persona)}],
    ) as stream:
        async for text in stream.text_stream:
            if ttft is None:
                ttft = time.monotonic()
            buffer += text
            if signals_at is None:
                # Each signal is complete once its numeric value has been closed
                # off by the following comma — no partial-JSON parser needed.
                for field in SIGNAL_FIELDS:
                    if field not in seen and re.search(rf'"{field}"\s*:\s*[\d.]+\s*,', buffer):
                        seen.add(field)
                if len(seen) == len(SIGNAL_FIELDS):
                    signals_at = time.monotonic()
        final = await stream.get_final_message()

    done = time.monotonic()
    usage = final.usage
    return {
        "ttft_ms": 1000 * ((ttft or done) - started),
        "signals_ms": 1000 * ((signals_at or done) - started),
        "total_ms": 1000 * (done - started),
        "cache_read": float(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "output_tokens": float(usage.output_tokens),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default="claude-opus-5,claude-sonnet-5,claude-haiku-4-5")
    parser.add_argument("--efforts", default="low,medium")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--personas", type=Path, default=Path("personas"))
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set")

    import anthropic

    client = anthropic.AsyncAnthropic()
    cast = PanelCast.from_dir(args.personas)
    persona = cast["melia"]
    state = scenario(cast)

    print(f"scenario: {len(HISTORY)} turns of history, melia directly addressed")
    print(f"repeat={args.repeat} (first call of each pair primes the prompt cache)\n")
    print(f"{'model':<22} {'effort':<8} {'ttft':>8} {'signals':>9} {'total':>8} {'cached':>8}")
    print("-" * 68)

    rows = []
    for model in args.models.split(","):
        for effort in args.efforts.split(","):
            samples = []
            for _ in range(args.repeat):
                try:
                    samples.append(await measure(client, model.strip(), effort.strip(), persona, state))
                except Exception as exc:  # noqa: BLE001 — report and move on
                    print(f"{model:<22} {effort:<8} failed: {str(exc)[:40]}")
                    break
            if not samples:
                continue
            med = {k: statistics.median(s[k] for s in samples) for k in samples[0]}
            rows.append((model.strip(), effort.strip(), med))
            print(
                f"{model.strip():<22} {effort.strip():<8} "
                f"{med['ttft_ms']:>7.0f}ms {med['signals_ms']:>8.0f}ms "
                f"{med['total_ms']:>7.0f}ms {med['cache_read']:>8.0f}"
            )

    print("-" * 68)
    print(
        "\nsignals < total shows the field ordering paying off: the floor can be\n"
        "scored before the utterance finishes generating.\n"
        "Budget: a proposal must land inside the human's turn, so under ~1.5s.\n"
    )

    out = Path(".cache/bench_brains.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps([{"model": m, "effort": e, **v} for m, e, v in rows], indent=2))
    print(f"written to {out}")


if __name__ == "__main__":
    asyncio.run(main())
