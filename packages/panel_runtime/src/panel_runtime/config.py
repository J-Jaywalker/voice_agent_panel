"""Runtime configuration.

Every device, rate and buffer size is configuration. The panel deploys to a
machine that is not the one it was developed on, so nothing here may be
hardcoded and no latency figure measured on a dev box is a result — only a
budget. See CLAUDE.md § Deployment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Silero accepts 8k or 16k only. 16k is the better operating point for speech.
VAD_SAMPLE_RATE = 16_000

# The Anthropic SDK's own default. Named here because we pass it explicitly.
ANTHROPIC_DIRECT_URL = "https://api.anthropic.com"


def anthropic_base_url() -> str:
    """Where the brains send their proposals — deliberately *not* `ANTHROPIC_BASE_URL`.

    The SDK resolves the `base_url` constructor argument first, then
    `ANTHROPIC_BASE_URL`, then its default. Every client in this project passes
    this value explicitly, so the panel never inherits a proxy from whatever
    shell happens to launch it.

    That is not hypothetical. A token-saving proxy was intercepting every
    proposal via an `ANTHROPIC_BASE_URL` exported in the dev shell, and it cost
    the panel about six seconds per turn: it reported `cache_mode:
    cold_start_full`, so the origin paid a full cold prefill of the ~1400-token
    persona system prompt on every request instead of reading the ~2800-token
    cache hit, and it added its own hop on top. Prompt caching is a byte-exact
    prefix match, so anything that rewrites the prompt in flight cannot
    preserve one — and this proxy rewrites (its own header:
    `router:text:0.91`, saving twelve tokens of 185).

    Two reasons this is pinned in code rather than left to the launch
    environment. The panel deploys to a machine that is not this one
    (CLAUDE.md § Deployment), and a stray `ANTHROPIC_BASE_URL` in a profile at
    the venue would reproduce the fault on stage with no clue in the console.
    And a rewriting proxy silently breaks the guarantee that a persona tuned in
    `panel-sim` behaves identically live, because the two would be tuned and
    run against different prompts.

    `PANEL_ANTHROPIC_BASE_URL` remains as the deliberate override — a mock
    server in a test, or a proxy chosen on purpose rather than inherited.
    """
    return os.environ.get("PANEL_ANTHROPIC_BASE_URL", ANTHROPIC_DIRECT_URL)


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
