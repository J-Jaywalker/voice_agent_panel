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

    # --- interrupting a *speaking agent* ---
    interrupt_threshold: float = 0.55
    # Never interrupt inside the opening of a turn; it just looks broken.
    interrupt_grace_s: float = 2.5
    # An agent that just spoke may not immediately interrupt someone else.
    interrupt_cooldown_s: float = 12.0
    interrupt_overlap_ms: int = 380

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
    # last activity: `Invitation.spent()` refreshes the clock, so a live
    # exchange never ages out. The introduction round is exempt.
    invitation_ttl_s: float = 25.0
    # A later, vaguer invitation may not downgrade a live specific one inside
    # this window — "Melia, can you continue? ... is that okay?" is one act of
    # moderation arriving as two transcript segments. A fresh ADDRESS always
    # supersedes; only an OPEN is held off, and only for this long.
    invitation_supersede_window_s: float = 8.0

    # --- speculation ---
    speculation_interval_s: float = 0.8

    # --- safety valve ---
    # After this many agent turns in a row, hand back to the moderator so the
    # panel cannot drift into an unbounded machine-to-machine conversation.
    max_consecutive_agent_turns: int = 3


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


def interrupt_score(signals: Signals, persona: Persona) -> float:
    """Whether this is worth cutting another agent off for.

    Multiplicative rather than additive: an interruption needs *both* strong
    disagreement and urgency, filtered through how interrupt-prone this
    persona is. Merely having something relevant to say is not enough.
    """
    return signals.disagreement * signals.urgency * (0.5 + persona.interrupt_tendency)


def may_interrupt(
    *,
    signals: Signals,
    persona: Persona,
    now: float,
    speaker_started_at: float | None,
    challenger_last_spoke_at: float | None,
    config: FloorConfig,
) -> bool:
    if interrupt_score(signals, persona) < config.interrupt_threshold:
        return False
    if speaker_started_at is not None and now - speaker_started_at < config.interrupt_grace_s:
        return False
    return not (
        challenger_last_spoke_at is not None
        and now - challenger_last_spoke_at < config.interrupt_cooldown_s
    )
