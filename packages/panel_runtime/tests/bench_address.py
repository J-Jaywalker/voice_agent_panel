"""Can a lightweight model replace the address-detection regex?

    uv run python packages/panel_runtime/tests/bench_address.py
    uv run python packages/panel_runtime/tests/bench_address.py --model claude-sonnet-5
    uv run python packages/panel_runtime/tests/bench_address.py --repeat 3 --verbose

Two questions, and the second one is the one that kills the idea if it fails.

**Is it more accurate?** Scored against `CORPUS` in
`packages/panel_core/tests/test_address.py` — every row a real thing a moderator
says on stage, with the outcome each one is allowed to produce. That file is the spec for the
regex detector, so it is also the only fair regression bar for anything
replacing it. Imported rather than copied: two versions of an eval set means the
one you are not looking at is wrong.

**Is it fast enough?** The classifier would sit where `_apply_detection` sits —
on the final that opens the floor, which is the moment we have spent this whole
project clearing. So the number reported is **time to verdict**, not time to
completion: the stream is decoded as soon as the set of names provably closes,
which `address_verdicts` keeps cheap by giving every token a distinct initial
and `decode_address_verdict` pays one character for (a complete name is also a
legal prefix of "MELIA+WAYNE"). Time to completion is reported too, but nothing
waits on it — the reason text is for the operator console.

**Does the context help?** Rows may carry a conversation context — who the
panel has just heard from — in exactly the shape `prompts.build_address_context`
renders at runtime. That is what makes "I'd like to hear from the other two"
answerable at all, and those rows live in `NEW_CAPABILITY` with the rest of
what no regex can do.

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
import functools
import importlib.util
import json
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import anthropic
import typesafe_sdk
from panel_core import PanelCast
from panel_core.prompts import (
    AMBIGUOUS_VERDICT,
    INTRO_VERDICT,
    JOINT_REQUEST_QUESTION,
    NO_VERDICT,
    OPEN_VERDICT,
    UNIDENTIFIABLE_QUESTION,
    VERDICT_JOIN,
    _addressed_question_name,
    address_verdicts,
    build_address_prompt,
    build_address_questions,
    build_address_state,
    compose_address_verdict,
    decode_address_verdict,
)

# Private on purpose, imported on purpose: the user turn the classifier is
# scored against has to be byte-identical to the one it runs, and this is the
# same single-sourcing argument as importing `CORPUS` rather than copying it.
from panel_runtime.address import _user_turn as user_turn
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

# Pinned, never `jev-latest` — that alias moves, and a bench result against a
# moving target is not a claim about what runs on show night, the same reason
# the system prompt above is byte-identical and cached rather than re-derived
# per call.
TYPESAFE_DEFAULT_MODEL = "jev-1.13.0"
# Bench headroom, not the stage budget — `ADDRESS_HOLD_TIMEOUT_S` (0.9s) is
# what bounds the live path; this just keeps one slow case from stalling a run.
TYPESAFE_TIMEOUT_S = 0.6

REPO_ROOT = Path(__file__).resolve().parents[3]
CORPUS_PATH = REPO_ROOT / "packages" / "panel_core" / "tests" / "test_address.py"

# What the regex was never asked to do. The descriptive references and the
# context-dependent rows are the capability argument for the whole change; the
# traps come *with* it — "introverted" must not start the introduction round,
# and Ricky talking to the AV desk must not put an agent on the PA.
#
# A third column is the conversation context, in the shape
# `prompts.build_address_context` renders. "" means none, which is how most
# rows run and how the classifier is asked before the panel has spoken.
_AFTER_DEXTER = (
    "PANEL ACTIVITY — spoken recently, most recent first: Dexter. Not heard from: Melia, Wayne."
)
_AFTER_DEXTER_AND_MELIA = (
    "PANEL ACTIVITY — spoken recently, most recent first: Melia, Dexter. Not heard from: Wayne."
)

NEW_CAPABILITY: list[tuple[str, str, str]] = [
    ("What does the financial side make of that?", "wayne", ""),
    ("I'd love the ethics view on this one.", "melia", ""),
    ("Who owns the security question here?", "dex", ""),
    # --- several panellists, named ------------------------------------------
    # Reported as AMBIGUOUS until 2 Oct 2026, which closed the floor on a
    # question Ricky had plainly put to two people.
    ("Melia and Wayne, can you take that between you?", "melia+wayne", ""),
    ("Melia, Dexter, thoughts?", "dex+melia", ""),
    ("Can Melia and Wayne both take that?", "melia+wayne", ""),
    ("Dexter, Melia, Wayne — thoughts?", OPEN_VERDICT, ""),
    ("All three of you, then.", OPEN_VERDICT, ""),
    # --- several panellists, resolvable only from the context ---------------
    # The reason the classifier is given one at all. Each of these is
    # genuinely ambiguous as a sentence and obvious in the room.
    ("I'd like to hear from the other two.", "melia+wayne", _AFTER_DEXTER),
    ("You two — where does that leave you?", "melia+wayne", _AFTER_DEXTER),
    ("What about the rest of you?", "melia+wayne", _AFTER_DEXTER),
    ("And the one we haven't heard from?", "wayne", _AFTER_DEXTER_AND_MELIA),
    ("Carry on.", "dex", _AFTER_DEXTER),
    # ...and the context must not invent an invitation out of a statement.
    ("That is roughly where the market sits.", NO_VERDICT, _AFTER_DEXTER),
    # --- introductions ------------------------------------------------------
    ("Right, let's do quick introductions.", INTRO_VERDICT, ""),
    ("Could you introduce yourselves for the audience?", INTRO_VERDICT, ""),
    ("Who have we got with us tonight?", INTRO_VERDICT, ""),
    ("Can we get the slides up?", NO_VERDICT, ""),
    ("She's quite introverted, actually.", NO_VERDICT, ""),
    ("Sorry, can we fix the mic on Dexter?", NO_VERDICT, ""),
    # A welcome is not a request for introductions. Seen live 21 Sept: "Welcome
    # to the panel." came back INTRO and ran the whole round over the top of
    # Ricky's next sentence, "My name is Ricky."
    ("Welcome to the panel.", NO_VERDICT, ""),
    ("Good evening, thanks for coming.", NO_VERDICT, ""),
    ("My name is Ricky.", NO_VERDICT, ""),
    ("Joining me tonight are Dexter, Melia and Wayne.", NO_VERDICT, ""),
    # One panellist asked to introduce themselves is that panellist, not a round.
    ("Dexter, tell us a bit about yourself.", "dex", ""),
]


def load_corpus() -> list[tuple[str, str, str]]:
    """Read `CORPUS` out of the test module without copying it.

    The tests directory is not a package, so this goes through the file path.
    Importing the module runs its top level — a `pytest` import and a list
    literal — which is harmless and keeps the eval set single-sourced.

    Returns `(text, expected, context)` rows. The regression corpus has no
    context by construction: it is the regex's acceptance set, and the regex
    never had any.
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
    return [
        (text, sentinels.get(expected, expected), "") for text, expected, _role in module.CORPUS
    ]


