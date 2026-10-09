"""The in-flight control surface: the force chord, and the STT health line.

Two things an operator needs while the show is running, tested at the layer
that owns each.

**The chord.** `1`, `2`, `3` name panellists in cast order and combine when
struck together, so `1`+`2` is one command naming a pair rather than two
commands naming one each. What that *means* is `panel_core`'s decision
(`FloorController._force`, proven in `packages/panel_core/tests/test_floor.py`);
what is proven here is the part only this layer can get wrong — turning a
stream of keystrokes into the right set, inside a window, without splitting a
burst or merging two deliberate presses.

Driven through a real pty rather than a stubbed file object, because the
watcher is deliberately terminal-shaped: `termios.tcgetattr`, `tty.setcbreak`
and `select` all want a real descriptor, and a stub good enough to satisfy
them would be testing the stub. It is also the only way to exercise the bug
that shaped the loop — `sys.stdin.read(1)` leaves the rest of a burst in
Python's buffer where `select` cannot see it, which split every fast chord and
stranded its second key until an unrelated keypress flushed it.

**The health line.** `PanelSTT` tells the runtime when a socket starts
carrying transcripts and when it stops; the runtime prints that on change.
Nothing acts on it — the sessions reconnect by themselves — so the assertions
are about what reaches the console, which is the entire feature: a dropped mic
socket and a closed floor are the same silence from the stalls, and only one
of them is fixed by asking the question again.
"""

from __future__ import annotations

import os
import pty
import threading
import time
from pathlib import Path

import pytest
from panel_core import OperatorAction, OperatorCommand, PanelCast
from panel_runtime.panel import CHORD_WINDOW_S, FORCE_REPEAT_COOLDOWN_S, PanelRuntime

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"

# Comfortably past `CHORD_WINDOW_S` without making the suite slow. Used where a
# test needs two presses to be *separate* commands; the opposite case needs no
# delay at all, since two bytes written together are never 50ms apart.
BEYOND_WINDOW_S = CHORD_WINDOW_S * 6


class _ImmediateLoop:
    """Stands in for the event loop the key thread posts commands to.

    `call_soon_threadsafe` is the only thing `_operator` wants a loop for, and
    what this file tests is which command a sequence of keystrokes produces,
    not the hop between threads. Calling straight through keeps the assertion
    on the same thread as the test.
    """

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


@pytest.fixture
def runtime(monkeypatch) -> PanelRuntime:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    return PanelRuntime(PanelCast.from_dir(PERSONA_DIR), use_tts=False)


def press(runtime: PanelRuntime, *script, monkeypatch) -> list[OperatorCommand]:
    """Type `script` at `_watch_console_keys` through a pty; return what it posted.

    Each element is either a string of keystrokes, written in one go the way a
    terminal delivers a burst, or a float to pause for — which is how a gap
    wider than the chord window is expressed.
    """
    posted: list[OperatorCommand] = []
    runtime._loop = _ImmediateLoop()
    monkeypatch.setattr(runtime, "emit", posted.append)

    primary, secondary = pty.openpty()

    class _Stdin:
        def fileno(self) -> int:
            return secondary

    monkeypatch.setattr("sys.stdin", _Stdin())

    watcher = threading.Thread(target=runtime._watch_console_keys, daemon=True)
    watcher.start()
    try:
        # The watcher has to reach `select` before anything is written, or the
        # first burst lands while the descriptor is still in canonical mode.
        time.sleep(0.05)
        for step in script:
            if isinstance(step, (int, float)):
                time.sleep(step)
            else:
                os.write(primary, step.encode())
        # Long enough for the last chord's window to expire and fire.
        time.sleep(BEYOND_WINDOW_S)
    finally:
        runtime._running = False
        os.close(primary)
        watcher.join(timeout=2.0)
        os.close(secondary)
    return posted


def forced(posted: list[OperatorCommand]) -> list[tuple[str, ...]]:
    return [c.agents for c in posted if c.action is OperatorAction.FORCE_AGENT]


# ------------------------------------------------------------------ the chord


def test_one_key_forces_one_agent(runtime, monkeypatch):
    posted = press(runtime, "1", monkeypatch=monkeypatch)
    assert forced(posted) == [(runtime.cast.ids()[0],)]


def test_two_keys_together_are_one_command_naming_both(runtime, monkeypatch):
    """The whole point of the chord: a pair, not two singles.

    Two singles would put one agent on the PA and then immediately cut them
    off for the other — the operator asked for an exchange and got an
    interruption.
    """
    dex, melia, _ = runtime.cast.ids()
    posted = press(runtime, "12", monkeypatch=monkeypatch)
    assert forced(posted) == [(dex, melia)]


