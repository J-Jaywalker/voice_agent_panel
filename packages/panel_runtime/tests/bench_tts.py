"""S0.4 — does the TTS provider clear the gates?

Two questions, and only two:

**Time to first byte.** Speculative generation collapses the post-turn gap to
TTS TTFB and nothing else (FEASIBILITY.md 3.6), so this number *is* the panel's
responsiveness. Anything much over 300ms and the panel sounds like it is
thinking, which is exactly the impression the whole architecture exists to
avoid. `--cold` reconnects per utterance to show what the connection pool buys.

**Cancellation.** Not "does the provider stop" — we never wait for that. The
claim under test is that `cancel()` returns immediately and no further audio is
delivered, so the mixer can take the gain down inside one output buffer. If
cancel ever blocks on the network, the 150ms hard limit becomes somebody else's
SLA instead of our own code, which is not acceptable on a live stage.

    uv run python packages/panel_runtime/tests/bench_tts.py --voice <id>

Needs ELEVENLABS_API_KEY. Re-run on the venue rig: TTFB includes a network leg,
and the venue's uplink is not this one.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

from panel_runtime.tts import ElevenLabsTTS, TTSConfig

# Length matters for TTFB comparisons — a panel turn is one or two sentences,
# not a paragraph, so measuring on a paragraph would flatter the result.
LINES = [
    "I think that framing gets the causation backwards.",
    "We ran exactly that experiment last year and it did not hold up.",
    "Right, but the interesting part is what happens at scale.",
    "No — and I would push back on the premise there.",
    "That is fair, though it assumes the data is clean to begin with.",
]


async def measure_ttfb(engine: ElevenLabsTTS, voice: str, text: str) -> float:
    stream = await engine.synthesise(text, voice_id=voice)
    async for _chunk in stream.chunks():
        pass
    return stream.metrics.ttfb_ms or float("nan")


async def measure_cancel(engine: ElevenLabsTTS, voice: str) -> dict[str, float]:
    """Cancel mid-utterance and assert nothing arrives afterwards."""
    long_line = " ".join(LINES) + " And that is really the whole argument."
    stream = await engine.synthesise(long_line, voice_id=voice)

    chunks_before = 0
    chunks_after = 0
    cancel_call_ms = float("nan")

    async for _chunk in stream.chunks():
        chunks_before += 1
        if chunks_before == 3:
            t0 = time.monotonic()
            stream.cancel()
            cancel_call_ms = 1000.0 * (time.monotonic() - t0)
        elif chunks_before > 3:
            chunks_after += 1

    return {
        "cancel_call_ms": cancel_call_ms,
        "chunks_after_cancel": float(chunks_after),
        "audio_delivered_s": stream.metrics.audio_seconds,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice", required=True, help="ElevenLabs voice id")
    parser.add_argument("--model", default=TTSConfig().model_id)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--cold", action="store_true", help="reconnect per utterance, as the old design did"
    )
    args = parser.parse_args()

    engine = ElevenLabsTTS(TTSConfig(model_id=args.model))
    mode = "cold (reconnect per turn)" if args.cold else "warm (pooled connection)"
    print(f"model={args.model}  voice={args.voice}  {mode}\n")

    if not args.cold:
        t0 = time.monotonic()
        await engine.prewarm([args.voice])
        print(f"pre-show handshake: {1000 * (time.monotonic() - t0):.0f}ms (paid once)\n")

    ttfbs: list[float] = []
    print(f"{'line':<56} {'ttfb':>9}")
    print("-" * 68)
    for _ in range(args.repeat):
        for line in LINES:
            if args.cold:
                await engine.aclose()
            ttfb = await measure_ttfb(engine, args.voice, line)
            ttfbs.append(ttfb)
            print(f"{line[:54]:<56} {ttfb:>8.0f}ms")

    print("-" * 68)
    print(f"{'median':<56} {statistics.median(ttfbs):>8.0f}ms")
    print(f"{'worst':<56} {max(ttfbs):>8.0f}ms")
    print("\nWorst case is the number that matters — the audience hears the bad turn.\n")

    c = await measure_cancel(engine, args.voice)
    print("cancellation")
    print("-" * 72)
    print(f"  cancel() returned in         {c['cancel_call_ms']:.3f}ms")
    print(f"  chunks delivered after       {int(c['chunks_after_cancel'])}")
    print(f"  audio synthesised before stop {c['audio_delivered_s']:.2f}s")
    verdict = "PASS" if c["chunks_after_cancel"] == 0 and c["cancel_call_ms"] < 1.0 else "FAIL"
    print(f"\n  {verdict}: cancel must return in <1ms and deliver nothing after.")
    await engine.aclose()


if __name__ == "__main__":
    asyncio.run(main())
