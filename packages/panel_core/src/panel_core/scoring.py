"""Floor-priority arithmetic.

Deliberately simple and fully deterministic. The point is not mathematical
sophistication — it is that agents want the floor for *different reasons*, and
that the same inputs always produce the same decision so a rehearsal can be
replayed and a threshold change can be regression-tested.

No LLM call happens anywhere in this path (FEASIBILITY.md 3.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .events import Signals
from .personas import Persona

_WORD_RE = re.compile(r"[a-z']+")


@dataclass(frozen=True, slots=True)
class FloorConfig:
    # --- floor priority weights ---
    w_relevance: float = 1.0
    w_urgency: float = 0.6
    w_disagreement: float = 0.7
    w_expertise: float = 0.5
    w_novelty: float = 0.3

    # Discourage the same agent taking back-to-back turns.
    recency_penalty: float = 0.8
    recency_window_s: float = 25.0

    # A proposal below this never gets the floor — silence beats filler.
    min_floor_priority: float = 0.45

    # --- humans ---
    # Ricky's mic wins instantly and with no overlap.
    human_duck_ms: int = 90

    # --- backchannel discrimination (ADR 0001) ---
    # The capability given up by not using LiveKit's AgentSession. Their
    # defaults were min_duration=0.5 + resume_false_interruption; ours ducks
    # first and classifies after, so responsiveness never trades against
    # correctness. See FEASIBILITY.md 8.1 S0.2.
    backchannel_duck_db: float = -15.0
    duck_ramp_ms: int = 120
    resume_ramp_ms: int = 220
    # Human speech longer than this is an interruption regardless of content.
    backchannel_max_duration_s: float = 0.6
    # ...or shorter, if it carries this many non-backchannel words.
    interrupt_min_words: int = 3

    # --- invitation ---
    # The floor is CLOSED by default. Agents propose constantly (speculation is
    # what keeps the post-turn gap short) but may only *take* the floor when
    # Ricky opens it. A statement invites nobody; a question invites the room.
    # How many agent turns one open invitation is worth before the floor goes
    # back to the moderator:
    open_invitation_turns: int = 2
    # A named agent always gets exactly one.
    address_invitation_turns: int = 1
    # An invitation nobody ever acts on must not sit on the floor for the rest
    # of the show. `no_candidate` deliberately does NOT clear the invitation —
    # an empty proposal set for two or three seconds is normal — so this is the
    # only thing that reaps a mis-addressed one. Measured from the invitation's
    # last activity, which `Invitation.spent()` stamps when a turn starts and
    # `Invitation.touched()` stamps again when it ends, so a live exchange
    # never ages out. Both are needed: with only the first, any turn longer
    # than this closed the floor the moment it finished. The introduction round
    # is exempt.
    invitation_ttl_s: float = 25.0
    # A later, vaguer invitation may not downgrade a live specific one inside
    # this window — "Melia, can you continue? ... is that okay?" is one act of
    # moderation arriving as two transcript segments. A fresh ADDRESS always
    # supersedes; only an OPEN is held off, and only for this long.
    invitation_supersede_window_s: float = 8.0

    # --- speculation ---
    speculation_interval_s: float = 0.8
    # Don't ask the panel to answer a scrap. Finals arrive on acoustics, not on
    # sentence boundaries, so a turn routinely opens with "So, Wayne, uh," — and
    # an agent asked to propose against three words writes a holding line ("Take
    # your time, Ricky") rather than an answer, which a named invitation then
    # airs unconditionally because it bypasses `min_floor_priority`. Partials
    # only: a *final* must always ask, however short, because a two-word direct
    # question ("Wayne, thoughts?") is a real thing Ricky says and by then
    # detection has run.
    speculation_min_words: int = 5
    # How far before a direct question an answer to it may have been written.
    # Speculative generation is the whole reason the post-turn gap is short, so
    # a proposal started against a partial of the question must still count —
    # those run 1-3s ahead of the final. A proposal older than this was written
    # against a different moment (the previous turn, or Ricky's preamble before
    # he had asked anything) and must not be aired in answer to a question it
    # never heard. Applies to *named* invitations; `open_proposal_lookback_s`
    # below is the same test for an open floor, and says why one number does
    # not serve both.
    #
    # **This number is not "6.0, lowered".** The thing it measures changed.
    # `_stale` used to compare the invitation against `Proposal.t`, the moment
    # the finished line *arrived*, and 6.0 was calibrated against that. Arrival
    # was the wrong clock — a line whose input predated the question by 3.2s
    # arrived 0.77s *after* the invitation and measured as maximally fresh — so
    # it now compares against `Proposal.written_against_t`, the timestamp of
    # the transcript its generation was started from. Input times are earlier
    # than arrival times by the whole generation latency, so the same window
    # measured this way is far tighter: 6.0 would still admit that line, since
    # Ricky's entire question only took 3.2s.
    #
    # A rehearsal dial, not a derived number, and the floor under it is set by
    # `invited_agent_grace_s`: a generation that lands just inside the beat was
    # started roughly (generation latency - grace) before the question, so at
    # 2.4s to signals and a 1.0s beat, anything below ~1.4s would refuse the
    # answers the beat exists to wait for. 2.0 leaves headroom for that and
    # still refuses a whole moderator preamble. Move them together, and
    # re-measure generation latency on the venue rig first (CLAUDE.md
    # § Deployment).
    named_proposal_lookback_s: float = 2.0
    # The same test on an open floor. The comment above used to end "only
    # applies to named invitations: an open invitation is already protected by
    # the score floor, which filler loses to" — and that was wrong twice over.
    # A holding line written against Ricky's preamble ("I'll wait to hear where
    # he's actually pointing this") clears `min_floor_priority` comfortably,
    # and took an open floor on stage 21 Sept 2026 one millisecond after the
    # invitation — far sooner than any generation started from the question
    # could have finished. The second reason is new: `_agent_ended` now keeps
    # proposals across a turn boundary, so the open candidate set routinely
    # holds lines aimed at an earlier moment and something has to bound them.
    #
    # Measured against `Invitation.t`, the moment the floor opened, which no
    # longer moves as turns are spent. So a bid written mid-turn is *newer*
    # than the invitation and always fresh — which is the point; this window
    # governs only how far ahead of the invitation speculation may have run.
    #
    # Not calibrated to catch that Melia line on its own: her input was frozen
    # roughly 1.9s before the final, inside this window. The fix for the line
    # itself is the speculative branch of `build_turn_prompt`, which asks an
    # agent to answer a question Ricky has not asked yet. This is the backstop
    # for the grossly old, and the dial to lower in rehearsal if filler still
    # wins an open floor. Separate from the named window because the trade is
    # different: refusing a named agent leaves a direct question unanswered,
    # refusing here only means the panel does not volunteer for a beat.
    open_proposal_lookback_s: float = 2.0
    # How long a named agent gets to finish writing before the floor gives up on
    # them and cues Ricky. Speechmatics' `EndOfTurn` lands within a few ms of the
    # final that names the agent, so arbitration runs before any generation
    # started against that final could possibly have produced signals — measured
    # 1.7-2.4s to signals (`tests/bench_brains.py`). Without this the cue is
    # certain, not occasional.
    #
    # A rehearsal dial, not a derived number. Too short and Ricky is told to fill
    # a gap the panel was about to fill itself; too long and the audience hears
    # dead air. One second is about a natural beat before an answer; it does not
    # cover a cold 2.4s generation on its own and is not meant to — letting
    # generations race (so the answer usually exists before the question ends) is
    # what closes the rest, and this only has to cover the residue.
    invited_agent_grace_s: float = 1.0

    # --- who did Ricky address? ---
    # Off by default, and off is the tested path. With this on, the regex
    # detector in `FloorController._detect` no longer runs on a human final:
    # `panel_runtime.address` asks a model the same question and the answer
    # arrives back as an `AddressDetected` event, which is what keeps the
    # reducer a pure function of events and a recording replayable.
    #
    # It exists because a regex can never resolve a *descriptive* reference —
    # "what does the financial side make of that?" is Wayne, and no pattern
    # over the transcript knows that. Measured at 153/153 on the regression
    # corpus (`packages/panel_runtime/tests/bench_address.py` against
    # `packages/panel_core/tests/test_address.py`) with p50 526ms to verdict,
    # which is why it is a dial and not yet the default: 526ms of that sits in
    # series with arbitration unless speculation on partials has already hidden
    # it, and the on-stage cache-hit rate is still unmeasured.
    #
    # With it off, every path below behaves exactly as it did before the
    # classifier existed, including the one-shot introduction latch. With it
    # on, `AddressDetected` owns that latch too — two things able to start the
    # introduction round is one too many.
    llm_address_detection: bool = False

    # --- safety valve ---
    # After this many agent turns in a row, hand back to the moderator so the
    # panel cannot drift into an unbounded machine-to-machine conversation.
    max_consecutive_agent_turns: int = 3

    # --- liveness watchdog ---
    #
    # **Neither of these is a turn-length limit.** Mid-turn steering was cut on
    # 11 Sept 2026: turn length is a prompt instruction (GUARDRAILS in
    # prompts.py) with no orchestrator-enforced ceiling, and the moderator
    # handles overruns live (CLAUDE.md). A wall-clock ceiling on *speaking*
    # would quietly reinstate that, and would genuinely fire on a healthy turn
    # — `BrainConfig.max_tokens` is 1200, so a model ignoring "two or three
    # sentences" can produce minutes of legitimate speech. So the question these
    # ask is not "has this agent talked too long?" but "is any sound still
    # coming out?". A turn producing audio is never touched at any length; a
    # turn producing silence is broken however briefly it has been running.
    #
    # Without them, `state.speaking` is cleared by exactly one thing —
    # `AgentSpeechEnded` — and every path that can fail to emit it (a dead TTS
    # socket, a raising `_guard`, a crashed speak task, a hung brain stream, a
    # faulted audio device) pins the floor to an agent who is not speaking for
    # the rest of the show. The only recovery was Ricky talking, which forces
    # the floor back via `_commit_human_interrupt`. That is a human noticing,
    # not a system recovering.
    #
    # How long the agent gets to produce its *first* audio after the grant.
    # Measured TTFB on the multi-stream endpoint is ~440ms median (tts.py), so
    # this is ~10x headroom: it is here to catch "nothing ever arrived", not to
    # police a slow start.
    agent_first_audio_timeout_s: float = 5.0
    # ...and how long a gap in audio is tolerated once it has started. The floor
    # under this is the natural inter-chunk gap on a healthy turn, which should
    # be near zero — the model writes at ~40 tok/s and the agent speaks at ~2.8
    # words/s, so the mixer buffer should never run dry mid-turn (see
    # `panel_runtime.panel.Candidate`) — but that is the assumption, not a
    # measurement. The runtime counts buffered-but-unplayed audio as progress,
    # so the end-of-turn drain does not read as a stall.
    #
    # Log inter-heartbeat gaps across a rehearsal and set this above their p99
    # before trusting it, on the venue rig rather than a dev box (CLAUDE.md
    # § Deployment). 2.0 is a starting point chosen so the audience hears about
    # two seconds of dead air rather than a minute; it is not derived.
    agent_audio_stall_timeout_s: float = 2.0


BACKCHANNEL_LEXICON: frozenset[str] = frozenset(
    {
        "mm", "mmm", "mhm", "mmhm", "uhhuh", "uhuh", "hm", "hmm",
        "yeah", "yep", "yes", "yup", "right", "sure", "ok", "okay",
        "quite", "indeed", "true", "exactly", "totally", "wow", "oh",
        "i", "see", "got", "it", "of", "course", "fair", "enough",
    }
)


def is_backchannel(text: str, *, min_words: int) -> bool:
    """Is this an acknowledgement rather than a bid for the floor?

    Conservative by construction: unknown words count against backchannel, so
    anything substantive interrupts. Being wrong towards 'interrupt' is safe
    (the human wanted the floor anyway); being wrong towards 'backchannel'
    means talking over Ricky, which is not.
    """
    tokens = [t for t in _WORD_RE.findall(text.lower()) if t]
    if not tokens:
        return True  # VAD fired but nothing transcribed yet — assume backchannel
    substantive = [t for t in tokens if t not in BACKCHANNEL_LEXICON]
    return len(substantive) < min_words


def floor_priority(
    signals: Signals,
    persona: Persona,
    *,
    now: float,
    last_spoke_at: float | None,
    config: FloorConfig,
) -> float:
    """How much this agent deserves the floor, on an open floor."""
    score = (
        config.w_relevance * signals.relevance * signals.confidence
        + config.w_urgency * signals.urgency
        + config.w_disagreement * signals.disagreement
        + config.w_expertise * signals.expertise
        + config.w_novelty * signals.novelty
    )

    if last_spoke_at is not None:
        elapsed = now - last_spoke_at
        if elapsed < config.recency_window_s:
            decay = 1.0 - (elapsed / config.recency_window_s)
            score -= config.recency_penalty * decay

    return score


# An agent never cuts off a speaking agent. Scoped out 21 Sept 2026 — agents
# pass turns, they do not interrupt each other — so `interrupt_score` and
# `may_interrupt` are gone along with their tuning. A proposal arriving while
# another agent speaks is stored for the next arbitration and nothing else;
# see `FloorController._proposal`. Ricky interrupting an agent is a different
# path entirely (`_human_started`) and is unaffected.