def test_a_chord_is_ordered_by_the_cast_not_by_typing(runtime, monkeypatch):
    dex, melia, _ = runtime.cast.ids()
    posted = press(runtime, "21", monkeypatch=monkeypatch)
    assert forced(posted) == [(dex, melia)]


def test_all_three_together_name_the_whole_panel(runtime, monkeypatch):
    posted = press(runtime, "123", monkeypatch=monkeypatch)
    assert forced(posted) == [runtime.cast.ids()]


def test_keys_further_apart_than_the_window_are_separate_commands(runtime, monkeypatch):
    dex, melia, _ = runtime.cast.ids()
    posted = press(runtime, "1", BEYOND_WINDOW_S, "2", monkeypatch=monkeypatch)
    assert forced(posted) == [(dex,), (melia,)]


def test_a_third_key_mid_chord_joins_it_rather_than_starting_a_new_one(runtime, monkeypatch):
    """The reason the window is restarted by each new key.

    50ms is tight for three fingers. Measured from the first key, a third
    press landing 60ms in would close the pair and open a fresh chord — so
    "all three" would reach the stage as "those two, and then that one", which
    is an agent being cut off on the PA. Restarting can only ever make the
    command fire later than the operator meant.
    """
    gap = CHORD_WINDOW_S * 0.6
    posted = press(runtime, "1", gap, "2", gap, "3", monkeypatch=monkeypatch)
    assert forced(posted) == [runtime.cast.ids()]


def test_a_held_key_cannot_hold_the_window_open(runtime, monkeypatch):
    """Autorepeat re-sends a key that is already in the chord.

    Two separate guards meet here. The window is only restarted by an id not
    already named, so a leaned-on key cannot hold the chord open; and the
    repeat cooldown then swallows the identical command the next window would
    otherwise produce. Without the second, a stuck key re-forces the same
    agent every 50ms — and a re-force stops him mid-sentence and sends him
    back for a fresh generation, so the key pressed to end a silence makes a
    longer one.
    """
    gap = CHORD_WINDOW_S * 0.6
    posted = press(runtime, "1", gap, "1", gap, "1", monkeypatch=monkeypatch)
    assert forced(posted) == [(runtime.cast.ids()[0],)]


def test_a_nervous_double_tap_does_not_restart_the_agent(runtime, monkeypatch):
    """The likeliest operator error, and the most expensive.

    A force is inaudible for the 2-4s its generation takes and nothing says
    so, which is exactly the situation that invites a second press.
    """
    posted = press(runtime, "1", BEYOND_WINDOW_S, "1", monkeypatch=monkeypatch)
    assert forced(posted) == [(runtime.cast.ids()[0],)]


def test_changing_your_mind_is_never_held_off(runtime, monkeypatch):
    """Only an identical set waits. A different one is a different decision,
    and the thing being overruled is the panel, not the operator."""
    dex, melia, _ = runtime.cast.ids()
    posted = press(runtime, "1", BEYOND_WINDOW_S, "2", monkeypatch=monkeypatch)
    assert forced(posted) == [(dex,), (melia,)]


def test_the_same_force_is_allowed_again_once_the_cooldown_passes(runtime, monkeypatch):
    dex = runtime.cast.ids()[0]
    posted = press(
        runtime, "1", FORCE_REPEAT_COOLDOWN_S + BEYOND_WINDOW_S, "1", monkeypatch=monkeypatch
    )
    assert forced(posted) == [(dex,), (dex,)]


def test_an_unmapped_digit_is_ignored(runtime, monkeypatch):
    """The cast has three seats; `9` names nobody and must not force anyone."""
    posted = press(runtime, "9", monkeypatch=monkeypatch)
    assert forced(posted) == []


def test_j_still_hands_the_floor_to_ricky(runtime, monkeypatch):
    posted = press(runtime, "j", monkeypatch=monkeypatch)
    assert [c.action for c in posted] == [OperatorAction.HAND_TO_MODERATOR]


def test_a_chord_fires_before_an_emergency_interrupt_typed_on_its_heels(runtime, monkeypatch):
    """Typed order is the order they reach the reducer.

    `j` arriving while a chord is still open must not overtake it — the
    operator forced a pair and then changed his mind, and replaying that the
    other way round leaves agents speaking after the emergency stop.
    """
    posted = press(runtime, "12j", monkeypatch=monkeypatch)
    assert [c.action for c in posted] == [
        OperatorAction.FORCE_AGENT,
        OperatorAction.HAND_TO_MODERATOR,
    ]


