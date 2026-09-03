"""Runtime configuration.

Every device, rate and buffer size is configuration. The panel deploys to a
machine that is not the one it was developed on, so nothing here may be
hardcoded and no latency figure measured on a dev box is a result — only a
budget. See CLAUDE.md § Deployment.
"""

from __future__ import annotations

from dataclasses import dataclass

# Silero accepts 8k or 16k only. 16k is the better operating point for speech.
VAD_SAMPLE_RATE = 16_000


@dataclass(frozen=True, slots=True)
class AudioConfig:
    """Capture/playback settings for one deployment."""

    input_device: str | int | None = None  # None -> system default
    output_device: str | int | None = None
    sample_rate: int = 48_000
    # Dominant latency tunable: cost is paid on input AND output.
    # 128 ~2.7ms · 256 ~5.3ms · 512 ~10.7ms · 1024 ~21.3ms (each way, at 48kHz)
    block_size: int = 256
    input_channels: int = 1

    @property
    def block_ms(self) -> float:
        return 1000.0 * self.block_size / self.sample_rate


@dataclass(frozen=True, slots=True)
class BargeInConfig:
    """Silero VAD tuning for the barge-in reflex.

    The duck-first architecture (ADR 0001) makes false positives cheap: a
    spurious duck is a brief 15dB dip that resumes, not a stopped agent. That
    lets us run the VAD hot — low thresholds, fast reflex — where a
    stop-on-detect design would have to be conservative and therefore slow.
    """

    # Fast path: duck as soon as a single inference clears this probability.
    # Fires on `inference_done`, ahead of Silero's own min_speech_duration gate.
    #
    # 0.5 is measured, not guessed (tests/bench_vad_latency.py). Worst-case
    # onset-to-duck across five onset types: 0.3 -> 66.8ms, 0.5 -> 66.9ms,
    # 0.7 -> 98.9ms. Dropping to 0.3 buys nothing and costs false ducks.
    duck_probability: float = 0.5
    # Confirmation path: Silero's debounced start_of_speech.
    activation_threshold: float = 0.5
    min_speech_duration: float = 0.05
    min_silence_duration: float = 0.40
    # Budget for true speech onset -> ducked gain reaching an output buffer.
    #
    # Measured decomposition (48kHz, block_size=256):
    #   Silero detection   ~67ms worst case   <- dominates
    #   resample 48k->16k   ~1-2ms
    #   floor reducer       <1ms
    #   output block         5.3ms
    #   -------------------------------
    #   internal total      ~75ms
    # Leaves ~75ms of the 150ms hard limit for ADC/DAC and input buffering,
    # which are measured on the venue rig, not here.
    internal_budget_ms: float = 75.0
    hard_limit_ms: float = 150.0
