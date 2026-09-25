"""Run the wall on its own, with nobody on stage.

    uv run panel-display              # geometry and type only, everything idle
    uv run panel-display --demo       # a synthetic panel, on a loop

Why this exists rather than "just run the panel": the wall has to be checked
against the real hardware, and the two things that matter most at load-in —
does the 8:3 layout actually fill the LED processor's output, and can the back
row read a name — need a picture on the wall, not a working panel. This needs
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

SCRIPT = [
    "So let's start where the disagreement actually is.",
    "Wayne, you think adoption already happened and nobody noticed.",
    "Melia, I can see you want to come back on that.",
    "Dexter — is any of this survivable at fleet scale?",
    "Let's take one more before we open it up.",
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

    async def levels(self) -> None:
        """One envelope per agent at the real frame rate, forever.

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
                values[self.speaking] = value * (0.28 if self.ducked else 1.0)
            # Ricky's mic. Live only while he is mid-question, which is what
            # makes the moderator dot worth looking at.
            values[HUMAN] = 0.18 * random.uniform(0.6, 1.0) if self.human else 0.0
            self.server.set_levels(values)

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

    async def moderator(self, line: str) -> None:
        """Ricky asks something, one word at a time, then it finalises."""
        self.human = True
        self.server.on_event(HumanSpeechStarted(t=self.t))
        self.paint()
        words = line.split()
        for index in range(1, len(words) + 1):
            self.server.on_event(
                TranscriptUpdated(
                    t=self.t, speaker=HUMAN, text=" ".join(words[:index]), is_final=False
                )
            )
            await asyncio.sleep(0.16)
        self.server.on_event(
            TranscriptUpdated(t=self.t, speaker=HUMAN, text=line, is_final=True)
        )
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
        await asyncio.sleep(random.uniform(3.0, 5.0))

        # A backchannel mid-turn: "mm-hm" ducks but does not stop.
        self.ducked = True
        self.server.on_command(DuckSpeech(agent=agent, gain_db=-12.0, ramp_ms=120))
        await asyncio.sleep(1.1)
        self.ducked = False
        self.server.on_command(ResumeSpeech(agent=agent, ramp_ms=180))
        await asyncio.sleep(random.uniform(2.5, 4.0))

        self.speaking = None
        self.invited = None
        self.server.on_event(
            AgentSpeechEnded(t=self.t, agent=agent, completed=True, utterance="…")
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
