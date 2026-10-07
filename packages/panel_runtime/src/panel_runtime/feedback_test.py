"""Feedback test — loops every agent's real voice through the speakers while
the real speaker-gated mic pipeline listens, flagging anything that could be
actioned as a barge-in while a clip is playing.

    uv run feedback-test
    uv run feedback-test --aec --aec-delay-ms 40
    uv run feedback-test --no-speaker-lock   # skip enrolment, faster iteration
    uv run feedback-test --confirm-partials 1  # worst case: one partial stops an agent

Dexter, Melia and Wayne's `introduction` lines, in their real ElevenLabs
voices, ship as committed WAVs under `assets/feedback/` (see
`generate_feedback_assets.py`) so this needs no network or API key to run.
They loop in cast order with a `--silence-s` gap after each — real speech and
real silence, not noise, since the thing under test is whether *this signal*
gets transcribed as words.

Two questions are asked of the same bleed, and they are not the same question.

**Could this signal in principle be weaponised?** Two raw detectors answer
that, both now arriving on the same `stt.events` queue since the barge-in
reflex moved onto the STT socket — but enrolment affects them differently:

* **Endpointing** (`SpeechStarted`) is identity-blind by design — it owns
  stopping and never checks who. Enrolment cannot prevent this one either way,
  so it flags the same regardless of speaker lock.
* **Transcripts** only produce `Ricky:` for diarised, enrolled segments. With
  speaker lock on, a `Ricky:` line during playback is a diarisation failure;
  with `--no-speaker-lock` it's expected — every voice counts as Ricky's, by
  design.

**Does today's mechanism actually act on it, and by how much?** A raw signal
is not an interrupt. `FloorController` stops an agent on a *final* attributed
to Ricky, or on `FloorConfig.interrupt_confirm_partials` consecutive
Ricky-attributed *partials* over the same turn — so a clip that trips the
endpointer every loop and never clears the streak costs the show nothing,
while one creeping to within a partial of the threshold is a venue problem
that no raw-signal count would have shown. So every event off the socket goes
through a real `FloorController`, and the harness reports both the stops it
commits (`FLOOR INTERRUPT`) and the streak it reaches when it doesn't
(`floor: confirmed streak n/N`). `--confirm-partials` overrides the threshold
for deliberate sensitivity sweeps.

The simulated floor is real `panel_core` and nothing else: each clip is one
agent turn, bracketed by `AgentSpeechStarted`/`AgentSpeechEnded`, which is
what makes `state.speaking` non-None and the interrupt rule reachable at all.
No brain, no API token, no `Tick` — the liveness watchdog would stop a speaker
that never emits `AgentAudioProgress`, and a watchdog stop is not the
measurement. Commands are read, never executed: nothing here puts audio on an
output beyond the clip already looping.

Nothing non-actioned is printed — no raw transcript the floor would never
read, no "dropped" line for a rejection that worked as intended.
"""

from __future__ import annotations

import argparse
import asyncio
import wave
from dataclasses import replace
from pathlib import Path

import numpy as np
import sounddevice as sd
from panel_core import (
    AgentSpeechEnded,
    AgentSpeechStarted,
    FloorConfig,
    FloorController,
    HumanSpeechStarted,
    PanelCast,
    PanelState,
    Persona,
    StopSpeech,
    TranscriptUpdated,
)
from rich.console import Console

from .aec import EchoCanceller
from .config import PIPELINE_SAMPLE_RATE, AECConfig
from .enrolment import DEFAULT_STORE_PATH, SpeakerEnrolment, SpeakerStore
from .stt import PanelSTT, STTConfig

console = Console()
SR = PIPELINE_SAMPLE_RATE
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


def _build_cycle(cast, silence_s: float) -> tuple[np.ndarray, list[tuple[Persona | None, float]]]:
    """Every persona's clip in cast order, each followed by a silence gap.

    Also returns a `(playing, duration_s)` schedule for `_announce_cycle` —
    `playing` is the persona during the clip, `None` during the gap — so the
    narration, the "currently playing" state, the simulated floor's turn
    boundaries and the audio all agree on the same segment boundaries. The
    whole persona and not just its name, because the floor is driven by id.
    """
    silence = np.zeros(int(silence_s * SR), dtype=np.float32)
    segments: list[np.ndarray] = []
    schedule: list[tuple[Persona | None, float]] = []
    for persona in cast.personas.values():
        clip = _load_clip(persona)
        segments.extend((clip, silence))
        schedule.append((persona, len(clip) / SR))
        schedule.append((None, silence_s))
    return np.concatenate(segments), schedule