def expected_token(expected: str, verdicts: dict[str, str | None]) -> str:
    """Normalise an expectation to a canonical verdict.

    An expectation is a verdict token, an agent id, or several agent ids joined
    by `VERDICT_JOIN` — "melia+wayne", which is how both corpora write a
    question put to two panellists.
    """
    parts = expected.split(VERDICT_JOIN)
    tokens: list[str] = []
    for part in parts:
        if part in verdicts:
            tokens.append(part)
            continue
        named = next((t for t, agent_id in verdicts.items() if agent_id == part), None)
        if named is None:
            raise ValueError(f"cannot express {part!r} as a verdict")
        tokens.append(named)
    # Canonical order is the vocabulary's own, which is cast order — the same
    # order `decode_address_verdict` sorts a decoded set into, so a comparison
    # of the two strings is a comparison of the two sets.
    order = {token: index for index, token in enumerate(verdicts)}
    return VERDICT_JOIN.join(sorted(tokens, key=order.__getitem__))


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
    context: str = "",
) -> Result:
    """One streamed classification, timed at the verdict rather than the end.

    The user turn is built by `panel_runtime.address._user_turn`, imported
    rather than reproduced: the prompt the classifier is *scored* against has
    to be the prompt it runs, and two copies of it means the one you are not
    looking at is wrong — the same argument as importing the corpus.
    """
    started = time.perf_counter()
    buffer = ""
    got: str | None = None
    verdict_ms = 0.0
    try:
        async with client.messages.stream(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_turn(text, context)}],
        ) as stream:
            async for delta in stream.text_stream:
                buffer += delta
                if got is None:
                    got = decode_address_verdict(buffer, verdicts)
                    if got is not None:
                        verdict_ms = (time.perf_counter() - started) * 1000
        if got is None:
            # The stream ended with the verdict still open — a reply of one
            # verdict and no reason. Same final decode the runtime does.
            got = decode_address_verdict(buffer, verdicts, final=True)
            if got is not None:
                verdict_ms = (time.perf_counter() - started) * 1000
    except Exception as exc:  # noqa: BLE001 — a bench reports failures, it does not raise them
        elapsed = (time.perf_counter() - started) * 1000
        return Result(text, want, None, "", 0.0, elapsed, repr(exc))

    total_ms = (time.perf_counter() - started) * 1000
    reason = buffer.split("-", 1)[1].strip() if "-" in buffer else buffer.strip()
    return Result(text, want, got, reason, verdict_ms, total_ms)


