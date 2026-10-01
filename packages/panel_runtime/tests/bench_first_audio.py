"""S0.7 — the number that actually matters: request to first audio.

    uv run python packages/panel_runtime/tests/bench_first_audio.py --voice <id>

Everything else measured so far is an intermediate. This is what the audience
experiences: the gap between Ricky finishing his question and an agent's voice
arriving.

Two paths, measured against each other:

**one-shot** — generate the whole proposal, then synthesise it. First audio waits
on the last token, so it is roughly `total_generation + tts_ttfb`.

**streamed** — send each sentence to TTS as the model finishes writing it. First
audio waits on the first *clause*, so it is roughly
`time_to_first_sentence + tts_ttfb`, and the rest is written far faster than it
is spoken.

Needs ANTHROPIC_API_KEY and ELEVENLABS_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

from panel_core import PanelCast
from panel_runtime.brains import (
    BrainConfig,
    ClaudeBrain,
    ProposalComplete,
    SentenceReady,
    SignalsReady,
    StreamingClaudeBrain,
)
from panel_runtime.tts import ElevenLabsTTS, TTSConfig

sys.path.insert(0, str(Path(__file__).parent))
from bench_brains import scenario


async def run_oneshot(brain: ClaudeBrain, tts, persona, state, voice: str) -> dict[str, float]:
    t0 = time.monotonic()
    result = await brain.propose(persona, state)
    generated = time.monotonic()
    if result is None:
        return {}
    utterance, _ = result

    stream = await tts.synthesise(utterance, voice_id=voice)
    first_audio = None
    async for _chunk in stream.chunks():
        first_audio = time.monotonic()
        break
    stream.cancel()
    return {
        "generation_ms": 1000 * (generated - t0),
        "first_audio_ms": 1000 * ((first_audio or time.monotonic()) - t0),
        "words": float(len(utterance.split())),
    }


async def run_streamed(
    brain: StreamingClaudeBrain, tts, persona, state, voice: str
) -> dict[str, float]:
    t0 = time.monotonic()
    turn = await tts.open(voice_id=voice)

    first_audio: float | None = None
    signals_ms = first_sentence_ms = None
    words = 0

    async def drain() -> None:
        nonlocal first_audio
        async for _chunk in turn.chunks():
            if first_audio is None:
                first_audio = time.monotonic()

    drain_task = asyncio.create_task(drain())

    async for event in brain.stream(persona, state):
        if isinstance(event, SignalsReady) and signals_ms is None:
            signals_ms = event.elapsed_ms
        elif isinstance(event, SentenceReady):
            if first_sentence_ms is None:
                first_sentence_ms = event.elapsed_ms
            words += len(event.text.split())
            await turn.push(event.text)
        elif isinstance(event, ProposalComplete):
            await turn.finish()

    # Wait for first audio only — the rest is spoken over time and irrelevant here.
    deadline = time.monotonic() + 5.0
    while first_audio is None and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    turn.cancel()
    drain_task.cancel()

    return {
        "signals_ms": signals_ms or float("nan"),
        "first_sentence_ms": first_sentence_ms or float("nan"),
        "first_audio_ms": 1000 * ((first_audio or time.monotonic()) - t0),
        "words": float(words),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice", required=True)
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--personas", type=Path, default=Path("personas"))
    args = parser.parse_args()

    cast = PanelCast.from_dir(args.personas)
    persona, state = cast["melia"], scenario(cast)
    config = BrainConfig(model=args.model, effort=args.effort)

    tts = ElevenLabsTTS(TTSConfig())
    await tts.prewarm([args.voice])
    print(f"model={args.model} effort={args.effort}  (TTS pre-warmed)\n")

    oneshot = ClaudeBrain(config)
    streaming = StreamingClaudeBrain(config)

    rows: dict[str, list[dict[str, float]]] = {"one-shot": [], "streamed": []}
    for _ in range(args.repeat):
        rows["one-shot"].append(await run_oneshot(oneshot, tts, persona, state, args.voice))
        rows["streamed"].append(await run_streamed(streaming, tts, persona, state, args.voice))

    def med(samples: list[dict[str, float]], key: str) -> float:
        values = [s[key] for s in samples if key in s]
        return statistics.median(values) if values else float("nan")

    print(f"{'path':<12} {'signals':>9} {'1st sent':>10} {'FIRST AUDIO':>13} {'words':>7}")
    print("-" * 56)
    print(
        f"{'one-shot':<12} {'-':>9} {'-':>10} "
        f"{med(rows['one-shot'], 'first_audio_ms'):>12.0f}ms "
        f"{med(rows['one-shot'], 'words'):>7.0f}"
    )
    print(
        f"{'streamed':<12} {med(rows['streamed'], 'signals_ms'):>8.0f}ms "
        f"{med(rows['streamed'], 'first_sentence_ms'):>9.0f}ms "
        f"{med(rows['streamed'], 'first_audio_ms'):>12.0f}ms "
        f"{med(rows['streamed'], 'words'):>7.0f}"
    )
    print("-" * 56)

    saved = med(rows["one-shot"], "first_audio_ms") - med(rows["streamed"], "first_audio_ms")
    print(f"\nstreaming saves {saved:.0f}ms of dead air on every single turn.\n")
    await tts.aclose()


if __name__ == "__main__":
    asyncio.run(main())
