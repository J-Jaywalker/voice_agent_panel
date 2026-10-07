"""Tests for `stable_prefix()`: the fix for `sanitise()`'s non-monotonicity.

`StreamingClaudeBrain.stream` (packages/panel_runtime/src/panel_runtime/
brains.py) sanitises the model's utterance as it grows, token by token, and
sends only the newly-revealed tail to TTS. That is only safe if a longer raw
string always sanitises to a longer string that keeps everything the shorter
one produced, as a literal prefix. `sanitise()` does not have that property
on its own: an unclosed `<tag`, an unclosed `[bracket`, an unclosed stage-
direction `(parenthetical`, and a leading `Name:` label can all still change
shape once more text arrives, and running `sanitise()` on the raw text before
they resolve either leaks the opening character (it vanishes once the
construct closes, but only after it may already have reached TTS) or strips
text that later input proves should have stayed.

`stable_prefix()` withholds everything from the first such unresolved
construct onward, so a caller that always sanitises `stable_prefix(text)`
rather than `text` itself only ever sanitises the part nothing left in the
stream can rewrite. These tests are the acceptance criteria for that promise:
the individual withholding rules below, and then the growing-prefix property
they exist to guarantee.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from panel_core.events import HUMAN
from panel_core.personas import ACCENT_TAGS, AUDIO_TAGS, PACE_TAGS, Beat, PanelCast, Persona
from panel_core.prompts import (
    GUARDRAILS,
    build_system_prompt,
    build_turn_prompt,
    sanitise,
    stable_prefix,
)
from panel_core.state import Invitation, InvitationSource, PanelState, Utterance

PERSONA_DIR = Path(__file__).resolve().parents[3] / "personas"

# --------------------------------------------------------------------------
# Approved knowledge — anecdotes rendered into the system prompt
# --------------------------------------------------------------------------


def _persona(**overrides) -> Persona:
    defaults = {
        "id": "dex",
        "name": "Dexter",
        "job_title": "x",
        "employer": "x",
        "background": "x",
        "stance": "x",
        "introduction": "x",
        "intro_position": 0,
        "voice_id": "x",
        "communication_style": "x",
    }
    defaults.update(overrides)
    return Persona(**defaults)


def test_anecdotes_default_to_empty_and_add_nothing_to_the_prompt() -> None:
    """A persona with no `anecdotes:` in its YAML (schema default) gets no
    "Recurring experiences" block — nothing new for the model to key off."""
    persona = _persona()
    assert persona.anecdotes == []
    assert "Recurring experiences" not in build_system_prompt(persona)


def test_anecdotes_render_into_the_system_prompt_and_instruct_reuse() -> None:
    """Approved knowledge (docs/beat-sheet.md "Anecdote spines", signed off
    16 Sept 2026) has to actually reach the model, and the point of writing
    it as a small, fixed set is recurrence — so the prompt must tell the
    model to reuse it rather than treat it as one example among many."""
    persona = _persona(anecdotes=["The eval that passed."])
    prompt = build_system_prompt(persona)
    assert "The eval that passed." in prompt
    assert "rather than inventing a fresh example each time" in prompt


# --------------------------------------------------------------------------
# Beats — prepared material scoped to a discussion topic
# --------------------------------------------------------------------------


def _beat_persona() -> Persona:
    return _persona(
        beats={
            "q1": Beat(
                cue="what changed once agents started dealing with other agents",
                material=["Delegation moved from tasks to outcomes."],
                reacting_to={"wayne": "Agree about delegation before you complicate it."},
            )
        }
    )


def test_a_beat_renders_its_cue_material_and_reaction_guidance() -> None:
    """A beat is only worth carrying if all three halves reach the model: the
    cue is what lets it recognise the topic has arrived, the material is what
    it answers from, and `reacting_to` is what stops three agents answering
    the same open question as three unrelated statements."""
    prompt = build_system_prompt(_beat_persona())
    assert "what changed once agents started dealing with other agents" in prompt
    assert "Delegation moved from tasks to outcomes." in prompt
    assert "Agree about delegation before you complicate it." in prompt


def test_reaction_guidance_is_scoped_to_that_colleague_on_that_topic() -> None:
    """`reacting_to` is per beat, not a standing view of a colleague — that is
    `relationships`. Rendered without the scope it would read as advice for
    every exchange with Wayne for the whole show."""
    prompt = build_system_prompt(_beat_persona())
    assert "If wayne has just spoken on this same topic" in prompt


def test_material_renders_before_its_reaction_guidance() -> None:
    """Order inside a beat decides what a turn opens on. `material` is the
    view this persona holds on their own and `reacting_to` only applies if a
    colleague happens to have spoken first — rendered the other way round, the
    colleague becomes the frame for the whole turn instead of a qualifier on
    it, which is exactly how Dexter's q1 came out before it was reordered."""
    prompt = build_system_prompt(_beat_persona())
    assert prompt.index("Delegation moved from tasks to outcomes.") < prompt.index(
        "Agree about delegation before you complicate it."
    )


