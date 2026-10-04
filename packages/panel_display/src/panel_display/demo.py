"""Run the wall on its own, with nobody on stage.

    uv run panel-display              # geometry and type only, everything idle
    uv run panel-display --demo       # a synthetic panel, on a loop

Why this exists rather than "just run the panel": the wall has to be checked
against the real hardware, and the two things that matter most at load-in —
does the 8:3 layout actually fill the display's output, and can the back row
read a name — need a picture on the wall, not a working panel. This needs
no mic, no API keys, no speakers and no moderator. Bring up the wall first,
then bring up the show.

`--demo` drives a loop of the beats the wall has to render: the moderator
talking, the panel thinking, an agent invited and then speaking, a
backchannel duck mid-turn, a raised hand nobody took, and the floor going
back to Ricky. If a state never appears here, nobody will see it before the
night.

The envelope is synthesised rather than sampled, and that is the one thing
here that is a *model* of the real input rather than a copy of it — real
speech is peakier. Set the orb's amplitude constants against a real turn
through `uv run panel --display`, never against this.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import math
import random
from pathlib import Path

from panel_core import (
    HUMAN,
    AgentSpeechEnded,
    AgentSpeechStarted,
    CueModerator,
    DuckSpeech,
    HandsRaised,
    HumanSpeechEnded,
    HumanSpeechStarted,
    PanelCast,
    RequestProposals,
    ResumeSpeech,
    StateChanged,
    TranscriptUpdated,
)

from .server import DEFAULT_PORT, LEVEL_HZ, DisplayServer

# Bands in a synthetic spectrum. The same count `panel_runtime.mixer` sends,
# restated rather than imported: this package does not depend on the runtime
# and `panel-display --demo` has to run on a machine with no audio stack on
# it. Nothing breaks if the two drift — the client resamples whatever arrives
# onto its own bar count — so this is a matter of the demo looking like the
# show, not a contract.
SPECTRUM_BANDS = 32

# Where a synthetic voice puts its energy: `(centre, width, weight)` as a
# fraction of the way up the orb's log-spaced bands. Two formants and the
# sibilance above them, which is the coarsest description of a vowel that
# still moves like one — the corona gets a low lobe, a mid lobe and a bright
# edge rather than a smooth hump.
#
# The weights are the part worth getting approximately right. Speech rolls
# off steeply with frequency, and the orb corrects for that on the client
# (BAND_TILT in static/js/orb.js); a synthetic spectrum that is *flat* would
# therefore arrive on the wall tilted the wrong way and peg the top half of
# every corona, making the demo flatter than the show rather than merely
# unlike it. A factor of fifteen from the first formant to the sibilance is
# in the right region.
#
# Still a model, like the envelope below it, and the same warning applies —
# set the orb's spectrum constants against a real turn through
# `uv run panel --display`, never against this.
_FORMANTS = ((0.22, 0.16, 1.00), (0.45, 0.13, 0.28), (0.82, 0.22, 0.07))

# What one band's amplitude actually comes out at, as a fraction of the
# formant model above.
#
# **Added 4 Oct, and it is a calibration fix rather than a taste one.** Without
# it this generator emitted peak band amplitudes around 0.36. Real speech off
# `Mixer.take_bands` puts roughly a tenth of that in any *single* band — the
# mixer's overall RMS is 0.15-0.35 and no voice puts all of itself in one band
# — so the demo was running about three times hot. The orb normalises against
# `BAND_FULL_SCALE` and then clamps, so the result was twelve to fifteen of
# the thirty-two bands pegged at full excursion on every loud syllable, and a
# corona with a flat top is a circle. The spectrum was arriving correctly and
# being flattened on the way to the screen.
#
# This is deliberately fixed *here* and not by raising `BAND_FULL_SCALE` in
# static/js/orb.js, which would have made the demo look right by de-tuning the
# show. That constant is set against a synthesised voice at the RMS the mixer
# meters real speech at; this file is a model of a model and is the one that
# was wrong. The warning above still stands — set the orb's constants against
# a real turn through `uv run panel --display`, never against this.
#
# At 0.30 the model pegs no band at any loudness it generates, and a loud
# syllable spans about 0.19 to 0.98 of the corona's depth.
_BAND_SCALE = 0.30

SCRIPT = [
    "So let's start where the disagreement actually is.",
    "Wayne, you think adoption already happened and nobody noticed.",
    "Melia, I can see you want to come back on that.",
    "Dexter — is any of this survivable at fleet scale?",
    "Let's take one more before we open it up.",
]

# What an agent's turn says, sentence by sentence. Delivered here exactly the
# way the real runtime delivers it — as `TranscriptUpdated` partials and finals
# off that agent's own transcription session (`PanelRuntime._run_agent_stt`) —
# so `--demo` exercises the code path the show runs on rather than a
# display-only shortcut. Not attributed to any one agent; `turn_for` below just
# draws a handful per turn, same as the real panel draws from whichever
# candidate won arbitration.
AGENT_LINES = [
    "Adoption already happened, and nobody noticed the day it did.",
    "Every workflow that used to need a sign-off still has one on paper.",
    "The sign-off is theatre at this point.",
    "None of that makes it survivable at fleet scale.",
    "I'd want to see the incident numbers before anyone calls it safe.",
    "The numbers are the whole argument, not a footnote to it.",
]


class SyntheticPanel:
    """Feeds a `DisplayServer` a plausible show, forever."""

    def __init__(self, server: DisplayServer, cast: PanelCast) -> None:
        self.server = server
        self.cast = cast
        self.ids = list(cast.ids())
        self.t = 0.0
        self.turn = 0
        self.speaking: str | None = None
        self.human = False
        self.ducked = False
        self.invited: str | None = None

    # ------------------------------------------------------------- envelopes

    def spectrum(self, loudness: float) -> list[float]:
        """A vowel's worth of spectrum, for one frame.

        Three formants that drift against each other, plus per-band jitter so
        no two bars are ever the same height. The drift is the part worth
        having: a fixed formant set gives a corona that only ever scales, and
        the whole reason the orb draws a spectrum rather than an envelope is
        that a real voice changes *shape* as it talks.
        """
        bands = []
        for i in range(SPECTRUM_BANDS):
            x = i / (SPECTRUM_BANDS - 1)
            value = 0.0
            for index, (centre, width, weight) in enumerate(_FORMANTS):
                # Each formant wanders by a few percent of the range, at its
                # own irrational rate, so the lobes slide rather than pulse.
                drift = 0.05 * math.sin(self.t * 2 * math.pi * (0.37 + 0.23 * index))
                value += weight * math.exp(-(((x - centre - drift) / width) ** 2))
            bands.append(
                round(loudness * value * random.uniform(0.6, 1.0) * _BAND_SCALE, 4)
            )
        return bands

    async def levels(self) -> None:
        """One envelope and one spectrum per agent at the real frame rate, forever.

        Syllables at roughly 4Hz with a jittered floor, which is close enough
        to the shape of speech that the orb's attack and release can be judged
        by eye. A ducked agent is attenuated here rather than silenced,
        because that is what the mixer does.
        """
        step = 1.0 / LEVEL_HZ
        while True:
            await asyncio.sleep(step)
            self.t += step
            values = dict.fromkeys(self.ids, 0.0)
            bands: dict[str, list[float]] = {}
            if self.speaking is not None:
                # Three detuned oscillators rather than one. A single clean
                # sine produces a perfectly regular ring of identical lobes,
                # which flatters the orb into looking better than real speech
                # will make it look — the lobes are the *point* of not doing
                # that. Irrational ratios keep it from repeating.
                syllable = 0.5 + 0.5 * math.sin(self.t * 2 * math.pi * 4.1)
                stress = 0.5 + 0.5 * math.sin(self.t * 2 * math.pi * 1.37 + 1.1)
                breath = 0.55 + 0.45 * math.sin(self.t * 2 * math.pi * 0.42)
                # Occasional near-silence, for the gaps between clauses.
                gate = 0.0 if math.sin(self.t * 2 * math.pi * 0.31) < -0.72 else 1.0
                value = 0.34 * syllable * (0.4 + 0.6 * stress) * breath * gate
                value *= random.uniform(0.75, 1.0)
                value *= 0.28 if self.ducked else 1.0
                values[self.speaking] = value
                # Off the same number, so the corona and the envelope agree
                # with each other the way the mixer's two meters do.
                bands[self.speaking] = self.spectrum(value)
            # Ricky's mic. Live only while he is mid-question, which is what
            # makes the moderator dot worth looking at. No spectrum — he has a
            # dot, not an orb.
            values[HUMAN] = 0.18 * random.uniform(0.6, 1.0) if self.human else 0.0
            self.server.set_levels(values, bands)

    # ----------------------------------------------------------------- beats

    def paint(self, **extra) -> None:
        """A `StateChanged` shaped like the reducer's, with the bits that matter."""
        self.server.on_command(
            StateChanged(
                floor_holder=self.speaking,
                speaking=self.speaking,
                turn_id=self.turn,
                extra={
                    "invited": self.invited,
                    "invitation_source": "address" if self.invited else None,
                    "invitation_role": "vocative",
                    "invitation_rule": "demo",
                    "address_conflict": (),
                    "awaiting": None,
                    "killed": False,
                    "intro_remaining": None,
                    "intro_done": True,
                }
                | extra,
            )
        )

    async def transcribe(
        self, speaker: str, line: str, *, per_word_s: float
    ) -> None:
        """One sentence arriving the way a transcription session delivers it.

        Growing partials, then a final. The same shape for the moderator's mic
        and for an agent's own played audio, because in the real runtime they
        are the same event off the same protocol — only the socket differs
        (`PanelSTT`, one per voice). Paced at roughly speaking rate here
        because in the real runtime the pace *is* the speech: the agents'
        audio is tapped at `Mixer.render`, so a partial arrives when the word
        is heard rather than when the model wrote it.
        """
        words = line.split()
        for index in range(1, len(words) + 1):
            self.server.on_event(
                TranscriptUpdated(
                    t=self.t, speaker=speaker, text=" ".join(words[:index]), is_final=False
                )
            )
            await asyncio.sleep(per_word_s)
        self.server.on_event(
            TranscriptUpdated(t=self.t, speaker=speaker, text=line, is_final=True)
        )

    async def moderator(self, line: str) -> None:
        """Ricky asks something, one word at a time, then it finalises."""
        self.human = True
        self.server.on_event(HumanSpeechStarted(t=self.t))
        self.paint()
        await self.transcribe(HUMAN, line, per_word_s=0.16)
        self.human = False
        self.server.on_event(HumanSpeechEnded(t=self.t))

    async def turn_for(self, agent: str) -> None:
        self.turn += 1
        self.invited = agent
        self.paint()
        await asyncio.sleep(0.5)

        self.speaking = agent
        self.server.on_event(AgentSpeechStarted(t=self.t, agent=agent))
        self.paint()

        sentences = random.sample(AGENT_LINES, k=random.randint(3, 4))
        spoken: list[str] = []

        # 2.8 words/sec, which is what the panel has measured itself at. The
        # turn therefore takes as long as it would take to say — there is no
        # separate "how long would the audio have run" padding any more,
        # because the transcript and the audio are the same clock now.
        async def say(sentence: str) -> None:
            spoken.append(sentence)
            await self.transcribe(agent, sentence, per_word_s=1 / 2.8)

        await say(sentences[0])

        # A backchannel mid-turn: "mm-hm" ducks but does not stop — and the
        # agent keeps talking through it, so its transcript keeps arriving.
        self.ducked = True
        self.server.on_command(DuckSpeech(agent=agent, gain_db=-12.0, ramp_ms=120))
        await say(sentences[1])
        self.ducked = False
        self.server.on_command(ResumeSpeech(agent=agent, ramp_ms=180))

        for sentence in sentences[2:]:
            await say(sentence)

        self.speaking = None
        self.invited = None
        self.server.on_event(
            AgentSpeechEnded(
                t=self.t, agent=agent, completed=True, utterance=" ".join(spoken)
            )
        )
        self.paint()

    async def run(self) -> None:
        self.paint()
        while True:
            for index, line in enumerate(SCRIPT):
                await self.moderator(line)

                self.server.on_command(
                    RequestProposals(agents=tuple(self.ids), reason="demo")
                )
                await asyncio.sleep(1.6)

                # Every few beats, show hands raised and nobody taking the
                # floor — the state the wall exists to make legible.
                if index % 3 == 2:
                    raised = tuple(
                        (agent, round(random.uniform(0.4, 0.9), 2))
                        for agent in random.sample(self.ids, 2)
                    )
                    self.server.on_command(HandsRaised(agents=raised))
                    await asyncio.sleep(2.4)
                    self.server.on_command(CueModerator(reason="no_invitation"))
                    await asyncio.sleep(1.8)
                    continue

                await self.turn_for(self.ids[index % len(self.ids)])
                await asyncio.sleep(0.8)

            self.server.on_command(CueModerator(reason="beat_complete"))
            await asyncio.sleep(3.0)


async def main_async(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cast = PanelCast.from_dir(args.personas)
    server = DisplayServer(cast, port=args.port)

    url = await server.start()
    if url is None:
        raise SystemExit(f"could not bind port {args.port}")
    print(f"video wall: {url}")
    print("open it on the wall machine, fullscreen, 8:3.")

    tasks = []
    if args.demo:
        panel = SyntheticPanel(server, cast)
        tasks = [
            asyncio.create_task(panel.run(), name="demo-script"),
            asyncio.create_task(panel.levels(), name="demo-levels"),
        ]
        print("driving a synthetic panel. Ctrl-C to stop.")

    try:
        await asyncio.Event().wait()
    finally:
        for task in tasks:
            task.cancel()
        await server.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="panel-display", description=__doc__)
    parser.add_argument("--personas", type=Path, default=Path("personas"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--demo", action="store_true", help="drive it with a fake panel")
    args = parser.parse_args()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
