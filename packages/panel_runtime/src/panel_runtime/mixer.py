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
"""

from __future__ import annotations

import threading

import numpy as np

from .gain import GainEnvelope

# A stop should be inaudible as a transition but immediate as a silence.
STOP_RAMP_MS = 20.0
SILENCE_DB = -80.0


class AgentVoice:
    """One agent's audio buffer and gain, on the output bus."""

    def __init__(self, agent_id: str, sample_rate: int) -> None:
        self.agent_id = agent_id
        self.sample_rate = sample_rate
        self.envelope = GainEnvelope(sample_rate, 0.0)
        self._buffer = np.zeros(0, dtype=np.float32)
        self._finished = False  # TTS delivered everything it is going to
        # Loudest block RMS since the meter was last read. See `take_level`.
        self._level = 0.0

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
        self.envelope = GainEnvelope(self.sample_rate, 0.0)

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

    @property
    def buffered_seconds(self) -> float:
        return len(self._buffer) / self.sample_rate

    @property
    def drained(self) -> bool:
        """Finished speaking: no audio left and none coming."""
        return self._finished and len(self._buffer) == 0

    # ----------------------------------------------------------- audio thread

    def render(self, frames: int) -> np.ndarray:
        gain = self.envelope.render(frames)
        if len(self._buffer) == 0:
            return np.zeros(frames, dtype=np.float32)
        take = min(frames, len(self._buffer))
        chunk = np.zeros(frames, dtype=np.float32)
        chunk[:take] = self._buffer[:take]
        self._buffer = self._buffer[take:]
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
        return out


class Mixer:
    """Sums every agent voice into the output bus."""

    def __init__(self, agent_ids: tuple[str, ...], sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.voices = {a: AgentVoice(a, sample_rate) for a in agent_ids}
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
                voice.envelope.ramp_to(0.0, ramp_ms)

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

    def take_levels(self) -> dict[str, float]:
        """Every voice's peak since the last call. Drives the video wall's orbs.

        Read-and-clear, so calling this from anywhere other than the one
        display pump will quietly steal frames from it. There is exactly one
        caller (`PanelRuntime._pump_levels`) and there should stay exactly one.
        """
        with self._lock:
            return {agent_id: voice.take_level() for agent_id, voice in self.voices.items()}

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
        with self._lock:
            for agent_id, voice in self.voices.items():
                out += voice.render(frames)
                # The stop ramp has completed and been heard — now drop the rest.
                if agent_id in self._stopping and voice.envelope.at_target:
                    voice.reset()
                    self._stopping.discard(agent_id)
        # Lanes rarely sum — nothing deliberately overlaps two agents since
        # agent-to-agent interrupts were removed — but a draining tail under a
        # new grant can. Clip rather than let the PA do it for us.
        return np.clip(out, -1.0, 1.0)
