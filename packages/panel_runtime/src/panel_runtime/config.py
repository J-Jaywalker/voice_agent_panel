"""Runtime configuration.

Every device, rate and buffer size is configuration. The panel deploys to a
machine that is not the one it was developed on, so nothing here may be
hardcoded and no latency figure measured on a dev box is a result — only a
budget. See CLAUDE.md § Deployment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Mic capture, mixer and AEC all run here. 16k is what Agent STT wants, so a mic
# block reaches the socket without a resample.
PIPELINE_SAMPLE_RATE = 16_000

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
    """Latency budget for the barge-in reflex.

    No tuning knobs any more. The reflex fires on Speechmatics' own
    `SpeechStarted` (`stt.py`), which has no thresholds to set — the local
    Silero VAD and its four tunables were removed 6 Oct 2026, ADR 0001
    addendum.
    """

    # Budget for true speech onset -> ducked gain reaching an output buffer.
    #
    # A **target, not a measurement**. The old 150ms came with a measured
    # decomposition dominated by Silero's ~67ms detection, and that figure died
    # with Silero. What replaced it is a network round-trip to a preview
    # endpoint whose endpointing latency is documented nowhere, so the real
    # number is unknown until someone measures it on the venue rig:
    # `uv run barge-in` reports onset-to-`HumanSpeechStarted` against a live
    # session. Re-set this once that has been run there.
    hard_limit_ms: float = 150.0


@dataclass(frozen=True, slots=True)
class AECConfig:
    """Acoustic echo cancellation — this Mac's own speaker output reaching its
    own mic, not a telephony echo path.

    Diarisation already keeps that bleed from ever being attributed to Ricky
    (`stt.py`), but it cannot stop the barge-in *reflex*: `SpeechStarted`
    carries no speaker and fires before any identity check, by design. Speaker
    bleed can still duck or stop an agent that was never actually interrupted.
    AEC is a defense against that, upstream of both endpointing and
    diarisation; diarisation stays in place for whatever it does not cancel.

    Off by default: the venue's PA/mic setup is not expected to need this.
    This exists as diligence, not as the primary mitigation — wearing
    headphones, or a venue mic pointed away from the speakers, remains the
    real fix.
    """

    enabled: bool = False
    # Acoustic + buffering delay between a sample leaving `Mixer.render` and
    # its echo arriving back at the mic. Venue- and device-specific; measured
    # by `uv run aec-test`, not guessed. 0 is only ever correct on a machine
    # with no echo path at all.
    delay_ms: float = 0.0
    # Adaptive filter tail. Must cover `delay_ms` plus the room's own
    # reflections, or the canceller has nothing to converge against. 300ms
    # covers a laptop's own speaker-to-mic path with room to spare; revisit
    # only if `aec-test`'s measured ERLE says otherwise for a given room.
    filter_length_ms: float = 300.0