def test_beats_are_recognised_by_topic_because_ricky_paraphrases() -> None:
    """Ricky's lines are scripted, but he is a live human and he will not say
    them as written. A beat matched on wording is a beat that never fires."""
    prompt = build_system_prompt(_beat_persona())
    assert "never by the words used to get there" in prompt
    assert "Ricky paraphrases, he is not reading" in prompt


def test_the_material_is_the_evidence_so_nothing_is_invented_under_it() -> None:
    """The 2026-10-07 rehearsal failure. Every other instruction the model
    holds wants evidence under a claim — GUARDRAILS' "say the claim, then the
    thing that made you believe it", `build_turn_prompt`'s "a figure, date,
    count, deployment", Wayne's delivery wanting the week a decision closed —
    and `anecdotes`/`citable_figures` are empty by design since the ground-zero
    reset. With nothing real left to reach for, all three agents invented a
    first-person deployment on the spot (a settlement closed at two in the
    morning, a board review two years ago, a cascade through four agents) and
    buried the prepared point inside it.

    So the beat block has to say the material *is* the evidence and that it
    already discharges whatever else asked for a contribution — one statement
    that closes every one of those demands, rather than a ban list that only
    closes the ones somebody thought to enumerate.
    """
    prompt = build_system_prompt(_beat_persona())
    assert "The points themselves are the evidence" in prompt
    assert "nothing goes on top of them" in prompt
    assert "Do not invent a deployment, an incident, a meeting, a date" in prompt


def test_own_words_is_scoped_to_phrasing_and_never_to_content() -> None:
    """"Built fresh in the moment" was true and unbounded: it licensed a fresh
    *example* as readily as a fresh sentence. The instinct is still right —
    a recited beat sounds like one — so the framing stays and the scope is
    pinned to it."""
    prompt = build_system_prompt(_beat_persona())
    assert "say that, point for point" in prompt
    assert "a different route through the same content, never different content" in prompt


def test_a_beat_is_delivered_for_comprehension_rather_than_for_brevity() -> None:
    """The brevity rules — each persona's `delivery`, GUARDRAILS' length rule
    — were written for reactions and banter, where terse is the whole point. A
    beat is exposition about a 2030 this audience has never had described to
    them, heard once and by ear, and nothing else in the prompt tells the model
    those are different jobs."""
    prompt = build_system_prompt(_beat_persona())
    assert "followed once, by ear" in prompt
    assert "being understood matters more than being brisk" in prompt


def test_the_mechanism_itself_is_forbidden_out_loud() -> None:
    """The single worst failure of this feature is an agent saying "my
    prepared material" or "that's my cue" over a PA to a live audience. The
    model knows the material exists, so it has to be told the mechanism is
    never mentioned — unlike `anecdotes`, which reads as autobiography and
    carries no vocabulary to leak."""
    prompt = build_system_prompt(_beat_persona())
    assert "Never acknowledge any of this out loud" in prompt
    assert "no cue, no brief, no notes, no prepared material, no script" in prompt


def test_an_unreached_topic_falls_back_rather_than_being_steered_towards() -> None:
    """There is no sampling lever to lean on — Sonnet 5 rejects `temperature`
    and `top_p` outright (see `panel_runtime.brains.BrainConfig`) — so the only
    thing stopping the model dragging a turn onto a topic it has material for
    is this instruction."""
    prompt = build_system_prompt(_beat_persona())
    assert "you have nothing prepared for it" in prompt
    assert "do not steer a turn towards it" in prompt


