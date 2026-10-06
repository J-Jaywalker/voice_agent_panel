"""Live barge-in harness — speak into your mic and hear the agent duck.

    uv run barge-in

Exercises the real `panel_core` FloorController against real audio: a live
Speechmatics Agent STT session on your mic, an "agent" talking continuously, and
the duck / resume / stop behaviour from ADR 0001.

**This is the measurement tool for the one number the 6 Oct 2026 change left
unverified.** The barge-in reflex now fires on Speechmatics' `SpeechStarted`
rather than a local Silero VAD, so onset-to-duck is a network round-trip whose
latency is documented nowhere for `/v2/agent`. Silero's measured 66.8ms does not
transfer and must not be quoted. Run this on the venue rig and read the two
numbers it prints:

* **onset → SpeechStarted** — the new term. Voice onset at the ADC to
  `HumanSpeechStarted` arriving off the socket.
* **onset → DAC** — the whole budget, onset to ducked gain reaching an output
  buffer, against `BargeInConfig.hard_limit_ms` (a target, not a result).

Needs `SPEECHMATICS_API_KEY`.

⚠️  WEAR HEADPHONES. On open speakers the agent's own audio reaches the mic and
    the endpointer triggers on it — exactly the feedback loop FEASIBILITY.md
    §3.1 warns about, and exactly why the venue needs a pre-PA mic split.

Two simplifications, both deliberate:

* **Onset is found by an RMS threshold**, not by a detector. It is only a
  reference marker for the measurement — the thing being measured is the
  socket's answer, so timing it against another detector would measure the
  difference between two detectors. An energy gate is unfit as a production
  reflex (it fires on a desk tap) and fine as a stopwatch in a quiet room with
  an operator speaking deliberately. `--onset-rms` tunes it; the UI shows the
  live level so it can be set by eye.
* **No STT-free mode.** The old harness classified on duration alone because it
  had no transcript. This one has a real session, so content-based
  classification is live and "mm-hm" vs "sorry, hold on" behaves as on stage.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
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
    TranscriptUpdated,
)
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .config import PIPELINE_SAMPLE_RATE, BargeInConfig
from .gain import GainEnvelope
from .stt import PanelSTT, STTConfig

SR = PIPELINE_SAMPLE_RATE
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
    def __init__(self, block: int, cfg: BargeInConfig, stt: PanelSTT, onset_rms: float) -> None:
        self.block, self.cfg, self.stt = block, cfg, stt
        self.onset_rms = onset_rms
        self.audio = agent_loop_audio(Path(".cache/agent_loop.wav"))
        self.pos = 0
        self.env = GainEnvelope(SR, 0.0)
        self.stopped = False

        self.cast = PanelCast.from_dir(Path("personas"))
        self.fc = FloorController(self.cast, FloorConfig())
        self.state = PanelState.for_agents(self.cast.ids())
        self.state, _ = self.fc.reduce(self.state, AgentSpeechStarted(t=0.0, agent="wayne"))

        self.lock = threading.Lock()
        # Onset marker, in `time.monotonic()`. Set by the callback's RMS gate,
        # read by both consumers below, cleared once both have been paid.
        self.onset_mono: float | None = None
        self.onset_pending_socket = False
        self.onset_pending_dac = False
        self.level = 0.0
        self.quiet_blocks = 0

        self.socket_ms: float | None = None
        self.socket_worst = 0.0
        self.dac_ms: float | None = None
        self.dac_worst = 0.0
        self.events: list[str] = []
        self.clock = 0.0

    # ---------------------------------------------------------- audio thread

    def callback(self, indata, outdata, frames, timeinfo, status) -> None:
        del status
        now = time.monotonic()
        mono = indata[:, 0]
        rms = float(np.sqrt(np.mean(np.square(mono))))
        self.level = rms

        pcm = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)
        self.stt.feed("ricky", pcm.tobytes())

        # PortAudio's ADC timestamp is in stream time; `now` is monotonic. One
        # subtraction puts them in the same base — this correlation is the
        # whole reason the number below is a true onset latency and not a
        # latency-plus-one-buffer estimate.
        adc_mono = now + (timeinfo.inputBufferAdcTime - timeinfo.currentTime)
        dac_mono = now + (timeinfo.outputBufferDacTime - timeinfo.currentTime)

        with self.lock:
            if rms < self.onset_rms:
                self.quiet_blocks += 1
            else:
                # Re-arm only after a gap, so one utterance marks one onset.
                if self.quiet_blocks * frames / SR > 0.5:
                    self.onset_mono = adc_mono
                    self.onset_pending_socket = True
                    self.onset_pending_dac = True
                self.quiet_blocks = 0

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
            if (
                self.onset_pending_dac
                and self.onset_mono is not None
                and abs(gain[-1] - gain[0]) > 1e-7
            ):
                self.dac_ms = (dac_mono - self.onset_mono) * 1000
                self.dac_worst = max(self.dac_worst, self.dac_ms)
                self.onset_pending_dac = False

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

    async def run_stt(self) -> None:
        """The real thing: every floor event here came off the socket."""
        while True:
            event = await self.stt.events.get()
            arrived = time.monotonic()
            if isinstance(event, HumanSpeechStarted):
                with self.lock:
                    if self.onset_pending_socket and self.onset_mono is not None:
                        self.socket_ms = (arrived - self.onset_mono) * 1000
                        self.socket_worst = max(self.socket_worst, self.socket_ms)
                        self.onset_pending_socket = False
                self.log("[cyan]SpeechStarted[/]")
            elif isinstance(event, HumanSpeechEnded):
                self.log("[dim]SpeechEnded[/]")
            elif isinstance(event, TranscriptUpdated) and event.is_final:
                self.log(f"[dim]“{event.text}”[/]")
            self.clock = arrived
            self.state, cmds = self.fc.reduce(self.state, event)
            self.apply(cmds)

    async def run_ticks(self) -> None:
        while True:
            await asyncio.sleep(0.02)
            self.state, cmds = self.fc.reduce(self.state, Tick(t=time.monotonic()))
            self.apply(cmds)

    # ---------------------------------------------------------------- render

    def view(self) -> Panel:
        bar = min(30, int(self.level / max(self.onset_rms, 1e-6) * 15))
        colour = "red" if self.level >= self.onset_rms else "grey50"
        status = (
            "[red]STOPPED[/]"
            if self.stopped
            else ("[yellow]DUCKED[/]" if self.env.current_db < -1 else "[green]SPEAKING[/]")
        )

        def pair(last: float | None, worst: float) -> str:
            lo = f"{last:.1f} ms" if last else "—"
            hi = f"{worst:.1f} ms" if worst else "—"
            return f"last {lo}   worst {hi}"

        limit = self.cfg.hard_limit_ms
        verdict = (
            ""
            if not self.dac_worst
            else (
                f"[green]within {limit:.0f}ms target[/]"
                if self.dac_worst <= limit
                else f"[red]OVER {limit:.0f}ms target[/]"
            )
        )
        t = Table.grid(padding=(0, 2))
        t.add_row("agent", f"{status}   gain {self.env.current_db:+6.1f} dB")
        t.add_row("mic", f"[{colour}]{'█' * bar}{'·' * (30 - bar)}[/] rms {self.level:.3f}")
        t.add_row("onset→SpeechStarted", pair(self.socket_ms, self.socket_worst))
        t.add_row("onset→DAC", f"{pair(self.dac_ms, self.dac_worst)}   {verdict}")
        return Panel(
            Group(t, "", *self.events),
            title="barge-in · speak to interrupt · ctrl-c to quit",
            subtitle="[dim]headphones required · live Speechmatics session[/]",
        )


async def main_async(args) -> None:
    cast = PanelCast.from_dir(Path("personas"))
    stt = PanelSTT({"ricky": "human"}, config=STTConfig.from_cast(cast))
    h = Harness(args.block, BargeInConfig(), stt, args.onset_rms)
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
        await stt.start()
        try:
            asyncio.create_task(h.run_stt())
            asyncio.create_task(h.run_ticks())
            with Live(h.view(), console=console, refresh_per_second=20) as live:
                while True:
                    await asyncio.sleep(0.05)
                    live.update(h.view())
        finally:
            await stt.stop()


def main() -> None:
    p = argparse.ArgumentParser(prog="barge-in")
    p.add_argument("--block", type=int, default=128, help="frames per buffer (8ms @16k)")
    p.add_argument(
        "--onset-rms",
        type=float,
        default=0.02,
        help="mic RMS counted as voice onset — the measurement's reference mark, not a detector",
    )
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
