"""The socket and the static files. Everything `wall.py` refuses to do.

One aiohttp app on one port: the page at `/`, its assets under `/static/`, and
a WebSocket at `/ws`. One URL to type into a browser on a machine wheeled in at
load-in, which is the only interface this has at 9pm the night before.

Two channels over that socket, and the split is the whole design:

* **State** — the full wall, sent on change and coalesced to `FLUSH_HZ`. Full
  snapshots rather than diffs, because a browser *will* be refreshed during the
  show and a delta-only client comes back blank.
* **Levels** — audio envelopes, `LEVEL_HZ` a second, never coalesced with state.
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
        # True while the last frame sent had sound in it, so silence sends one
        # trailing frame of zeroes and then stops rather than streaming zeroes
        # through every gap in the conversation.
        self._levels_live = False

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
        """Hook for every event the reducer sees. Never raises at the caller."""
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

    def set_levels(self, levels: dict[str, float]) -> None:
        """Latest audio envelope per agent, plus `human`. Replaces, never queues.

        A frame the pump did not get to is a frame nobody needed: the orb is
        showing *now*, and a 33ms-stale envelope is worth less than the one
        behind it.
        """
        self._levels = levels

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

    async def _pump(self) -> None:
        """One timer for both channels, at the faster of the two rates."""
        period = 1.0 / LEVEL_HZ
        state_every = max(1, round(LEVEL_HZ / FLUSH_HZ))
        frame = 0
        while True:
            await asyncio.sleep(period)
            frame += 1
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
        await self._broadcast({"type": "levels", "v": self._levels})

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
