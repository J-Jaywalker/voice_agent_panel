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

import pytest
from panel_core.prompts import build_system_prompt, sanitise, stable_prefix
from panel_core.personas import Persona

# --------------------------------------------------------------------------
# Approved knowledge — anecdotes rendered into the system prompt
# --------------------------------------------------------------------------


def _persona(**overrides) -> Persona:
    defaults = dict(
        id="dex",
        name="Dexter",
        job_title="x",
        employer="x",
        background="x",
        stance="x",
        introduction="x",
        voice_id="x",
        communication_style="x",
    )
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
