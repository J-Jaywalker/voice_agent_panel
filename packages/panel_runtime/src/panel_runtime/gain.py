"""Sample-accurate gain envelope.

The mixer's job during barge-in is to move an agent's gain to a target over a
ramp, without clicks. Ducking is a gain change, not a stop — see ADR 0001.
"""

from __future__ import annotations

import numpy as np


class GainEnvelope:
    """Linear-in-dB ramp toward a target gain, evaluated per sample."""

    def __init__(self, sample_rate: int, initial_db: float = 0.0) -> None:
        self.sample_rate = sample_rate
        self._current = _db_to_lin(initial_db)
        self._target = self._current
        self._step = 0.0

    @property
    def current_db(self) -> float:
        return _lin_to_db(self._current)

    @property
    def at_target(self) -> bool:
        return abs(self._current - self._target) < 1e-6

    def ramp_to(self, target_db: float, ramp_ms: float) -> None:
        self._target = _db_to_lin(target_db)
        samples = max(1, int(self.sample_rate * ramp_ms / 1000.0))
        self._step = (self._target - self._current) / samples

    def render(self, frames: int) -> np.ndarray:
        """Return the gain curve for the next `frames` samples and advance."""
        if self.at_target:
            self._current = self._target
            return np.full(frames, self._current, dtype=np.float32)

        curve = self._current + self._step * np.arange(1, frames + 1, dtype=np.float32)
        lo, hi = sorted((self._current, self._target))
        curve = np.clip(curve, lo, hi)
        self._current = float(curve[-1])
        return curve.astype(np.float32)


def _db_to_lin(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def _lin_to_db(lin: float) -> float:
    return -120.0 if lin <= 1e-6 else float(20.0 * np.log10(lin))