def test_a_persona_with_no_beats_is_never_told_the_mechanism_exists() -> None:
    """Mirrors the anecdotes default: nothing in the schema default may add a
    block, and a persona with no prepared material must not learn the concept
    — knowing it exists is what produces "I don't have anything on that"."""
    persona = _persona()
    assert persona.beats == {}
    prompt = build_system_prompt(persona)
    assert "Topics you have already thought hard about" not in prompt
    assert "no prepared material" not in prompt


def test_citable_figures_are_rendered_as_an_expectation_not_a_permission() -> None:
    """The figures block used to read as permission hedged with restrictions
    ("may quote ... never as a list, never as an opening") while the anecdote
    block immediately above it read as an instruction ("return to these").
    Given an ambiguous turn the model resolved that the way it was written and
    reached for the story, which is most of why two personas carrying checked
    numbers still came out as atmosphere (director's note, 25 Sept 2026).

    The restrictions are still there and still wanted — one per turn is what
    keeps a turn from becoming a recital. What changed is which half is the
    instruction.
    """
    prompt = build_system_prompt(_persona(citable_figures=["Eighty-eight per cent."]))
    assert "Eighty-eight per cent." in prompt
    assert "expected to use" in prompt
    assert "claim first, then the number, then what it means" in prompt
    assert "never two" in prompt


def test_a_blank_yaml_entry_does_not_render_an_empty_figures_block() -> None:
    """`citable_figures:` followed by a bare `- >` is one empty string, not an
    empty list, and a one-entry list is truthy. Melia and Dexter both carried
    one after the ground-zero reset, so both were told they had checked public
    figures and were expected to use them, under a blank bullet — and Melia
    invented a Brussels legislative deadline to fill it (2026-10-07). Dropped
    in the model rather than only in the YAML: the failure is silent, and what
    it produces is a fabricated statistic on a stage.
    """
    persona = _persona(citable_figures=["", "   "], anecdotes=[""])
    assert persona.citable_figures == []
    assert persona.anecdotes == []
    prompt = build_system_prompt(persona)
    assert "expected to use" not in prompt
    assert "Recurring experiences" not in prompt


def test_the_real_cast_carries_no_blank_prepared_material() -> None:
    """Ground zero is a deliberate state (docs/beat-sheet.md): the cast cites
    nothing standing, and everything they have is scoped to a beat."""
    cast = PanelCast.from_dir(PERSONA_DIR)
    for persona in cast.personas.values():
        assert persona.citable_figures == []
        assert persona.anecdotes == []


def test_the_evidence_rule_is_global_so_it_binds_a_persona_with_no_figures() -> None:
    """Wayne carries no `citable_figures` by design (docs/beat-sheet.md, Wayne
    "Never"), so a rule that lived only in the figures block would leave the
    one persona most prone to arguing from attitude entirely unbound. It is in
    GUARDRAILS instead, where a deployment, a count or a date all satisfy it.
    """
    assert "Every turn carries its own evidence" in GUARDRAILS
    assert "however sharply it is phrased" in GUARDRAILS
    prompt = build_system_prompt(_persona())
    assert "expected to use" not in prompt  # no figures block for this persona
    assert "Every turn carries its own evidence" in prompt


# --------------------------------------------------------------------------
# Guardrails the restructured beat sheet depends on (17 Sept 2026)
# --------------------------------------------------------------------------


def test_guardrails_forbid_naming_providers_and_repeating_jailbreaks() -> None:
    """Two rules added for the 17 Sept beat sheet revision, both of which
    exist because a beat now invites the failure directly rather than merely
    permitting it, and neither of which can be left to Ricky's reflexes on
    the night (docs/beat-sheet.md, Beat 3 and Beat 4):

    - **Beat 3** asks the agents what speech recognition can do now. Capability
      talk pulls hard towards naming a provider and comparing it, and the
      general "no claims about any real company" rule reads as being about
      customers and statistics rather than about vendors.
    - **Beat 4** ("Revenge of the Humans") asks all three to describe jailbreaks
      that worked on them. The room is four hundred customers; the anecdote is
      the material, the method is not, and an agent walking an audience through
      a working technique is the worst thing this panel could broadcast.

    Asserted on `GUARDRAILS` rather than on a rendered prompt because it is
    global — it must hold for every persona, including one added later with
    no anecdotes at all.
    """
    assert "Do not name" in GUARDRAILS
    assert "providers" in GUARDRAILS
    assert "never how it was done" in GUARDRAILS
    # And it must reach the model for a persona carrying no other material.
    prompt = build_system_prompt(_persona())
    assert "never how it was done" in prompt