async def _announce_cycle(
    schedule: list[tuple[Persona | None, float]],
    playing_box: list,
    fc: FloorController,
    state_box: list,
) -> None:
    """Narrates the loop, and makes each clip a real turn on the simulated floor.

    `playing_box[0]` is the segment the detectors read; `state_box[0]` is the
    `PanelState` both coroutines share. One asyncio loop, no threads, so the
    box is the whole synchronisation — same reasoning as `enrolling_box`.

    A clip only tests anything if the floor thinks someone is on the PA:
    `FloorController._transcript` counts the confirmation streak and commits a
    stop solely while `state.speaking` is non-None.
    """
    while True:
        for persona, duration_s in schedule:
            now = asyncio.get_running_loop().time()
            previous = playing_box[0]
            # Only if the clip still holds the floor. If it doesn't, a stop
            # already ended that turn, and ending it twice re-enters
            # arbitration for a turn that is over.
            if previous is not None and state_box[0].speaking == previous.id:
                state_box[0], _ = fc.reduce(
                    state_box[0],
                    AgentSpeechEnded(t=now, agent=previous.id, completed=True, utterance=""),
                )
            playing_box[0] = persona
            if persona is not None:
                state_box[0], _ = fc.reduce(
                    state_box[0], AgentSpeechStarted(t=now, agent=persona.id)
                )
            _print(f"[cyan]now playing:[/] {persona.name if persona is not None else '(silence)'}")
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


async def _print_detections(
    stt: PanelSTT,
    playing_box: list,
    enrolled_label: str | None,
    fc: FloorController,
    state_box: list,
) -> None:
    """One loop, both raw detectors and the simulated floor.

    Everything off the socket is reduced, not just what the raw detectors
    look at: `HumanSpeechEnded` is what clears a half-built streak, so a loop
    that fed only transcripts would report a streak the real floor never had.
    `reduce()` no-ops on what it doesn't handle, so this costs nothing.
    """
    threshold = fc.config.interrupt_confirm_partials

    while True:
        event = await stt.events.get()
        playing = playing_box[0]

        streak_before = state_box[0].human_interrupt_streak
        state_box[0], commands = fc.reduce(state_box[0], event)
        streak = state_box[0].human_interrupt_streak
        stop = next((c for c in commands if isinstance(c, StopSpeech)), None)

        # --- raw signal: could this in principle be weaponised? ---
        if isinstance(event, HumanSpeechStarted):
            if playing is not None:
                _print(
                    f"[bold red]TRANSCRIBED BARGE-IN[/] — {playing.name}'s audio tripped the "
                    "endpointing reflex (identity-blind — enrolment can't prevent this)"
                )
            else:
                _print("[green]barge-in reflex: real human speech detected[/]")
        elif isinstance(event, TranscriptUpdated) and event.is_final:
            if playing is None:
                _print(f"[green]Ricky:[/] {event.text}")
            elif enrolled_label is not None:
                _print(
                    f"[bold red]TRANSCRIBED BARGE-IN[/] — {playing.name}'s audio transcribed as "
                    f"{enrolled_label} (diarisation gate failed)"
                )
            else:
                _print(
                    f"[bold red]TRANSCRIBED BARGE-IN[/] — {playing.name}'s audio transcribed as "
                    "Ricky (--no-speaker-lock: expected, every voice counts)"
                )

        # --- the mechanism: did it act, and how close did it come? ---
        if playing is None:
            continue
        if stop is not None:
            via = (
                "a final"
                if isinstance(event, TranscriptUpdated) and event.is_final
                else f"the partial streak reaching {threshold}"
            )
            _print(
                f"[bold red]FLOOR INTERRUPT[/] — the real floor logic stopped {playing.name} "
                f"on {playing.name}'s own bleed, via {via} ({stop.reason.value}). "
                "On stage this is a lost turn."
            )
        elif streak > streak_before:
            _print(
                f"[yellow]floor: confirmed streak {streak}/{threshold}[/] — "
                f"{playing.name}'s bleed is counting towards a real interrupt"
            )


async def _run(args) -> None:
    cast = PanelCast.from_dir(args.personas)
    cycle, schedule = _build_cycle(cast, args.silence_s)
    floor_config = FloorConfig()
    if args.confirm_partials is not None:
        floor_config = replace(floor_config, interrupt_confirm_partials=args.confirm_partials)
    fc = FloorController(cast, floor_config)
    state_box: list = [PanelState.for_agents(cast.ids())]
    console.print(
        f"[bold]looping {', '.join(p.name for p in cast.personas.values())}'s voices[/] "
        f"(cast order, {args.silence_s:.0f}s silence between each)\n"
        f"[dim]floor: a stop needs a final, or {floor_config.interrupt_confirm_partials} "
        f"consecutive confirmed partials[/]\n"
    )

    stt = PanelSTT({"ricky": "human"}, config=STTConfig.from_cast(cast))
    aec = (
        EchoCanceller(AECConfig(enabled=True, delay_ms=args.aec_delay_ms), SR, args.block)
        if args.aec
        else None
    )

    pos = 0
    enrolling_box: list = [None]  # read by callback, set/cleared by `_enrol`
    playing_box: list = [None]  # read by the detectors, set by `_announce_cycle`
    # `state_box` above: shared by both coroutines, one asyncio loop, no lock.

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
            _print_detections(stt, playing_box, label, fc, state_box),
            _announce_cycle(schedule, playing_box, fc, state_box),
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
    p.add_argument(
        "--confirm-partials",
        type=int,
        default=None,
        help="override FloorConfig.interrupt_confirm_partials — 1 is the worst case, "
        "and the number to run if you want to know whether the bleed could ever land one",
    )
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
