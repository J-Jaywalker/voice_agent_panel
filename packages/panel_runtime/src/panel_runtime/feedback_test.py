"""Feedback test — loops every agent's real voice through the speakers while
the real speaker-gated mic pipeline listens, flagging anything that could be
actioned as a barge-in while a clip is playing.

    uv run feedback-test
    uv run feedback-test --aec --aec-delay-ms 40
    uv run feedback-test --no-speaker-lock   # skip enrolment, faster iteration

Dexter, Melia and Wayne's `introduction` lines, in their real ElevenLabs
voices, ship as committed WAVs under `assets/feedback/` (see
`generate_feedback_assets.py`) so this needs no network or API key to run.
They loop in cast order with a `--silence-s` gap after each — real speech and
real silence, not noise, since the thing under test is whether *this signal*
gets transcribed as words.

Two detectors can turn a clip's bleed into a barge-in, and enrolment affects
them differently:

* **VAD** (`_print_vad`) is identity-blind by design — it owns stopping and
  never checks who. Enrolment cannot prevent this one either way, so it
  flags the same regardless of speaker lock.
* **STT** (`_print_gated_stt`) only produces `Ricky:` for diarised, enrolled
  segments. With speaker lock on, a `Ricky:` line during playback is a
  diarisation failure; with `--no-speaker-lock` it's expected — every voice
  counts as Ricky's, by design.

Nothing non-actioned is printed — no raw transcript the floor would never
read, no "dropped" line for a rejection that worked as intended. Only a
flagged `TRANSCRIBED BARGE-IN` while a clip is playing, or a plain
confirmation line for your own real speech, so you can still tell the mic
itself works.

Deliberately does not touch `FloorController` or any brain — nothing here can
grant an agent the floor or spend an API token on Claude.
"""

from __future__ import annotations

import argparse
import asyncio
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
from panel_core import PanelCast
from rich.console import Console

from .aec import EchoCanceller
from .config import VAD_SAMPLE_RATE, AECConfig
from .enrolment import DEFAULT_STORE_PATH, SpeakerEnrolment, SpeakerStore
from .stt import PanelSTT, STTConfig

console = Console()
SR = VAD_SAMPLE_RATE
# Committed, not `.cache/` (gitignored) — the point is no network needed.
ASSETS_DIR = Path(__file__).parent / "assets" / "feedback"


def _print(markup: str) -> None:
    console.print(f"[dim]{asyncio.get_running_loop().time():7.2f}s[/] {markup}")


def _load_clip(persona) -> np.ndarray:
    """One persona's `introduction`, as float32 PCM at `SR`."""
    path = ASSETS_DIR / f"{persona.id}.wav"
    if not path.exists():
        raise FileNotFoundError(
            f"no committed clip for {persona.id!r} at {path} — run "
            "`uv run generate-feedback-assets` first (needs ELEVENLABS_API_KEY, "
            "one-time per persona)"
        )
    with wave.open(str(path)) as w:
        pcm = w.readframes(w.getnframes())
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def _build_cycle(cast, silence_s: float) -> tuple[np.ndarray, list[tuple[str | None, float]]]:
    """Every persona's clip in cast order, each followed by a silence gap.

    Also returns a `(playing, duration_s)` schedule for `_announce_cycle` —
    `playing` is the persona name during the clip, `None` during the gap —
    so the narration, the "currently playing" state and the audio all agree
    on the same segment boundaries.
    """
    silence = np.zeros(int(silence_s * SR), dtype=np.float32)
    segments: list[np.ndarray] = []
    schedule: list[tuple[str | None, float]] = []
    for persona in cast.personas.values():
        clip = _load_clip(persona)
        segments.extend((clip, silence))
        schedule.append((persona.name, len(clip) / SR))
        schedule.append((None, silence_s))
    return np.concatenate(segments), schedule


async def _announce_cycle(schedule: list[tuple[str | None, float]], playing_box: list) -> None:
    """Narrates the loop and keeps `playing_box[0]` current for the detectors."""
    while True:
        for playing, duration_s in schedule:
            playing_box[0] = playing
            _print(f"[cyan]now playing:[/] {playing or '(silence)'}")
            await asyncio.sleep(duration_s)


async def _enrol(stt: PanelSTT, args, enrolling_box: list) -> str | None:
    """Condensed `PanelRuntime._enrol` — same store, same two-phase capture.

    `enrolling_box[0]` is read by the audio callback and set/cleared here,
    mirroring `PanelRuntime._enrolling`.
    """
    if args.no_speaker_lock:
        console.print("[yellow]--no-speaker-lock[/]: every voice on the mic counts as Ricky's.")
        return None

    store = SpeakerStore(args.speakers)
    if not args.re_enrol:
        stored = store.load(model=stt.config.model)
        if stored is not None:
            console.print(f"[dim]enrolment loaded from {store.path} ({stored.label})[/]")
            stt.identify(label=stored.label, speaker_identifiers=stored.speaker_identifiers)
            return stored.label

    console.print("\n[bold]Speaker enrolment.[/] Talk normally for up to 30 seconds.\n")
    enrolment = SpeakerEnrolment(
        config=stt.config,
        on_progress=lambda phase, detail: console.print(f"[dim]enrol: {phase} {detail}[/]"),
    )
    enrolling_box[0] = enrolment
    try:
        speaker = await enrolment.run()
    finally:
        enrolling_box[0] = None
    if speaker is None:
        console.print("[yellow]enrolment failed — mic runs ungated for this test[/]")
        return None
    store.save(speaker)
    stt.identify(label=speaker.label, speaker_identifiers=speaker.speaker_identifiers)
    console.print(f"[dim]enrolment saved to {store.path}[/]\n")
    return speaker.label