# --------------------------------------------------------------------------
# The speculative pass — asked while Ricky is still talking
# --------------------------------------------------------------------------


def _mid_sentence(partial: str) -> PanelState:
    """Ricky part-way through a sentence: a live partial, no invitation yet."""
    return replace(PanelState.for_agents(("dex", "wayne")), partial=partial)


def test_a_mid_sentence_partial_is_not_described_as_a_closed_floor() -> None:
    """Invitations are read off finals only, so during speculation there is
    never a live invitation — and the old wording told the agent it "will
    almost certainly not be speaking" and to score itself low. Asked that
    while Ricky was three words into naming it, an agent wrote "Take your
    time, Ricky — we'll be here." and a direct address then aired it, because
    a named invitation bypasses the score floor. The speculative pass has to
    be told what it actually is."""
    prompt = build_turn_prompt(_mid_sentence("So, Wayne, uh, where are we"), _persona())
    assert "still mid-sentence" in prompt
    assert "never offer to wait" in prompt
    assert "NOT opened the floor" not in prompt


def test_a_settled_statement_still_gets_the_closed_floor_wording() -> None:
    """With no partial in flight, Ricky has finished and said nothing that
    opens the floor — the branch `brains.py` documents measuring against."""
    prompt = build_turn_prompt(PanelState.for_agents(("dex", "wayne")), _persona())
    assert "NOT opened the floor" in prompt
    assert "still mid-sentence" not in prompt


# --------------------------------------------------------------------------
# Agents replying to agents — the exchange, not the question
# --------------------------------------------------------------------------


def _after(speaker: str, text: str, **overrides) -> PanelState:
    """A settled floor whose most recent final came from `speaker`."""
    state = PanelState.for_agents(("dex", "wayne"))
    return replace(state, transcript=(Utterance(speaker=speaker, text=text, t=1.0),), **overrides)


def test_replying_to_another_agent_demands_something_they_did_not_have() -> None:
    """Agents pass turns to each other without Ricky re-opening the floor
    (`FloorController._maybe_rearbitrate`), and every one of those exchanges
    lands in the open-floor branch, which says nothing about who just spoke.

    Left at that, the model writes a *reply*: it takes the last speaker's own
    words and hands them back reframed. That reads as sharp and carries no
    information, and it is what the whole 25 Sept revision is aimed at — the
    live failures were all in agent-to-agent exchanges, never in answers to
    Ricky's questions.
    """
    prompt = build_turn_prompt(_after("wayne", "Fix the channel."), _persona())
    assert "wayne spoke last, not Ricky" in prompt
    assert "rephrasing, however sharply, is not a contribution" in prompt.lower()


def test_the_speculative_pass_is_never_treated_as_an_exchange() -> None:
    """Ricky mid-sentence means the last final is stale by construction and he
    is about to be the one answered. Telling the agent it is replying to Wayne
    while Ricky is three words into naming it would put the two instructions
    in direct contradiction on the one pass where latency matters most."""
    state = _after("wayne", "Fix the channel.", partial="So Dexter, what changed")
    prompt = build_turn_prompt(state, _persona())
    assert "spoke last, not Ricky" not in prompt
    assert "still mid-sentence" in prompt


def test_your_own_last_turn_and_rickys_are_not_exchanges() -> None:
    """Continuing yourself is not answering somebody, and answering Ricky is
    what every other branch in this function is already about."""
    assert "spoke last" not in build_turn_prompt(_after("dex", "Mine."), _persona())
    assert "spoke last" not in build_turn_prompt(_after(HUMAN, "Ricky's."), _persona())


