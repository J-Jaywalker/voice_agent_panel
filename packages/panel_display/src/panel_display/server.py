"""The socket and the static files. Everything `wall.py` refuses to do.

One aiohttp app on one port: the page at `/`, its assets under `/static/`, and
a WebSocket at `/ws`. One URL to type into a browser on a machine wheeled in at
load-in, which is the only interface this has at 9pm the night before.

Two channels over that socket, and the split is the whole design:

* **State** — the full wall, sent on change and coalesced to `FLUSH_HZ`. Full
  snapshots rather than diffs, because a browser *will* be refreshed during the
  show and a delta-only client comes back blank.
* **Levels** — audio envelopes and spectra, `LEVEL_HZ` a second, never coalesced with state.
  They change every frame and would otherwise mark the wall dirty continuously,
  turning a 2KB snapshot into a 60KB/s stream of mostly unchanged fields.

Nothing here is allowed to take the panel down with it. A wall that fails is a
show with no pictures; an exception raised into `PanelRuntime._drain_events` is
a show with no sound.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web
from panel_core import PanelCast

from .wall import WallState

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"

DEFAULT_PORT = 8765

# Wall repaints per second. The wall changes on human decisions — someone
# starts talking, the floor moves — so 20Hz is already far finer than anything
# it describes, and it exists to *coalesce*: `StateChanged` fires on nearly
# every transition and several can land in one pass of the reducer.
FLUSH_HZ = 20

# Audio-envelope frames per second. The orb interpolates between these at
# display rate, so this only has to be fast enough that a syllable is not
# missed — 33ms per frame against syllables of 150-250ms. Raising it makes the
# orb no smoother, because the smoothing is a filter on the client, not a
# consequence of the sample rate.
LEVEL_HZ = 30

# How long an in-progress line may go unchanged before the band stops calling
# it in progress.
#
# Every other way a partial ends is an event: a final replaces it, an empty
# update clears it, a handover closes the previous agent's, and
# `UnverifiedSpeechDetected` closes Ricky's. The case left over has no event by
# design — `panel_runtime/stt.py` drops a segment silently when diarisation
# attributed nothing at all, so Ricky's partial can simply stop being updated.
# Before this existed that partial stayed on the wall for the rest of the show,
# and since the band grew bubbles that size with their text it held the room's
# full attention while doing it.
#
# 6s, which is long rather than tight on purpose. A partial that stops arriving
# is a display defect; a partial dropped out from under a speaker who was only
# pausing is a *wrong* display, and the gap between a mid-sentence pause and a
# dead socket is wide — the engine's own `EndOfTurn` fires well inside this.
# Erring long means the worst case is six seconds of a stale line instead of a
# band that flickers whenever Ricky stops to think.
PARTIAL_TTL_S = 6.0

# There is deliberately no words-per-second constant here any more.
#
# This server used to hold each `AgentUtteranceProgress` in a per-agent queue
# and pay it out at an assumed 2.8 words/sec, because that event is emitted
# when a sentence is handed to the TTS provider rather than when it is heard,
# and applied straight to the wall a whole turn landed as one clump of text
# followed by a long dead band. The estimate was the problem: real pacing
# varies by sentence, by pause and by provider, so the band drifted against
# the audio over a turn and the error was cumulative.
#
# Agent lines now arrive as `TranscriptUpdated` off a real transcription
# session over the agent's own played audio (`PanelRuntime._run_agent_stt`),
# tapped at `Mixer.render` and therefore paced by the output device. Timing is
# measured rather than assumed, this hook has nothing left to do, and the
# queue, the drain tasks and the constant are gone rather than left dead.


class DisplayServer:
    """Serves the wall and keeps every connected browser in step.

    Args:
        cast: The panel, for names, roles and accent assignment.
        port: TCP port for both the page and the socket.
        host: Interface to bind. Defaults to every interface, because the
            wall is routinely a second machine on the venue's switch rather
            than a second window on this one.
    """

    def __init__(
        self,
        cast: PanelCast,
        *,
        port: int = DEFAULT_PORT,
        # Every interface, deliberately: the wall is routinely a second
        # machine on the venue's switch rather than a second window on this
        # one, and the socket carries no control channel in either direction.
        host: str = "0.0.0.0",
    ) -> None:
        self.wall = WallState.for_cast(cast)
        self.port = port
        self.host = host

        self._clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None
        self._flush: asyncio.Task | None = None
        # False, not True. Every client is painted from a snapshot the moment
        # it connects, so there is nothing to flush until something actually
        # changes — starting dirty just broadcast the opening state twice.
        self._dirty = False
        self._levels: dict[str, float] = {}
        self._bands: dict[str, list[float]] = {}
        # True while the last frame sent had sound in it, so silence sends one
        # trailing frame of zeroes and then stops rather than streaming zeroes
        # through every gap in the conversation.
        self._levels_live = False
        # speaker -> (the text last seen, when this process first saw it).
        # Arrival time, which is this side's knowledge and not the wall's:
        # `WallState` reads no clock. See `_expire_partials`.
        self._partial_seen: dict[str, tuple[str, float]] = {}

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> str | None:
        """Bind and serve. Returns the URL, or None if the port was unusable.

        Deliberately does not raise. A wall that cannot bind is worth a loud
        line and a show that still has audio — the alternative is `uv run
        panel` refusing to start twenty minutes before doors because something
        else is on 8765.
        """
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._socket)
        app.router.add_static("/static/", STATIC)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        try:
            await site.start()
            # Port 0 means "any free port", which the tests use so they never
            # collide with a wall someone left running. Resolve it back so
            # `self.port` and the returned URL always name the real one.
            if self.port == 0:
                # aiohttp exposes no public accessor for the resolved port.
                self.port = site._server.sockets[0].getsockname()[1]
        except OSError as exc:
            log.warning("display: cannot bind %s:%s — %s", self.host, self.port, exc)
            await self._runner.cleanup()
            self._runner = None
            return None

        self._flush = asyncio.create_task(self._pump(), name="display-flush")
        return f"http://localhost:{self.port}/"

    async def close(self) -> None:
        if self._flush is not None:
            self._flush.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush
            self._flush = None
        for client in list(self._clients):
            with contextlib.suppress(Exception):
                await client.close()
        self._clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------ the panel's

    # Both hooks are called from inside `PanelRuntime._drain_events`, on the
    # same loop as the audio path, so both swallow everything. A bare `except
    # Exception` is usually a smell; here it is the requirement. The worst a
    # broken wall may cost is the picture.

    def on_event(self, event: Any) -> None:
        """Hook for every event the wall is shown. Never raises at the caller.

        Straight through to `WallState` now, with no queue in front of it.
        Every event on this hook is a fact whose arrival time means something:
        `TranscriptUpdated` — the only source of transcript text, Ricky's mic
        or an agent's own played audio — is paced by the speech it came from,
        and needs no help from here.

        Two callers, and the second is the whole safety property of the agent
        transcripts: `PanelRuntime._drain_events` passes everything the
        reducer sees, while `PanelRuntime._run_agent_stt` passes the agents'
        `TranscriptUpdated` events *only* here and nowhere else. This hook is
        therefore not a mirror of the event log, and must not be treated as
        one.
        """
        try:
            self._dirty |= self.wall.apply_event(event)
        except Exception:
            log.exception("display: event %r", type(event).__name__)

    def on_command(self, command: Any) -> None:
        """Hook for every command the reducer emits. Never raises at the caller."""
        try:
            self._dirty |= self.wall.apply_command(command)
        except Exception:
            log.exception("display: command %r", type(command).__name__)

    def set_levels(
        self, levels: dict[str, float], bands: dict[str, list[float]] | None = None
    ) -> None:
        """Latest audio envelope per agent, plus `human`. Replaces, never queues.

        A frame the pump did not get to is a frame nobody needed: the orb is
        showing *now*, and a 33ms-stale envelope is worth less than the one
        behind it.

        Args:
            levels: Envelope per voice, 0-1. `human` included.
            bands: Frequency spectrum per *agent*, `SPECTRUM_BANDS` amplitudes
                in the same units. Optional, and the client draws a corona
                without it — anything driving this server that has no spectrum
                to give (a test, an older runtime) should leave it out rather
                than invent one.
        """
        self._levels = levels
        self._bands = bands or {}

    # ------------------------------------------------------------------ HTTP

    async def _index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(
            STATIC / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    async def _socket(self, request: web.Request) -> web.StreamResponse:
        socket = web.WebSocketResponse(heartbeat=20)
        await socket.prepare(request)
        self._clients.add(socket)
        log.info("display: client connected (%d total)", len(self._clients))
        try:
            # The new client is painted from the current snapshot before it is
            # told anything else. Mid-show refreshes are the normal case, not
            # the exception.
            await socket.send_json(self.wall.snapshot())
            async for message in socket:
                # Nothing is read from the wall. It is a display, and giving it
                # a control channel would make a browser tab something that can
                # change what happens on stage.
                if message.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self._clients.discard(socket)
            log.info("display: client gone (%d left)", len(self._clients))
        return socket

    # ------------------------------------------------------------------- pump

    def _expire_partials(self, now: float) -> None:
        """Drop in-progress lines that have stopped arriving. See `PARTIAL_TTL_S`.

        Polled rather than scheduled, and stamped here rather than from
        `TranscriptUpdated.t`, for two separate reasons.

        Polled, because the event that would have scheduled it is the one that
        never comes: a silently dropped segment (diarisation attributed
        nothing) leaves the wall holding a partial with nothing following it.
        Only a clock can notice that, and only this side has one.

        Stamped on arrival, because `t` on those events is whatever clock
        produced them — `time.monotonic()` from `panel_runtime/stt.py`, a
        synthetic counter from `demo.py` — and comparing either against this
        loop's `monotonic()` would expire every partial instantly in the demo
        while appearing to work in the show. Arrival time is the thing being
        measured anyway: the question is how long it has been since the wall
        last heard anything, not when the words were spoken.
        """
        live = self.wall.partials
        for speaker in [s for s in self._partial_seen if s not in live]:
            del self._partial_seen[speaker]
        for speaker, text in list(live.items()):
            seen = self._partial_seen.get(speaker)
            if seen is None or seen[0] != text:
                # Still growing — a new voice, or another word. Restamp.
                self._partial_seen[speaker] = (text, now)
            elif now - seen[1] > PARTIAL_TTL_S:
                self._dirty |= self.wall.drop_partial(speaker)
                del self._partial_seen[speaker]

    async def _pump(self) -> None:
        """One timer for both channels, at the faster of the two rates."""
        period = 1.0 / LEVEL_HZ
        state_every = max(1, round(LEVEL_HZ / FLUSH_HZ))
        frame = 0
        while True:
            await asyncio.sleep(period)
            frame += 1
            # Before the `_clients` check, not after: a wall nobody is watching
            # yet still has to be *correct* when a browser connects, and the
            # snapshot it is painted from is this state. Left behind the check,
            # a stale partial would survive any gap in viewers and then be the
            # first thing a reconnecting wall showed.
            self._expire_partials(asyncio.get_running_loop().time())
            if not self._clients:
                continue
            if self._dirty and frame % state_every == 0:
                self._dirty = False
                await self._broadcast(self.wall.snapshot())
            await self._broadcast_levels()

    async def _broadcast_levels(self) -> None:
        sounding = any(v > 0.0 for v in self._levels.values())
        if not sounding and not self._levels_live:
            return
        self._levels_live = sounding
        # `b` is omitted entirely when there is no spectrum, rather than sent
        # as an empty object: silence already sends one trailing frame and
        # then stops, so the bytes a spectrum costs are only ever spent while
        # somebody is actually talking.
        payload: dict[str, Any] = {"type": "levels", "v": self._levels}
        if self._bands:
            payload["b"] = self._bands
        await self._broadcast(payload)

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, separators=(",", ":"))
        dead = []
        for client in self._clients:
            try:
                await client.send_str(text)
            except (ConnectionResetError, RuntimeError):
                # A wall unplugged mid-show, or a laptop lid closed. Drop it
                # and keep painting for everyone else.
                dead.append(client)
        for client in dead:
            self._clients.discard(client)
