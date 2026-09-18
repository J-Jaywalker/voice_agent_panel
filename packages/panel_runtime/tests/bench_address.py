"""Can a lightweight model replace the address-detection regex?

    uv run python packages/panel_runtime/tests/bench_address.py
    uv run python packages/panel_runtime/tests/bench_address.py --model claude-sonnet-5
    uv run python packages/panel_runtime/tests/bench_address.py --repeat 3 --verbose

Two questions, and the second one is the one that kills the idea if it fails.

**Is it more accurate?** Scored against `CORPUS` in
`packages/panel_core/tests/test_address.py` — 50 rows of real moderator phrasing
with the outcome each one is allowed to produce. That file is the spec for the
regex detector, so it is also the only fair regression bar for anything
replacing it. Imported rather than copied: two versions of an eval set means the
one you are not looking at is wrong.

**Is it fast enough?** The classifier would sit where `_apply_detection` sits —
on the final that opens the floor, which is the moment we have spent this whole
project clearing. So the number reported is **time to verdict**, not time to
completion: the stream is decoded at the first content delta, which
`address_verdicts` keeps unambiguous by giving every token a distinct initial.
Time to completion is reported too, but nothing waits on it — the reason text is
for the operator console.

Budget: `EndOfTurn` lands within a few ms of the naming final, so this is in
series with arbitration and TTS. Under ~250ms is free (speculation on partials
hides it entirely). Above ~600ms it is a real cost and has to buy real accuracy.

Neither number means anything until it is re-measured on the venue rig
(CLAUDE.md, Deployment) — and the accuracy number is the one that transfers.

`NEW_CAPABILITY` is scored separately and deliberately not mixed into the
headline: those rows are why we would do this at all, and none of them are
things the regex was ever asked to do, so counting them as passes would flatter
the comparison.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import anthropic
from panel_core import PanelCast
from panel_core.prompts import (
    AMBIGUOUS_VERDICT,
    INTRO_VERDICT,
    NO_VERDICT,
    OPEN_VERDICT,
    address_verdicts,
    build_address_prompt,
    decode_address_verdict,
)
from panel_runtime.config import anthropic_base_url

# Haiku 4.5 is a pre-4.6 model: `thinking`, `output_config` and `effort` are all
# rejected outright. Only temperature, max_tokens and prompt caching are in
# play, which is exactly why the verdict is decoded from characters rather than
# from a JSON schema.
#
# `temperature` is *not* set, and cannot be: the SDK dropped it from
# `messages.stream()` at 1.x (verified against 1.2.0, which `panel_runtime`
# pins). `~/git/FDE/amazon_alexa_demo/wake.py` sends `temperature=0.0` and is
# on the 0.x line, so do not copy that line across. The consequence is that a
# verdict is not reproducible for free — run with `--repeat` to see how stable
# one actually is, and treat any case that flips as unresolved rather than
# passing.
DEFAULT_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 32
TIMEOUT_S = 5.0

REPO_ROOT = Path(__file__).resolve().parents[3]
CORPUS_PATH = REPO_ROOT / "packages" / "panel_core" / "tests" / "test_address.py"

# What the regex was never asked to do. The first three are the capability
# argument for the whole change; the last two are traps that come *with* it —
# "introverted" must not start the introduction round, and Ricky talking to the
# AV desk must not put an agent on the PA.
NEW_CAPABILITY: list[tuple[str, str]] = [
    ("What does the financial side make of that?", "wayne"),
    ("I'd love the ethics view on this one.", "melia"),
    ("Who owns the security question here?", "dex"),
    ("Right, let's do quick introductions.", INTRO_VERDICT),
    ("Could you introduce yourselves for the audience?", INTRO_VERDICT),
    ("Can we get the slides up?", NO_VERDICT),
    ("She's quite introverted, actually.", NO_VERDICT),
    ("Sorry, can we fix the mic on Dexter?", NO_VERDICT),
]


def load_corpus() -> list[tuple[str, str]]:
    """Read `CORPUS` out of the test module without copying it.

    The tests directory is not a package, so this goes through the file path.
    Importing the module runs its top level — a `pytest` import and a list
    literal — which is harmless and keeps the eval set single-sourced.
    """
    spec = importlib.util.spec_from_file_location("_corpus", CORPUS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the address corpus from {CORPUS_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # The corpus speaks in the reducer's terms. Translate to verdict tokens.
    sentinels = {
        module.OPEN: OPEN_VERDICT,
        module.CLOSED: NO_VERDICT,
        module.AMBIGUOUS: AMBIGUOUS_VERDICT,
    }
    return [(text, sentinels.get(expected, expected)) for text, expected, _role in module.CORPUS]


def expected_token(expected: str, verdicts: dict[str, str | None]) -> str:
    """Normalise an expectation — agent id or verdict token — to a token."""
    if expected in verdicts:
        return expected
    for token, agent_id in verdicts.items():
        if agent_id == expected:
            return token
    raise ValueError(f"cannot express {expected!r} as a verdict")


@dataclass(frozen=True, slots=True)
class Result:
    text: str
    want: str
    got: str | None
    reason: str
    verdict_ms: float
    total_ms: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.got == self.want


async def classify(
    client: anthropic.AsyncAnthropic,
    model: str,
    system: str,
    verdicts: dict[str, str | None],
    text: str,
    want: str,
) -> Result:
    """One streamed classification, timed at the verdict rather than the end."""
    started = time.perf_counter()
    buffer = ""
    got: str | None = None
    verdict_ms = 0.0
    try:
        async with client.messages.stream(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": f'RICKY SAID: "{text}"'}],
        ) as stream:
            async for delta in stream.text_stream:
                buffer += delta
                if got is None:
                    got = decode_address_verdict(buffer, verdicts)
                    if got is not None:
                        verdict_ms = (time.perf_counter() - started) * 1000
    except Exception as exc:  # noqa: BLE001 — a bench reports failures, it does not raise them
        elapsed = (time.perf_counter() - started) * 1000
        return Result(text, want, None, "", 0.0, elapsed, repr(exc))

    total_ms = (time.perf_counter() - started) * 1000
    reason = buffer.split("-", 1)[1].strip() if "-" in buffer else buffer.strip()
    return Result(text, want, got, reason, verdict_ms, total_ms)


async def run_set(
    client: anthropic.AsyncAnthropic,
    model: str,
    system: str,
    verdicts: dict[str, str | None],
    cases: list[tuple[str, str]],
    repeat: int,
    concurrency: int,
) -> list[Result]:
    gate = asyncio.Semaphore(concurrency)

    async def one(text: str, want: str) -> Result:
        async with gate:
            return await asyncio.wait_for(
                classify(client, model, system, verdicts, text, want), TIMEOUT_S * 2
            )

    jobs = [one(text, want) for _ in range(repeat) for text, want in cases]
    return list(await asyncio.gather(*jobs))


def report(name: str, results: list[Result], *, verbose: bool) -> int:
    passed = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    print(f"\n{name}: {len(passed)}/{len(results)} correct")

    timed = [r for r in results if r.verdict_ms > 0]
    if timed:
        verdict = sorted(r.verdict_ms for r in timed)
        total = sorted(r.total_ms for r in timed)
        print(
            f"  time to verdict   p50 {statistics.median(verdict):6.0f}ms  "
            f"p95 {verdict[int(len(verdict) * 0.95) - 1]:6.0f}ms  "
            f"max {verdict[-1]:6.0f}ms"
        )
        print(f"  time to complete  p50 {statistics.median(total):6.0f}ms  max {total[-1]:6.0f}ms")

    for r in failed:
        got = r.got or ("ERROR " + r.error if r.error else "no verdict")
        print(f"  ✗ want {r.want:<9} got {got:<9} | {r.text}")
        if r.reason:
            print(f"      model said: {r.reason}")
    if verbose:
        for r in passed:
            print(f"  ✓ {r.got:<9} {r.verdict_ms:5.0f}ms | {r.text}")
    return len(failed)


async def main_async(args: argparse.Namespace) -> int:
    cast = PanelCast.from_dir(args.personas)
    verdicts = address_verdicts(cast)
    system = build_address_prompt(cast)

    corpus = [(text, expected_token(want, verdicts)) for text, want in load_corpus()]
    new_cases = [(text, expected_token(want, verdicts)) for text, want in NEW_CAPABILITY]

    print(f"model:     {args.model}")
    print(f"endpoint:  {anthropic_base_url()}")
    print(f"verdicts:  {', '.join(sorted(verdicts))}")
    print(f"prompt:    {len(system)} chars")
    print(f"cases:     {len(corpus)} regression + {len(new_cases)} new, ×{args.repeat}")

    client = anthropic.AsyncAnthropic(base_url=anthropic_base_url())
    try:
        # Prime the prompt cache so the latency figures are not dominated by
        # whichever case happened to go first.
        await classify(client, args.model, system, verdicts, "Thanks, everybody.", NO_VERDICT)

        corpus_results = await run_set(
            client, args.model, system, verdicts, corpus, args.repeat, args.concurrency
        )
        new_results = await run_set(
            client, args.model, system, verdicts, new_cases, args.repeat, args.concurrency
        )
    finally:
        await client.close()

    regressions = report("regression corpus", corpus_results, verbose=args.verbose)
    report("new capability (not in the regression score)", new_results, verbose=args.verbose)

    print(
        "\nThe regression corpus is the bar: the regex passes 50/50 by construction, "
        "so anything below that is a trade, not an upgrade."
    )
    return 1 if regressions else 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="bench_address")
    parser.add_argument("--personas", type=Path, default=REPO_ROOT / "personas")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--repeat", type=int, default=1, help="runs per case, for stability")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--verbose", action="store_true", help="also list the passes")
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