def test_every_branch_that_can_be_waited_out_forbids_offering_to_wait() -> None:
    """The same prohibition the mid-sentence branch carries, on the three
    branches that lacked it.

    "Write the line you'd say when they finish" is satisfiable, literally, by
    writing "let him finish" — and on stage it was: agents narrated "Let him
    finish, Ricky's still building it" and "Yeah, go on Ricky, we're
    listening" into their actual spoken turns. An agent with nothing declines
    by score (`PROPOSAL_SCHEMA`'s utterance is never empty and the controller
    may still grant it), so meta-commentary about whose turn it is has no
    branch it belongs in.
    """
    mid_turn = replace(
        PanelState.for_agents(("dex", "wayne", "melia")),
        speaking="melia",
        agent_partial="Reported adoption and actual adoption are different curves.",
    )
    open_floor = replace(
        PanelState.for_agents(("dex", "wayne")),
        invitation=Invitation(agents=(), turns_remaining=1, source=InvitationSource.OPEN, t=0.0),
    )
    for state in (mid_turn, _after("wayne", "Fix the channel."), open_floor):
        assert "never offer to wait" in build_turn_prompt(state, _persona())


# --------------------------------------------------------------------------
# Individual withholding rules
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # An unclosed tag must not leak even its opening angle bracket to
        # TTS: the bug this replaces sent "<em" out loud the instant it
        # arrived, then deleted it from underneath words already spoken once
        # "<em>" closed and the diff-based slicing corrupted the offset.
        ("Hello <em", "Hello "),
        ("Hello <em>world", "Hello <em>world"),  # closed: nothing to withhold
        # Same shape for the bracket alternative in sanitise()'s regex.
        ("List [one two", "List "),
        ("List [one two] three", "List [one two] three"),
        # An unclosed paren is withheld only while it is still short enough
        # to plausibly become one of sanitise()'s stage-direction triggers
        # ("laughs", "pauses", "beat", "sighs") — see `_PAREN_HOLD_CHARS`.
        ("She said (la", "She said "),
        ("She said (that", "She said "),  # still short; still a candidate
        ("She said (laughs) ok", "She said (laughs) ok"),  # closed
        (
            # Long enough to no longer be a plausible short stage direction:
            # released as ordinary punctuation rather than held to the end
            # of the turn on the chance a trigger word appears eventually.
            "She said (that was a long parenthetical remark going on and on",
            "She said (that was a long parenthetical remark going on and on",
        ),
    ],
)
def test_stable_prefix_withholds_exactly_the_unresolved_construct(raw: str, expected: str) -> None:
    assert stable_prefix(raw) == expected


@pytest.mark.parametrize(
    "raw, ambiguous",
    [
        ("", True),  # nothing yet
        ("Wayne", True),  # could still become "Wayne:" any moment
        ("Wayne,", False),  # comma disqualifies a label immediately
        ("Wayne:", False),  # resolved *as* a label — sanitise() may strip it
        ("wayne", False),  # lower-case first letter is never a label
        ("Right, but", False),  # comma disqualifies within a handful of chars
    ],
)
def test_leading_label_withheld_only_while_genuinely_ambiguous(raw: str, ambiguous: bool) -> None:
    """`^\\s*[A-Z][\\w .-]{0,24}:\\s*` is anchored at the very start of the
    text and only confirmed by its trailing colon, so every character of a
    candidate label is provisional until the colon arrives or something the
    pattern could never accept does. Withholding the wrong amount here is
    exactly how "Wayne" lost its first seven characters the moment "Wayne: "
    finished streaming in — the label was stripped from underneath text
    already sent to TTS as itself."""
    if ambiguous:
        assert stable_prefix(raw) == ""
    else:
        assert stable_prefix(raw) == raw


# --------------------------------------------------------------------------
# The property the whole fix rests on
# --------------------------------------------------------------------------

# Each of these resolves every construct it opens by the time the string
# ends — closed tags, closed brackets, a decided label either way — so the
# fully-streamed result should equal `sanitise()` run on the whole thing, and
# every partial prefix along the way should be a literal prefix of that.
_GROWING_PREFIX_SOURCES = [
    "Right, but historically that is the part people skip. Adoption is real and uneven at once.",
    "Wayne: settle down, that is not what the data says at all.",
    "Look <em>this</em> matters, and [aside] it always will, believe me.",
    "She said (laughs) that was the whole point, and then went quiet for a moment.",
    "An ordinary parenthetical (that never closes within the character hold) still reads fine.",
    "Not a label: Wayne is fine either way this one resolves.",
]


