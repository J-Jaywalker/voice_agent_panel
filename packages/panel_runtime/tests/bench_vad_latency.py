"""Barge-in latency bench: true speech onset -> duck signal available.

Measures the VAD term of the barge-in budget (FEASIBILITY.md §8.1 S0.3). This
is the only term we control; ADC/DAC and input buffering are properties of the
deployment machine and must be measured on the venue rig.

Run:  uv run python packages/panel_runtime/tests/bench_vad_latency.py

Fixtures are macOS `say` output, committed so the bench is deterministic and
offline. They are clean synthetic speech — real speech through a mic in a noisy
room will differ, so treat these numbers as a budget, not a result.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import numpy as np
from livekit import rtc
from livekit.agents import vad as lkvad
from livekit.plugins import silero

SR = 16_000
LEAD_SILENCE_S = 1.0
THRESHOLDS = (0.3, 0.5, 0.7)
FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(path: Path) -> tuple[np.ndarray, int]:
    """Return PCM and the sample index of true speech onset."""
    with wave.open(str(path)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    envelope = np.abs(pcm.astype(np.float32))
    onset = int(np.argmax(envelope > envelope.max() * 0.02))
    return pcm, onset


async def measure(path: Path, *, block: int = 256) -> tuple[dict[float, float | None], float | None]:
    pcm, onset = load_fixture(path)
    lead = np.zeros(int(SR * LEAD_SILENCE_S), dtype=np.int16)
    audio = np.concatenate([lead, pcm])
    true_onset = len(lead) + onset

    detector = silero.VAD.load(sample_rate=SR, min_speech_duration=0.05, activation_threshold=0.5)
    stream = detector.stream()
    first_cross: dict[float, int] = {}
    confirmed: int | None = None

    async def consume() -> None:
        nonlocal confirmed
        async for event in stream:
            if event.type == lkvad.VADEventType.INFERENCE_DONE:
                for threshold in THRESHOLDS:
                    if threshold not in first_cross and event.probability >= threshold:
                        first_cross[threshold] = event.samples_index
            elif event.type == lkvad.VADEventType.START_OF_SPEECH:
                confirmed = event.samples_index
                return

    task = asyncio.create_task(consume())
    for i in range(0, len(audio) - block, block):
        chunk = audio[i : i + block]
        stream.push_frame(rtc.AudioFrame(chunk.tobytes(), SR, 1, len(chunk)))
        await asyncio.sleep(0)
        if task.done():
            break
    stream.end_input()
    try:
        await asyncio.wait_for(task, timeout=10)
    except TimeoutError:
        task.cancel()
    await stream.aclose()

    def to_ms(sample: int | None) -> float | None:
        return None if sample is None else (sample - true_onset) / SR * 1000

    return {t: to_ms(first_cross.get(t)) for t in THRESHOLDS}, to_ms(confirmed)


async def main() -> None:
    fixtures = sorted(FIXTURES.glob("*.wav"))
    if not fixtures:
        raise SystemExit(f"no fixtures in {FIXTURES}")

    header = "".join(f"{f'p>={t}':>9}" for t in THRESHOLDS)
    print(f"{'onset type':<14}{header}{'confirm':>10}")

    per_threshold: dict[float, list[float]] = {t: [] for t in THRESHOLDS}
    confirms: list[float] = []

    for path in fixtures:
        crossings, confirmed = await measure(path)
        cells = "".join(
            f"{v:>9.1f}" if (v := crossings[t]) is not None else f"{'MISS':>9}" for t in THRESHOLDS
        )
        conf = f"{confirmed:>10.1f}" if confirmed is not None else f"{'MISS':>10}"
        print(f"{path.stem:<14}{cells}{conf}")
        for threshold, value in crossings.items():
            if value is not None:
                per_threshold[threshold].append(value)
        if confirmed is not None:
            confirms.append(confirmed)

    print("\nworst case is what the design must survive:")
    for threshold in THRESHOLDS:
        values = per_threshold[threshold]
        print(f"  p>={threshold}   mean {np.mean(values):6.1f}ms   WORST {np.max(values):6.1f}ms")
    print(f"  confirm  mean {np.mean(confirms):6.1f}ms   WORST {np.max(confirms):6.1f}ms")


if __name__ == "__main__":
    asyncio.run(main())
