"""Acoustic echo cancellation — cancels this Mac's own speakers from its own mic.

Built on `speexdsp`'s `EchoCanceller`, a real-time adaptive filter (the class
of cost it was designed to run per-frame, not an FFT — see `mixer.py`'s note
on why the spectrum transform stays *out* of the audio callback). This one
runs inside it, like the RMS meter beside it.

Requires the native `libspeexdsp` library at install time, e.g. on this Mac:

    brew install swig speexdsp
    CFLAGS="-I$(brew --prefix speexdsp)/include" \\
    LDFLAGS="-L$(brew --prefix speexdsp)/lib" \\
    uv add --package panel-runtime speexdsp

`speexdsp` is a compiled SWIG extension against that library, not a pure
wheel — `swig` and the `-I`/`-L` flags above are needed only to *build* it,
once, on the machine that will run the show. See CLAUDE.md § Deployment.

Far-end reference is whatever `Mixer.render` actually wrote to `outdata` —
not a copy taken earlier in the TTS pipeline — because that is the signal
actually reaching the room. Same reasoning `Mixer.on_played`'s docstring
gives for timing the video wall's transcript off `render` rather than
`feed()`, applied here to cancellation instead of transcription.
"""

from __future__ import annotations

import numpy as np
from speexdsp import EchoCanceller as _SpeexEchoCanceller

from .config import AECConfig


class _FarendRing:
    """Delay line of rendered output, read back one `frame_size` block at a
    time, `delay_frames` behind whatever was most recently written.

    Pure bookkeeping — no audio processing — so it is tested directly, apart
    from `EchoCanceller`'s dependency on the native `speexdsp` extension.
    """

    def __init__(self, delay_frames: int, frame_size: int) -> None:
        self.frame_size = frame_size
        self.delay_frames = delay_frames
        # One extra block of slack so a write and a same-offset read never
        # alias each other's half-written frame.
        self._ring = np.zeros(delay_frames + frame_size, dtype=np.float32)
        self._write_i = 0
        # How many frames have ever been written — lets `read` signal "not
        # enough history yet" rather than handing back the zeros it started
        # with as though they were a real delayed sample.
        self._filled = 0

    def push(self, block: np.ndarray) -> None:
        n = len(block)
        size = self._ring.size
        end = self._write_i + n
        if end <= size:
            self._ring[self._write_i : end] = block
        else:
            split = size - self._write_i
            self._ring[self._write_i :] = block[:split]
            self._ring[: end - size] = block[split:]
        self._write_i = end % size
        self._filled = min(size, self._filled + n)

    def read(self) -> np.ndarray | None:
        """The block `delay_frames` behind the latest write, or `None` if the
        ring does not yet hold that much history."""
        if self._filled < self.delay_frames + self.frame_size:
            return None
        size = self._ring.size
        read_i = (self._write_i - self.delay_frames - self.frame_size) % size
        end = read_i + self.frame_size
        if end <= size:
            return self._ring[read_i:end]
        return np.concatenate((self._ring[read_i:], self._ring[: end - size]))


class EchoCanceller:
    """Cancels a known far-end reference out of a near-end (mic) signal.

    Both `push_farend` and `process` are called from the PortAudio callback,
    once per block, in that order relative to `Mixer.render` — see
    `PanelRuntime._callback`. Neither blocks or allocates more than one
    fixed-size buffer per call.
    """

    def __init__(self, cfg: AECConfig, sample_rate: int, frame_size: int) -> None:
        self.frame_size = frame_size
        filter_length = _round_up(int(cfg.filter_length_ms * sample_rate / 1000), frame_size)
        delay_frames = _round_up(int(cfg.delay_ms * sample_rate / 1000), frame_size)
        self._ring = _FarendRing(delay_frames, frame_size)
        self._ec = _SpeexEchoCanceller.create(frame_size, filter_length, sample_rate)

    # ----------------------------------------------------------- audio thread

    def push_farend(self, rendered: np.ndarray) -> None:
        """Record one block of what was just sent to the DAC. Audio thread."""
        self._ring.push(rendered)

    def process(self, near: np.ndarray) -> np.ndarray:
        """Cancel the delayed far-end reference out of one mic block.

        Passthrough, unchanged, until the ring holds a full delay's worth of
        history — the first `delay_ms` of audio each run is uncancelled
        rather than processed against silence it would mistake for echo.
        """
        far = self._ring.read()
        if far is None:
            return near
        near_i16 = (np.clip(near, -1.0, 1.0) * 32767).astype(np.int16)
        far_i16 = (np.clip(far, -1.0, 1.0) * 32767).astype(np.int16)
        cleaned = self._ec.process(near_i16.tobytes(), far_i16.tobytes())
        return np.frombuffer(cleaned, dtype=np.int16).astype(np.float32) / 32768.0


def _round_up(value: int, multiple: int) -> int:
    return ((value + multiple - 1) // multiple) * multiple