def test_i_skips_the_introductions(runtime, monkeypatch):
    """The gate `panel_core` will not open for anything Ricky says once the
    cue has been missed — so the recovery has to arrive from the console."""
    posted = press(runtime, "i", monkeypatch=monkeypatch)
    assert [c.action for c in posted] == [OperatorAction.SKIP_INTRODUCTIONS]


def test_a_chord_fires_before_a_skip_typed_on_its_heels(runtime, monkeypatch):
    """Same ordering rule as `j`: a key that is not part of the open chord
    closes it first, so the reducer sees them in the order they were typed."""
    posted = press(runtime, "12i", monkeypatch=monkeypatch)
    assert [c.action for c in posted] == [
        OperatorAction.FORCE_AGENT,
        OperatorAction.SKIP_INTRODUCTIONS,
    ]


def test_m_toggles_the_mic_without_posting_a_command(runtime, monkeypatch):
    """The mute is a runtime flag the audio callback reads, not floor state."""
    posted = press(runtime, "m", monkeypatch=monkeypatch)
    assert posted == []
    assert runtime._muted is True


def test_m_is_inert_under_the_feedback_guard(monkeypatch):
    """That mode gates the mic automatically; a second gate is a trap."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    rt = PanelRuntime(
        PanelCast.from_dir(PERSONA_DIR), use_tts=False, mute_while_agents_speak=True
    )
    posted = press(rt, "m", monkeypatch=monkeypatch)
    assert posted == []
    assert rt._muted is False


def test_the_chord_still_works_under_the_feedback_guard(monkeypatch):
    """`--mute-while-agents-speak` closes the mic, not the console.

    This is the mode the show is expected to run in, and it is the one where
    Ricky cannot talk his way out of trouble — the mic is shut for as long as
    an agent is on the PA. The keys are the whole recovery surface there.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    rt = PanelRuntime(
        PanelCast.from_dir(PERSONA_DIR), use_tts=False, mute_while_agents_speak=True
    )
    dex, melia, _ = rt.cast.ids()
    posted = press(rt, "12", monkeypatch=monkeypatch)
    assert forced(posted) == [(dex, melia)]


# ------------------------------------------------------------- the health line


def lines(capsys) -> str:
    return capsys.readouterr().out


def test_the_mic_socket_announces_itself_when_it_comes_up(runtime, capsys):
    runtime._stt_status("ricky", True, "")
    assert "STT up" in lines(capsys), "no positive confirmation before the room fills"


def test_a_dropped_mic_socket_is_loud(runtime, capsys):
    runtime._stt_status("ricky", True, "")
    capsys.readouterr()
    runtime._stt_status("ricky", False, "connection closed")
    out = lines(capsys)
    assert "STT DOWN" in out
    assert "connection closed" in out, "the operator needs to know which failure this was"


def test_recovery_reports_how_long_the_gap_was(runtime, capsys):
    """That number is what says whether the silence the audience sat through
    was this, or was the panel having nothing to say."""
    runtime._stt_status("ricky", True, "")
    runtime._stt_status("ricky", False, "boom")
    capsys.readouterr()
    runtime._stt_status("ricky", True, "")
    out = lines(capsys)
    assert "STT BACK" in out
    assert "after" in out and "s" in out


def test_a_flapping_socket_paints_one_line_per_change(runtime, capsys):
    """Backoff retries must not scroll the console away at the worst moment."""
    runtime._stt_status("ricky", True, "")
    capsys.readouterr()
    for _ in range(5):
        runtime._stt_status("ricky", False, "boom")
    assert lines(capsys).count("STT DOWN") == 1


def test_an_agents_display_socket_is_reported_quietly(monkeypatch, capsys):
    """Losing one costs that voice's lane on the wall and nothing the panel
    knows, so it may not read like the mic going down."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")

    class _Display:
        pass

    rt = PanelRuntime(PanelCast.from_dir(PERSONA_DIR), use_tts=False, display=_Display())
    agent_id = rt.cast.ids()[0]
    rt._stt_status(agent_id, True, "")
    capsys.readouterr()
    rt._stt_status(agent_id, False, "boom")
    out = lines(capsys)
    assert "STT DOWN" not in out
    assert "stt down" in out
    assert rt.cast[agent_id].name in out


def test_an_ordinary_shutdown_is_not_reported_as_a_fault(runtime, capsys):
    """`_AgentSTTSession.run` returns without a status call once it is told to
    stop — "STT DOWN" in red as the operator presses Ctrl-C is a fault report
    for something that is not a fault. Proven here by the runtime never being
    told, which is the contract the session keeps."""
    runtime._stt_status("ricky", True, "")
    capsys.readouterr()
    assert "STT DOWN" not in lines(capsys)
