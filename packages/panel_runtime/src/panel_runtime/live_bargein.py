"""Live barge-in harness — speak into your mic and hear the agent duck.

    uv run barge-in

Exercises the real `panel_core` FloorController against real audio: Silero VAD
on your mic, an "agent" talking continuously, and the duck / resume / stop
behaviour from ADR 0001. Reports true ADC-to-DAC latency using PortAudio's
hardware timestamps — no loopback device required.

⚠️  WEAR HEADPHONES. On open speakers the agent's own audio reaches the mic and
    the VAD triggers on it — exactly the feedback loop FEASIBILITY.md §3.1 warns
    about, and exactly why the venue needs a pre-PA mic split.

Without STT this classifies on duration alone: a short burst ducks and resumes,
a sustained one stops the agent. Content-based classification (saying "mm-hm"
vs "sorry, hold on") arrives with S0.5.
"""

from __future__ import annotations

import argparse
import asyncio
import queue
import subprocess
import threading
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
from livekit import rtc
from livekit.agents import vad as lkvad
from livekit.plugins import silero
from panel_core import (
    AgentSpeechStarted,
    DuckSpeech,
    FloorConfig,
    FloorController,
    HumanSpeechEnded,
    HumanSpeechStarted,
    PanelCast,
    PanelState,
    ResumeSpeech,
    StopSpeech,
    Tick,
)
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .config import BargeInConfig
from .gain import GainEnvelope

SR = 16_000
AGENT_SCRIPT = (
    "Look, the thing everyone gets wrong about agent adoption is that they "
    "measure capability when they should be measuring trust. I have watched "
    "three organisations deploy the same system. Two of them got nowhere, not "
    "because the technology failed, but because nobody could agree who was "
    "accountable when it made a call. That is not an engineering problem. "
    "The speed of an agent far outpaces the speed of the committee that has to "
    "approve what the agent did, and until that changes we are all just "
    "building very fast machines that wait."
)
console = Console()


