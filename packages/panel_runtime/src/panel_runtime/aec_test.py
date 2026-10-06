"""Venue feedback test — measures the real echo path and calibrates `--aec-delay-ms`.

    uv run aec-test

Plays several independent bursts of white noise, on-off, out of the real
output device at demo volume while recording the real input device, then
cross-correlates each burst against its own capture window to find the
acoustic + buffering delay between them — the number `AECConfig.delay_ms`
needs, and one that cannot be guessed: it includes PortAudio's own buffering
*and* however far sound has to travel from speaker to mic, which on a laptop
is negligible but on a venue PA is not.

Run this with the real demo volume and the real devices, not headphones —
the whole point is to measure whatever echo path exists, the same way
`barge-in.py` measures ADC-to-DAC latency on the real rig rather than
assuming a number from a different machine.

Several bursts, not one long one, because a *single* draw of white noise has
its own correlation sidelobes — statistical noise specific to that one random
sequence — and averaging independent draws smooths that out, the same reason
any measurement gets repeated rather than trusted once. On-off, not
continuous, both because the gap gives each burst's echo room to decay before
the next one starts and because it is closer to the actual on/off cadence of
speech than a continuous tone — see `feedback_test.py` for the complementary
end-to-end check using real speech instead of noise.

White noise, not a tone, because a periodic signal's autocorrelation has one
peak per period and would misreport the delay by a cycle as readily as
correctly — see the exchange that led to this file for the fuller reasoning.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
from rich.console import Console
from rich.table import Table

from .aec import EchoCanceller
from .config import PIPELINE_SAMPLE_RATE, AECConfig

console = Console()

SR = PIPELINE_SAMPLE_RATE
STIMULUS_AMPLITUDE = 0.3  # loud enough to measure over room noise, quiet enough not to clip
WARMUP_S = 0.5  # dropped from the recording — output buffers still filling
MAX_DELAY_S = 1.0  # search window for the correlation peak, per burst
# A peak has to clear the rest of its own search window by this many standard
# deviations to count — otherwise a burst with a weak or absent echo would
# report *some* lag anyway, because `argmax` always returns something.
CONFIDENCE_THRESHOLD = 4.0


def _white_noise(rng: np.random.Generator, n: int) -> np.ndarray:
    return (rng.standard_normal(n).astype(np.float32) * STIMULUS_AMPLITUDE).clip(-1.0, 1.0)


@dataclass
class _BurstResult:
    index: int
    delay_samples: int | None  # None if this burst's peak was not confident
    confidence: float


def _estimate_delay(far: np.ndarray, near: np.ndarray, sr: int) -> tuple[int, float]:
    """Samples by which `near` lags `far`, via FFT cross-correlation, and a
    confidence score — how many standard deviations the peak clears the rest
    of its own search window.

    Searches only `±MAX_DELAY_S` around zero lag — a venue PA's echo path is
    milliseconds, not seconds, and bounding the search keeps one FFT over one
    burst cheap instead of needing a sliding window.
    """
    n = len(far) + len(near)
    size = 1 << int(np.ceil(np.log2(n)))
    far_f = np.fft.rfft(far, size)
    near_f = np.fft.rfft(near, size)
    corr = np.fft.irfft(near_f * np.conj(far_f), size)
    max_lag = int(sr * MAX_DELAY_S)
    # `corr[0]` is zero lag; `corr[-k]` is `near` lagging `far` by k samples,
    # which is the only direction a loudspeaker-to-mic echo can go.
    window = np.concatenate((corr[-max_lag:], corr[: max_lag + 1]))
    peak_i = int(np.argmax(window))
    peak = window[peak_i]
    rest = np.delete(window, peak_i)
    confidence = float((peak - rest.mean()) / (rest.std() + 1e-9))
    return peak_i - max_lag, confidence


def _erle_db(far: np.ndarray, near: np.ndarray, sr: int, block_size: int, delay_ms: float) -> float:
    """Echo return loss enhancement: how much quieter `near` gets once the
    known `far` reference is cancelled out of it, at the measured delay."""
    cfg = AECConfig(enabled=True, delay_ms=delay_ms)
    ec = EchoCanceller(cfg, sr, block_size)
    n_blocks = min(len(far), len(near)) // block_size
    before = np.zeros(0, dtype=np.float32)
    after = np.zeros(0, dtype=np.float32)
    for i in range(n_blocks):
        s = slice(i * block_size, (i + 1) * block_size)
        ec.push_farend(far[s])
        cleaned = ec.process(near[s])
        before = np.concatenate((before, near[s]))
        after = np.concatenate((after, cleaned))
    # Skip the filter's own convergence tail — the first second is the
    # adaptive filter still learning the room, not a fair measurement of it.
    skip = sr
    before, after = before[skip:], after[skip:]
    before_rms = float(np.sqrt(np.mean(np.square(before)))) or 1e-9
    after_rms = float(np.sqrt(np.mean(np.square(after)))) or 1e-9
    return 20 * np.log10(before_rms / after_rms)


async def _run(args) -> None:
    rng = np.random.default_rng()
    pulse_n = int(args.pulse_s * SR)
    gap_n = int(args.gap_s * SR)
    bursts = [_white_noise(rng, pulse_n) for _ in range(args.pulses)]
    stimulus = np.concatenate(
        [np.concatenate((b, np.zeros(gap_n, dtype=np.float32))) for b in bursts]
    )
    total_s = WARMUP_S + len(stimulus) / SR
    pos = 0
    captured: list[np.ndarray] = []

    def callback(indata, outdata, frames, timeinfo, status) -> None:
        nonlocal pos
        del timeinfo, status
        captured.append(indata[:, 0].copy())
        end = pos + frames
        if end <= len(stimulus):
            outdata[:, 0] = stimulus[pos:end]
        else:
            # Only the trailing margin after the last burst's gap should ever
            # land here — nothing plays, there is nothing left to measure.
            outdata[:, 0] = 0.0
        pos = end

    console.print(f"[dim]in={sd.query_devices(args.input_device, 'input')['name']}[/]")
    console.print(f"[dim]out={sd.query_devices(args.output_device, 'output')['name']}[/]")
    console.print(
        f"[bold]playing {args.pulses} bursts of noise[/] "
        f"({args.pulse_s:.1f}s on, {args.gap_s:.1f}s off) — "
        "no headphones, real volume, quiet room otherwise"
    )
    with sd.Stream(
        samplerate=SR,
        blocksize=args.block,
        dtype="float32",
        channels=1,
        device=(args.input_device, args.output_device),
        callback=callback,
    ):
        await asyncio.sleep(total_s)

    near_full = np.concatenate(captured)[int(WARMUP_S * SR) :]
    far_full = stimulus[: len(near_full)]

    if float(np.sqrt(np.mean(np.square(near_full)))) < 1e-4:
        console.print(
            "[red]mic captured almost nothing[/] — check levels/devices; "
            "no echo path to measure"
        )
        return

    results: list[_BurstResult] = []
    burst_n = pulse_n + gap_n
    for i in range(args.pulses):
        s = slice(i * burst_n, (i + 1) * burst_n)
        far_burst, near_burst = far_full[s], near_full[s]
        if len(far_burst) < burst_n or len(near_burst) < burst_n:
            break  # the trailing margin was too short to fill this burst's window
        delay, confidence = _estimate_delay(far_burst, near_burst, SR)
        confident = confidence >= CONFIDENCE_THRESHOLD and delay > 0
        results.append(_BurstResult(i, delay if confident else None, confidence))

    table = Table(title="per-burst delay")
    table.add_column("burst")
    table.add_column("delay")
    table.add_column("confidence")
    for r in results:
        delay_str = f"{1000 * r.delay_samples / SR:.1f} ms" if r.delay_samples is not None else "—"
        style = "" if r.delay_samples is not None else "dim red"
        table.add_row(str(r.index), delay_str, f"{r.confidence:.1f}", style=style)
    console.print(table)

    confident_delays = [r.delay_samples for r in results if r.delay_samples is not None]
    if len(confident_delays) < max(1, len(results) // 2):
        console.print(
            "[yellow]fewer than half the bursts produced a confident peak[/] — "
            "either there's no audible echo path (good news), or levels are too "
            "low/noisy to correlate. Re-run at a louder volume before trusting this."
        )
        return

    median_samples = statistics.median(confident_delays)
    delay_ms = 1000 * median_samples / SR
    spread_ms = 1000 * statistics.pstdev(confident_delays) / SR if len(confident_delays) > 1 else 0.0
    erle = _erle_db(far_full, near_full, SR, args.block, delay_ms)

    console.print()
    console.print(
        f"[bold]median delay:[/] {delay_ms:.1f} ms  "
        f"(±{spread_ms:.1f} ms across {len(confident_delays)}/{len(results)} confident bursts)"
    )
    console.print(f"[bold]ERLE at that delay:[/] {erle:.1f} dB  (higher is better; <3dB is not working)")
    console.print()
    console.print(f"[green]uv run panel --aec --aec-delay-ms {delay_ms:.0f}[/]")


def main() -> None:
    p = argparse.ArgumentParser(prog="aec-test", description=__doc__)
    p.add_argument("--block", type=int, default=256)
    p.add_argument("--pulses", type=int, default=8, help="number of noise bursts to average over")
    p.add_argument("--pulse-s", type=float, default=1.0, help="burst (noise-on) duration")
    p.add_argument("--gap-s", type=float, default=1.0, help="silence (noise-off) duration between bursts")
    p.add_argument("--input-device", default=None)
    p.add_argument("--output-device", default=None)
    p.add_argument("--list-devices", action="store_true")
    args = p.parse_args()
    if args.list_devices:
        console.print(str(sd.query_devices()))
        return
    for name in ("input_device", "output_device"):
        v = getattr(args, name)
        if v is not None and v.isdigit():
            setattr(args, name, int(v))
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")


if __name__ == "__main__":
    main()
