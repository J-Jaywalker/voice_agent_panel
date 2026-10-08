"""The live runtime — every piece wired to every other piece.

    uv run panel                    # full pipeline, real everything
    uv run panel --no-tts           # floor + STT + brains, printed not spoken
    uv run panel --log recordings/rehearsal.jsonl

This is the adapter layer `panel_core` refuses to be. It owns the clock, the
sockets and the audio devices, and it stamps every event with `time.monotonic()`
on the way in. `panel_core` stays a pure reducer, which is what makes the log
this writes replayable through modified floor logic afterwards.

Shape of it:

    mic ──> Agent STT ──┬──> HumanSpeechStarted/Ended ──┐
                        │                               ├─> FloorController ─> commands
                        ├──> TranscriptUpdated ─────────┤        (pure)             │
                        └──> TurnYielded ───────────────┘                           │
                                                                                    v
        speakers <── Mixer <── TTS <── StartSpeech ·  DuckSpeech · StopSpeech · ResumeSpeech
                       │               ^
                       │               └── brains (speculative, during the human's turn)
                       │
                       └─> Agent STT (display only) ──> video wall transcript band
                           played audio, never the floor — see `_run_agent_stt`

Two rules the wiring exists to enforce, both from CLAUDE.md:

**Agent speech never reaches the floor through STT.** Agent turns enter
conversation state as text, because we generated them and know them verbatim.
A lossy, latent transcription of our own voices arriving where the verbatim
text already is, is the feedback loop that ends the show.

With `--display` there is a *second* `PanelSTT` (`self.agent_stt`) over each
agent's played audio, and it exists so the video wall's transcript band can be
timed off real speech rather than an assumed words-per-second. It is wired to
exactly one sink — `panel_display` — by `_run_agent_stt`, which never calls
`emit()`. Nothing on it reaches `self.events`, `self.fc.reduce()`, or the
rehearsal log. It changes what the audience *reads*, never what the panel
*knows*. Without `--display` it is not constructed and no socket is opened.

**Endpointing owns stopping, transcripts own understanding.** The barge-in
reflex fires off Speechmatics' `SpeechStarted`, never off a transcript —
`stt.py` turns it into `HumanSpeechStarted` without waiting for words.
Transcripts only ever *refine* that decision. The local Silero VAD this used to
run on is gone (6 Oct 2026, ADR 0001 addendum): one fewer dependency, and the
reflex now shares the STT socket's fate, which is the accepted cost.

One phase runs before any of that exists: **speaker enrolment**. `run()` opens
the PortAudio stream, then awaits `_enrol()` to completion before creating a
single floor task, so the show's first event cannot be a stranger's. Until
enrolment finishes, `_callback` routes mic blocks to the enrolment session
instead of to the floor's transcription; afterwards it routes them back and
never looks again. See `enrolment.py` for the two-session capture and
verification, and `stt.py` for what the identifiers then buy per segment.
`--no-speaker-lock` skips the phase outright and runs the mic ungated, which is
the same mode a *failed* enrolment already falls back to. `--mute-while-agents-
speak` goes one step further for a venue where even that is not good enough:
it implies `--no-speaker-lock` and gates the mic for as long as any agent is
on the PA (`AgentSpeechStarted`/`AgentSpeechEnded`), so there is nothing for
the PA's bleed to reach even unfiltered. The emergency mute key (`m`) is
disabled while it is on, because a manual mute stacked on an automatic one is
a second thing to track mid-show for no gain; the emergency interrupt (`j`)
is untouched, since it reaches the reducer from the keypress, never the mic.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
from panel_core import (
    HUMAN,
    AddressDetected,
    AgentAudioProgress,
    AgentProposal,
    AgentSpeechEnded,
    AgentSpeechStarted,
    AgentUtteranceProgress,
    CueModerator,
    DuckSpeech,
    FloorConfig,
    FloorController,
    HandsRaised,
    OperatorAction,
    OperatorCommand,
    PanelCast,
    PanelState,
    RequestProposals,
    ResumeSpeech,
    StartSpeech,
    StateChanged,
    StopSpeech,
    Tick,
    TranscriptUpdated,
    TurnYielded,
)
from panel_core.prompts import AMBIGUOUS_VERDICT, build_address_context, build_address_recent
from panel_display import DISPLAY_PORT, DisplayServer
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.text import Text

from .address import AddressClassifier, AddressVerdict, BaseAddressClassifier
from .address_typesafe import TypeSafeAddressClassifier
from .aec import EchoCanceller
from .brains import (
    BrainConfig,
    ProposalComplete,
    SentenceReady,
    SignalsReady,
    StreamingClaudeBrain,
)
from .config import PIPELINE_SAMPLE_RATE, AECConfig, BargeInConfig
from .enrolment import (
    DEFAULT_STORE_PATH,
    EnrolledSpeaker,
    SpeakerEnrolment,
    SpeakerStore,
)
from .mixer import Mixer
from .stt import PanelSTT, STTConfig
from .tts import ElevenLabsTTS, TTSConfig

console = Console()

TICK_INTERVAL_S = 0.1  # drives turn-length deadlines and duration classification

# How often a speaking agent reports that sound is still coming out of it.
#
# Well inside `FloorConfig.agent_audio_stall_timeout_s` (2.0s), because the
# watchdog measures the gap between heartbeats: at this interval a healthy turn
# has four chances to report before the floor is taken off it, so one late
# event loop iteration is not a false positive. Cheap — it is a `put_nowait`
# onto a queue the reducer is already draining, and `_agent_audio_progress`
# emits no commands and does not repaint.
HEARTBEAT_INTERVAL_S = 0.5

# How long a `TurnYielded` may be held back waiting for an address verdict.
#
# Speechmatics' `EndOfTurn` lands within a few milliseconds of the final that
# names an agent, so `TurnYielded` normally beats the verdict. Arbitrating first
# means arbitrating with the floor still closed: Ricky gets cued, the panel says
# nothing, and the audience hears the dead air this project spent a week
# removing. So one event — and only that one — waits.
#
# 0.9s against a measured p50 of 611ms, p95 of 728ms and max of 1408ms to
# verdict (`tests/bench_address.py --repeat 3`, 231 calls, dev box).
#
# **This is 0.7 raised, and both halves of that number moved.** The verdict got
# slower: a verdict may now name several panellists, so it is decoded one
# character past the last name rather than at the first delta, which measured
# +115ms p50 (`prompts.decode_address_verdict`). And the fallback got more
# expensive: an expired hold hands the question to the regex, which is still
# correct for every row of the regression corpus but cannot resolve "I'd like
# to hear from the other two" at all — that phrasing is the capability the
# classifier is there for, and it has no regex answer to fall back to.
#
# The hold is not a fixed delay — it ends when the verdict lands — so raising
# the ceiling costs nothing on a normal turn and only buys the tail. What it
# does cost is silence on stage in the case where the classifier is genuinely
# slow, which is why it is 0.9 and not 2.0: past about a second the audience
# hears a gap, and a cued moderator is a better outcome than a late invitation.
# Re-measure on the venue rig before trusting any of it (CLAUDE.md
# § Deployment) — this dial is the first thing to move if the tail is worse
# there.
ADDRESS_HOLD_TIMEOUT_S = 0.9

# How often the video wall is handed a fresh set of audio envelopes.
#
# Matches `panel_display.server.LEVEL_HZ`. The orb interpolates between these
# at display rate, so this only has to be fine enough not to miss a syllable —
# 33ms against syllables of 150-250ms. Raising it does not make the wall
# smoother, because the smoothing is a filter in the browser rather than a
# consequence of the sample rate; it only puts more work on the loop that the
# audio path shares.
DISPLAY_LEVEL_INTERVAL_S = 1 / 30

# How `AddressVerdict.source` reads on the console. Whether a verdict was
# already decided before Ricky stopped talking is the open question about this
# whole approach — free at a cache hit, ~600ms in series with arbitration at a
# fresh call — and nothing measures it today, so every verdict prints one line.
_ADDRESS_SOURCE_LABELS = {
    "speculative_hit": "cache hit",
    "joined": "joined in-flight",
    "fresh": "fresh call",
    "recomputed": "fresh call, partial revised",
    "unavailable": "unavailable — regex fallback",
    "timeout": "timed out — regex fallback",
}


class Candidate:
    """An agent's turn, being written and possibly already being spoken.

    Bridges two different clocks: the model writes at ~40 tokens/sec, the agent
    speaks at ~2.8 words/sec. Writing therefore stays comfortably ahead, and
    `sentences()` simply yields each chunk as it lands — buffered ones
    immediately, later ones as they arrive.
    """

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._closed = False
        # True while `sentences()` is blocked waiting for the model to write
        # the next chunk. Read by `PanelRuntime._pump_heartbeat`, which counts
        # it as progress: an agent whose audio has caught up with its own
        # generation is not a broken audio path, and the liveness watchdog in
        # `panel_core` must not take the floor off it. Bounding *generation*
        # is a separate job with a separate mechanism — see the note in
        # `_pump_heartbeat`.
        self.awaiting_text = False

    async def add(self, sentence: str) -> None:
        await self._queue.put(sentence)

    def close(self) -> None:
        """Synchronous on purpose: it has to be safe from a cancel handler.

        The queue is unbounded, so putting the sentinel never blocks and there
        is nothing to await. Making that explicit matters because the one
        caller that must not be skipped is `_stream_one`'s `CancelledError`
        path — a stream torn down mid-flight while `speak()` is consuming it
        would otherwise leave the sentinel unsent, and `speak()` would wait on
        a queue nobody is ever going to close, holding the floor for the rest
        of the show.
        """
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(None)

    async def sentences(self):
        while True:
            self.awaiting_text = True
            try:
                sentence = await self._queue.get()
            finally:
                # In a `finally` so a cancelled turn does not leave the flag
                # set: a stuck `awaiting_text` would suppress the liveness
                # watchdog for whatever runs next.
                self.awaiting_text = False
            if sentence is None:
                return
            yield sentence

    @classmethod
    def fixed(cls, agent: str, sentences: list[str]) -> Candidate:
        """Wrap an utterance that is already fully known — an introduction's
        fixed text (`FloorController._grant_introduction`) — as a `Candidate`,
        so `speak()` treats it exactly like a streamed one: same TTS calls,
        same mixer, same completion event. There is nothing left to arrive,
        so every sentence is enqueued up front and the candidate is closed
        immediately rather than waiting on a brain that was never asked.
        """
        candidate = cls(agent)
        for sentence in sentences:
            candidate._queue.put_nowait(sentence)
        candidate.close()
        return candidate


class _AudioProgress:
    """Evidence that a turn is still producing sound, for the heartbeat.

    A counter rather than a flag, so `_pump_heartbeat` can tell "audio arrived
    since I last looked" from "audio arrived at some point during this turn".
    Deliberately *not* a liveness ping on the speaking task: a task blocked
    forever awaiting a queue nobody will close is alive in that sense, and is
    exactly the failure the watchdog exists to catch.
    """

    __slots__ = ("chunks",)

    def __init__(self) -> None:
        self.chunks = 0

    def bump(self) -> None:
        self.chunks += 1


class PanelRuntime:
    def __init__(
        self,
        cast: PanelCast,
        *,
        floor_config: FloorConfig | None = None,
        barge_in: BargeInConfig | None = None,
        aec: AECConfig | None = None,
        block_size: int = 256,
        use_tts: bool = True,
        log_path: Path | None = None,
        address_classifier: BaseAddressClassifier | None = None,
        address_backend: str = "typesafe",
        display: DisplayServer | None = None,
        speakers_path: Path | None = None,
        re_enrol: bool = False,
        speaker_lock: bool = True,
        mute_while_agents_speak: bool = False,
    ) -> None:
        self.cast = cast
        self.fc = FloorController(cast, floor_config or FloorConfig())
        self.state = PanelState.for_agents(cast.ids())
        self.barge_in = barge_in or BargeInConfig()
        self.block_size = block_size
        # Off by default — see `AECConfig`. When off this is `None` and the
        # callback below takes the same path it always has; when on it sits
        # upstream of both VAD and the floor's STT, never replacing
        # diarisation's job, only reducing what reaches it.
        aec_cfg = aec or AECConfig()
        self._aec = EchoCanceller(aec_cfg, PIPELINE_SAMPLE_RATE, block_size) if aec_cfg.enabled else None
        self.use_tts = use_tts
        self.log_path = log_path

        self.brain = StreamingClaudeBrain(BrainConfig(), cast=cast)
        # Built undiarized, exactly as before, and reconfigured in `run()` once
        # enrolment has identifiers for Ricky — see `_enrol` and
        # `PanelSTT.identify`. The diarization default is not moved, because
        # `self.agent_stt` below shares `STTConfig` and must stay undiarized.
        self.stt = PanelSTT({"ricky": "human"}, config=STTConfig.from_cast(cast))

        # Speaker enrolment: where Ricky's voiceprint is kept between runs,
        # and whether to capture a fresh one regardless.
        #
        # `speaker_lock=False` turns the whole phase off and makes the other
        # two moot — there is nothing to load and nothing to capture, so
        # `_enrol` returns before it reads either. Default on: this repo's
        # convention is safe-by-default with an explicit opt-out.
        self._store = SpeakerStore(speakers_path)
        self._re_enrol = re_enrol
        # `mute_while_agents_speak` is the last-resort feedback guard: it
        # forces the ungated mode regardless of what was passed, because its
        # whole premise is that enrolment cannot be trusted to tell Ricky's
        # voice apart from the agents' PA bleed, and the automatic mute below
        # is meant to stand in for that distinction, not sit alongside it.
        self._speaker_lock = speaker_lock and not mute_while_agents_speak
        # The enrolment in progress, or None. Read once per audio block by
        # `_callback` to decide where mic audio goes, and set only while the
        # floor's own tasks do not yet exist — see `run()`.
        self._enrolling: SpeakerEnrolment | None = None

        # A second transcription pass, over the agents' *own* played audio,
        # built only when there is a wall to render it on.
        #
        # This is display-only and the isolation is enforced in exactly one
        # place: `_run_agent_stt` drains this queue and never calls `emit()`.
        # Nothing here reaches `self.events`, the reducer, or the log — see
        # this module's docstring and `stt.py`'s. It replaces a pacing
        # estimate the wall used to apply to `AgentUtteranceProgress`, which
        # is emitted when a sentence is handed to the TTS provider and so runs
        # seconds ahead of the room.
        #
        # Channels map each agent id to itself, so `TranscriptUpdated.speaker`
        # is the agent id the band attributes the line to — the same "identity
        # is a fact about the wiring" property that lets Ricky's socket run
        # with diarisation off. Only one agent is ever on the PA
        # (`AgentSpeechStarted`/`Ended`), so there is nothing to diarise here
        # either.
        #
        # Three extra sockets for a surface nobody is looking at is pure
        # waste, so `uv run panel` without `--display` is unchanged: no
        # `PanelSTT`, no mixer tap, no drain task.
        self.agent_stt = (
            PanelSTT(
                {agent_id: agent_id for agent_id in cast.ids()},
                config=STTConfig.from_cast(cast),
            )
            if display is not None
            else None
        )

        unity_db = {p.id: p.output_gain_db for p in cast.personas.values() if p.output_gain_db}
        self.mixer = Mixer(
            cast.ids(),
            PIPELINE_SAMPLE_RATE,
            unity_db=unity_db,
            # The tap is taken at `Mixer.render`, not where chunks arrive from
            # ElevenLabs, and that is the entire point of this change. TTS
            # generates far faster than anyone speaks, so `mixer.feed()`
            # accumulates — `Mixer.buffered_seconds` is seconds deep by the
            # middle of a long turn — and a recogniser fed at arrival time
            # would be transcribing the generation clock, which is the same
            # class of error as the words-per-second estimate this replaces,
            # just with the sign flipped. `render` runs at the output device's
            # rate and is the only clock in this process that matches the
            # room. Same reasoning the orb's post-gain meter already documents
            # in `mixer.py`.
            on_played=self.agent_stt.feed if self.agent_stt is not None else None,
        )
        voice_overrides = {
            p.voice_id: p.voice_settings for p in cast.personas.values() if p.voice_settings
        }
        accent_tags = {p.voice_id: p.accent for p in cast.personas.values() if p.accent}
        pace_tags = {p.voice_id: p.pace for p in cast.personas.values() if p.pace}
        self.tts = (
            ElevenLabsTTS(
                TTSConfig(),
                voice_overrides=voice_overrides,
                accent_tags=accent_tags,
                pace_tags=pace_tags,
            )
            if use_tts
            else None
        )

        # Built only when `FloorConfig.llm_address_detection` is on: it holds an
        # HTTP client and a model choice, and the regex path must cost nothing
        # at all. Injectable so the runtime tests can drive the deferred-
        # TurnYielded logic with no network call and no API key.
        self._address = address_classifier
        if self._address is None and self.fc.config.llm_address_detection:
            self._address = (
                TypeSafeAddressClassifier(cast)
                if address_backend == "typesafe"
                else AddressClassifier(cast)
            )

        # The 12m video wall, or None. It is handed every event and every
        # command and is never asked anything, so nothing on stage depends on
        # it being up — see `panel_display.server.DisplayServer`.
        self._display = display
        # Peak mic RMS since the display last read it, written from the
        # PortAudio callback. Same peak-and-clear contract as the mixer's
        # meters (`Mixer.take_levels`) and for the same reason: the audio
        # blocks and the wall's frames are on different clocks.
        self._mic_level = 0.0
        # Emergency mute, toggled by the `m` key on the console — see
        # `_watch_mute_key`. Read once per audio block by `_callback`, which
        # simply stops feeding the mic to VAD/STT while it is set; nothing
        # downstream needs to know a mute happened. Deliberately invisible on
        # the video wall: `_mic_level` is left unwritten while muted (same
        # branch), so the wall's meter just reads silence, same as if Ricky
        # had stopped talking.
        self._muted = False
        # True during the intro + closing round (`StateChanged.extra`); mic
        # audio is withheld from STT entirely while set, same as `_muted`.
        # `j` still works — it reaches the reducer from the keypress, not the mic.
        self._intro_active = False
        # Feedback guard for a venue where enrolment cannot be trusted: with
        # no speaker lock, nothing downstream can tell Ricky's voice apart
        # from the PA bleeding an agent's own speech back into his mic, so
        # instead of guessing we gate the mic for the whole time an agent is
        # on it. Set from CLI; `_agent_on_floor` is flipped by
        # `AgentSpeechStarted`/`AgentSpeechEnded` in `_drain_events` and read
        # here by `_callback`, the same cross-thread pattern as `_muted`.
        # `j` is unaffected either way — it reaches the reducer from the
        # keypress, not the mic — and `m` is disabled in `_watch_console_keys`
        # while this is on, so there is only ever one thing gating the mic.
        self._mute_while_agents_speak = mute_while_agents_speak
        self._agent_on_floor = False

        self.events: asyncio.Queue = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._speaking_task: asyncio.Task | None = None
        self._speaking_turn = None
        self._ricky_final_text = ""
        self._ricky_live: Live | None = None
        # Live candidates filling while the human is still talking. The floor
        # decides *who* speaks; this holds *what* they say.
        #
        # Keyed by `(agent, epoch)`, not by agent. Several generations for one
        # agent are deliberately in flight at once — see `_request_proposals` —
        # so "Wayne's words" is ambiguous and `StartSpeech.epoch` is what
        # resolves it.
        self._candidates: dict[tuple[str, int], Candidate] = {}
        # The brain stream feeding each of those candidates, same key. A
        # generation is identified by the `speculation_epoch` it was started
        # against: `epoch` moves when a final transcript segment lands and
        # changes the text being answered.
        self._proposal_tasks: dict[tuple[str, int], asyncio.Task] = {}
        # The turn each generation was started against, so a grant elsewhere
        # can retire work that is answering a moment nobody can act on any
        # more. Kept separate from the key because `turn_id` is not part of a
        # generation's identity — two generations in the same turn differ by
        # epoch, and epoch alone is what `StartSpeech` can name.
        self._proposal_turn: dict[tuple[str, int], int] = {}
        self._running = True
        self._last_intro_remaining: tuple[str, ...] | None = None
        self._last_intro_done = False
        # Diagnostic-only state for `_show_state_change`: what was last
        # printed, so a repaint that changed nothing stays silent.
        self._last_invitation: tuple[tuple[str, ...], str | None] = ((), None)
        self._last_address_conflict: tuple[str, ...] = ()
        self._last_awaiting: tuple[str, ...] = ()
        # True from the moment a synthetic TurnYielded is queued until its
        # arbitration resolves. Blocks a second proposal from queuing another
        # one in the meantime — without it, two proposals landing back to
        # back before the first grant's AgentSpeechStarted comes back around
        # the queue can each re-open arbitration and award the floor to two
        # different agents at once.
        self._rearbitration_inflight = False
        # The specific synthetic TurnYielded this runtime is waiting on, if
        # any. Cleared unconditionally once *that exact event* has finished
        # being reduced — see `_drain_events`. Two panel_core paths
        # (`_turn_yielded`'s already-speaking guard, `_grant`'s
        # missing-proposal path) resolve an arbitration with `state, []`: no
        # `AgentSpeechStarted`, no `CueModerator`. Clearing only on those two
        # commands left the flag stuck forever whenever one of those paths
        # fired, gating every later proposal out of re-arbitration. Identity
        # on the event, not the command it produced, is what makes this
        # robust to outcomes we cannot enumerate from here.
        self._pending_rearbitration_event: TurnYielded | None = None
        # The address classification running against the most recent human
        # final, and the one `TurnYielded` waiting behind it. Deliberately
        # *runtime* state: the reducer gets no new field for this race, it just
        # sees `AddressDetected` and then `TurnYielded`, in that order.
        self._address_task: asyncio.Task | None = None
        self._held_turn: TurnYielded | None = None
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("")

    # ------------------------------------------------------------ console clock

    def _stamp(self) -> str:
        """`HH:MM:SS.mmm`, wall clock.

        Wall clock so a console line can be lined up against a rehearsal
        recording, the venue's own logs or someone's note of when a thing went
        wrong. Milliseconds because the numbers worth arguing about — dead air
        before an agent takes the floor, barge-in reflex latency — are the
        differences between two of these lines.
        """
        return f"[dim]{datetime.now(UTC).astimezone().strftime('%H:%M:%S.%f')[:-3]}[/]"

    def _print(self, markup: str) -> None:
        """Every console line in the runtime goes through here, stamped."""
        console.print(f"{self._stamp()} {markup}")

    # ------------------------------------------------------------ audio thread

    def _callback(self, indata, outdata, frames, timeinfo, status) -> None:
        """PortAudio callback. Never blocks, never awaits, never allocates much."""
        del timeinfo, status
        mono = indata[:, 0]
        # Off by default (`AECConfig.enabled`) — when off, `self._aec` is
        # `None` and this is exactly the line it always was. When on, this
        # cancels the known, correlated echo of `self.mixer`'s own output out
        # of the mic signal before anything downstream — VAD or STT — ever
        # sees it. See `aec.py`.
        if self._aec is not None:
            mono = self._aec.process(mono)
        pcm = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)

        # One PortAudio stream serves both the enrolment phase and the show, so
        # this is where a mic block finds out which one is running. Deliberately
        # one attribute read and one branch: this is the most latency-sensitive
        # function in the file, and the two consumers are mutually exclusive in
        # time rather than concurrent, so there is nothing to fan out to.
        #
        # During enrolment the floor's transcription is not merely idle, it does
        # not exist yet — `self.stt` has not been started. Feeding it here would
        # grow a queue nobody reads.
        enrolling = self._enrolling
        # The feedback guard's own gate — see `self._mute_while_agents_speak`
        # above — collapsed into one flag so the branches below stay the same
        # shape they always were.
        agent_feedback_muted = self._mute_while_agents_speak and self._agent_on_floor
        if enrolling is not None:
            enrolling.feed(pcm.tobytes())
        elif not self._muted and not self._intro_active and not agent_feedback_muted:
            # Mic audio goes to the STT that feeds the floor — both the words
            # and, via `SpeechStarted`, the barge-in reflex. Agent audio never
            # goes here.
            #
            # Unattenuated, always, with respect to Ricky's own voice. The PA
            # does bleed back into this mic while an agent is speaking, and
            # what that bleed must not do is get attributed to Ricky — that
            # is diarisation's job, downstream in `panel_runtime/stt.py`,
            # which answers it per segment on the evidence rather than
            # pre-emptively turning the signal down. AEC above (when enabled)
            # does not change this: it subtracts only the known, correlated
            # copy of this process's own output, never a blind gain dip, so
            # it costs nothing of Ricky's own words even when he talks over
            # an agent — diarisation remains the layer that decides whose
            # words these are.
            self.stt.feed("ricky", pcm.tobytes())
        # Muted or mid-intro: the block is simply dropped here. VAD and STT
        # never see it, so there is nothing for them to react to and nothing
        # for the wall's mic meter to show — see `self._muted`/`self._intro_active`.

        if (
            self._display is not None
            and not self._muted
            and not self._intro_active
            and not agent_feedback_muted
        ):
            self._mic_level = max(self._mic_level, float(np.sqrt(np.mean(np.square(mono)))))

        # `render` also drives `Mixer.on_played`, which with `--display` hands
        # each agent's block to `self.agent_stt` — a transcription session
        # whose output only ever reaches the video wall (`_run_agent_stt`).
        # That is not the path above: nothing off it is emitted, so no agent's
        # voice can arrive at the reducer through a microphone-shaped hole.
        rendered = self.mixer.render(frames)
        if self._aec is not None:
            # What is actually reaching the room, recorded for a future
            # block's `process()` call to cancel out of the mic — see
            # `EchoCanceller`'s docstring on why this is taken here and not
            # earlier in the TTS pipeline.
            self._aec.push_farend(rendered)
        outdata[:, 0] = rendered

    def _watch_console_keys(self) -> None:
        """Console-only `m`/`j` key watcher.

        Runs on its own thread, blocked in `read(1)` between presses — cheap,
        and keeps the audio callback above untouched by anything stdin-shaped.
        `m` toggles `self._muted`, which `_callback` is the only other reader
        of. Disabled outright while `self._mute_while_agents_speak` is on —
        that mode already gates the mic automatically off `_agent_on_floor`,
        and a manual mute stacked on top of it is a second, independent thing
        for an operator to track mid-show for no gain. `j` is the emergency
        interrupt: it posts an `OperatorCommand`
        (`HAND_TO_MODERATOR`) onto the same event queue a real barge-in would
        land on, via `call_soon_threadsafe` since this thread is not the event
        loop's — `panel_core` already stops whoever is speaking and hands the
        floor to Ricky for that action, so there is no new floor logic here,
        only the keypress. Neither key's *press* reaches the wall, the log or
        any event by itself; only `j`'s resulting `StopSpeech`/state change
        does, exactly as a spoken interrupt's would. Silently does nothing if
        stdin is not a real terminal (e.g. piped input, a test harness).
        """
        try:
            import termios
            import tty
        except ImportError:
            return
        try:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
        except (OSError, ValueError, termios.error):
            return
        try:
            tty.setcbreak(fd)
            while self._running:
                ch = sys.stdin.read(1).lower()
                if ch == "m":
                    if self._mute_while_agents_speak:
                        self._print(
                            "[dim]m is disabled: --mute-while-agents-speak already "
                            "gates the mic[/]"
                        )
                    else:
                        self._muted = not self._muted
                        if self._muted:
                            self._print("[bold red]MIC MUTED[/] (press m to unmute)")
                        else:
                            self._print("[bold green]mic live[/]")
                elif ch == "j":
                    self._print("[bold red]EMERGENCY INTERRUPT[/] (j) — floor to Ricky")
                    if self._loop is not None:
                        self._loop.call_soon_threadsafe(
                            self.emit,
                            OperatorCommand(t=time.monotonic(), action=OperatorAction.HAND_TO_MODERATOR),
                        )
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    # ------------------------------------------------------------- event path

    def emit(self, event) -> None:
        """The single ordered way into the reducer."""
        self.events.put_nowait(event)

    async def _drain_events(self) -> None:
        while self._running:
            event = await self.events.get()
            if self._mute_while_agents_speak:
                # Read by `_callback` on the audio thread — a plain bool
                # assignment, same cross-thread contract as `_muted`.
                if isinstance(event, AgentSpeechStarted):
                    self._agent_on_floor = True
                elif isinstance(event, AgentSpeechEnded):
                    self._agent_on_floor = False
            self._record(event)
            if self._display is not None:
                # Before the reducer, so the wall sees cause then effect in
                # the order they happened. `_record` is not the hook because
                # it returns early when there is no log path, and a wall that
                # only works with `--log` is a trap.
                self._display.on_event(event)
            self.state, commands = self.fc.reduce(self.state, event)
            for command in commands:
                await self._execute(command)
            if isinstance(event, OperatorCommand) and event.action in (
                OperatorAction.HAND_TO_MODERATOR,
                OperatorAction.KILL_ALL,
            ):
                # Both bump `PanelState.turn_id` in the reducer for exactly
                # this reason: an emergency interrupt must stop agents
                # thinking on the spot, not leave their generations running
                # against pre-interrupt text until some later round happens
                # to open and retires them as a side effect. `speaking` is
                # already None by now, so nothing is exempted.
                self._retire_stale_turns(self.state.turn_id, self.state.speaking)
            if isinstance(event, AgentSpeechStarted):
                # The floor is genuinely occupied now — safe to consider a
                # fresh re-arbitration once this agent's turn ends.
                self._rearbitration_inflight = False
            if event is self._pending_rearbitration_event:
                # The synthetic TurnYielded we emitted to trigger this
                # arbitration attempt has now been fully reduced, whatever the
                # outcome — a grant, a CueModerator, or one of the silent
                # `state, []` guards in panel_core that emits neither. The
                # attempt is resolved either way, so the gate comes down
                # unconditionally rather than only on the two commands that
                # happen to fire on the happy path.
                self._rearbitration_inflight = False
                self._pending_rearbitration_event = None
            self._maybe_rearbitrate(event)

    def _maybe_rearbitrate(self, event) -> None:
        """Re-open arbitration once a proposal lands with the floor idle.

        `_proposal` (panel_core's `floor.py`) only *stores* a candidate — it
        never grants the floor itself, because a pure reducer must not
        schedule anything for itself. The floor is only granted from inside
        `_turn_yielded`, which fires solely on `TurnYielded`, and this
        runtime's only source of that event is Speechmatics' `EndOfTurn`
        (`stt.py`). With Ricky silent after handing off multiple turns (e.g.
        an open invitation worth more than one turn), nothing would otherwise
        re-trigger arbitration and the panel would stall with a live
        invitation and idle proposals forever. The introduction round used to
        be the motivating example here, but it no longer goes through this
        path at all — it never asks for proposals in the first place, and
        `_turn_yielded` treats a `TurnYielded` arriving mid-round as a no-op
        rather than something to arbitrate (see `panel_core.floor.
        _turn_yielded` and `_advance_introductions`).

        `panel_sim` has the same requirement and synthesises `TurnYielded`
        once after gathering a batch of proposals (see its `_gather`). Here
        proposals stream in one at a time over the wire, so the check runs
        after each one instead — it is a no-op once someone is already
        speaking or the invitation is spent.
        """
        if not isinstance(event, AgentProposal) or self._rearbitration_inflight:
            return
        state = self.state
        invitation = state.invitation
        if (
            state.speaking is None
            and state.floor_holder is None
            and not state.human_speaking
            and invitation is not None
            and invitation.is_live()
        ):
            self._rearbitration_inflight = True
            turn_yielded = TurnYielded(t=time.monotonic())
            self._pending_rearbitration_event = turn_yielded
            self.emit(turn_yielded)

    def _record(self, event) -> None:
        if not self.log_path:
            return
        payload = {"type": type(event).__name__}
        if is_dataclass(event):
            payload |= {k: _jsonable(v) for k, v in asdict(event).items()}
        with self.log_path.open("a") as fh:
            fh.write(json.dumps(payload) + "\n")

    async def _execute(self, command) -> None:
        if self._display is not None:
            # `HandsRaised`, `CueModerator` and `StateChanged` exist for this
            # surface and nothing else reads them. `DuckSpeech`/`ResumeSpeech`
            # never reach `PanelState` at all, so the backchannel reflex would
            # be invisible on a wall built from state alone.
            self._display.on_command(command)

        match command:
            case RequestProposals():
                self._print(f"  [dim]… gathering proposals ({command.reason})[/]")
                self._request_proposals(command.agents)

            case StartSpeech():
                self._start_speaking(command)

            case StopSpeech():
                # Audible immediately; the provider is told afterwards.
                self.mixer.stop(command.agent)
                task = self._speaking_task
                if self._speaking_turn is not None:
                    self._speaking_turn.cancel()
                if task is not None:
                    task.cancel()
                self._print(
                    f"  [red]⏹ {self.cast[command.agent].name}[/] [dim]({command.reason.value})[/]"
                )
                # `speak()`'s own CancelledError handler emits AgentSpeechEnded
                # with the utterance actually spoken so far. Emitting it again
                # here would double-fire the floor's continuation logic (e.g.
                # popping the same agent out of an introduction round twice).
                # Only fall back to emitting it ourselves when there is no
                # task to cancel — nothing else will ever report the stop.
                if task is None:
                    self.emit(
                        AgentSpeechEnded(t=time.monotonic(), agent=command.agent, completed=False)
                    )

            case DuckSpeech():
                self.mixer.duck(command.agent, command.gain_db, command.ramp_ms)

            case ResumeSpeech():
                self.mixer.resume(command.agent, command.ramp_ms)

            case HandsRaised():
                hands = "  ".join(f"{self.cast[a].name} {s:.2f}" for a, s in command.agents)
                self._print(f"  [yellow]✋ wants in:[/] {hands} [dim](not invited)[/]")

            case CueModerator():
                # Arbitration resolved with nobody granted — the floor is
                # still idle, so a later proposal is free to try again.
                self._rearbitration_inflight = False
                # `command.reason` is a `CueReason` — a `str`-mixin `Enum`, so
                # plain interpolation prints "CueReason.NO_PROPOSALS" rather
                # than the value. `.value` is what tells "nobody proposed"
                # apart from "the one agent Ricky named had nothing", which is
                # the entire point of the enum replacing the old undifferen-
                # tiated `no_candidate` string.
                self._print(f"  [magenta]▸ back to Ricky ({command.reason.value})[/]")

            case StateChanged():
                self._intro_active = bool(
                    command.extra.get("intro_remaining") or command.extra.get("closing_remaining")
                )
                self._show_state_change(command)

            case _:
                pass

    def _show_state_change(self, command: StateChanged) -> None:
        """Surface intro-round progress and invitation/addressee diagnostics.

        `StateChanged` fires on nearly every transition, so this only prints
        on the fields that changed rather than on every paint. The invitation
        fields are the whole reason this rewrite happened: the original
        failure printed `▸ back to Ricky (no_candidate)` twice with no way to
        tell "nobody proposed" from "the invitation was held by the wrong
        agent" — that answer was sitting in `extra["invited"]` all along, just
        never rendered.
        """
        remaining = command.extra.get("intro_remaining")
        if remaining != self._last_intro_remaining:
            self._last_intro_remaining = remaining
            if remaining:
                names = ", ".join(self.cast[a].name for a in remaining)
                self._print(f"  [dim]intros: {len(remaining)} remaining — {names}[/]")

        done = command.extra.get("intro_done")
        if done and not self._last_intro_done:
            self._last_intro_done = True
            self._print("  [dim]intros: all done[/]")

        # `invited_agents`, not `invited`: the latter is None for both an open
        # floor and a named pair, and "invited the panel" is the wrong thing to
        # print when Ricky named two of the three.
        invited = tuple(command.extra.get("invited_agents") or ())
        source = command.extra.get("invitation_source")
        if (invited, source) != self._last_invitation:
            self._last_invitation = (invited, source)
            if source is None:
                self._print("  [dim]floor: closed (no live invitation)[/]")
            else:
                who = self._names(invited) or "the panel"
                role = command.extra.get("invitation_role") or "-"
                rule = command.extra.get("invitation_rule") or "-"
                self._print(f"  [dim]floor: invited {who} — {source}/{role} ({rule})[/]")

        awaiting = tuple(command.extra.get("awaiting_agents") or ())
        if awaiting != self._last_awaiting:
            self._last_awaiting = awaiting
            if awaiting:
                self._print(
                    f"  [dim]… holding for {self._names(awaiting)} "
                    f"({self.fc.config.invited_agent_grace_s:.1f}s)[/]"
                )

        conflict = command.extra.get("address_conflict") or ()
        if conflict != self._last_address_conflict:
            self._last_address_conflict = conflict
            if conflict:
                self._print(
                    f"  [yellow]✋ ambiguous address:[/] {self._names(conflict)} "
                    "[dim](floor stays closed)[/]"
                )

    def _names(self, agent_ids: Iterable[str]) -> str:
        """Agent ids as the names a human reads, comma-separated."""
        return ", ".join(self.cast[a].name for a in agent_ids if a in self.cast.personas)

    # --------------------------------------------------------------- proposals

    def _request_proposals(self, agents: tuple[str, ...]) -> None:
        """Speculate during the human's turn. Generations race; none is discarded.

        The floor re-requests proposals on roughly every 0.8s of partial
        transcript and unconditionally on every final segment, while a brain
        takes 1.7-2.4s to reach `SignalsReady` (`tests/bench_brains.py`). Those
        two facts do not fit together under any cancel-and-restart rule, and
        this method has now been wrong in both directions:

        Cancelling on every *request* meant no agent ever finished at all.
        Cancelling on every *final* — the version this replaces — looked
        careful and was worse, because of where the finals fall. Speechmatics'
        `EndOfTurn` arrives within a few milliseconds of the final that names an
        agent, so the last teardown of a turn always landed at the exact instant
        the work was needed. Arbitration ran against an empty proposal set on
        every invitation and the full cold generation latency sat on the
        critical path, which on stage was about six seconds of Ricky covering
        for a panel that had thrown its answer away.

        So nothing is cancelled here. Every round starts a *new* generation per
        agent and leaves the running ones alone; they all finish; `panel_core`'s
        `_proposal` keeps whichever is newest by epoch rather than whichever
        arrives last. An answer to "Thanks, everybody. Um, so," is a poor answer
        to "where are we on the adoption curve" — but it exists, it is one
        `_stale` check away from being rejected on its own merits, and having it
        is strictly better than having nothing. Cost is not a constraint
        (CLAUDE.md): a discarded proposal is cheap and a silence in front of 400
        people is not.

        *Every* round, and that word is the fix to the second half of the same
        stage failure. `speculation_epoch` used to move only when a final
        landed, so every speculative round inside one human turn asked under
        the same `(agent, epoch)` key — and the still-running check below then
        refused to start a second generation for an agent whose first one had
        not finished. Each agent therefore got at most *one* in-flight
        generation per human turn. The two fast agents finished in ~2.1s, freed
        their key, and were re-asked against a later partial; the slow one
        (3967ms, spanning Ricky's entire question) was not, so its single answer
        was necessarily written against the oldest input of the three. The
        slower the agent, the staler the input behind its winning line — every
        single time, and always the same agent. `_ask_for_proposals` now takes a
        fresh label per round, so a slow generation no longer holds the key
        against its own replacement.

        The cost of that is concurrency: a round opens at most every
        `speculation_interval_s` (0.8s) and a generation lives 2-4s, so expect
        up to ~5 in flight per agent during a long human turn. That is the
        number to watch on the venue rig — not the spend, which is not a
        constraint, but provider concurrency limits and any TTFB degradation
        under it. `speculation_interval_s` is the dial if the rig cannot take
        it; measure before moving it (CLAUDE.md § Deployment).

        Two things are still retired, both after the fact rather than
        pre-emptively, and neither can leave an agent with nothing:

        * `_retire_superseded` drops an agent's older generations once a newer
          one has actually produced a proposal — never before, so the fallback
          is only released when something better is genuinely in hand.
        * A turn boundary (`_retire_stale_turns`) drops work scored against a
          conversational moment a grant has since closed.

        The agent currently on the PA is never touched by either: its stream may
        still be feeding `speak()`'s `Candidate` queue live, and cancelling it
        would cut off audio already playing.

        Each agent streams. Signals arrive first and go straight to the floor
        controller, so arbitration can run while the text is still being
        written. Sentences accumulate in a `Candidate`, ready to be spoken the
        moment that agent is granted the floor.
        """
        snapshot = self.state
        epoch = snapshot.speculation_epoch
        turn_id = snapshot.turn_id
        speaking = snapshot.speaking
        # The transcript timestamp this round's input is frozen at — which
        # question these generations are answers to. `panel_core`'s
        # `_ask_for_proposals` stamped it, alongside the epoch above, in the
        # same `reduce()` call that produced the command being executed here,
        # and `_drain_events` installs the new state before running any command
        # — so `self.state` already carries this round's pair and the two can
        # never be read from different rounds. It rides out on every
        # `AgentProposal` as `input_t`, and `FloorController._stale` measures
        # it. Read anything off `snapshot`, never off `self.state` again below:
        # a grant landing mid-loop would otherwise split the round in two.
        input_t = snapshot.last_proposal_request_t

        self._retire_stale_turns(turn_id, speaking)

        for agent_id in agents:
            if agent_id == speaking:
                continue
            key = (agent_id, epoch)
            existing = self._proposal_tasks.get(key)
            if existing is not None and not existing.done():
                # Unreachable as the code stands, and kept as a guard rather
                # than deleted. `_ask_for_proposals` takes a fresh epoch per
                # round and no single `reduce()` emits two `RequestProposals`,
                # so no key can be asked twice — but this is what enforces "at
                # most one live generation per key", and the key is what
                # `StartSpeech.epoch` uses to find an agent's words. Without
                # it, a second generation on one key would overwrite
                # `_candidates[key]` and orphan the first task with nothing
                # left holding a reference to cancel it. Cheap insurance
                # against the epoch ever stopping being per-round; if it does,
                # this skip is the bug it caused, not the cause.
                continue
            candidate = Candidate(agent_id)
            self._candidates[key] = candidate
            self._proposal_turn[key] = turn_id
            self._proposal_tasks[key] = asyncio.create_task(
                self._stream_one(candidate, snapshot, epoch, input_t),
                name=f"propose-{agent_id}-e{epoch}",
            )

    def _retire_superseded(self, agent_id: str, epoch: int) -> None:
        """Drop `agent_id`'s generations older than `epoch`.

        Called only once `epoch` has produced a proposal. That ordering is the
        whole safety property: an older generation is the fallback answer until
        a better one exists, so it is released when the replacement is in hand
        and not a moment earlier. Doing this at request time instead is exactly
        the bug this design replaces.

        Per-round epochs make this fire on every round rather than only at a
        turn boundary, so the *window* between dropping the old candidates and
        the replacement proposal being reduced now matters. There isn't one:
        `_stream_one` calls this and then `emit`s the replacement with no
        `await` in between, and `_drain_events` only ever suspends on an empty
        queue — so nothing can arbitrate against a dropped candidate, because
        the proposal that replaces it is already queued ahead of any event that
        could. Keep those two statements adjacent.
        """
        if agent_id == self.state.speaking:
            return
        for key in [k for k in self._proposal_tasks if k[0] == agent_id and k[1] < epoch]:
            task = self._proposal_tasks.pop(key)
            if not task.done():
                task.cancel()
            self._proposal_turn.pop(key, None)
            self._candidates.pop(key, None)

    def _retire_stale_turns(self, turn_id: int, speaking: str | None) -> None:
        """Drop work generated for a turn that has since been granted away.

        `turn_id` moves when someone takes the floor, so these proposals were
        scored against a conversational moment nobody can act on any more. This
        is a genuine supersede rather than a guess about freshness, which is
        why it is safe to do pre-emptively where the epoch test was not.
        """
        for key in [
            k for k in self._proposal_tasks if self._proposal_turn.get(k, turn_id) != turn_id
        ]:
            if key[0] == speaking:
                continue
            task = self._proposal_tasks.pop(key)
            if not task.done():
                task.cancel()
            self._proposal_turn.pop(key, None)
            self._candidates.pop(key, None)

    async def _stream_one(
        self, candidate: Candidate, snapshot: PanelState, epoch: int, input_t: float
    ) -> None:
        """Run one generation and report it.

        Args:
            candidate: Where the streamed sentences accumulate, ready for
                `speak()` if this generation wins the floor.
            snapshot: The conversation state this generation answers. Frozen at
                request time, which is what makes `input_t` meaningful.
            epoch: This round's label, from `PanelState.speculation_epoch`.
            input_t: The transcript timestamp `snapshot` was taken at. Goes out
                on the `AgentProposal` so `FloorController._stale` can judge
                whether this line is an answer to the question that eventually
                opened the floor, rather than to the preamble in front of it.
        """
        persona = self.cast[candidate.agent]
        spoke = False
        # Whether a turn granted off this generation would be the last one
        # before the floor returns to Ricky — read off the same snapshot the
        # prompt is built from, so it describes the moment this line answers.
        near_turn_limit = (
            snapshot.consecutive_agent_turns + 1 >= self.fc.config.max_consecutive_agent_turns
        )
        try:
            async for event in self.brain.stream(
                persona, snapshot, near_turn_limit=near_turn_limit
            ):
                if isinstance(event, SignalsReady):
                    # The floor can be arbitrated now. The utterance is carried
                    # by the Candidate, not by the event — the reducer decides
                    # who speaks and never needs to know what they will say.
                    self._print(
                        f"  [dim]· {persona.name} ready ({event.elapsed_ms:.0f}ms, e{epoch})[/]"
                    )
                    # Now — and only now — this agent's older generations are
                    # surplus. Retiring them here rather than when the newer
                    # request went out is what guarantees the fallback outlives
                    # its replacement's latency. See `_retire_superseded`.
                    self._retire_superseded(candidate.agent, epoch)
                    self.emit(
                        AgentProposal(
                            t=time.monotonic(),
                            agent=candidate.agent,
                            utterance="",
                            signals=event.signals,
                            epoch=epoch,
                            # Not `t`: the floor has to know how old this
                            # line's *input* was, and a generation that raced
                            # past the question it was written before would
                            # otherwise arrive looking newer than the question.
                            input_t=input_t,
                        )
                    )
                elif isinstance(event, SentenceReady):
                    spoke = True
                    await candidate.add(event.text)
                elif isinstance(event, ProposalComplete):
                    candidate.close()
        except asyncio.CancelledError:
            # Close before re-raising. `speak()` may already be consuming this
            # queue — the agent whose turn is being granted is only protected
            # from `_request_proposals` once `AgentSpeechStarted` has come back
            # around the event queue and set `state.speaking`, and until then a
            # turn-superseded teardown can reach a stream that is feeding live
            # audio. Closing ends that turn with whatever was actually said;
            # leaving it open wedges the floor.
            candidate.close()
            raise
        except Exception as exc:  # noqa: BLE001 — one dead brain must not stop the panel
            self._print(f"  [red]x {candidate.agent} brain failed: {str(exc)[:60]}[/]")
            candidate.close()
        finally:
            if not spoke and self._candidates.get((candidate.agent, epoch)) is candidate:
                # This generation produced no speakable text, so it must not be
                # left parked where a grant can find it. `state.proposals` can
                # still hold an *earlier* generation's hand-raise for the same
                # agent — the proposal and the candidate are separate objects
                # with separate lifetimes, and `_request_proposals` replaces the
                # candidate whenever the previous stream has finished. Dropping
                # it makes `_start_speaking` report an agent with nothing to say
                # instead of printing a name over silence, which is the whole
                # failure this guard exists for. The identity check is what
                # keeps a late teardown from evicting a newer, live candidate.
                del self._candidates[(candidate.agent, epoch)]

    # ------------------------------------------------------------------ speech

    def _start_speaking(self, command: StartSpeech) -> None:
        """Dispatch a grant, honouring `command.lead_in_s` if it has one.

        A non-zero `lead_in_s` (introduction round only) is a silent beat
        before this agent's first word — the instant jump from Ricky's cue
        straight to Dexter's opening line read as a glitch on stage-adjacent
        testing, not a person taking a moment to go first. Scheduled as its
        own task rather than an `await` inline here: `_execute` runs on
        `_drain_events`'s single event loop, and blocking that loop for the
        pause would stall every other event — barge-in included — for its
        duration. Everything else about the grant is unaffected; it just
        starts a beat later.
        """
        if command.lead_in_s:
            asyncio.create_task(
                self._start_speaking_after_delay(command), name=f"lead-in-{command.agent}"
            )
        else:
            self._start_speaking_now(command)

    async def _start_speaking_after_delay(self, command: StartSpeech) -> None:
        await asyncio.sleep(command.lead_in_s)
        self._start_speaking_now(command)

    def _start_speaking_now(self, command: StartSpeech) -> None:
        persona = self.cast[command.agent]
        candidate: Candidate | None = None
        if command.utterance:
            # A fixed line, not a generated one, and it wins unconditionally.
            # An ordinary grant's `Proposal.utterance` is always `""` in this
            # runtime (the real words live in a `Candidate`, filled in by
            # `_stream_one` as the model streams), so a *non-empty*
            # `command.utterance` means the words are already fully known and
            # there was never anything to stream from a model. Today the
            # introduction round is the only source of these
            # (`FloorController._grant_introduction`).
            #
            # This must not defer to a cached candidate, and that is the bug
            # this branch was written wrong for: it used to require
            # `self._candidates.get(...) is None`, which held only in theory.
            # Ricky's opening sentence arrives as a long run of *partial*
            # transcripts, each one re-firing speculation every 0.8s
            # (`scoring.speculation_interval_s`), while the introduction latch
            # in `panel_core.floor` fires only on the *final* transcript. So by
            # the time the round starts, `_request_proposals` has already parked
            # a speculative candidate for every agent, the fixed-text branch was
            # skipped, and each agent read out the model's improvised line
            # instead of the hand-authored `Persona.introduction` — the one
            # piece of the show that was deliberately taken away from the model
            # so it could not come back empty on stage. Any candidate sitting
            # here is speculation about a moment that has passed; it is
            # discarded, not preferred.
            #
            # `_grant_introduction` has already run the text through
            # `sanitise()` — fixed text does not get to bypass that rule just
            # because nobody generated it live (CLAUDE.md) — so it is spoken
            # as-is and must not be sanitised a second time. The sentence split
            # is only for pacing symmetry with a streamed turn: the whole line
            # is already known, so there is no generation latency to save.
            sentences = [s for s in re.split(r"(?<=[.!?])\s+", command.utterance) if s]
            if sentences:
                # Tear down this agent's stale speculation before installing
                # the fixed candidate, so nothing is left running that could
                # write into a queue nobody reads, or evict what we install.
                # The eviction race is real: `_stream_one`'s `finally` does
                # `del self._candidates[agent]` for a generation that produced
                # no speakable text. Two things close it, in this order. First,
                # `_start_speaking` is synchronous and never awaits, so a
                # cancelled task cannot reach its `finally` until we have
                # returned to the event loop — by which point the fixed
                # candidate is already in place. Second, that `finally` is
                # guarded by an identity check against its *own* candidate, so
                # once ours is the one in the dict a late teardown no longer
                # matches and leaves it alone. Cancelling first is therefore
                # safe rather than load-bearing, but it keeps the teardown
                # shape identical to `_request_proposals`. The cancellation
                # also runs `_stream_one`'s `CancelledError` path, which closes
                # the *old* candidate — correct: nothing is consuming it, and
                # an unclosed queue is what wedges the floor.
                #
                # *Every* generation for this agent goes, not just one: several
                # run concurrently now, and a fixed line supersedes all of them
                # unconditionally.
                for key in [k for k in self._proposal_tasks if k[0] == command.agent]:
                    stale = self._proposal_tasks.pop(key)
                    if not stale.done():
                        stale.cancel()
                    self._proposal_turn.pop(key, None)
                    self._candidates.pop(key, None)
                candidate = Candidate.fixed(command.agent, sentences)
                self._candidates[(command.agent, command.epoch)] = candidate
        else:
            # The ordinary streamed grant: the words are still arriving, so the
            # candidate `_request_proposals` parked is the whole point.
            # `command.epoch` names *which* of this agent's parked candidates
            # won arbitration — the floor scored one specific generation's
            # signals, and speaking a different generation's words would air an
            # answer nobody arbitrated.
            candidate = self._candidates.get((command.agent, command.epoch))
        if candidate is None:
            # Either nothing was ever requested for this agent, or the stream
            # that was requested finished without producing a speakable word
            # and `_stream_one` dropped it. Both mean the same thing here and
            # both must be *loud*: the failure this replaces was an agent's
            # name appearing on stage with nothing under it and the turn quietly
            # consumed, which from the console was indistinguishable from an
            # agent choosing to say nothing.
            self._print(f"  [red]x {persona.name} has nothing to say — turn skipped[/]")
            self.emit(AgentSpeechEnded(t=time.monotonic(), agent=command.agent, completed=False))
            return

        console.print()
        self._print(f"[bold cyan]{persona.name}[/]")
        self.emit(AgentSpeechStarted(t=time.monotonic(), agent=command.agent))

        progress = _AudioProgress()
        heartbeat = asyncio.create_task(
            self._pump_heartbeat(command.agent, progress, candidate),
            name=f"heartbeat-{command.agent}",
        )

        async def speak() -> None:
            spoken: list[str] = []
            try:
                if self.tts is None:
                    # --no-tts: hold the floor for a plausible speaking duration
                    # so floor behaviour can be exercised without audio.
                    async for sentence in candidate.sentences():
                        spoken.append(sentence)
                        self._print(f"  {sentence}")
                        self.emit(
                            AgentUtteranceProgress(
                                t=time.monotonic(), agent=command.agent, text=sentence
                            )
                        )
                        # Slept in slices rather than one long sleep so the
                        # heartbeat keeps reporting through a long sentence, and
                        # the watchdog is therefore exercised in --no-tts too.
                        # A single `sleep(len/2.8)` is 10s for a 30-word
                        # sentence, well past `agent_audio_stall_timeout_s`, so
                        # the floor would be taken off every printed turn.
                        remaining = len(sentence.split()) / 2.8
                        while remaining > 0:
                            slice_s = min(remaining, HEARTBEAT_INTERVAL_S / 2)
                            await asyncio.sleep(slice_s)
                            remaining -= slice_s
                            progress.bump()
                else:
                    turn = await self.tts.open(voice_id=persona.voice_id)
                    self._speaking_turn = turn
                    self.mixer.clear(command.agent)

                    async def pump_audio() -> None:
                        async for chunk in turn.chunks():
                            self.mixer.feed(command.agent, chunk)
                            # One bump per chunk off the socket: the heartbeat's
                            # primary evidence, and the only one that
                            # distinguishes a live provider from a dead one.
                            progress.bump()

                    audio = asyncio.create_task(pump_audio(), name="tts-audio")
                    async for sentence in candidate.sentences():
                        spoken.append(sentence)
                        # Stamped when the sentence is *pushed*, which runs
                        # ahead of the audio — the model writes faster than
                        # the agent speaks (`Candidate`). Read these gaps as
                        # generation pace, not as what the room hears.
                        self._print(f"  {sentence}")
                        # Emitted before the push, not after: `push()` awaits a
                        # socket and the whole value of this event is lead time
                        # for the other two agents' generations. The reducer
                        # only folds it in for whoever is currently `speaking`,
                        # so a sentence from a turn the floor has already moved
                        # past is discarded there rather than guarded here.
                        self.emit(
                            AgentUtteranceProgress(
                                t=time.monotonic(), agent=command.agent, text=sentence
                            )
                        )
                        await turn.push(sentence)
                    await turn.finish()
                    await audio
                    self.mixer.finish(command.agent)
                    while not self.mixer.is_drained(command.agent):
                        await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                # Interrupted. What was said still counts — record it.
                self.emit(
                    AgentSpeechEnded(
                        t=time.monotonic(),
                        agent=command.agent,
                        completed=False,
                        utterance=" ".join(spoken),
                    )
                )
                return
            except Exception as exc:  # noqa: BLE001 — see below; this must not be fatal
                # Every raising path in the block above used to kill this task
                # silently: `tts.open()` on a dead socket, `ElevenLabsTTS._guard`
                # refusing a sentence as empty or as markup, `push()` on a socket
                # that died mid-turn. `AgentSpeechEnded` was then never emitted,
                # so `state.speaking` stayed pinned to this agent for the rest of
                # the show — and nobody saw the traceback either, because this
                # task is only ever cancelled, never awaited, so asyncio reported
                # it as "Task exception was never retrieved" at GC time.
                #
                # `_stalled_speaker` in `panel_core` is the backstop and would
                # recover the floor within `agent_audio_stall_timeout_s` even
                # without this arm. Reporting it here is still worth it: the
                # floor recovers immediately rather than two seconds later, the
                # console says which agent broke and why, and the turn is
                # recorded as `completed=False` with the words actually spoken
                # instead of being inferred from silence.
                self._print(f"  [red]x {persona.name} speech failed: {str(exc)[:80]}[/]")
                self.emit(
                    AgentSpeechEnded(
                        t=time.monotonic(),
                        agent=command.agent,
                        completed=False,
                        utterance=" ".join(spoken),
                    )
                )
                return
            finally:
                heartbeat.cancel()
                self._speaking_turn = None
                # The turn is over, so this generation's words are spent. Only
                # this one: any other generation for the same agent is a fresh
                # answer to a later moment and must survive the turn that just
                # ended.
                self._candidates.pop((command.agent, command.epoch), None)

            self.emit(
                AgentSpeechEnded(
                    t=time.monotonic(),
                    agent=command.agent,
                    completed=True,
                    utterance=" ".join(spoken),
                )
            )

        self._speaking_task = asyncio.create_task(speak(), name=f"speak-{command.agent}")

    async def _pump_heartbeat(
        self, agent: str, progress: _AudioProgress, candidate: Candidate
    ) -> None:
        """Report, while `agent` holds the PA, that sound is still coming out.

        Consumed by `FloorController._stalled_speaker`, which takes the floor
        off an agent that stops reporting. So this must only ever fire on real
        evidence, and there are two kinds:

        * `progress.chunks` advanced — audio arrived from the provider (or, in
          --no-tts, the printed turn advanced).
        * the mixer's buffer *shrank* — nothing new arrived, but the room is
          hearing what did. Without this the tail of every turn reads as a
          stall: `turn.finish()` is followed by a drain of whatever is
          buffered, during which no chunk arrives by definition.

        Shrank, not merely non-empty, and that distinction is load-bearing.
        "The buffer has audio in it" is true forever if the audio device has
        faulted and `Mixer.render` is no longer being called from the PortAudio
        callback — which is one of the failures being caught, and is also the
        one that hangs `speak()`'s unbounded `while not is_drained` loop. A
        buffer that is not going down is not being played.

        Neither test is a check that this coroutine, or the speaking task, is
        running. That is the whole design: a `speak()` blocked forever on a
        `Candidate` queue nobody will *close* would pass any liveness ping, and
        is another of the failures being caught.

        There is one deliberate blind spot, `Candidate.awaiting_text`. If the
        agent's audio has caught up with the model still writing its turn, the
        buffer empties and no chunk arrives — and that is not a broken audio
        path, so it counts as progress and the floor is left alone. It does
        mean a brain stream that hangs *mid-turn* is not caught here. That is
        the right split: bounding generation belongs to the generation, and
        `StreamingClaudeBrain.stream` currently has no timeout of its own
        (`BrainConfig.timeout_s` is wired only to the non-streaming
        `ClaudeBrain.propose`), so it is bounded by the Anthropic client's
        600s read timeout. Fixing that is a separate change; conflating the two
        here would only trade a real hang for a false positive on every agent
        whose first sentence is short.

        Cancelled from `speak()`'s `finally`, so it cannot outlive the turn and
        refresh the clock for the next one. `_agent_audio_progress` ignores
        heartbeats for an agent that is not `state.speaking` anyway, which
        covers the window between cancellation and the reducer catching up.
        """
        seen = progress.chunks
        buffered = self.mixer.buffered_seconds(agent)
        while self._running:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            buffered_now = self.mixer.buffered_seconds(agent)
            advanced = progress.chunks != seen or buffered_now < buffered or candidate.awaiting_text
            seen = progress.chunks
            buffered = buffered_now
            if advanced:
                self.emit(AgentAudioProgress(t=time.monotonic(), agent=agent))

    # -------------------------------------------------------------------- pumps

    def _ricky_renderable(self, partial: str = "") -> Text:
        line = Text.from_markup(self._stamp())
        line.append("   ")
        line.append("Ricky: ", style="bold")
        line.append(self._ricky_final_text, style="default")
        if partial:
            if self._ricky_final_text:
                line.append(" ")
            line.append(partial, style="grey58 italic")
        return line

    def _render_ricky_line(self, partial: str = "") -> None:
        if self._ricky_live is None:
            self._ricky_live = Live(console=console, auto_refresh=False, transient=False)
            self._ricky_live.start()
        self._ricky_live.update(self._ricky_renderable(partial), refresh=True)

    def _close_ricky_line(self) -> None:
        if self._ricky_live is not None:
            # Repainted once more so the stamp left on screen is `TurnYielded`
            # — the moment Ricky stopped, not the moment he started.
            self._ricky_live.update(self._ricky_renderable(), refresh=True)
            self._ricky_live.stop()
            self._ricky_live = None
        self._ricky_final_text = ""

    async def _run_stt(self) -> None:
        """Forward Speechmatics events into the single ordered event path.

        `TranscriptUpdated` is emitted immediately, always. It drives the
        barge-in content check and speculative generation, and delaying it by
        even one classifier round trip would undo both.

        With a model address backend on, a human final additionally kicks off address
        classification — non-blocking; this loop must keep pumping or every
        other STT event stalls behind it — and `TurnYielded`, and only
        `TurnYielded`, is held back until that verdict lands. See
        `_defer_turn_yielded`.
        """
        while self._running:
            event = await self.stt.events.get()
            if isinstance(event, TranscriptUpdated):
                if event.is_final:
                    self._ricky_final_text = f"{self._ricky_final_text} {event.text}".strip()
                    self._render_ricky_line()
                else:
                    self._render_ricky_line(event.text)
                self.emit(event)
                self._classify_address_from(event)
                continue
            if isinstance(event, TurnYielded):
                self._close_ricky_line()
                if not self._defer_turn_yielded(event):
                    self._end_of_turn(event)
                continue
            self.emit(event)

    async def _run_agent_stt(self) -> None:
        """Drain the agents' own transcription straight onto the video wall.

        The one loop in this file that deliberately does not `emit()`, and the
        safety property of this whole feature is that single omission. Agent
        speech reaching `FloorController.reduce()` through a transcript is the
        feedback loop CLAUDE.md forbids; agent speech reaching the *audience*
        through a transcript is the only way the band can be timed off real
        speech rather than an assumed words-per-second. So this hands
        `TranscriptUpdated` to `panel_display` and to nothing else.

        The filter is an **allowlist**, and that is load-bearing. It admits
        `TranscriptUpdated` and drops everything else unread, rather than
        naming the events it refuses. Nothing arriving here is inert at its
        source: `TurnYielded` is `EndOfTurn` on an agent's own voice (which the
        floor already knows from `AgentSpeechEnded`, verbatim, sooner), and
        since 6 Oct 2026 `HumanSpeechStarted`/`HumanSpeechEnded` arrive here
        too — `_AgentSTTSession` emits them generically on every session it
        runs, including these three. Forwarded, they would let an agent
        barge in on itself. They do not reach the reducer because this loop
        never asked for them, and a new event type added to `PanelSTT` is
        dropped here by default rather than leaking until someone notices.
        """
        stt = self.agent_stt
        if stt is None:
            return
        while self._running:
            event = await stt.events.get()
            if not isinstance(event, TranscriptUpdated):
                continue
            display = self._display
            if display is not None:
                display.on_event(event)

    # ----------------------------------------------------------- addressing

    def _classify_address_from(self, event: TranscriptUpdated) -> None:
        """Feed one transcript segment to the address classifier.

        Partials go to `speculate()`, which is fire-and-forget and gated, so
        the answer to the question Ricky is still asking is usually already
        cached by the time he finishes it. Finals go to `classify()`, whose
        verdict becomes an `AddressDetected` event.

        Both carry the conversation context with them —
        `prompts.build_address_context`, read off the state *this* segment was
        heard against. "I'd like to hear from the other two" is not resolvable
        from the sentence, and this is the only thing that tells the classifier
        who just spoke. It is part of the classifier's cache key, so a verdict
        decided during one exchange is never served to the next.
        """
        classifier = self._address
        if classifier is None or event.speaker != HUMAN:
            return
        context = build_address_context(self.state, self.cast)
        # `state.transcript` does not yet contain this segment — the reducer
        # runs on its own task, after `emit()` — so `event.text` is passed as
        # `current` rather than read back off state.
        recent = tuple(build_address_recent(self.state, self.cast, current=event.text))
        if not event.is_final:
            classifier.speculate(event.text, context=context, recent=recent)
            return

        # One classification outstanding at a time. A turn arrives as several
        # finals and only the last of them can be the question; an earlier one
        # is answering text that has since been extended.
        previous = self._address_task
        self._address_task = asyncio.create_task(
            self._classify_address(classifier, event.text, context=context, recent=recent, t=event.t),
            name="address-classify",
        )
        if previous is not None and not previous.done():
            # Cancelled *after* the replacement is installed, and that order is
            # load-bearing: the cancelled task's `finally` checks whether it is
            # still the current classification before releasing a held
            # `TurnYielded`, so installing first is what stops it letting go
            # early and arbitrating ahead of the newer verdict.
            previous.cancel()

    async def _classify_address(
        self,
        classifier: BaseAddressClassifier,
        text: str,
        *,
        context: str,
        recent: tuple[dict[str, str], ...] = (),
        t: float,
    ) -> None:
        """Classify one human final and emit the verdict as an event.

        Bounded by `ADDRESS_HOLD_TIMEOUT_S` — the same number that bounds the
        hold — so every human final produces exactly one `AddressDetected`, and
        it always arrives *before* arbitration rather than after it. A verdict
        allowed to land late could only install a surprise invitation on top of
        a moderator cue that had already gone out; on stage, predictable beats
        salvaged. On expiry the event carries `verdict=None`, which is the
        reducer's instruction to fall back to the regex.
        """
        current = asyncio.current_task()
        started = time.monotonic()
        try:
            try:
                outcome = await asyncio.wait_for(
                    classifier.classify(text, context=context, recent=recent),
                    ADDRESS_HOLD_TIMEOUT_S,
                )
            except TimeoutError:
                outcome = AddressVerdict(
                    verdict=None,
                    agents=(),
                    reason=f"no verdict within {ADDRESS_HOLD_TIMEOUT_S * 1000:.0f}ms",
                    latency_ms=(time.monotonic() - started) * 1000.0,
                    source="timeout",
                )
            detected = self._as_detected(outcome, text=text, t=t)
            # Emitted before it is rendered, deliberately. `emit` is a
            # `put_nowait` and nothing is reduced until this task yields, so
            # the console ordering is unaffected — but a console line that
            # somehow threw would otherwise take the verdict with it, and the
            # floor would be released with no invitation and no fallback.
            self.emit(detected)
            self._show_address_verdict(detected)
        finally:
            # Release the held `TurnYielded` unless a newer final has taken over
            # this turn's classification — then the hold is that task's to
            # release, and letting go here would arbitrate before its verdict
            # lands. This runs on cancellation too, on purpose: a held
            # `TurnYielded` that is never released is a panel that never
            # arbitrates again, which is the worst failure this file can cause.
            if self._address_task is current:
                self._release_held_turn()

    def _as_detected(self, outcome: AddressVerdict, *, text: str, t: float) -> AddressDetected:
        """Translate a classifier result into the event the reducer reads.

        `t` is the *final's* timestamp, not the verdict's arrival time. The
        invitation has to be dated to the question, or `named_proposal_lookback_
        s` and the invitation TTL would quietly measure something different on
        this path than on the regex one — and the classifier's own cost is
        already carried separately, on `latency_ms`.
        """
        return AddressDetected(
            t=t,
            text=text,
            verdict=outcome.verdict,
            # One agent, or several when Ricky named a group. Not a tie —
            # `conflict` below is the tie, and the two never co-occur.
            agents=outcome.agents,
            # The verdict token cannot name who it could not choose between.
            # The floor needs a non-empty set to report `AMBIGUOUS_ADDRESS` at
            # all, so the cast stands in for "between these, and we cannot say
            # which".
            conflict=self.cast.ids() if outcome.verdict == AMBIGUOUS_VERDICT else (),
            reason=outcome.reason,
            latency_ms=outcome.latency_ms,
            source=outcome.source,
        )

    def _defer_turn_yielded(self, event: TurnYielded) -> bool:
        """Hold `TurnYielded` back if this final's verdict is still outstanding.

        Speechmatics' `EndOfTurn` lands within a few milliseconds of the final
        that names an agent, so without this the floor is arbitrated before the
        invitation exists: floor closed, Ricky cued, dead air — the exact bug
        removed the week before this was written. Only `TurnYielded` ever waits;
        everything else, `TranscriptUpdated` above all, goes straight through.

        Args:
            event: The end-of-turn event Speechmatics just produced.

        Returns:
            True if this runtime has taken the event over and will emit it
            later, False if the caller should emit it now.
        """
        task = self._address_task
        if task is None or task.done():
            return False
        if self._held_turn is not None:
            # Already holding one. Emitting this now would put it *ahead* of
            # the one already waiting, and holding both would let
            # `_turn_yielded` arbitrate twice for one end of turn — the second
            # run before `AgentSpeechStarted` had set `state.speaking`, which is
            # how two agents end up granted the floor at once. One is enough:
            # a `TurnYielded` carries nothing beyond "the turn ended".
            return True
        self._held_turn = event
        self._print("  [dim]⌛ holding end-of-turn for the address verdict[/]")
        return True

    def _release_held_turn(self) -> None:
        """Emit the `TurnYielded` that was held behind a verdict, if any."""
        held = self._held_turn
        self._held_turn = None
        if held is not None:
            self._end_of_turn(held)

    def _end_of_turn(self, event: TurnYielded) -> None:
        """Emit a `TurnYielded` and close the classifier's books on the turn.

        The reset is not optional. `AddressClassifier`'s growth gate carries the
        previous turn's word count, so without it the next turn's early partials
        — the ones whose head start is worth most — would never be speculated
        on at all.
        """
        self.emit(event)
        if self._address is not None:
            self._address.reset()

    def _show_address_verdict(self, detected: AddressDetected) -> None:
        """One dim line per verdict. This is the cache-hit diagnostic.

        Whether the verdict was already decided before Ricky stopped talking is
        the open question about this whole approach — free at a cache hit,
        ~500ms in series with arbitration at a fresh call — and nothing else
        measures it, on stage or in rehearsal.
        """
        who = detected.verdict or "unavailable"
        named = [self.cast.personas[a].name for a in detected.agents if a in self.cast.personas]
        if named:
            who = f"{who} → {' + '.join(named)}"
        label = _ADDRESS_SOURCE_LABELS.get(detected.source, detected.source)
        line = f"  [dim]⌖ address: {who}  {detected.latency_ms:.0f}ms ({label})[/]"
        if detected.reason:
            # Model output on its way to a rich console: escaped, because a
            # stray "[" in the reason would otherwise be read as markup.
            line += f" [dim]— {escape(detected.reason)}[/]"
        self._print(line)

    async def _run_ticks(self) -> None:
        while self._running:
            await asyncio.sleep(TICK_INTERVAL_S)
            self.emit(Tick(t=time.monotonic()))

    async def _pump_levels(self) -> None:
        """Hand the video wall what each voice is actually doing, 30 times a second.

        Deliberately not an event. Envelopes are not facts about the floor —
        the reducer has no use for them, they would be 30 entries a second in
        a log meant for replaying floor decisions, and `panel_core` stays a
        pure function of things that happened rather than of how loud they
        were. So this reaches around the event path and talks to the display
        directly. It is the only thing in the runtime that does.
        """
        display = self._display
        if display is None:
            return
        while self._running:
            await asyncio.sleep(DISPLAY_LEVEL_INTERVAL_S)
            levels = self.mixer.take_levels()
            levels[HUMAN] = self._mic_level
            self._mic_level = 0.0
            # Two measurements of the same audio, sent together: how loud each
            # voice is, and what it is made of. The orb needs both — the
            # corona is the spectrum, everything inside the seat ring is the
            # envelope. No spectrum for the mic: Ricky has a dot, not an orb,
            # and a dot has one number's worth of room in it.
            display.set_levels(levels, self.mixer.take_bands())

    # -------------------------------------------------------------- enrolment

    async def _enrol(self) -> EnrolledSpeaker | None:
        """Establish whose voice is the moderator's, before the show starts.

        A structural gate rather than a flag: this is awaited to completion
        before `_run_stt`, `_drain_events` or `_run_ticks` exist,
        so there is no window in which a floor task could act on an
        unidentified voice. The mic is already open — one PortAudio stream
        serves both phases — and `_callback` routes to the enrolment while
        `self._enrolling` is set.

        **A failed enrolment does not stop the show.** It prints loudly and
        returns None, and the panel then runs exactly as it did before this
        feature existed: undiarized mic, every word transcribed, anyone
        audible able to interrupt. That is the same fail-towards-a-working-show
        instinct as `_VocabRejected`, the address classifier falling back to
        the regex, and a video wall that will not bind — and here it is the
        only defensible choice, because the alternative is a rig where a bad
        socket or a dead identifier means the panel cannot be run at all.
        Refusing to start is a worse failure on a stage than an ungated mic,
        and there is no operator override to recover with (CLAUDE.md).

        `--no-speaker-lock` reaches that same ungated mode *on purpose*,
        returning None here before the store is read or a socket is opened. It
        is one branch rather than a second code path precisely so the
        deliberate choice and the failure land the operator in a mode that has
        already been exercised. It says so in a different register, though: a
        skipped phase is not a broken one.

        Returns:
            The enrolled speaker — freshly captured or loaded from the store —
            or None if enrolment did not produce one, or was not run at all.
        """
        if not self._speaker_lock:
            console.print(
                "\n[bold]Speaker lock disabled.[/] Every voice on the mic will "
                "be treated as Ricky's: transcribed, and able to interrupt an "
                "agent.\n"
                "[dim]Drop[/] --no-speaker-lock [dim]to enrol a voice "
                "instead.[/]\n"
            )
            return None

        model = self.stt.config.model
        if not self._re_enrol:
            stored = self._store.load(model=model)
            if stored is not None:
                self._print(
                    f"[dim]speaker enrolment loaded from[/] {self._store.path} "
                    f"[dim]({stored.label}, {model}, enrolled "
                    f"{stored.enrolled_at or 'unknown'})[/]"
                )
                return stored

        console.print(
            "\n[bold]Speaker enrolment.[/] Only your voice will be transcribed "
            "or able to interrupt the panel.\n"
            "[dim]Talk normally for up to 30 seconds — a few sentences about "
            "anything is plenty.[/]"
        )

        enrolment = SpeakerEnrolment(
            config=self.stt.config,
            on_progress=self._show_enrolment_progress,
        )
        self._enrolling = enrolment
        try:
            speaker = await enrolment.run()
        finally:
            # Cleared even on cancellation, so a Ctrl-C during enrolment can
            # never leave the audio callback feeding a dead session.
            self._enrolling = None

        if speaker is None:
            console.print(
                "\n[bold yellow]Speaker enrolment failed.[/] The panel will "
                "run with the mic ungated: every voice it hears is "
                "transcribed, and any of them can interrupt an agent.\n"
                "[dim]Retry with[/] --re-enrol [dim]to try again.[/]\n"
            )
            return None

        try:
            self._store.save(speaker)
        except OSError as exc:
            # Enrolment worked; only persisting it did not. The show has the
            # identifiers in hand and must not be held up by a filesystem.
            self._print(
                f"[yellow]could not write {self._store.path} ({exc}) — "
                "enrolled for this run only[/]"
            )
        else:
            self._print(f"[dim]enrolment saved to[/] {self._store.path}")
        return speaker

    def _show_enrolment_progress(self, phase: str, detail: dict) -> None:
        """Render one enrolment phase change. The only consumer of `on_progress`.

        Console-only, in both TTS modes. With `--no-tts` there is nothing to
        speak through; with TTS there is, but a synthesised voice explaining
        enrolment while the thing being enrolled is the operator's own
        microphone invites him to talk over it, and the whole phase depends on
        clean audio of one voice.
        """
        match phase:
            case "capture_segment":
                self._print(
                    f"  [dim]listening… {detail['segments']} segments ({detail['speaker']})[/]"
                )
            case "capture_done":
                self._print(
                    f"  [dim]captured {detail['segments']} segments as "
                    f"{detail['source_label']}; "
                    f"{detail['speakers_seen']} voice(s) in the room[/]"
                )
                console.print(
                    "[dim]Now say a couple more sentences so I can check I recognise you.[/]"
                )
            case "verify_segment":
                self._print(f"  [dim]recognised you {detail['matched']}/{detail['needed']}[/]")
            case "verify_done":
                self._print("  [green]voice confirmed[/]")
            case "enrolled":
                self._print(f"[bold green]enrolled as {detail['label']}[/]")
            case "capture_failed" | "verify_failed" | "session_failed":
                self._print(f"  [yellow]{phase}: {detail.get('reason', '')}[/]")
            case _:
                pass

    # --------------------------------------------------------------------- run

    async def run(self, *, input_device=None, output_device=None) -> None:
        self._loop = asyncio.get_running_loop()

        if self._display is not None:
            url = await self._display.start()
            if url:
                self._print(f"[dim]video wall on[/] {url}")
            else:
                # Already reported by the server. The panel carries on: a wall
                # that will not bind is worth a line, not a cancelled show.
                self._print("[yellow]video wall unavailable — continuing without it[/]")
                self._display = None

        if self._display is None and self.agent_stt is not None:
            # The wall did not come up, so the only consumer of the agents'
            # transcription is gone. Drop it — and drop the mixer's tap with
            # it, so the audio callback is exactly what it is without
            # `--display`. Done here rather than left running because three
            # sockets nobody reads is spend with no picture to show for it.
            self.agent_stt = None
            self.mixer.on_played = None

        if self.tts is not None:
            voices = [p.voice_id for p in self.cast.personas.values()]
            # Connections *and* voices: on eleven_v3_conversational this also
            # generates and discards one utterance per voice, because the model
            # has a per-voice warm-up the handshake does not cover. See
            # `ElevenLabsTTS.prewarm`.
            self._print("[dim]pre-warming TTS connections and voices…[/]")
            t0 = time.monotonic()
            await self.tts.prewarm(voices)
            self._print(f"[dim]  {1000 * (time.monotonic() - t0):.0f}ms (paid once)[/]")

        # Hoisted above enrolment: one PortAudio stream serves both the
        # enrolment phase and the show, so the mic is open before the first
        # phase needs it and is never re-opened with the venue's device
        # configuration a second time. `_callback` decides which consumer each
        # block belongs to.
        stream = sd.Stream(
            samplerate=PIPELINE_SAMPLE_RATE,
            blocksize=self.block_size,
            dtype="float32",
            channels=1,
            device=(input_device, output_device),
            callback=self._callback,
        )

        tasks: list[asyncio.Task] = []
        with stream:
            try:
                # The gate. Awaited to completion before any floor task
                # exists, so nothing can act on an unidentified voice: there
                # is no reducer running to act, and no started session to hear
                # one. See `_enrol`.
                speaker = await self._enrol()
                if speaker is not None:
                    # The one call site in the show that turns diarization on,
                    # and it is this instance only. `self.agent_stt` is never
                    # touched: the agents' own played audio has one known voice
                    # per socket and must stay undiarized (stt.py docstring).
                    self.stt.identify(
                        label=speaker.label,
                        speaker_identifiers=speaker.speaker_identifiers,
                    )

                await self.stt.start()
                if self.agent_stt is not None:
                    await self.agent_stt.start()

                tasks = [
                    asyncio.create_task(self._drain_events(), name="events"),
                    asyncio.create_task(self._run_stt(), name="stt"),
                    asyncio.create_task(self._run_ticks(), name="ticks"),
                ]
                if self._display is not None:
                    tasks.append(asyncio.create_task(self._pump_levels(), name="display-levels"))
                if self.agent_stt is not None:
                    tasks.append(asyncio.create_task(self._run_agent_stt(), name="agent-stt"))

                threading.Thread(target=self._watch_console_keys, daemon=True).start()

                gated = "" if speaker is None else f" [dim]mic gated to {speaker.label}.[/]"
                mute_line = (
                    "[dim]Mic auto-mutes while an agent is on the PA (m disabled). "
                    "Press j for an emergency interrupt (stops the floor, hands it to "
                    "Ricky).[/]\n"
                    if self._mute_while_agents_speak
                    else "[dim]Press m to mute the mic (emergency), m again to unmute. "
                    "Press j for an emergency interrupt (stops the floor, hands it to "
                    "Ricky).[/]\n"
                )
                console.print(
                    f"[bold]Panel live.[/] "
                    f"{', '.join(p.name for p in self.cast.personas.values())}"
                    f"{gated}\n"
                    "[dim]Ask a question to open the floor. A statement invites "
                    "nobody. Ctrl-C to stop.[/]\n"
                    "[dim]Left columns: seconds since start, +gap since the line "
                    "above.[/]\n" + mute_line
                )

                await asyncio.gather(*tasks)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                self._running = False
                for task in tasks:
                    task.cancel()
                # The tap runs on the audio thread and the sessions it feeds
                # are about to go away, so drop it before closing them.
                self.mixer.on_played = None
                await self.stt.stop()
                if self.agent_stt is not None:
                    await self.agent_stt.stop()
                if self.tts is not None:
                    await self.tts.aclose()
                if self._address is not None:
                    await self._address.close()
                if self._display is not None:
                    await self._display.close()


def _jsonable(value):
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser(prog="panel", description=__doc__)
    parser.add_argument("--personas", type=Path, default=Path("personas"))
    parser.add_argument("--block", type=int, default=256)
    parser.add_argument("--no-tts", action="store_true", help="print turns instead of speaking")
    parser.add_argument(
        "--address-backend",
        choices=("regex", "haiku", "typesafe"),
        default="typesafe",
        help=(
            "who resolves the addressee: the regex, Haiku (ANTHROPIC_API_KEY), "
            "or TypeSafe/Jev (TYPESAFE_API_KEY). Default: typesafe"
        ),
    )
    parser.add_argument("--input-device", default=None)
    parser.add_argument("--output-device", default=None)
    parser.add_argument("--log", type=Path, default=None, help="event log for replay")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument(
        "--display",
        action="store_true",
        help="serve the 12m video wall (open the printed URL on the wall machine)",
    )
    parser.add_argument("--display-port", type=int, default=DISPLAY_PORT)
    parser.add_argument(
        "--speakers",
        type=Path,
        default=DEFAULT_STORE_PATH,
        help=(
            f"where Ricky's speaker enrolment is kept between runs (default: {DEFAULT_STORE_PATH})"
        ),
    )
    parser.add_argument(
        "--re-enrol",
        action="store_true",
        help="capture a fresh voice enrolment even if a stored one is valid",
    )
    parser.add_argument(
        "--no-speaker-lock",
        action="store_true",
        help=(
            "skip enrolment and treat every voice on the mic as Ricky's "
            "(makes --speakers and --re-enrol no-ops)"
        ),
    )
    parser.add_argument(
        "--mute-while-agents-speak",
        action="store_true",
        help=(
            "last resort when enrolment can't be trusted: skip it entirely "
            "(implies --no-speaker-lock) and gate the mic for as long as an "
            "agent is on the PA, so its own bleed can never reach the floor. "
            "Disables the m emergency-mute key; j still works"
        ),
    )
    parser.add_argument(
        "--aec",
        action="store_true",
        help=(
            "cancel this machine's own speaker output from its own mic "
            "(off by default — see `uv run aec-test` to calibrate --aec-delay-ms)"
        ),
    )
    parser.add_argument(
        "--aec-delay-ms",
        type=float,
        default=0.0,
        help="acoustic+buffering delay for --aec, measured by `uv run aec-test`",
    )
    args = parser.parse_args()

    if args.list_devices:
        console.print(str(sd.query_devices()))
        return

    cast = PanelCast.from_dir(args.personas)
    runtime = PanelRuntime(
        cast,
        floor_config=FloorConfig(
            # `FloorConfig` means only "read `AddressDetected`", never which
            # backend produced it — so an old recording replays identically
            # whatever is wired up here.
            llm_address_detection=args.address_backend != "regex",
        ),
        address_backend=args.address_backend,
        block_size=args.block,
        use_tts=not args.no_tts,
        log_path=args.log,
        display=DisplayServer(cast, port=args.display_port) if args.display else None,
        speakers_path=args.speakers,
        re_enrol=args.re_enrol,
        speaker_lock=not args.no_speaker_lock,
        mute_while_agents_speak=args.mute_while_agents_speak,
        aec=AECConfig(enabled=args.aec, delay_ms=args.aec_delay_ms),
    )

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runtime.run(input_device=args.input_device, output_device=args.output_device))


if __name__ == "__main__":
    main()