def agent_loop_audio(cache: Path) -> np.ndarray:
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        console.print("[dim]generating agent audio…[/]")
        subprocess.run(
            ["say", "-o", str(cache), f"--data-format=LEI16@{SR}", AGENT_SCRIPT],
            check=True,
        )
    with wave.open(str(cache)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


class Harness:
    def __init__(self, block: int, cfg: BargeInConfig) -> None:
        self.block, self.cfg = block, cfg
        self.audio = agent_loop_audio(Path(".cache/agent_loop.wav"))
        self.pos = 0
        self.env = GainEnvelope(SR, 0.0)
        self.stopped = False

        self.cast = PanelCast.from_dir(Path("personas"))
        self.fc = FloorController(self.cast, FloorConfig())
        self.state = PanelState.for_agents(self.cast.ids())
        self.state, _ = self.fc.reduce(self.state, AgentSpeechStarted(t=0.0, agent="wayne"))

        self.q: queue.Queue = queue.Queue()
        self.blocks: list[tuple[int, float]] = []  # (start_sample, adc_time)
        self.pushed = 0
        self.lock = threading.Lock()

        self.pending_adc: float | None = None  # adc time of the onset sample
        self.latency_ms: float | None = None
        self.worst_ms: float = 0.0
        self.prob = 0.0
        self.events: list[str] = []
        self.clock = 0.0

    # ---------------------------------------------------------- audio thread

    def callback(self, indata, outdata, frames, timeinfo, status) -> None:
        with self.lock:
            self.blocks.append((self.pushed, timeinfo.inputBufferAdcTime))
            if len(self.blocks) > 200:
                del self.blocks[:100]
        self.pushed += frames
        self.q.put_nowait(indata[:, 0].copy())

        gain = self.env.render(frames)
        if self.stopped:
            outdata[:, 0] = 0.0
            return
        end = self.pos + frames
        if end <= len(self.audio):
            chunk = self.audio[self.pos : end]
        else:
            wrap = end - len(self.audio)
            chunk = np.concatenate([self.audio[self.pos :], self.audio[:wrap]])
            end = wrap
        self.pos = end
        outdata[:, 0] = (chunk.astype(np.float32) / 32768.0) * gain

        # First output block carrying a changed gain: the duck has landed.
        with self.lock:
            if self.pending_adc is not None and abs(gain[-1] - gain[0]) > 1e-7:
                self.latency_ms = (timeinfo.outputBufferDacTime - self.pending_adc) * 1000
                self.worst_ms = max(self.worst_ms, self.latency_ms)
                self.pending_adc = None

    def adc_time_for(self, sample_index: int) -> float | None:
        with self.lock:
            for start, adc in reversed(self.blocks):
                if sample_index >= start:
                    return adc + (sample_index - start) / SR
        return None

    # ---------------------------------------------------------- logic thread

    def apply(self, commands) -> None:
        for c in commands:
            if isinstance(c, DuckSpeech):
                self.env.ramp_to(c.gain_db, c.ramp_ms)
                self.log(f"[yellow]DUCK[/]  {c.gain_db:+.0f}dB over {c.ramp_ms}ms")
            elif isinstance(c, ResumeSpeech):
                self.env.ramp_to(0.0, c.ramp_ms)
                self.log("[green]RESUME[/] backchannel — agent keeps the floor")
            elif isinstance(c, StopSpeech):
                self.stopped = True
                self.env.ramp_to(0.0, 1)
                self.log(f"[red]STOP[/]  {c.reason.value}")

    def log(self, msg: str) -> None:
        self.events.append(msg)
        del self.events[:-8]

    async def run_vad(self) -> None:
        detector = silero.VAD.load(
            sample_rate=SR,
            min_speech_duration=self.cfg.min_speech_duration,
            min_silence_duration=self.cfg.min_silence_duration,
            activation_threshold=self.cfg.activation_threshold,
        )
        stream = detector.stream()
        speaking = False

        async def pump() -> None:
            while True:
                try:
                    chunk = self.q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.002)
                    continue
                pcm = (np.clip(chunk, -1, 1) * 32767).astype(np.int16)
                stream.push_frame(rtc.AudioFrame(pcm.tobytes(), SR, 1, len(pcm)))

        asyncio.create_task(pump())

        async for ev in stream:
            self.clock += 0.0
            if ev.type == lkvad.VADEventType.INFERENCE_DONE:
                self.prob = ev.probability
                if not speaking and ev.probability >= self.cfg.duck_probability:
                    speaking = True
                    with self.lock:
                        self.pending_adc = self.adc_time_for(ev.samples_index)
                    self.clock = ev.timestamp
                    self.state, cmds = self.fc.reduce(
                        self.state, HumanSpeechStarted(t=ev.timestamp)
                    )
                    self.apply(cmds)
            elif ev.type == lkvad.VADEventType.END_OF_SPEECH:
                speaking = False
                self.clock = ev.timestamp
                self.state, cmds = self.fc.reduce(self.state, HumanSpeechEnded(t=ev.timestamp))
                self.apply(cmds)

    async def run_ticks(self) -> None:
        while True:
            await asyncio.sleep(0.02)
            self.clock += 0.02
            self.state, cmds = self.fc.reduce(self.state, Tick(t=self.clock))
            self.apply(cmds)

    # ---------------------------------------------------------------- render

    def view(self) -> Panel:
        bar = int(self.prob * 30)
        colour = "red" if self.prob >= self.cfg.duck_probability else "grey50"
        status = (
            "[red]STOPPED[/]"
            if self.stopped
            else ("[yellow]DUCKED[/]" if self.env.current_db < -1 else "[green]SPEAKING[/]")
        )
        t = Table.grid(padding=(0, 2))
        t.add_row("agent", f"{status}   gain {self.env.current_db:+6.1f} dB")
        t.add_row("VAD", f"[{colour}]{'█' * bar}{'·' * (30 - bar)}[/] {self.prob:.2f}")
        last = f"{self.latency_ms:.1f} ms" if self.latency_ms else "—"
        worst = f"{self.worst_ms:.1f} ms" if self.worst_ms else "—"
        verdict = (
            "[green]within 150ms[/]"
            if self.worst_ms and self.worst_ms <= 150
            else ("[red]OVER 150ms[/]" if self.worst_ms else "")
        )
        t.add_row("ADC→DAC", f"last {last}   worst {worst}   {verdict}")
        return Panel(
            Group(t, "", *self.events),
            title="barge-in · speak to interrupt · ctrl-c to quit",
            subtitle="[dim]headphones required[/]",
        )


async def main_async(args) -> None:
    h = Harness(args.block, BargeInConfig())
    console.print(f"[dim]in={sd.query_devices(args.input_device, 'input')['name']}[/]")
    with sd.Stream(
        samplerate=SR,
        blocksize=args.block,
        channels=(1, 1),
        dtype="float32",
        device=(args.input_device, args.output_device),
        callback=h.callback,
        latency="low",
    ):
        asyncio.create_task(h.run_vad())
        asyncio.create_task(h.run_ticks())
        with Live(h.view(), console=console, refresh_per_second=20) as live:
            while True:
                await asyncio.sleep(0.05)
                live.update(h.view())


def main() -> None:
    p = argparse.ArgumentParser(prog="barge-in")
    p.add_argument("--block", type=int, default=128, help="frames per buffer (8ms @16k)")
    p.add_argument("--input-device", default=None)
    p.add_argument("--output-device", default=None)
    p.add_argument("--list-devices", action="store_true")
    args = p.parse_args()
    if args.list_devices:
        console.print(sd.query_devices())
        return
    for name in ("input_device", "output_device"):
        v = getattr(args, name)
        if v is not None and v.isdigit():
            setattr(args, name, int(v))
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")


if __name__ == "__main__":
    main()
