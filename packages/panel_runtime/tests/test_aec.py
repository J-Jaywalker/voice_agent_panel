"""AEC acceptance criteria.

The ring/delay-alignment logic is pure and tested directly. The actual
cancellation quality of `speexdsp` is not meaningfully assertable offline —
that is what `uv run aec-test` is for, on the real rig — but one sanity check
here (`test_cancels_a_known_echo`) catches "the wiring is backwards" class
bugs: a perfectly known echo at zero delay should converge to something much
quieter than the uncancelled signal.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from panel_core import PanelCast
from panel_runtime.aec import EchoCanceller, _FarendRing
from panel_runtime.config import AECConfig
from panel_runtime.panel import PanelRuntime

SR = 16_000
BLOCK = 256
PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"


def block(amplitude: float = 0.5) -> np.ndarray:
    return np.full(BLOCK, amplitude, dtype=np.float32)


def test_ring_is_passthrough_signal_before_enough_history():
    ring = _FarendRing(delay_frames=BLOCK, frame_size=BLOCK)
    assert ring.read() is None
    ring.push(block(0.1))
    # One block written, one block of delay plus one block of frame_size
    # required — still not enough.
    assert ring.read() is None


def test_ring_reads_back_the_delayed_block():
    ring = _FarendRing(delay_frames=BLOCK, frame_size=BLOCK)
    first = block(0.1)
    second = block(0.2)
    ring.push(first)
    ring.push(second)
    # One block of delay behind the latest write is the *first* block.
    np.testing.assert_allclose(ring.read(), first)


def test_ring_tracks_a_longer_delay_across_many_pushes():
    delay_frames = BLOCK * 4
    ring = _FarendRing(delay_frames=delay_frames, frame_size=BLOCK)
    blocks = [np.full(BLOCK, i / 10.0, dtype=np.float32) for i in range(8)]
    for b in blocks:
        ring.push(b)
    # After 8 pushes with a 4-block delay, the oldest readable block is index 3.
    np.testing.assert_allclose(ring.read(), blocks[3])


def test_echo_canceller_passthrough_until_delay_elapses():
    cfg = AECConfig(enabled=True, delay_ms=1000 * BLOCK / SR, filter_length_ms=1000 * BLOCK / SR)
    ec = EchoCanceller(cfg, SR, BLOCK)
    near = block(0.3)
    # No far-end pushed yet — must hand the mic signal back untouched rather
    # than cancel against a ring still full of zeros.
    np.testing.assert_allclose(ec.process(near), near)


def test_cancels_a_known_echo():
    """A perfectly known echo at zero configured delay converges to quiet."""
    cfg = AECConfig(enabled=True, delay_ms=0.0, filter_length_ms=1000 * BLOCK / SR)
    ec = EchoCanceller(cfg, SR, BLOCK)
    rng = np.random.default_rng(0)
    before_rms = []
    after_rms = []
    for _ in range(200):  # ~3.2s @16kHz/256 — enough for NLMS to converge
        far = (rng.standard_normal(BLOCK).astype(np.float32) * 0.3).clip(-1, 1)
        ec.push_farend(far)
        cleaned = ec.process(far)  # mic hears exactly what was just played
        before_rms.append(float(np.sqrt(np.mean(np.square(far)))))
        after_rms.append(float(np.sqrt(np.mean(np.square(cleaned)))))
    # Converged tail only — the first second is the filter still learning.
    tail_before = np.mean(before_rms[-40:])
    tail_after = np.mean(after_rms[-40:])
    assert tail_after < tail_before * 0.5


@pytest.fixture
def runtime(monkeypatch) -> PanelRuntime:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    cast = PanelCast.from_dir(PERSONA_DIR)
    return PanelRuntime(cast, use_tts=False, block_size=BLOCK)


def test_aec_off_by_default(runtime: PanelRuntime):
    assert runtime._aec is None


def test_aec_disabled_leaves_the_callback_unchanged(runtime: PanelRuntime, monkeypatch):
    """`--aec` off (the default) must be provably identical to today, not just
    "should be" — same guarantee `--display` off already gets elsewhere."""
    fed: list[bytes] = []
    monkeypatch.setattr(runtime.stt, "feed", lambda _speaker, pcm: fed.append(pcm))
    rng = np.random.default_rng(1)
    indata = (rng.standard_normal((BLOCK, 1)).astype(np.float32) * 0.2).clip(-1, 1)
    outdata = np.zeros((BLOCK, 1), dtype=np.float32)
    runtime._callback(indata, outdata, BLOCK, None, None)
    expected = (np.clip(indata[:, 0], -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    # One consumer, not two: the VAD queue this used to also assert on is gone
    # with the local VAD (6 Oct 2026). STT is now the only sink in the callback.
    assert fed == [expected]


def test_aec_enabled_cleans_the_fed_signal(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    cast = PanelCast.from_dir(PERSONA_DIR)
    rt = PanelRuntime(
        cast,
        use_tts=False,
        block_size=BLOCK,
        aec=AECConfig(enabled=True, delay_ms=0.0, filter_length_ms=1000 * BLOCK / SR),
    )
    assert rt._aec is not None
    fed: list[bytes] = []
    monkeypatch.setattr(rt.stt, "feed", lambda _speaker, pcm: fed.append(pcm))
    # A known, correlated "echo": the mixer is silent, so feed loud mic input
    # through many blocks and confirm the pipeline never crashes and still
    # produces one `feed()` call of the right shape per callback — the
    # cancellation quality itself is `test_cancels_a_known_echo`'s job above.
    rng = np.random.default_rng(2)
    outdata = np.zeros((BLOCK, 1), dtype=np.float32)
    for _ in range(5):
        indata = (rng.standard_normal((BLOCK, 1)).astype(np.float32) * 0.2).clip(-1, 1)
        rt._callback(indata, outdata, BLOCK, None, None)
    assert len(fed) == 5
    assert all(len(pcm) == BLOCK * 2 for pcm in fed)
