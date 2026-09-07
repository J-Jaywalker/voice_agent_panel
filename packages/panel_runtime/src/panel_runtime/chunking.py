"""Sentence-boundary chunking for streamed speech.

The measurement that made this necessary: a proposal is ~200 output tokens and
takes 4-6s to generate, which is not thinking time — it is generation at roughly
40 tokens/sec. Waiting for the whole utterance before speaking put first audio
4-6 seconds after the request, which is unusable.

But an agent does not need its last sentence written before it can say its
first. Feeding TTS sentence by sentence makes first audio depend on the *first*
clause rather than the whole turn, and the rest generates comfortably faster
than it is spoken (~2.8 words/sec out loud vs ~40 tokens/sec generated), so the
speech never catches up with the writing.

Splitting well matters because the boundary is where prosody is decided. Two
rules do most of the work:

- **Never split inside an abbreviation or a number.** "Dr. Chen" and "1.5
  million" must not become two utterances; the pause is audible and wrong.
- **Never emit a fragment.** A very short first chunk ("No.") gets held and
  merged with what follows, because a two-word utterance sent alone to TTS comes
  back with the intonation of a complete sentence and sounds clipped.

Pure and synchronous, so it is tested in the fast suite with no audio at all.
"""

from __future__ import annotations

import re

# Abbreviations whose full stop is not a sentence end.
_ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st",
        "e.g", "i.e", "etc", "vs", "approx", "no",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    }
)

_BOUNDARY = re.compile(r"([.!?])([\s ]+|$)")
# A comma or dash boundary is a fallback for a clause that runs long without
# ever reaching a full stop — better a clause break than a 30-second breath.
_CLAUSE = re.compile(r"([,;:—–])(\s+)")

# Below this, a chunk is a fragment: hold it and merge forward.
MIN_CHUNK_CHARS = 24
# Above this with no sentence end in sight, break on the nearest clause.
# ~180 chars is ~30 words, ~11 seconds spoken — already a long single breath.
MAX_CHUNK_CHARS = 180


def _ends_in_abbreviation(text: str) -> bool:
    tail = text.rstrip()
    if not tail.endswith("."):
        return False
    word = re.split(r"[\s(]", tail[:-1])[-1].lower()
    if word in _ABBREVIATIONS:
        return True
    # A single initial ("J." in "J. Smith") or a decimal ("1.5") is not an end.
    return len(word) <= 1 or word.replace(".", "").isdigit()


class SentenceChunker:
    """Accumulates streamed text and yields speakable chunks.

    Stateful by nature — a boundary can only be judged once the character after
    it has arrived, so text is held until the next token proves it safe to emit.
    """

    def __init__(
        self,
        *,
        min_chars: int = MIN_CHUNK_CHARS,
        max_chars: int = MAX_CHUNK_CHARS,
    ) -> None:
        self.min_chars = min_chars
        self.max_chars = max_chars
        self._buffer = ""

    def push(self, text: str) -> list[str]:
        """Add streamed text; return any chunks that are now safe to speak."""
        self._buffer += text
        chunks: list[str] = []

        while True:
            chunk = self._take()
            if chunk is None:
                break
            chunks.append(chunk)
        return chunks

    def flush(self) -> str | None:
        """End of stream: emit whatever is left, fragment or not."""
        remaining = self._buffer.strip()
        self._buffer = ""
        return remaining or None

    # ------------------------------------------------------------------ internals

    def _take(self) -> str | None:
        for match in _BOUNDARY.finditer(self._buffer):
            end = match.end(1)
            candidate = self._buffer[:end]
            if _ends_in_abbreviation(candidate):
                continue
            if len(candidate.strip()) < self.min_chars:
                # A fragment. Keep looking for a later boundary to merge into.
                continue
            self._buffer = self._buffer[match.end() :]
            return candidate.strip()

        # No usable sentence end, and the buffer is getting long enough that
        # waiting would be an audible stall. Break on the last clause boundary.
        if len(self._buffer) > self.max_chars:
            clauses = list(_CLAUSE.finditer(self._buffer))
            if clauses:
                match = clauses[-1]
                candidate = self._buffer[: match.end(1)].strip()
                if len(candidate) >= self.min_chars:
                    self._buffer = self._buffer[match.end() :]
                    return candidate
        return None