@pytest.mark.parametrize("source", _GROWING_PREFIX_SOURCES)
def test_stable_prefix_only_ever_grows_by_literal_extension(source: str) -> None:
    """As more raw text streams in, `sanitise(stable_prefix(...))` must only
    ever gain a literal suffix, never rewrite what it already produced.
    `StreamingClaudeBrain.stream` asserts this same invariant at runtime on
    real streamed output (see its module docstring for why it must); this is
    the equivalent check against every possible token boundary, with nothing
    but pure functions and no network involved."""
    previous = ""
    for k in range(len(source) + 1):
        current = sanitise(stable_prefix(source[:k]))
        assert current.startswith(previous), (
            f"stable_prefix broke its own invariant at k={k}: "
            f"{current!r} does not extend {previous!r}"
        )
        previous = current
    # And once the stream has genuinely ended, nothing should still be held
    # back: the fully-streamed result must equal sanitise() run on the whole
    # utterance in one go, exactly as `StreamingClaudeBrain.stream`'s final
    # resolution pass computes it once no more text is coming.
    assert previous == sanitise(source)


def test_being_asked_mid_turn_says_so_and_says_you_are_not_cutting_in() -> None:
    """The round `FloorController._agent_utterance_progress` opens.

    Told nothing, an agent asked while somebody else is speaking writes as
    though the floor were open now — either an interruption (which the
    runtime will never air: agents never interrupt agents) or an answer to
    Ricky that ignores the thirty seconds in between. It has to know it is
    writing the *next* turn, against a turn still in progress.
    """
    state = replace(
        PanelState.for_agents(("dex", "wayne", "melia")),
        speaking="melia",
        agent_partial="Reported adoption and actual adoption are different curves.",
        transcript=(Utterance(speaker=HUMAN, text="Where are we?", t=0.0),),
    )
    prompt = build_turn_prompt(state, _persona())
    assert "melia is speaking right now" in prompt
    assert "nobody here interrupts anybody" in prompt
    # The running text has to be visible, or there is nothing to write against.
    assert "melia (speaking): Reported adoption" in prompt


def test_the_speaker_is_never_told_it_is_following_itself() -> None:
    """`idle_agents()` already excludes whoever is on the PA, so this branch
    should be unreachable from the floor — but `build_turn_prompt` is also
    called from the sim and the bench, and "you are not going to cut in" is a
    nonsense instruction to give an agent about its own turn."""
    state = replace(
        PanelState.for_agents(("dex", "wayne")),
        speaking="dex",
        agent_partial="Something I am part-way through saying.",
    )
    assert "is speaking right now" not in build_turn_prompt(state, _persona())


# --------------------------------------------------------------------------
# Audio tags — the closed allowlist `sanitise()` enforces
# --------------------------------------------------------------------------
#
# Added 5 Oct 2026 with `eleven_v3_conversational`, which *performs* a
# bracketed tag rather than reading it aloud. That makes brackets no longer
# inert, and the risk inverts: the hazard is not a stray word over the PA (an
# unrecognised tag was measured being swallowed, not spoken) but a tag the
# model acts on — `[applause]`, `[gunshot]`, `[strong French accent]` are all
# real, documented and functional. The vendor's vocabulary is free text and
# grows without us, so the rule has to be an allowlist, and these tests are
# what make "closed" true.


def test_an_allowlisted_tag_survives_sanitise() -> None:
    assert sanitise("[laughs] that is the whole argument") == (
        "[laughs] that is the whole argument"
    )


def test_tag_case_and_inner_spacing_are_normalised_not_rejected() -> None:
    """The model writes `[Laughs]` often enough that strictness would silently
    drop a tag the persona was told to use."""
    assert sanitise("[Laughs] ok") == "[laughs] ok"
    assert sanitise("[  CLEARS   THROAT ] ok") == "[clears throat] ok"


