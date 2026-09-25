"""Mixer acceptance criteria.

These are the audible half of the floor decisions. `panel_core` is tested on
what it *decides*; this is tested on what the audience would actually hear.

Pure numpy, no devices, no network — so it runs in the same sub-second suite.
"""

from __future__ import annotations

import numpy as np
import pytest
from panel_runtime.mixer import SILENCE_DB, Mixer

SR = 16_000
AGENTS = ("dex", "wayne", "melia")


def tone(seconds: float, amplitude: float = 0.5) -> bytes:
    n = int(SR * seconds)
    return (np.full(n, amplitude, dtype=np.float32) * 32767).astype(np.int16).tobytes()


@pytest.fixture
def mixer() -> Mixer:
    return Mixer(AGENTS, SR)


def render_ms(mixer: Mixer, ms: float, block: int = 256) -> np.ndarray:
    blocks = max(1, int(SR * ms / 1000 / block))
    return np.concatenate([mixer.render(block) for _ in range(blocks)])


def test_silence_when_nobody_is_speaking(mixer):
    assert np.all(render_ms(mixer, 50) == 0.0)


def test_a_speaking_agent_is_audible(mixer):
    mixer.feed("dex", tone(1.0))
    assert np.max(np.abs(render_ms(mixer, 50))) > 0.4


def test_duck_attenuates_without_silencing(mixer):
    """A backchannel duck must be a dip, not a stop — the agent keeps the floor."""
    mixer.feed("dex", tone(2.0))
    render_ms(mixer, 20)
    mixer.duck("dex", -15.0, ramp_ms=120)
    render_ms(mixer, 200)  # let the ramp settle

    ducked = np.max(np.abs(render_ms(mixer, 50)))
    assert 0.0 < ducked < 0.2, "ducked audio must still be audible, just quieter"


def test_resume_returns_to_full_gain(mixer):
    mixer.feed("dex", tone(3.0))
    mixer.duck("dex", -15.0, ramp_ms=120)
    render_ms(mixer, 200)
    mixer.resume("dex", ramp_ms=220)
    render_ms(mixer, 400)

    assert np.max(np.abs(render_ms(mixer, 50))) > 0.4


def test_stop_silences_and_drops_the_rest_of_the_turn(mixer):
    """A stopped agent does not resume where it left off."""
    mixer.feed("dex", tone(5.0))
    render_ms(mixer, 20)
    mixer.stop("dex")
    render_ms(mixer, 100)  # ramp out

    assert np.max(np.abs(render_ms(mixer, 100))) < 1e-3
    assert mixer.voices["dex"].buffered_seconds == 0.0, "buffered audio must be discarded"


def test_stop_ramps_rather_than_cutting(mixer):
    """A hard cut clicks through a PA and reads as a fault."""
    mixer.feed("dex", tone(2.0))
    render_ms(mixer, 20)
    mixer.stop("dex", ramp_ms=20.0)

    out = render_ms(mixer, 20, block=64)
    # The ramp must pass through intermediate values on its way down.
    assert np.max(np.abs(out)) > 0.05
    assert np.min(np.abs(out)) < 0.05


def test_ducking_one_agent_leaves_the_others_alone(mixer):
    mixer.feed("dex", tone(2.0, amplitude=0.4))
    mixer.feed("wayne", tone(2.0, amplitude=0.4))
    mixer.duck("dex", SILENCE_DB, ramp_ms=1)
    render_ms(mixer, 50)

    assert np.max(np.abs(render_ms(mixer, 50))) > 0.3, "wayne is still talking"


def test_overlapping_agents_do_not_clip_the_output(mixer):
    """Two lanes summing must not clip, however they came to be summing.

    No path deliberately overlaps two agents any more — agent-to-agent
    interrupts were removed 21 Sept 2026 — but the mixer takes one lane per
    agent and a tail draining under a new grant can still sum briefly. Clip
    here rather than let the PA do it.
    """
    for agent in AGENTS:
        mixer.feed(agent, tone(1.0, amplitude=0.9))
    out = render_ms(mixer, 50)
    assert np.max(np.abs(out)) <= 1.0


def test_drained_reports_when_a_turn_is_finished(mixer):
    mixer.feed("dex", tone(0.02))
    mixer.finish("dex")
    assert not mixer.is_drained("dex")
    render_ms(mixer, 50)
    assert mixer.is_drained("dex")


def test_audio_arriving_after_a_stop_is_refused(mixer):
    """TTS chunks in flight when the interrupt landed must not leak out."""
    mixer.feed("dex", tone(1.0))
    mixer.stop("dex")
    mixer.feed("dex", tone(1.0))  # a late chunk from the provider
    render_ms(mixer, 100)
    assert np.max(np.abs(render_ms(mixer, 100))) < 1e-3


# ------------------------------------------------------------------ metering

# What the video wall's orbs are drawn from. The claim the orb makes is that
# the ring pulsing on the wall is the audio coming out of the PA, so these
# test the meter against the same thing the listener hears: post-gain, after
# ducking, after a stop.


def test_a_silent_agent_meters_nothing(mixer):
    render_ms(mixer, 50)
    assert mixer.take_levels() == dict.fromkeys(AGENTS, 0.0)


def test_the_meter_follows_the_audio(mixer):
    mixer.feed("dex", tone(1.0, amplitude=0.5))
    render_ms(mixer, 50)
    levels = mixer.take_levels()
    assert levels["dex"] == pytest.approx(0.5, abs=0.02)
    assert levels["wayne"] == 0.0


def test_the_meter_is_taken_after_gain_so_a_ducked_agent_shrinks(mixer):
    """A duck has to be visible on the wall, not just audible in the room."""
    mixer.feed("dex", tone(2.0, amplitude=0.5))
    render_ms(mixer, 50)
    full = mixer.take_levels()["dex"]

    mixer.duck("dex", -12.0, ramp_ms=10)
    # The meter is a peak since the last read, so a read taken across the ramp
    # would report the loudest moment of it rather than where it landed.
    # Render the ramp out, discard, then measure the destination.
    render_ms(mixer, 50)
    mixer.take_levels()
    render_ms(mixer, 50)
    ducked = mixer.take_levels()["dex"]

    assert ducked < full
    assert ducked == pytest.approx(full * 0.25, rel=0.05)  # -12dB


def test_reading_the_meter_clears_it(mixer):
    """Peak-since-last-read, so the 30Hz wall cannot alias against 16ms blocks."""
    mixer.feed("dex", tone(1.0))
    render_ms(mixer, 50)
    assert mixer.take_levels()["dex"] > 0.0
    assert mixer.take_levels()["dex"] == 0.0


def test_a_stopped_agent_meters_back_to_silence(mixer):
    mixer.feed("dex", tone(1.0))
    render_ms(mixer, 50)
    mixer.stop("dex")
    render_ms(mixer, 100)
    mixer.take_levels()
    render_ms(mixer, 50)
    assert mixer.take_levels()["dex"] == 0.0
