"""Address detection by model, for the live runtime.

Answers the one question `FloorController._detect` answers with regex: *who did
Ricky just invite to speak?* The regex is accurate on the phrasings it was
written for — 153/153 of `packages/panel_core/tests/test_address.py`, by
construction — and structurally incapable of resolving a descriptive reference.
"What does the financial side make of that?" is Wayne, and no pattern over the
transcript can know that, because the fact that makes it true lives in
`personas/wayne.yaml`.

This module is the network half. The decision half stays in `panel_core`, which
never sees a socket: the verdict is emitted as an `AddressDetected` event and
reduced there, so a recorded session still replays identically through modified
floor logic.

**Latency is the whole design.** The classifier sits exactly where
`_apply_detection` sits — on the final that opens the floor, which is the moment
this project has spent months clearing. `~/git/FDE/amazon_alexa_demo/wake.py` is
the reference implementation and solves the identical problem; this mirrors its
structure, and it is worth reading before changing any of this:

- the verdict is decoded from the **first content delta** rather than the
  finished message. `prompts.address_verdicts` enforces a distinct initial per
  token, so one character normally settles it and time-to-verdict equals
  time-to-first-token;
- the human-readable reason keeps streaming in a background task and is filed
  against the cache entry when it lands. Nothing ever waits on prose;
- interim transcripts are speculatively classified while Ricky is still
  talking, so the common case at finalisation is a cache hit at zero measured
  latency. That hit rate is the main open question about this whole approach
  and is why `AddressVerdict.source` exists;
- an in-flight speculation for *exactly* the finalised text is **joined**, not
  cancelled — cancelling throws away the head start and pays a whole fresh
  round trip;
- it fails closed, and closed here means `verdict=None`: hand the question back
  to the regex, which is a correct answer for every phrasing in the corpus.
  Inventing a `NONE` would silently swallow a real invitation.

Two differences from `wake.py`, both deliberate:

**No wake-word gate.** `wake.py`'s first speculation gate requires the wake word
to plausibly be present, because otherwise every interim from every speaker in
the room becomes an API call. There is no analogue here: Ricky's mic is the only
input, every word on it is addressed to the panel in some sense, and the whole
capability being bought is resolving invitations that name *nobody*. So the gate
is dropped and only the growth and throttle gates remain. Cost is not a
constraint (CLAUDE.md).

**No `on_reason` callback.** `wake.py` delivers the reason to a UI. Here it is
filed against the cache entry so a speculative hit carries it into
`AddressDetected.reason` for the operator console, and a fresh verdict simply
has none yet. One less moving part on the hot path, and no late console line
landing on top of an agent who has already started speaking.

Model notes, all verified rather than assumed:

- `claude-haiku-4-5` is a pre-4.6 model. `thinking`, `output_config` and
  `effort` are rejected outright, which is exactly why the verdict is decoded
  from characters rather than from a JSON schema.
- **`temperature` is not passed, and cannot be.** The Anthropic SDK dropped it
  from `messages.stream()` at 1.x (checked against 1.2.0, which
  `panel_runtime` pins — `inspect.signature` has no such parameter).
  `wake.py` sends `temperature=0.0` because it is on the 0.x line; copying that
  line raises `TypeError`. The call shape below is copied from
  `tests/bench_address.py`, which is the verified-working one. The consequence
  is that a verdict is not reproducible for free — `bench_address.py --repeat`
  is how you find out whether one is stable.
- the system prompt is cached (`cache_control: ephemeral`) and never changes
  mid-show, so the volatile utterance goes in the user turn.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from typing import Literal

import anthropic
from panel_core import PanelCast
from panel_core.prompts import (
    address_verdicts,
    build_address_prompt,
    decode_address_verdict,
)

from .config import anthropic_base_url

log = logging.getLogger(__name__)

# Latency knobs. They live together because they are a budget, not a set of
# unrelated constants. Re-measure on the venue rig (CLAUDE.md § Deployment).
DEFAULT_MODEL = "claude-haiku-4-5"

# Eight words of reason behind a one-word verdict; the prompt asks for no more.
_MAX_TOKENS = 32
# Backstop for a genuinely stuck stream, not a latency budget — the budget is
# `panel.ADDRESS_HOLD_TIMEOUT_S`, which bounds how long the floor waits.
_VERDICT_TIMEOUT_S = 3.0
# Wall-clock throttle on speculation, independent of how fast interims arrive.
_SPECULATION_INTERVAL_S = 0.25
# How long `classify` will wait to join a speculation already running against
# this exact text before giving up and asking again.
_SPECULATION_JOIN_S = 0.6
# The partial must have actually grown. Speechmatics emits interims far faster
# than the classifier can answer them. Counting from zero also means the first
# speculation of a turn needs two words, which is the floor we want: "So," is
# not a question yet.
_SPECULATION_MIN_WORD_GROWTH = 2
_CACHE_MAX_ENTRIES = 32

_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
_REASON_LEAD_CHARS = "—–-:,. \t"

VerdictSource = Literal[
    "speculative_hit",  # decided before Ricky stopped talking; zero measured cost
    "joined",  # the speculation for this exact text was still running
    "fresh",  # no speculation had been attempted
    "recomputed",  # speculation ran, but against text that was then revised
    "unavailable",  # not consulted, or it failed — fall back to the regex
    "timeout",  # the floor's hold window expired first — fall back to the regex
]


@dataclass(frozen=True, slots=True)
class AddressVerdict:
    """One classification of one utterance.

    Attributes:
        verdict: A token from `prompts.address_verdicts()`, or None meaning the
            classifier was not consulted or could not answer. None is the
            fail-closed value and means "let the regex decide"; it is never
            "nobody was invited", which is `NO_VERDICT`.
        agent: The agent id `verdict` resolves to, or None for the four
            non-agent verdicts.
        reason: The model's own short justification, or "". Only a verdict
            whose stream had already finished carries one — the floor never
            waits on prose.
        latency_ms: Time to the *verdict token*, not to completion. Zero on a
            cache hit, because the decision predates the question.
        source: How it was obtained. This is the field the on-stage cache-hit
            rate is read off.
    """

    verdict: str | None
    agent: str | None
    reason: str
    latency_ms: float
    source: VerdictSource


def _normalise(text: str) -> str:
    """Lowercase, drop punctuation and collapse whitespace.

    This is the cache key. Punctuation becomes a space rather than being
    deleted so that token boundaries survive — "melia's" must not collapse into
    one token.

    Args:
        text: Raw transcript text.

    Returns:
        The normalised form, or "" if nothing survives.
    """
    lowered = _PUNCTUATION_RE.sub(" ", (text or "").lower())
    return _WHITESPACE_RE.sub(" ", lowered).strip()


def _user_turn(text: str) -> str:
    """Render the per-utterance user message.

    Kept byte-identical in shape to `tests/bench_address.py`, which is where
    the 153/153 accuracy figure was measured — the prompt the classifier is
    scored against has to be the prompt it runs. Double quotes are folded to
    single so a transcript containing one cannot close the delimiter early;
    Speechmatics segments do not contain them, so in practice this changes
    nothing and is only a guard.

    Args:
        text: The transcript segment.

    Returns:
        The user message body.
    """
    segment = (text or "").strip().replace('"', "'")
    return f'RICKY SAID: "{segment}"'


def _extract_reason(buffer: str) -> str:
    """Pull the trailing justification out of a completed response.

    Args:
        buffer: The full completion, e.g. `"MELIA - named as the subject."`.

    Returns:
        The reason with its leading separator stripped, or "".
    """
    parts = buffer.strip().split(None, 1)
    if len(parts) < 2:
        return ""
    return parts[1].lstrip(_REASON_LEAD_CHARS).strip()


def _unavailable(*, latency_ms: float = 0.0) -> AddressVerdict:
    """The fail-closed verdict: no answer, so the regex decides."""
    return AddressVerdict(
        verdict=None,
        agent=None,
        reason="",
        latency_ms=latency_ms,
        source="unavailable",
    )


class AddressClassifier:
    """Streaming addressee classifier, optimised for time-to-verdict.

    One instance per show. Not thread-safe and not meant to be: every method
    runs on the runtime's single event loop.
    """

    def __init__(
        self,
        cast: PanelCast,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
    ) -> None:
        """Initialise the classifier.

        Args:
            cast: The panel. Both the prompt and the verdict vocabulary are
                rendered from it — personas are the source of truth (CLAUDE.md),
                and the classifier cannot resolve "the financial side" to Wayne
                without reading Wayne's own topics of authority.
            model: Model id. Pre-4.6 by default, so no `thinking`,
                `output_config` or `effort` may ever be sent.
            api_key: Overrides `ANTHROPIC_API_KEY`. Only used by tests.

        Raises:
            RuntimeError: If no API key is available.
            ValueError: From `address_verdicts` if two verdict tokens share an
                initial, which would silently cost a token per turn on stage.
                Renaming a persona is the moment to find out, not the show.
        """
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")

        self._model = model
        self._verdicts = address_verdicts(cast)
        self._system_prompt = build_address_prompt(cast)
        # `anthropic_base_url()`, never the ambient `ANTHROPIC_BASE_URL`: a
        # token-saving proxy in a dev shell rewrites the prompt in flight,
        # which destroys the byte-exact prefix prompt caching depends on. See
        # `panel_runtime.config.anthropic_base_url` for the measured cost.
        self._client = anthropic.AsyncAnthropic(api_key=key, base_url=anthropic_base_url())

        # Keyed on the WHOLE normalised text. Never a prefix — see `classify`.
        self._cache: dict[str, AddressVerdict] = {}
        # Reasons arrive after their verdict, so they are filed separately and
        # merged at read time rather than racing the cache write.
        self._reasons: dict[str, str] = {}
        self._inflight: asyncio.Task[None] | None = None
        self._inflight_key: str | None = None
        self._background: set[asyncio.Task[None]] = set()
        self._last_spec_words = 0
        self._last_spec_at = 0.0
        self._spec_attempted = False

        log.info(
            "address: classifier ready — model=%s, verdicts=%s, prompt=%d chars",
            self._model,
            ",".join(sorted(self._verdicts)),
            len(self._system_prompt),
        )

    # -- public API ---------------------------------------------------

    def speculate(self, partial_text: str) -> None:
        """Fire-and-forget speculative classification of an interim result.

        Called from the STT pump, so it must never block and never raise.
        Anything that goes wrong is swallowed and logged: a failed speculation
        costs the head start, never the turn.

        Args:
            partial_text: The interim transcript so far.
        """
        try:
            self._speculate(partial_text)
        except Exception as exc:  # noqa: BLE001 — the hot path must not raise
            log.debug("address: speculation suppressed: %r", exc)

    async def classify(self, text: str) -> AddressVerdict:
        """Classify a finalised utterance, returning at the verdict token.

        Returns as soon as the leading token is decodable, which is normally
        the first content delta. The trailing reason keeps streaming in a
        background task; the floor is never blocked on it.

        Args:
            text: The finalised transcript segment.

        Returns:
            An `AddressVerdict`. On empty input, timeout, API failure or a
            response that never decodes to a known token, it fails closed with
            `verdict=None` and `source="unavailable"`, which tells the reducer
            to fall back to the regex.
        """
        entered = time.perf_counter()
        key = _normalise(text)
        if not key:
            return _unavailable()

        # Exact normalised match only. A speculative verdict whose text is
        # merely a *prefix* of the final must never be reused, and keying on the
        # whole string makes prefix reuse structurally impossible rather than
        # merely discouraged. The panel case: a partial of "Wayne" looks like a
        # vocative and resolves to WAYNE, while the final turns out to be
        # "Wayne's point earlier was wrong, Melia, what do you think?" — which
        # is MELIA, and Wayne is the one agent who must not get the floor.
        # Never write a `startswith` lookup here as an optimisation.
        cached = self._cache.get(key)
        if cached is not None:
            log.debug("address: speculative hit (%s) for %r", cached.verdict, key)
            return replace(cached, latency_ms=0.0, reason=self._reasons.get(key, cached.reason))

        # The last partial was this exact text and the model is still thinking
        # about it. Joining that call beats cancelling it and paying for a
        # whole fresh round trip.
        inflight = self._inflight
        if inflight is not None and self._inflight_key == key and not inflight.done():
            await asyncio.wait({inflight}, timeout=_SPECULATION_JOIN_S)
            cached = self._cache.get(key)
            if cached is not None:
                waited = (time.perf_counter() - entered) * 1000.0
                log.debug("address: joined speculation (%.0f ms)", waited)
                return replace(
                    cached,
                    latency_ms=waited,
                    reason=self._reasons.get(key, cached.reason),
                    source="joined",
                )

        # Anything still running is for text that has since been revised.
        self._cancel_inflight()
        source: VerdictSource = "recomputed" if self._spec_attempted else "fresh"
        return await self._classify_once(text, key=key, source=source, store=False)

    def reset(self) -> None:
        """Clear the cache and the speculation gates. Call on turn boundaries.

        The gates in particular *must* be reset per turn: `_last_spec_words`
        carries the previous turn's word count, and left alone it would block
        every early speculation of the next turn — the one place where the head
        start is worth most. In-flight speculation is cancelled; reason-drain
        tasks are deliberately left running so a late reason for the turn just
        decided still reaches the log.
        """
        self._cache.clear()
        self._reasons.clear()
        self._last_spec_words = 0
        self._last_spec_at = 0.0
        self._spec_attempted = False
        self._cancel_inflight()

    async def close(self) -> None:
        """Cancel every outstanding task and close the HTTP client."""
        self._cancel_inflight()
        pending = [task for task in self._background if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._background.clear()
        self._cache.clear()
        self._reasons.clear()
        await self._client.close()

    # -- speculation --------------------------------------------------

    def _speculate(self, partial_text: str) -> None:
        """Apply the speculation gates and launch a call if they all pass."""
        key = _normalise(partial_text)
        if not key:
            return
        if key in self._cache:
            return
        if self._inflight_key == key and self._inflight is not None and not self._inflight.done():
            return

        # `wake.py`'s gate 1 — "the wake word must plausibly be in there" — has
        # no analogue here and is deliberately absent; see the module docstring.
        # Gate: the partial must have actually grown.
        words = len(key.split())
        if words < self._last_spec_words + _SPECULATION_MIN_WORD_GROWTH:
            return

        # Gate: wall-clock throttle, independent of how fast interims arrive.
        now = time.monotonic()
        if now - self._last_spec_at < _SPECULATION_INTERVAL_S:
            return

        self._cancel_inflight()
        self._last_spec_words = words
        self._last_spec_at = now
        self._spec_attempted = True
        self._inflight_key = key
        self._inflight = asyncio.get_running_loop().create_task(
            self._run_speculation(partial_text, key), name="address-speculate"
        )

    async def _run_speculation(self, text: str, key: str) -> None:
        """Classify an interim result and cache it if it succeeds."""
        try:
            verdict = await self._classify_once(
                text, key=key, source="speculative_hit", store=True
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — speculation is best-effort
            log.debug("address: speculative classify failed: %r", exc)
            return
        if verdict.verdict is None:
            return
        log.debug(
            "address: speculated %s in %.0f ms for %r", verdict.verdict, verdict.latency_ms, key
        )

    def _store(self, key: str, verdict: AddressVerdict) -> None:
        """Cache a verdict, evicting the oldest entry past the cap."""
        self._cache[key] = verdict
        while len(self._cache) > _CACHE_MAX_ENTRIES:
            self._cache.pop(next(iter(self._cache)))

    def _cancel_inflight(self) -> None:
        """Cancel the current speculation, if any."""
        if self._inflight is not None and not self._inflight.done():
            self._inflight.cancel()
        self._inflight = None
        self._inflight_key = None

    # -- model call ---------------------------------------------------

    async def _classify_once(
        self,
        text: str,
        *,
        key: str,
        source: VerdictSource,
        store: bool,
    ) -> AddressVerdict:
        """Run one streamed classification and return at the verdict token.

        Args:
            text: The segment to classify.
            key: Its normalised form — the cache key.
            source: Provenance to stamp on the returned verdict.
            store: Whether to cache the result. True for speculation only: a
                *fresh* verdict must not be cached, or a repeat call would be
                reported as a `speculative_hit` at zero latency and the hit
                rate this whole design is judged on would flatter itself.

        Returns:
            An `AddressVerdict`, fail-closed on timeout or error.
        """
        started = time.perf_counter()
        loop = asyncio.get_running_loop()
        verdict_future: asyncio.Future[tuple[str, float]] = loop.create_future()
        # The stream runs in its own task so this coroutine can return the
        # moment the verdict resolves while the reason keeps arriving.
        worker = loop.create_task(
            self._stream(text, key, started, verdict_future), name="address-stream"
        )
        self._background.add(worker)
        worker.add_done_callback(self._background.discard)

        try:
            token, latency_ms = await asyncio.wait_for(verdict_future, _VERDICT_TIMEOUT_S)
        except asyncio.CancelledError:
            worker.cancel()
            raise
        except TimeoutError:
            worker.cancel()
            log.warning("address: verdict timed out after %.1fs", _VERDICT_TIMEOUT_S)
            return _unavailable(latency_ms=(time.perf_counter() - started) * 1000.0)
        except Exception as exc:  # noqa: BLE001 — one dead call must not stop the panel
            log.error("address: classification failed: %r", exc)
            return _unavailable(latency_ms=(time.perf_counter() - started) * 1000.0)

        verdict = AddressVerdict(
            verdict=token,
            agent=self._verdicts.get(token),
            reason="",
            latency_ms=latency_ms,
            source=source,
        )
        if store:
            self._store(key, verdict)
        return verdict

    async def _stream(
        self,
        text: str,
        key: str,
        started: float,
        verdict_future: asyncio.Future[tuple[str, float]],
    ) -> None:
        """Stream one completion, resolving the verdict at the first delta.

        Args:
            text: The segment to classify.
            key: Its normalised form, for filing the reason.
            started: `time.perf_counter()` at call time.
            verdict_future: Resolved with `(token, latency_ms)`.
        """
        buffer = ""
        token: str | None = None
        try:
            async with self._client.messages.stream(
                model=self._model,
                max_tokens=_MAX_TOKENS,
                # No `temperature` — the SDK dropped it from `stream()` at 1.x.
                # No `thinking`/`output_config`/`effort` — Haiku 4.5 rejects all
                # three. See the module docstring.
                system=[
                    {
                        "type": "text",
                        "text": self._system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": _user_turn(text)}],
            ) as stream:
                async for delta in stream.text_stream:
                    buffer += delta
                    if token is not None:
                        continue
                    token = decode_address_verdict(buffer, self._verdicts)
                    if token is None or verdict_future.done():
                        continue
                    elapsed = (time.perf_counter() - started) * 1000.0
                    verdict_future.set_result((token, elapsed))
        except asyncio.CancelledError:
            if not verdict_future.done():
                verdict_future.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 — reported via the future
            if not verdict_future.done():
                verdict_future.set_exception(exc)
            else:
                log.warning("address: reason stream failed: %r", exc)
            return

        if token is None:
            # The model went off-script: the leading word decoded to no known
            # verdict, so there is nothing to act on. Resolving the future here
            # matters — without it the caller hangs until its own timeout for a
            # stream that has already finished. Fail closed to the regex rather
            # than guessing which token was meant, and log the raw text so the
            # prompt can be fixed.
            log.warning("address: undecodable verdict %r", buffer)
            if not verdict_future.done():
                verdict_future.set_exception(ValueError(f"undecodable verdict {buffer!r}"))
            return

        reason = _extract_reason(buffer)
        if not reason:
            return
        # Filed separately from the verdict rather than written onto it, so the
        # reason can land after `classify` has already returned without racing
        # the cache write. Capped like the cache for the same reason: a turn
        # that never ends must not grow one of these without bound.
        self._reasons[key] = reason
        while len(self._reasons) > _CACHE_MAX_ENTRIES:
            self._reasons.pop(next(iter(self._reasons)))
