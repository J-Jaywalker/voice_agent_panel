"""Chunker acceptance criteria.

Every one of these is a thing the audience would otherwise hear go wrong.
"""

from __future__ import annotations

from panel_runtime.chunking import SentenceChunker


def stream(text: str, *, every: int = 7) -> tuple[list[str], str | None]:
    """Push text in small pieces, as a model streams it."""
    chunker = SentenceChunker()
    chunks: list[str] = []
    for i in range(0, len(text), every):
        chunks.extend(chunker.push(text[i : i + every]))
    return chunks, chunker.flush()


def test_splits_on_sentence_ends():
    chunks, tail = stream(
        "Trust is the real blocker here. Nobody agrees who is accountable. "
        "That is not an engineering problem."
    )
    assert chunks == [
        "Trust is the real blocker here.",
        "Nobody agrees who is accountable.",
        "That is not an engineering problem.",
    ]
    assert tail is None


def test_first_chunk_arrives_before_the_rest_is_written():
    """The whole point: speaking must not wait on the last sentence."""
    chunker = SentenceChunker()
    early = chunker.push("Trust is the real blocker here. And the")
    assert early == ["Trust is the real blocker here."]


def test_does_not_split_inside_an_abbreviation():
    text = "I worked with Dr. Chen on exactly this problem for two years."
    chunks, tail = stream(text)
    assert [*chunks, tail] .count(text) == 1, "must stay one utterance"
    assert not any(c.endswith("Dr.") for c in chunks)


def test_does_not_split_inside_a_decimal():
    chunks, tail = stream("It came to 1.5 million pounds over the first eighteen months.")
    spoken = [*chunks, tail]
    assert any(c and "1.5 million" in c for c in spoken), "the decimal must stay intact"
    assert not any(c and c.endswith("1.") for c in spoken)


def test_does_not_split_on_an_initial():
    chunks, tail = stream("That is R. Wayne's argument and I do not accept it at all.")
    spoken = [*chunks, tail]
    assert any(c and "R. Wayne" in c for c in spoken), "the initial must stay attached"


def test_short_fragments_are_merged_forward():
    """'No.' alone comes back from TTS sounding clipped."""
    chunks, tail = stream("No. That gets the causation exactly backwards, I think.")
    spoken = [c for c in [*chunks, tail] if c]
    assert "No." not in spoken, "a two-word chunk comes back from TTS sounding clipped"
    assert spoken[0].startswith("No.")


def test_questions_and_exclamations_are_boundaries():
    chunks, _ = stream(
        "So who actually signs it off in the end? Nobody has been able to tell me that."
    )
    assert chunks[0].endswith("?")


def test_a_long_clause_breaks_rather_than_stalling():
    """A sentence that runs on must not hold the speaker silent."""
    long_run = (
        "The thing everyone gets wrong about adoption is that they measure capability, "
        "when the actual constraint is trust and accountability, and that gap is where "
        "every single deployment I have watched has quietly stalled out, "
        "and none of them could tell me why afterwards, "
    )
    chunks, _ = stream(long_run)
    assert chunks, "a long run-on must emit something rather than buffer forever"
    assert len(chunks[0]) <= 250


def test_flush_returns_the_tail_and_empties():
    chunker = SentenceChunker()
    chunker.push("An unfinished thought")
    assert chunker.flush() == "An unfinished thought"
    assert chunker.flush() is None


def test_nothing_is_lost():
    text = (
        "Trust is the blocker. I have watched three organisations try it. "
        "Two got nowhere. Not because the technology failed."
    )
    chunks, tail = stream(text)
    rejoined = " ".join([*chunks, tail or ""]).strip()
    assert rejoined.replace("  ", " ") == text.strip()
