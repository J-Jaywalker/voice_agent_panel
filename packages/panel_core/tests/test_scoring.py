"""Tests for the pure predicates in `panel_core.scoring`.

`floor_priority` is exercised behaviourally through `test_floor.py`, where a
score only matters as the reason someone did or did not get the floor. The
lexicon predicates are the opposite: they are judged on individual lines, and
the lines are the test data, so they get their own file.
"""

from __future__ import annotations

import pytest
from panel_core import is_wait_narration

# Ids and display names from `personas/*.yaml`, the way
# `StreamingClaudeBrain` assembles them. Ricky is not a persona and is
# already in `WAIT_FILLER`.
NAMES = ("melia", "Melia", "wayne", "Wayne", "dex", "Dexter")


# Real lines from the rehearsal recordings (`recordings/*.jsonl`), every one
# of which actually reached the PA. 690 agent turns were recorded; these are
# the whole of what this predicate is for.
WAIT_NARRATION = [
    "Sorry, go ahead, Ricky.",
    "Yeah, go on Ricky, we're listening.",
    "Let him finish the sentence, Ricky.",
    "Happy to wait my turn, Ricky.",
    "Take your time, Ricky — floor's yours whenever you want it.",
    "Mm. Let's hear the answer first.",
]


# The first five are GUARDRAILS' protected short reactions, quoted in
# `build_turn_prompt` as the shape a reaction is allowed to take. Three of
# them contain a hold phrase verbatim, which is why this is a residue rule
# and not a phrase match: "wait", "hold on" and "let him finish" all open
# legitimate turns, and only a line with *nothing else in it* is filler.
NOT_WAIT_NARRATION = [
    "How do you mean?",
    "Come on, Dexter",
    "Wait, what?",
    "Yeah, no, that's fair.",
    "Hold on, Dexter, say that number again.",
    "Let him finish his story, but I've got my own point.",
    "Go on then — what is the actual number?",
    "That's not mine to be in — I'll wait for the fireproof cladding.",
    "Give it a second — there's a real answer here, not just a mood.",
    "Go on, Ricky — ask it. I've got an answer either way.",
]


@pytest.mark.parametrize("line", WAIT_NARRATION)
def test_a_line_that_is_only_an_offer_to_wait_is_flagged(line: str):
    assert is_wait_narration(line, names=NAMES)


@pytest.mark.parametrize("line", NOT_WAIT_NARRATION)
def test_a_line_with_anything_substantive_left_is_not_flagged(line: str):
    """Zero false positives is the requirement; recall is not.

    A missed filler line costs a few seconds of dead air. A false positive
    deletes a legitimate reaction mid-show, and the agent then looks like it
    had nothing to say. "Go on, Ricky — ask it. I've got an answer either
    way." is the honest cost of that bias: it *is* a weak line, and it passes,
    because something follows the hold phrase.
    """
    assert not is_wait_narration(line, names=NAMES)


def test_the_cast_s_own_names_are_vocatives_not_content():
    """A colleague's name left over after a hold phrase is still nothing."""
    assert is_wait_narration("Go on, Wayne.", names=NAMES)
    assert not is_wait_narration("Go on, Wayne.", names=()), (
        "without the cast the name is an unknown word, and the predicate fails open"
    )


def test_a_line_with_no_hold_phrase_is_never_flagged():
    """Thin is not the same as wait-narration — `is_backchannel` owns thin."""
    assert not is_wait_narration("Yeah. Sure. Right.", names=NAMES)
    assert not is_wait_narration("", names=NAMES)


# --------------------------------------------------------------------------
# complete=False — the same lines arriving one character at a time
# --------------------------------------------------------------------------


@pytest.mark.parametrize("line", WAIT_NARRATION)
def test_no_prefix_of_a_wait_line_is_ever_mistaken_for_a_turn(line: str):
    """The streaming caller's real question, asked at every length.

    `StreamingClaudeBrain` raises an agent's hand off the first characters of
    the utterance, so the predicate is put to "L", "Let him", "Let him finish
    the sentenc" long before it is ever put to the line. Judged as complete
    text every one of those passes — no hold phrase has finished arriving, so
    there is no residue and the default rule fails open by construction. The
    gate is then permanently open and the whole filter is decorative; this is
    the test that says so.
    """
    for end in range(1, len(line) + 1):
        assert is_wait_narration(line[:end], names=NAMES, complete=False), (
            f"the gate opened at {line[:end]!r}"
        )


@pytest.mark.parametrize(
    ("line", "opens_at"),
    [
        ("Reported adoption is not actual adoption.", "Reported "),
        ("Hold on, Dexter, say that number again.", "Hold on, Dexter, say "),
        ("Go on then — what is the actual number?", "Go on then — what "),
    ],
)
def test_a_real_turn_raises_its_hand_at_its_first_substantive_word(line: str, opens_at: str):
    """The hold costs one word, not a turn.

    The price of judging a prefix is that the hand goes up a word later than
    it used to — the first character is no longer enough, because the first
    character of a hold phrase looks exactly like the first character of a
    turn. One word is the whole cost: a line that opens inside the lexicon
    ("Go on then...") pays until it leaves it, and nothing pays longer.
    """
    assert is_wait_narration(opens_at[:-1], names=NAMES, complete=False)
    assert not is_wait_narration(opens_at, names=NAMES, complete=False)
    assert not is_wait_narration(line, names=NAMES)


def test_a_thin_honest_line_is_held_while_streaming_and_released_when_whole():
    """A hold is provisional; only `complete=True` is a verdict.

    "Come on, Dexter" is built entirely out of words the hold phrases use, so
    there is no point during generation at which it has left the lexicon — it
    is held to the last character. That is not a decision to silence it: the
    streaming caller settles every surviving hold with a `complete=True` call
    once the utterance is whole, and this line passes that one.
    """
    assert is_wait_narration("Come on, Dexter", names=NAMES, complete=False)
    assert not is_wait_narration("Come on, Dexter", names=NAMES)
