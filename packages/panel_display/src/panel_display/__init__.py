"""The video wall.

A 16:9 stage display, laid out at 3840 x 2160 and scaled to fit: the three
agents across the top, a third of the width each, and the transcript in a
full-width band beneath them. Each agent gets a ring that pulses with that
agent's own audio and lights up in that agent's colour.

It was drawn for a 12.00m x 4.50m ultrawide LED wall (8:3, four 3m columns,
transcript hard left) and is being shown first on an ordinary widescreen
display, which is why the rows used to be columns.

    uv run panel --display        # driven by the live panel
    uv run panel-display --demo   # synthetic, no mic and no API keys

FEASIBILITY.md 4.6 is explicit that this is comprehension infrastructure and
not decoration: three voices on one PA, out of one pair of speakers, with no
mouths to watch, is very hard to follow. The wall is how the audience knows
who is talking, who was asked, and who has something to say and is waiting.

Structure follows the rest of the repo — a pure part and an I/O part:

    wall.py     WallState. Events and commands in, snapshots out. No sockets,
                no clock, no browser. Testable, and therefore tested.
    server.py   DisplayServer. One aiohttp app: the page, its assets, and the
                socket that carries snapshots and audio envelopes.
    demo.py     A synthetic panel, for tuning the look and for checking the
                wall at load-in before the audio rig exists.
    static/     The page itself. No build step, no npm, no bundler — the venue
                machine gets a directory of files and a browser.
"""

from .server import DEFAULT_PORT as DISPLAY_PORT
from .server import DisplayServer
from .wall import ACCENTS, AgentView, WallState

__all__ = ["ACCENTS", "DISPLAY_PORT", "AgentView", "DisplayServer", "WallState"]
