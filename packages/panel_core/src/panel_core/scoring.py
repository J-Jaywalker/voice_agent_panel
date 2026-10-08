"""Floor-priority arithmetic.

Deliberately simple and fully deterministic. The point is not mathematical
sophistication — it is that agents want the floor for *different reasons*, and
that the same inputs always produce the same decision so a rehearsal can be
replayed and a threshold change can be regression-tested.

No LLM call happens anywhere in this path (FEASIBILITY.md 3.5).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
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
    # How many *consecutive* partials attributed to Ricky must land over a
    # speaking agent before the agent is stopped. Diarisation can mislabel one
    # short segment, and a single mislabelled partial used to be enough to cut
    # an agent off mid-sentence; the same voice surviving three partials in a
    # row is a far harder thing to get wrong, and at Speechmatics' partial rate
    # it costs a fraction of a second. Partials only — a *final* stops the
    # agent on its own, whatever the streak, because it is the engine having
    # committed to that attribution rather than revised it.
    interrupt_confirm_partials: int = 6

    # --- invitation ---
    # The floor is CLOSED by default. Agents propose constantly (speculation is
    # what keeps the post-turn gap short) but may only *take* the floor when
    # Ricky opens it. A statement invites nobody; a question invites the room.
    #
    # How many agent turns one invitation is worth before the floor goes back
    # to the moderator, at most. On an open floor every live agent chips in
    # once — `Invitation.admits()` admits whoever has not yet spoken, and
    # `FloorController._grant` closes the invitation outright the moment every
    # live agent has had a turn, so in ordinary running this number is not
    # what ends the round. It is the ceiling for when that mechanism should
    # not apply (an invitation nobody ever fully uses) or should not run away
    # (a persona added without updating this), so it defaults to the current
    # cast size — three — with headroom rather than exactly matching it. Set
    # lower in a config or an `OperatorAction.OPEN_FLOOR` to bound a round more
    # tightly; that override is still respected exactly (see
    # `test_an_open_invitation_is_spent_and_the_floor_goes_back`).
    open_invitation_turns: int = 4
    # A named invitation (one agent or several) is confined to exactly those
    # agents for its whole life — `Invitation.admits()` never opens it to the
    # rest of the panel, however many turns it produces. This is the ceiling
    # on how long a direct question, or a named exchange between two
    # panellists, may run before the floor goes back to Ricky regardless.
    address_invitation_turns: int = 4
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
    # The same idea during an *agent's* turn, and a separate dial because the
    # two turns are nothing like the same length. Ricky asks a question in
    # 5-10s; an agent answers in 20-30s, and until 25 Sept 2026 nothing asked
    # the other two for a line at any point inside that. The only proposals in
    # hand at a turn boundary were therefore written before the turn began —
    # `FloorController._agent_ended` drops exactly those (they answer a
    # question the speaker has since spent half a minute answering) — so every
    # agent-to-agent handover paid a full cold generation, about 2.4s of dead
    # air on a measured run, plus TTS on top.
    #
    # Rounds now open through the turn off `AgentUtteranceProgress`, so the
    # freshest completed proposal at a boundary is at worst this interval plus
    # one generation old, and the boundary filter starts keeping things
    # instead of always emptying.
    #
    # 2.5s rather than the human path's 0.8s, and the reasoning is generation
    # latency rather than taste: a proposal takes 2.0-2.9s to reach
    # `SignalsReady` (`tests/bench_brains.py`, plus the utterance gate in
    # `StreamingClaudeBrain.stream`), so asking faster than that mostly buys
    # concurrency rather than freshness. At 2.5s a 30s turn opens about twelve
    # rounds and each idle agent keeps roughly two generations in flight —
    # comparable to what a long human turn already produces. Lower it for
    # fresher answers at the cost of more concurrent generations; spend is not
    # the constraint (CLAUDE.md), the provider's concurrency limit and TTFB
    # under load are, and both have to be measured on the venue rig.
    agent_turn_speculation_interval_s: float = 2.5
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
    # It exists because a regex can never resolve a reference that lives
    # outside the sentence. Two kinds: a *descriptive* one — "what does the
    # financial side make of that?" is Wayne, and only `personas/wayne.yaml`
    # knows that — and a *conversational* one — "I'd like to hear from the
    # other two" is a different pair depending on who just spoke, which is
    # what `prompts.build_address_context` supplies. Measured at 156/156 on the
    # regression corpus and 75/75 on the new-capability set
    # (`packages/panel_runtime/tests/bench_address.py --repeat 3` against
    # `packages/panel_core/tests/test_address.py`) with p50 611ms to verdict,
    # which is why it is a dial and not yet the default: 611ms of that sits in
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
    max_consecutive_agent_turns: int = 6

    # --- agents inviting agents ---
    # On by default. A winning proposal may name the colleague who speaks next
    # when its turn ends, and that colleague takes the floor without having
    # proposed — see `FloorController._agent_invitation`.
    # `max_consecutive_agent_turns` is what bounds the resulting exchange.
    # Off still disables it outright: `Signals.invites_next` is read, validated
    # and stored but never installs an invitation, so the floor behaves exactly
    # as it did before the field existed.
    agent_invitations: bool = True

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


# Phrases that hold the floor for someone else and say nothing. Matched as
# whole phrases against normalised text, never as bare words: "wait" and "hold
# on" alone open plenty of legitimate reactions ("Wait, what?"), so only the
# multi-word forms are listed and the residue rule below decides the rest.
WAIT_PHRASES: tuple[str, ...] = (
    (
        r"let (?:him|her|them|me|us|you) finish"
        r"(?:(?: the| that| his| her| their| your)?"
        r"(?: sentence| thought| point| question| answer| story))?"
    ),
    r"(?:go|carry|crack) (?:on|ahead)",
    r"after you",
    r"you (?:go )?first",
    r"take your time",
    r"no rush",
    r"in your own time",
    r"(?:we|they)'re listening",
    r"i'm listening",
    r"(?:the )?floor'?s? (?:is )?(?:all )?(?:yours|his|hers|theirs)",
    r"whenever you(?:'re| are)? (?:want|like|ready)(?: it| to)?",
    (
        r"(?:i'll|i will|happy to|content to|glad to|i'm happy to) wait"
        r"(?: my turn| your turn| his turn| her turn| their turn)?"
        r"(?: for (?:the )?(?:floor|turn|him|her|them|you|it|that|this))?"
    ),
    r"wait(?:ing)? (?:my|his|her|their|your) turn",
    r"(?:hold|hang) on",
    r"give (?:it|him|her|them|us|me|you) a (?:sec|second|moment|minute)",
    r"(?:just )?(?:a|one) (?:sec|second|moment|minute)",
    (
        r"i'?ll (?:come|jump|chime|cut) in"
        r"(?: after(?:wards)?| later| next| behind (?:him|her|them|you))?"
    ),
    r"let'?s hear (?:the|his|her|their|that|this) \w+ first",
    r"don'?t let me (?:stop|interrupt|hold up) you",
)

_WAIT_RE = re.compile(r"\b(?:" + "|".join(WAIT_PHRASES) + r")\b")

# Digits count as words here, unlike in `_WORD_RE` above. The
# hold phrases contain none, so matching is unaffected — what changes is that
# "Hold on — 40%." keeps a residue. Under a letters-only tokeniser a figure is
# not thin content, it is *no* content, and the one line most worth protecting
# is the one that is nothing but a number: `build_turn_prompt` asks for "a
# figure, date, count, deployment" by name.
_WAIT_WORD_RE = re.compile(r"[a-z0-9']+")

# Every word any hold phrase can be built out of, read straight off the
# patterns above so adding a phrase cannot leave this behind. Only `complete=
# False` uses it, and only to decide that a *half-written* line is still
# inside the lexicon — "let", "let him", "take your" are each a hold phrase
# that has not finished arriving, and none of them is yet evidence of a turn.
# `\w`, `\b` and the group syntax carry no words; apostrophes are split so a
# pattern's `(?:we|they)'re` covers the normalised token "we're".
_WAIT_VOCAB: frozenset[str] = frozenset(
    part
    for source in WAIT_PHRASES
    for token in _WORD_RE.findall(re.sub(r"\\[a-z]", " ", source))
    for part in token.split("'")
    if part
)

# What is left over after a hold phrase and still cannot carry a turn:
# discourse particles, apologies, and the moderator's name as a vocative.
# "ricky" is here rather than passed in because he is not a persona — the
# system prompt already names him literally (`prompts.build_system_prompt`);
# the cast's own ids and names are data and arrive via `names`.
WAIT_FILLER: frozenset[str] = frozenset(
    {
        "sorry",
        "yeah",
        "yep",
        "yes",
        "yup",
        "ok",
        "okay",
        "mm",
        "mmm",
        "mhm",
        "mmhm",
        "mhhm",
        "hm",
        "hmm",
        "oh",
        "ah",
        "right",
        "sure",
        "please",
        "no",
        "nope",
        "and",
        "but",
        "so",
        "then",
        "well",
        "just",
        "first",
        "of",
        "course",
        "fine",
        "ricky",
    }
)


def is_wait_narration(text: str, *, names: Iterable[str] = (), complete: bool = True) -> bool:
    """Is this line *only* an offer to wait, with no turn inside it?

    "Let him finish the sentence, Ricky." and "Take your time, Ricky — floor's
    yours whenever you want it." are not turns. They clear the score floor
    anyway (`floor_priority` weights expertise and novelty whether or not the
    agent wants to speak), so the prompt alone cannot be the only thing
    stopping them reaching the PA.

    A *residue* rule, not a phrase match: the hold phrases are deleted and the
    line is wait-narration only if nothing substantive survives. Phrase
    matching alone fails on the short reactions GUARDRAILS exists to protect —
    "Wait, what?", "Hold on, Dexter, say that number again." — and on
    "Let him finish his story, but I've got my own point.", where the real
    turn follows the hold phrase. All three keep a residue and all three pass.

    **Biased to fail open.** A line
    with no hold phrase in it at all is never flagged, however thin. Missing a
    filler line costs a few seconds of dead air; a false positive deletes a
    legitimate short reaction, and on this stage that is the worse half of the
    trade.

    `complete=False` asks the *streaming* question instead — "is there a turn
    in this yet?" — and it is a different question, not a looser version of
    the same one. A caller watching an utterance arrive character by character
    sees "L", "Let him", "Let him finish the sentenc" before it ever sees the
    line, and every one of those passes the rule above: no hold phrase has
    finished arriving, so there is no residue and nothing is flagged. Judged
    that way the predicate never fires on a stream at all. So under
    `complete=False` the rule inverts: the line is held while everything
    certainly written so far is drawn from the hold lexicon (`_WAIT_VOCAB`) or
    the filler, with a trailing word that may still be growing set aside. That
    holds a thin-but-honest opening too ("Yeah. Sure.") — which is why a
    streaming caller must settle the hold with a `complete=True` call once the
    utterance is whole, rather than treating a hold as a verdict.

    Args:
        names: Cast ids and display names, so "go on, Wayne" reduces to a bare
            vocative. Personas are data; this function does not know the cast.
        complete: False while `text` is a prefix of an utterance still being
            generated. See above — the two modes answer different questions.
    """
    normalised = " ".join(_WAIT_WORD_RE.findall(text.lower().replace("’", "'")))
    if not normalised:
        return False  # the empty-utterance gate owns this case
    residue = _WAIT_RE.sub(" ", normalised)
    filler = WAIT_FILLER | {n.lower() for n in names}
    if not complete:
        tokens = _WAIT_WORD_RE.findall(residue)
        if tokens and text[-1:].isalpha():
            # The last word is still arriving — "sentenc" is not a word this
            # agent has chosen, and counting it as content opens the gate on
            # every line this predicate exists to catch.
            tokens.pop()
        return all(
            all(part in filler or part in _WAIT_VOCAB for part in t.split("'") if part)
            for t in tokens
        )
    if residue == normalised:
        return False  # no hold phrase — not this predicate's business
    return not [t for t in _WAIT_WORD_RE.findall(residue) if t not in filler]


_CLAUSE_BOUNDARY_RE = re.compile(r"[.!?]+")


def is_degenerate_repetition(
    text: str,
    *,
    min_clauses: int = 6,
    max_long_clauses: int = 1,
    long_clause_words: int = 8,
    max_repeat_ratio: float = 0.6,
) -> bool:
    """Is this utterance a run of many near-duplicate short clauses?

    Observed on stage: a generation that owed the floor a line and had
    nothing to say, but was barred by the prompt from saying so
    (`is_wait_narration` exists for exactly that gap), instead looped —
    "Let's hear it.Go on, Dex.None yet.Nothing from me." — a dozen-odd
    restatements of the same non-answer run together. `is_wait_narration`
    does not catch this: enough distinct residue words survive (names,
    "number", "comment") that the line reads as content to that predicate.

    A clause count rather than a phrase list, because the model's words for
    the loop vary and a blocklist only ever bans the ones already seen.
    Split on `.`/`!`/`?` directly rather than relying on whitespace after
    them, because the chunker that produced this text only treats a boundary
    as real when it is followed by a space (`SentenceChunker._BOUNDARY`) — a
    run like this one typically arrives with the inter-clause spaces missing,
    which is what glued the fragments into one chunk in the first place.

    Gated on `max_long_clauses` so a real multi-sentence turn with one or two
    longer sentences among shorter reactions is never caught here — the
    failure mode this guards against is *many* short clauses, not a short
    one anywhere in the text.

    Args:
        text: The utterance (or utterance-so-far, mid-stream) to check.
        min_clauses: Below this many clauses, there is nothing to call a
            repetition — a short turn is just short.
        max_long_clauses: How many clauses may exceed `long_clause_words`
            before this stops being "many short clauses" and becomes an
            ordinary turn that happens to be made of several sentences.
        long_clause_words: The word count above which a clause no longer
            counts as "short".
        max_repeat_ratio: The vocabulary must be at least this repetitive
            (unique words / total words at or below this) to flag — a long
            turn with a wide vocabulary is not this failure, however many
            clauses it has.

    Returns:
        True if the text looks like the same non-answer restated many times
        rather than a turn.
    """
    clauses = [c.strip() for c in _CLAUSE_BOUNDARY_RE.split(text) if c.strip()]
    if len(clauses) < min_clauses:
        return False
    long_clauses = sum(1 for c in clauses if len(c.split()) > long_clause_words)
    if long_clauses > max_long_clauses:
        return False
    tokens = _WORD_RE.findall(text.lower())
    if not tokens:
        return False
    return len(set(tokens)) / len(tokens) <= max_repeat_ratio


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