async def classify_typesafe(
    client: typesafe_sdk.AsyncTypeSafeClient,
    model: str,
    questions: dict[str, dict],
    cast: PanelCast,
    text: str,
    want: str,
    context: str = "",
    *,
    dump: TextIO | None = None,
) -> Result:
    """One atomic TypeSafe call, recomposed through `compose_address_verdict`.

    No streaming decode to time separately: the call is request-in,
    typed-answer-out, so `verdict_ms` and `total_ms` are the same number. That
    equality is itself the thing worth reading off a report — it is what "no
    streaming support" (see the TypeSafe spike plan) costs or saves in practice.
    """
    started = time.perf_counter()
    state = build_address_state(text, context, cast)
    try:
        response = await client.system_one(
            state=state,
            questions=questions,
            model=model,
            retry=typesafe_sdk.RetryPolicy(max_retries=0),
            timeout=TYPESAFE_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — a bench reports failures, it does not raise them
        elapsed = (time.perf_counter() - started) * 1000
        return Result(text, want, None, "", 0.0, elapsed, repr(exc))

    elapsed = (time.perf_counter() - started) * 1000
    mode_answer = response.choices.get("mode")
    if mode_answer is None:
        return Result(text, want, None, "", 0.0, elapsed, "no 'mode' answer in response")

    agent_probabilities = {
        agent_id: response.nouls[name].noul
        for agent_id in cast.personas
        if (name := _addressed_question_name(agent_id)) in response.nouls
    }

    def noul(name: str) -> float:
        answer = response.nouls.get(name)
        return answer.noul if answer is not None else 0.0

    # `.probabilities`, never `.confidence` — the latter is a distribution
    # spread statistic and reads well below p(top answer) on a four-option
    # Choice, so a confidence floor rejects answers the distribution supports.
    mode_probabilities = dict(mode_answer.probabilities)
    joint_request = noul(JOINT_REQUEST_QUESTION)
    unidentifiable = noul(UNIDENTIFIABLE_QUESTION)

    got = compose_address_verdict(
        mode_probabilities=mode_probabilities,
        agent_probabilities=agent_probabilities,
        joint_request=joint_request,
        unidentifiable=unidentifiable,
        cast=cast,
    )
    if dump is not None:
        dump.write(
            json.dumps(
                {
                    "text": text,
                    "want": want,
                    "got": got,
                    "mode_probabilities": mode_probabilities,
                    "agent_probabilities": agent_probabilities,
                    "joint_request": joint_request,
                    "unidentifiable": unidentifiable,
                }
            )
            + "\n"
        )
        dump.flush()
    # TypeSafe returns typed answers, not prose — there is no reason text to
    # show here, unlike the Haiku path's trailing explanation.
    return Result(text, want, got, "", elapsed, elapsed)


async def run_set(
    classify_one: Callable[[str, str, str], Awaitable[Result]],
    cases: list[tuple[str, str, str]],
    repeat: int,
    concurrency: int,
) -> list[Result]:
    gate = asyncio.Semaphore(concurrency)

    async def one(text: str, want: str, context: str) -> Result:
        async with gate:
            return await asyncio.wait_for(classify_one(text, want, context), TIMEOUT_S * 2)

    jobs = [one(*case) for _ in range(repeat) for case in cases]
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
        print(f"  ✗ want {r.want:<13} got {got:<13} | {r.text}")
        if r.reason:
            print(f"      model said: {r.reason}")
    if verbose:
        for r in passed:
            print(f"  ✓ {r.got:<13} {r.verdict_ms:5.0f}ms | {r.text}")
    return len(failed)


async def main_async(args: argparse.Namespace) -> int:
    cast = PanelCast.from_dir(args.personas)
    verdicts = address_verdicts(cast)
    model = args.model or (DEFAULT_MODEL if args.backend == "haiku" else TYPESAFE_DEFAULT_MODEL)

    corpus = [
        (text, expected_token(want, verdicts), context) for text, want, context in load_corpus()
    ]
    new_cases = [
        (text, expected_token(want, verdicts), context) for text, want, context in NEW_CAPABILITY
    ]

    print(f"backend:   {args.backend}")
    print(f"model:     {model}")
    print(f"verdicts:  {', '.join(sorted(verdicts))}")
    print(f"cases:     {len(corpus)} regression + {len(new_cases)} new, ×{args.repeat}")

    if args.backend == "haiku":
        system = build_address_prompt(cast)
        print(f"endpoint:  {anthropic_base_url()}")
        print(f"prompt:    {len(system)} chars")
        client = anthropic.AsyncAnthropic(base_url=anthropic_base_url())
        try:
            # Prime the prompt cache so the latency figures are not dominated
            # by whichever case happened to go first.
            await classify(client, model, system, verdicts, "Thanks, everybody.", NO_VERDICT)
            classify_one = functools.partial(classify, client, model, system, verdicts)
            corpus_results = await run_set(classify_one, corpus, args.repeat, args.concurrency)
            new_results = await run_set(classify_one, new_cases, args.repeat, args.concurrency)
        finally:
            await client.close()
    else:
        questions = build_address_questions(cast)
        print(f"questions: {', '.join(sorted(questions))}")
        with ExitStack() as files:
            dump = files.enter_context(args.dump.open("w")) if args.dump else None
            async with typesafe_sdk.AsyncTypeSafeClient() as client:
                classify_one = functools.partial(
                    classify_typesafe, client, model, questions, cast, dump=dump
                )
                corpus_results = await run_set(classify_one, corpus, args.repeat, args.concurrency)
                new_results = await run_set(classify_one, new_cases, args.repeat, args.concurrency)

    regressions = report("regression corpus", corpus_results, verbose=args.verbose)
    report("new capability (not in the regression score)", new_results, verbose=args.verbose)

    print(
        f"\nThe regression corpus is the bar: the regex passes {len(corpus)}/{len(corpus)} "
        "by construction, so anything below that is a trade, not an upgrade."
    )
    return 1 if regressions else 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="bench_address")
    parser.add_argument("--personas", type=Path, default=REPO_ROOT / "personas")
    parser.add_argument("--backend", choices=("haiku", "typesafe"), default="haiku")
    parser.add_argument("--model", default=None, help="overrides the backend's pinned default")
    parser.add_argument("--repeat", type=int, default=1, help="runs per case, for stability")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--verbose", action="store_true", help="also list the passes")
    parser.add_argument(
        "--dump",
        type=Path,
        default=None,
        help="TypeSafe backend only: write one JSON line of raw answers per call",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