async def _print_vad(mic: asyncio.Queue, playing_box: list) -> None:
    from livekit import rtc
    from livekit.agents import vad as lkvad
    from livekit.plugins import silero

    detector = silero.VAD.load(sample_rate=SR)
    stream = detector.stream()
    speaking = False

    async def pump() -> None:
        while True:
            chunk = await mic.get()
            pcm = (np.clip(chunk, -1, 1) * 32767).astype(np.int16)
            stream.push_frame(rtc.AudioFrame(pcm.tobytes(), SR, 1, len(pcm)))

    asyncio.create_task(pump(), name="feedback-test-vad-pump")
    async for ev in stream:
        if ev.type == lkvad.VADEventType.INFERENCE_DONE:
            if speaking or ev.probability < 0.5:
                continue
            speaking = True
            playing = playing_box[0]
            if playing is not None:
                _print(
                    f"[bold red]TRANSCRIBED BARGE-IN[/] — {playing}'s audio tripped the VAD "
                    "reflex (identity-blind — enrolment can't prevent this)"
                )
            else:
                _print("[green]barge-in reflex: real human speech detected[/]")
        elif ev.type == lkvad.VADEventType.END_OF_SPEECH:
            speaking = False


async def _print_gated_stt(stt: PanelSTT, playing_box: list, enrolled_label: str | None) -> None:
    from panel_core import TranscriptUpdated

    while True:
        event = await stt.events.get()
        if not (isinstance(event, TranscriptUpdated) and event.is_final):
            continue
        playing = playing_box[0]
        if playing is None:
            _print(f"[green]Ricky:[/] {event.text}")
        elif enrolled_label is not None:
            _print(
                f"[bold red]TRANSCRIBED BARGE-IN[/] — {playing}'s audio transcribed as "
                f"{enrolled_label} (diarisation gate failed)"
            )
        else:
            _print(
                f"[bold red]TRANSCRIBED BARGE-IN[/] — {playing}'s audio transcribed as Ricky "
                "(--no-speaker-lock: expected, every voice counts)"
            )


async def _run(args) -> None:
    cast = PanelCast.from_dir(args.personas)
    cycle, schedule = _build_cycle(cast, args.silence_s)
    console.print(
        f"[bold]looping {', '.join(p.name for p in cast.personas.values())}'s voices[/] "
        f"(cast order, {args.silence_s:.0f}s silence between each)\n"
    )

    stt = PanelSTT({"ricky": "human"}, config=STTConfig.from_cast(cast))
    aec = (
        EchoCanceller(AECConfig(enabled=True, delay_ms=args.aec_delay_ms), SR, args.block)
        if args.aec
        else None
    )

    mic: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    pos = 0
    enrolling_box: list = [None]  # read by callback, set/cleared by `_enrol`
    playing_box: list = [None]  # read by the detectors, set by `_announce_cycle`

    def callback(indata, outdata, frames, timeinfo, status) -> None:
        nonlocal pos
        del timeinfo, status
        mono = indata[:, 0]
        if aec is not None:
            mono = aec.process(mono)
        pcm = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)

        enrolling = enrolling_box[0]
        if enrolling is not None:
            enrolling.feed(pcm.tobytes())
        else:
            loop.call_soon_threadsafe(mic.put_nowait, mono.copy())
            stt.feed("ricky", pcm.tobytes())

        end = pos + frames
        if end <= len(cycle):
            chunk = cycle[pos:end]
        else:
            wrap = end - len(cycle)
            chunk = np.concatenate((cycle[pos:], cycle[:wrap]))
            end = wrap
        pos = end
        if aec is not None:
            aec.push_farend(chunk)
        outdata[:, 0] = chunk

    with sd.Stream(
        samplerate=SR,
        blocksize=args.block,
        dtype="float32",
        channels=1,
        device=(args.input_device, args.output_device),
        callback=callback,
    ):
        # Stream open before enrolment — one socket serves both phases.
        label = await _enrol(stt, args, enrolling_box)
        gated = f"mic gated to {label}" if label else "mic ungated"
        console.print(f"[bold]listening.[/] [dim]{gated}. Speak now. Ctrl-C to stop.[/]\n")
        await stt.start()
        await asyncio.gather(
            _print_vad(mic, playing_box),
            _print_gated_stt(stt, playing_box, label),
            _announce_cycle(schedule, playing_box),
        )


def main() -> None:
    p = argparse.ArgumentParser(prog="feedback-test", description=__doc__)
    p.add_argument("--personas", type=Path, default=Path("personas"))
    p.add_argument("--silence-s", type=float, default=5.0, help="gap between each persona's clip")
    p.add_argument("--block", type=int, default=256)
    p.add_argument("--input-device", default=None)
    p.add_argument("--output-device", default=None)
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--speakers", type=Path, default=DEFAULT_STORE_PATH)
    p.add_argument("--re-enrol", action="store_true")
    p.add_argument("--no-speaker-lock", action="store_true")
    p.add_argument("--aec", action="store_true")
    p.add_argument("--aec-delay-ms", type=float, default=0.0)
    args = p.parse_args()
    if args.list_devices:
        console.print(str(sd.query_devices()))
        return
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")


if __name__ == "__main__":
    main()
