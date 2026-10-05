"""The output stage: many agents, one pair of speakers.

This is where `DuckSpeech`, `ResumeSpeech` and `StopSpeech` become audible, and
it is the reason those commands can be honoured inside the barge-in budget. The
floor controller decides in microseconds and the TTS provider is somewhere on
the internet, but the gain change lands here, in the next output callback, with
no network in between.

Each agent gets a buffer and a `GainEnvelope`. Ducking is a ramp; stopping is a
fast ramp to silence followed by discarding the buffer. Nothing snaps to zero —
a hard cut is an audible click through a PA, which reads as a fault rather than
as a panellist yielding.

Everything here is called from two threads: the PortAudio callback (`render`)
and the asyncio loop (everything else). The lock is held for as short a time as
possible and never across an await, because a blocked audio callback is a glitch
in front of 400 people.

`on_played` is the one tap out of here that is not a meter. It hands each
agent's rendered block to a caller as PCM16, from the audio thread, paced by the
output device — see its note on `Mixer`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import numpy as np

from .gain import GainEnvelope

# Signature of the `on_played` tap: `(agent_id, pcm16le)`, audio thread.
PlayedTap = Callable[[str, bytes], None]

# A stop should be inaudible as a transition but immediate as a silence.
STOP_RAMP_MS = 20.0
SILENCE_DB = -80.0

# ---------------------------------------------------------------- spectrum
#
# The video wall's orbs draw a frequency spectrum, so the meter above is not
# enough on its own: one number per voice per frame can say how loud an agent
# is and nothing about what their voice is *doing*. These constants are what
# turns the rendered bus into that second measurement.
#
# Where the work happens matters more than how it is done. The audio callback
# only ever copies its finished block into a ring buffer — a memcpy of a few
# hundred floats, which is the same order of cost as the RMS already taken
# there. The transform itself runs on the asyncio loop, at display rate, off a
# snapshot of that ring (`Mixer.take_bands`). An FFT inside a callback that
# must not block is a glitch waiting for the one night it matters.

# Bands sent per voice. One per bar per side of the orb's corona — the client
# mirrors them, so 32 here is 64 bars around the circle and nothing is
# resampled at either end. See BANDS in static/js/orb.js; the two have to
# agree or the client pads.
SPECTRUM_BANDS = 32

# Analysis window, seconds. 40ms is two or three pitch periods of a male
# voice — long enough that the lowest band has bins to sit in, short enough
# that a consonant is still a separate event from the vowel after it. Rounded
# to a power of two against the real sample rate in `_window_length`, because
# `np.fft.rfft` is markedly faster on one and this runs 30 times a second.
_SPECTRUM_WINDOW_S = 0.040

# The range the bands span, Hz. Speech, not hi-fi: 80Hz is below any voice's
# fundamental and 7kHz is past the last of the sibilance, and spending bands
# outside that is spending bars on parts of the orb that would never move.
_SPECTRUM_LO_HZ = 80.0
_SPECTRUM_HI_HZ = 7000.0


def _window_length(sample_rate: int) -> int:
    """Power-of-two window closest to `_SPECTRUM_WINDOW_S` at this rate."""
    target = max(64.0, sample_rate * _SPECTRUM_WINDOW_S)
    return 1 << round(float(np.log2(target)))


def _band_edges(sample_rate: int, n: int) -> list[tuple[int, int]]:
    """Bin ranges for `SPECTRUM_BANDS` logarithmically spaced bands.

    Logarithmic because pitch is: linear bands would give three quarters of
    the corona to the sibilance nobody can see moving and squeeze every vowel
    into the first two bars.

    Bands at the bottom of the range are narrower than one bin and so overlap
    their neighbours. That is left alone rather than corrected — the honest
    alternative is a longer window, which costs transient detail the eye reads
    more readily than it reads bass resolution. Adjacent low bars moving
    together is what bass looks like anyway.
    """
    nyquist = sample_rate / 2.0
    hi = min(_SPECTRUM_HI_HZ, nyquist * 0.92)
    bins = n // 2 + 1
    per_bin = sample_rate / n
    edges: list[tuple[int, int]] = []
    for i in range(SPECTRUM_BANDS):
        lo_hz = _SPECTRUM_LO_HZ * (hi / _SPECTRUM_LO_HZ) ** (i / SPECTRUM_BANDS)
        hi_hz = _SPECTRUM_LO_HZ * (hi / _SPECTRUM_LO_HZ) ** ((i + 1) / SPECTRUM_BANDS)
        lo_bin = min(bins - 1, int(lo_hz / per_bin))
        hi_bin = min(bins, max(lo_bin + 1, int(hi_hz / per_bin) + 1))
        edges.append((lo_bin, hi_bin))
    return edges


def _spectrum(samples: np.ndarray, window: np.ndarray, edges: list[tuple[int, int]]) -> list[float]:
    """Band amplitudes for one window of audio, in the same units as the meter.

    Peak within each band rather than a mean across it, which is the
    difference between a spectrum that moves and one that does not: a band is
    several bins wide at the top of the range, a voice puts energy in one of
    them, and averaging in the empty ones divides the only thing there was to
    see by the width of the band it landed in.

    Scaled so a full-scale sine reads about 1.0 — the same 0-1 range
    `take_level` returns, so the client's `?gain=` dial means one thing for
    both. 0.5 is the Hann window's coherent gain.
    """
    mag = np.abs(np.fft.rfft(samples * window))
    scale = 2.0 / (len(window) * 0.5)
    return [round(float(mag[lo:hi].max()) * scale, 4) for lo, hi in edges]


def _soft_limit(out: np.ndarray) -> np.ndarray:
    """Turn a block down, never reshape its waveform.

    A hard `np.clip` here used to be audible as clipping on the rare
    overlap it was meant to catch. The first fix for that was a per-sample
    `tanh` knee above a fixed threshold — worse: waveshaping a sample
    individually is itself a distortion, and any threshold under ~1.0
    catches an isolated voice's own peaks too, since decoded ElevenLabs
    audio routinely sits close to full scale on a single voice alone. That
    read as one agent's voice clipping, not the mixer's.

    A block's peak from a *single* voice can never reach 1.0 — decoded
    16-bit PCM tops out at 32767/32768 — so scaling the whole block down
    uniformly, and only when the true summed peak exceeds 1.0, never
    touches ordinary speech. It only ever fires when two or more lanes are
    genuinely summing over the top, and it reads as a brief, transparent
    loudness dip rather than a crunch.
    """
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak <= 1.0:
        return out
    return out * (1.0 / peak)


class AgentVoice:
    """One agent's audio buffer and gain, on the output bus."""

    def __init__(
        self,
        agent_id: str,
        sample_rate: int,
        unity_db: float = 0.0,
        window: int | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.sample_rate = sample_rate
        # "Unity" here means this voice's resting level, not 0dB — a persona
        # can carry a fixed output trim (`Persona.output_gain_db`) for voices
        # that render quiet relative to the others. Duck/resume/stop all ramp
        # relative to this, never to a hardcoded 0dB, so a trimmed voice still
        # ducks and recovers to its own resting level.
        self.unity_db = unity_db
        self.envelope = GainEnvelope(sample_rate, unity_db)
        self._buffer = np.zeros(0, dtype=np.float32)
        self._finished = False  # TTS delivered everything it is going to
        # Loudest block RMS since the meter was last read. See `take_level`.
        self._level = 0.0
        # Whether the *last* `render()` call actually pulled samples out of
        # `_buffer`, as opposed to returning silence because there was
        # nothing queued yet or the turn's audio has already drained. See
        # `Mixer.is_playing`. This is a finer-grained fact than
        # `PanelState.speaking`, which spans an agent's whole turn including
        # any gap before its first TTS chunk lands or between sentences.
        self.active = False
        # The last `_ring.size` post-gain samples, as a ring. Written by the
        # audio thread and read — never written — by the loop thread, which is
        # what lets the transform live outside the callback. See the spectrum
        # note at the head of this module.
        self._ring = np.zeros(window or _window_length(sample_rate), dtype=np.float32)
        self._ring_i = 0

    # ------------------------------------------------------------ loop thread

    def append(self, pcm: bytes) -> None:
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        self._buffer = np.concatenate([self._buffer, samples])

    def mark_finished(self) -> None:
        self._finished = True

    def reset(self) -> None:
        self._buffer = np.zeros(0, dtype=np.float32)
        self._finished = False
        self._level = 0.0
        self.active = False
        self.envelope = GainEnvelope(self.sample_rate, self.unity_db)
        self._ring[:] = 0.0
        self._ring_i = 0

    def take_level(self) -> float:
        """Loudest block since the last call, then reset to zero.

        Peak-and-clear rather than "the level right now", because the two
        clocks do not line up: blocks arrive every 16ms and the video wall
        polls every 33ms, so sampling the instantaneous value would alias —
        reading whichever block happened to land under the poll and dropping
        the one next to it. Over a syllable that shows as a flicker.

        Clearing is what makes it a peak *since the last read* rather than a
        peak-hold that has to decay, which would be one more constant to tune
        and one more thing to get wrong in a room you have not stood in yet.
        """
        level = self._level
        self._level = 0.0
        return level

    def recent(self) -> np.ndarray:
        """The window of post-gain audio behind `now`, oldest sample first.

        Read-only and non-destructive, unlike `take_level`: the spectrum is
        "what this voice sounds like at this instant" rather than "what has
        happened since you last asked", so there is no frame to steal and
        nothing to clear. Allocates one copy per call — ~2KB, thirty times a
        second, on the loop thread.
        """
        i = self._ring_i
        return np.concatenate((self._ring[i:], self._ring[:i]))

    @property
    def buffered_seconds(self) -> float:
        return len(self._buffer) / self.sample_rate

    @property
    def drained(self) -> bool:
        """Finished speaking: no audio left and none coming."""
        return self._finished and len(self._buffer) == 0

    # ----------------------------------------------------------- audio thread

    def _record(self, block: np.ndarray) -> None:
        """Copy one rendered block into the spectrum ring. Audio thread.

        Silence counts and is written like anything else — a ring left holding
        the last thing an agent said would leave their orb frozen mid-syllable
        for the rest of the show.
        """
        size = self._ring.size
        n = len(block)
        if n >= size:
            self._ring[:] = block[n - size :]
            self._ring_i = 0
            return
        end = self._ring_i + n
        if end <= size:
            self._ring[self._ring_i : end] = block
        else:
            split = size - self._ring_i
            self._ring[self._ring_i :] = block[:split]
            self._ring[: end - size] = block[split:]
        self._ring_i = end % size

    def render(self, frames: int, tap: PlayedTap | None = None) -> np.ndarray:
        gain = self.envelope.render(frames)
        if len(self._buffer) == 0:
            self.active = False
            if tap is not None:
                # Silence is still a block that was played, and the tap's one
                # consumer (a transcription session — see `Mixer.on_played`)
                # needs the gaps: an endpointer with no trailing silence leaves
                # the last sentence of every turn sitting as a partial until
                # the agent's *next* turn pushes audio in behind it.
                tap(self.agent_id, b"\x00\x00" * frames)
            silence = np.zeros(frames, dtype=np.float32)
            self._record(silence)
            return silence
        self.active = True
        take = min(frames, len(self._buffer))
        chunk = np.zeros(frames, dtype=np.float32)
        chunk[:take] = self._buffer[:take]
        self._buffer = self._buffer[take:]
        if tap is not None:
            # Pre-gain, deliberately — unlike the meter below. The tap's job is
            # *what was said and when*, and the gain envelope is about how loud
            # the room heard it: a ducked agent is still talking and its words
            # are still owed to the transcript band, so feeding a recogniser
            # -12dB of it would only cost accuracy and buy nothing. A stop
            # still cuts the tap within the ramp, because `Mixer.render` drops
            # the buffer once the ramp has been heard and this branch then
            # stops being taken at all.
            tap(self.agent_id, (chunk * 32767.0).astype(np.int16).tobytes())
        out = chunk * gain
        # Metered *after* gain, so a ducked agent visibly shrinks on the video
        # wall and a stopped one collapses with the ramp. What the wall shows
        # is what the room hears — that is the whole claim the orb makes, and
        # metering pre-gain would quietly break it.
        #
        # One sqrt on 256 floats, inside a callback that must not block. It
        # costs a few microseconds against a 16ms budget; a float store is
        # atomic enough for a meter that is allowed to miss a block.
        self._level = max(self._level, float(np.sqrt(np.mean(np.square(out)))))
        # Post-gain for the same reason, and the same claim: the corona on the
        # wall is the spectrum of what the room is hearing, so a ducked agent's
        # bars come down with their voice.
        self._record(out)
        return out


class Mixer:
    """Sums every agent voice into the output bus.

    Args:
        agent_ids: Every voice on the bus.
        sample_rate: Output rate. Must match the TTS decode rate.
        unity_db: Per-persona resting trim, if any.
        on_played: Optional tap, called from the audio thread once per agent
            per output block with that block's PCM16. This is the *playback*
            clock, and that is the whole reason it exists here rather than at
            the point audio arrives from the provider. `feed()` is a
            concatenate onto an unbounded buffer and TTS generates faster than
            anyone speaks, so a turn's audio can sit here seconds deep
            (`buffered_seconds`); anything timed off arrival is therefore
            timed off generation. `render` is the only place in this repo that
            runs at the rate the room actually hears — the same argument the
            post-gain meter below makes, applied to words instead of levels.

            Called with the lock held, from the callback, so it must not
            block, must not raise, and must not touch the loop directly.
            `panel_runtime.stt.PushAudioSource.feed` is built for exactly
            this contract and is the only caller today. Left `None`, none of
            this costs anything.
    """

    def __init__(
        self,
        agent_ids: tuple[str, ...],
        sample_rate: int,
        unity_db: dict[str, float] | None = None,
        on_played: PlayedTap | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        # Public and reassignable: the runtime drops the tap if the video wall
        # turns out not to bind, and a wall-less show must not pay for it.
        self.on_played = on_played
        gains = unity_db or {}
        window = _window_length(sample_rate)
        self.voices = {a: AgentVoice(a, sample_rate, gains.get(a, 0.0), window) for a in agent_ids}
        # Both derived from the rate once, here, because both are pure
        # functions of it and `take_bands` runs thirty times a second.
        self._window = np.hanning(window).astype(np.float32)
        self._edges = _band_edges(sample_rate, window)
        self._lock = threading.Lock()
        # Set when a stop is ramping out, so the buffer is dropped only after
        # the ramp has actually been rendered — otherwise the stop clicks.
        self._stopping: set[str] = set()

    # ------------------------------------------------------------ loop thread

    def feed(self, agent_id: str, pcm: bytes) -> None:
        with self._lock:
            voice = self.voices.get(agent_id)
            if voice is not None and agent_id not in self._stopping:
                voice.append(pcm)

    def finish(self, agent_id: str) -> None:
        with self._lock:
            voice = self.voices.get(agent_id)
            if voice is not None:
                voice.mark_finished()

    def duck(self, agent_id: str, gain_db: float, ramp_ms: int) -> None:
        with self._lock:
            voice = self.voices.get(agent_id)
            if voice is not None:
                voice.envelope.ramp_to(gain_db, ramp_ms)

    def resume(self, agent_id: str, ramp_ms: int) -> None:
        with self._lock:
            voice = self.voices.get(agent_id)
            if voice is not None:
                voice.envelope.ramp_to(voice.unity_db, ramp_ms)

    def stop(self, agent_id: str, ramp_ms: float = STOP_RAMP_MS) -> None:
        """Silence an agent mid-utterance. The audible half of `StopSpeech`."""
        with self._lock:
            voice = self.voices.get(agent_id)
            if voice is None:
                return
            voice.envelope.ramp_to(SILENCE_DB, ramp_ms)
            self._stopping.add(agent_id)

    def clear(self, agent_id: str) -> None:
        """Drop buffered audio and restore unity gain, ready for the next turn."""
        with self._lock:
            voice = self.voices.get(agent_id)
            if voice is not None:
                voice.reset()
            self._stopping.discard(agent_id)

    def is_drained(self, agent_id: str) -> bool:
        with self._lock:
            voice = self.voices.get(agent_id)
            return voice is None or voice.drained

    def is_playing(self, agent_id: str) -> bool:
        """Whether this voice's most recent `render()` call played real audio.

        A plain read, not a peak-and-clear like `take_levels` — nothing else
        consumes `AgentVoice.active`, so there is no frame to steal by calling
        this from more than one place. A caller reading it from inside the
        audio callback sees the *previous* block's answer (one block, ~5-20ms,
        behind "now"), since `render()` for the current block has not run yet.
        """
        with self._lock:
            voice = self.voices.get(agent_id)
            return voice is not None and voice.active

    def take_levels(self) -> dict[str, float]:
        """Every voice's peak since the last call. Drives the video wall's orbs.

        Read-and-clear, so calling this from anywhere other than the one
        display pump will quietly steal frames from it. There is exactly one
        caller (`PanelRuntime._pump_levels`) and there should stay exactly one.
        """
        with self._lock:
            return {agent_id: voice.take_level() for agent_id, voice in self.voices.items()}

    def take_bands(self) -> dict[str, list[float]]:
        """Every voice's frequency spectrum right now. Drives the orbs' coronas.

        `SPECTRUM_BANDS` amplitudes per voice, log-spaced across the speech
        range, in the same 0-1 units as `take_levels`.

        Not read-and-clear, unlike the meter beside it: this is an instant
        rather than an interval, so a second caller costs a second transform
        and steals nothing. The lock is held only for the copy — the FFT runs
        outside it, which is the whole reason the snapshot is taken first.
        """
        with self._lock:
            recent = {agent_id: voice.recent() for agent_id, voice in self.voices.items()}
        return {
            agent_id: _spectrum(samples, self._window, self._edges)
            for agent_id, samples in recent.items()
        }

    def buffered_seconds(self, agent_id: str) -> float:
        """How much audio is queued but not yet played.

        Liveness evidence for the heartbeat (`PanelRuntime._pump_heartbeat`):
        audio sitting here is audio the room is about to hear, so a turn
        draining its tail is making progress even though no new chunk has
        arrived from the provider. Without this the end of every turn reads as
        a stall.
        """
        with self._lock:
            voice = self.voices.get(agent_id)
            return 0.0 if voice is None else voice.buffered_seconds

    # ----------------------------------------------------------- audio thread

    def render(self, frames: int) -> np.ndarray:
        """Called from the PortAudio callback. Must not block or allocate much."""
        out = np.zeros(frames, dtype=np.float32)
        tap = self.on_played
        with self._lock:
            for agent_id, voice in self.voices.items():
                out += voice.render(frames, tap)
                # The stop ramp has completed and been heard — now drop the rest.
                if agent_id in self._stopping and voice.envelope.at_target:
                    voice.reset()
                    self._stopping.discard(agent_id)
        # Lanes rarely sum — nothing deliberately overlaps two agents since
        # agent-to-agent interrupts were removed — but a draining tail under a
        # new grant can. Soft-limit rather than hard-clip: a hard `np.clip`
        # here was audible as clipping on ordinary single-voice peaks close
        # to full scale, not just on the rare overlap it was meant to catch.
        return np.clip(_soft_limit(out), -1.0, 1.0)