@pytest.mark.parametrize(
    "tag",
    [
        "[applause]",
        "[gunshot]",
        "[explosion]",
        "[sings]",
        "[strong French accent]",
        "[whispers]",
        "[laughs harder]",
        "[hesitates]",
        # The realistic shape of an invented one — plausible, not silly.
        "[leaning forward]",
        "[gestures at Melia]",
    ],
)
def test_every_tag_outside_the_allowlist_is_destroyed(tag: str) -> None:
    """Including documented, working tags we chose not to allow — the point of
    a closed list is that the vendor adding one does not add it to the show."""
    assert sanitise(f"{tag} that is the whole argument") == ("that is the whole argument")


def test_an_unclosed_allowlisted_tag_is_still_withheld_while_streaming() -> None:
    """`[laugh` could still become `[laughs]` (kept) or `[laughing]` (stripped),
    so its shape is unknowable until it closes — exactly as before the
    allowlist existed. The allowlist must not tempt anyone into releasing it
    early."""
    assert stable_prefix("that landed [laugh") == "that landed "
    assert stable_prefix("that landed [laughs]") == "that landed [laughs]"


def test_personas_audio_tags_must_be_allowlisted() -> None:
    """A typo would be stripped at runtime and silently do nothing, so the
    cast data is where it has to fail loudly."""
    with pytest.raises(ValueError, match="allowlist"):
        _persona(audio_tags=["guffaws"])


def test_audio_tags_render_into_the_system_prompt() -> None:
    persona = _persona(audio_tags=["sighs", "dryly"])
    prompt = build_system_prompt(persona)
    assert "[sighs], [dryly]" in prompt
    assert "performs it" in prompt


def test_a_persona_with_no_audio_tags_is_never_told_the_mechanism_exists() -> None:
    """Tags are characterisation. An agent not cast to laugh should not learn
    that laughing is available."""
    prompt = build_system_prompt(_persona())
    assert "Sounds your voice can actually make" not in prompt


def test_the_real_cast_only_uses_allowlisted_tags() -> None:
    """`PanelCast.from_dir` validates, so this is really asserting the shipped
    YAML parses — but it is the one test that fails if someone adds a tag to a
    persona without adding it to `AUDIO_TAGS`."""
    cast = PanelCast.from_dir(PERSONA_DIR)
    for persona in cast.personas.values():
        assert set(persona.audio_tags) <= AUDIO_TAGS


def test_personas_accent_must_be_allowlisted() -> None:
    """Same failure shape as `audio_tags`: a typo here is silently never applied."""
    with pytest.raises(ValueError, match="ACCENT_TAGS"):
        _persona(accent="scouse accent")


def test_accent_never_reaches_the_system_prompt() -> None:
    """Unlike `audio_tags`, an accent is not a performance cue the model is
    told about — `panel_runtime.tts` applies it directly. Rendering it here
    too would give the model a second, uncontrolled way to write it inline."""
    prompt = build_system_prompt(_persona(accent="irish accent"))
    assert "accent" not in prompt.lower()


def test_the_real_cast_only_uses_allowlisted_accents() -> None:
    """The `audio_tags` equivalent, for `accent`."""
    cast = PanelCast.from_dir(PERSONA_DIR)
    for persona in cast.personas.values():
        if persona.accent is not None:
            assert persona.accent in ACCENT_TAGS


def test_personas_pace_must_be_allowlisted() -> None:
    """Same failure shape again: a typo here is silently never applied."""
    with pytest.raises(ValueError, match="PACE_TAGS"):
        _persona(pace="quickly")


def test_pace_never_reaches_the_system_prompt() -> None:
    """Like `accent` and unlike `audio_tags`: `panel_runtime.tts` applies it
    directly, and telling the model about it would give it a second,
    uncontrolled way to write the tag inline."""
    prompt = build_system_prompt(_persona(pace="briskly"))
    assert "briskly" not in prompt.lower()


def test_the_real_cast_only_uses_allowlisted_paces() -> None:
    """The `accent` equivalent, for `pace` — and the assertion that Wayne's
    standing `[briskly]` is actually authored, since nothing else in the cast
    data would notice if it were dropped."""
    cast = PanelCast.from_dir(PERSONA_DIR)
    for persona in cast.personas.values():
        if persona.pace is not None:
            assert persona.pace in PACE_TAGS
    assert cast["wayne"].pace == "briskly"
