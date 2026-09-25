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

import pytest
from panel_core.events import HUMAN
from panel_core.personas import Persona
from panel_core.prompts import (
    GUARDRAILS,
    build_system_prompt,
    build_turn_prompt,
    sanitise,
    stable_prefix,
)
from panel_core.state import PanelState, Utterance

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
    assert "Rephrasing their point back at them" in prompt


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
def test_stable_prefix_withholds_exactly_the_unresolved_construct(
    raw: str, expected: str
) -> None:
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
def test_leading_label_withheld_only_while_genuinely_ambiguous(
    raw: str, ambiguous: bool
) -> None:
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
