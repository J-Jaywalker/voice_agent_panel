"""Address detection by TypeSafe (Jev), for the live runtime.

Answers the same question `AddressClassifier` does — *who did Ricky just
invite to speak?* — and returns the same `AddressVerdict`, so the floor,
`AddressDetected` and every address test are unchanged by which backend ran.
Everything about *when* to call a backend — the exact-normalised cache, the
speculation gates, the join-don't-cancel rule, the fail-closed contract —
lives in `BaseAddressClassifier` and is shared.

The call shape is one `system_one` request carrying the panel and the
utterance as `state` and `prompts.build_address_questions` as the questions:
one Choice for the structural outcome, one Noul per panellist, one for
"several at once" and one for "cannot tell who". They run in parallel over
shared state, so the whole classification is one round trip with no streaming
decode — `latency_ms` is the request itself.

`prompts.compose_address_verdict` turns the typed answers back into a verdict
token. It is pure and lives in `panel_core`; this module is the network half.

Measured against `packages/panel_core/tests/test_address.py` and the
new-capability rows in `packages/panel_runtime/tests/bench_address.py`.
"""

from __future__ import annotations

import logging
import os
import time

import typesafe_sdk
from panel_core import PanelCast
from panel_core.prompts import (
    JOINT_REQUEST_QUESTION,
    UNIDENTIFIABLE_QUESTION,
    _addressed_question_name,
    build_address_questions,
    build_address_state,
    compose_address_verdict,
    resolve_address_verdict,
)

from .address import AddressVerdict, BaseAddressClassifier, VerdictSource, _unavailable

log = logging.getLogger(__name__)

# Pinned, never `jev-latest` — that alias moves, and a verdict measured
# against one model is not a claim about what runs on show night.
DEFAULT_MODEL = "jev-1.13.0"

# Per-call budget on the live path. Under `panel.ADDRESS_HOLD_TIMEOUT_S`
# (0.9s), which bounds how long the floor waits, with room for the fallback.
_TIMEOUT_S = 0.55

# No retries on the live path: a retry plus its backoff outlives the hold
# window, so a second attempt can only arrive after the floor has already
# fallen back to the regex. `bench_address.py` sets its own policy.
_RETRY = typesafe_sdk.RetryPolicy(max_retries=0, timeout=_TIMEOUT_S)

# The SDK redacts sensitive headers but not request and response bodies, and
# the body carries Ricky's live speech.
_MIN_LOG_LEVEL = "WARNING"
_QUIET_LOG_LEVELS = frozenset({"WARNING", "WARN", "ERROR", "CRITICAL", "FATAL"})


def _quieten_sdk_logging() -> None:
    """Hold `TYPESAFE_LOG_LEVEL` at WARNING or above before the client reads it."""
    level = os.environ.get(typesafe_sdk.constants.LOG_LEVEL_ENV, "").strip().upper()
    if level not in _QUIET_LOG_LEVELS:
        os.environ[typesafe_sdk.constants.LOG_LEVEL_ENV] = _MIN_LOG_LEVEL


class TypeSafeAddressClassifier(BaseAddressClassifier):
    """Addressee classifier over TypeSafe's typed-answer API."""

    def __init__(
        self,
        cast: PanelCast,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
    ) -> None:
        """Initialise the classifier.

        Args:
            cast: The panel. The questions, the state and the verdict
                vocabulary are all rendered from it — personas are the source
                of truth (CLAUDE.md), and the classifier cannot resolve "the
                financial side" to Wayne without reading Wayne's own topics of
                authority.
            model: TypeSafe model id. Pinned by default.
            api_key: Overrides `TYPESAFE_API_KEY`. Only used by tests.

        Raises:
            RuntimeError: If no API key is available.
        """
        key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not key:
            raise RuntimeError("TYPESAFE_API_KEY is not set")

        super().__init__(cast)
        _quieten_sdk_logging()
        self._cast = cast
        self._model = model
        self._questions = build_address_questions(cast)
        self._client = typesafe_sdk.AsyncTypeSafeClient(
            api_key=key, retry=_RETRY, timeout=_TIMEOUT_S
        )

        log.info(
            "address: typesafe classifier ready — model=%s, verdicts=%s, questions=%s",
            self._model,
            ",".join(sorted(self._verdicts)),
            ",".join(sorted(self._questions)),
        )

    async def _close_client(self) -> None:
        await self._client.aclose()

    async def _classify_once(
        self,
        text: str,
        context: str,
        *,
        key: str,
        source: VerdictSource,
        store: bool,
        recent: tuple[dict[str, str], ...] = (),
    ) -> AddressVerdict:
        """Run one TypeSafe call and recompose its answers into a verdict.

        Args:
            text: The segment to classify.
            context: `prompts.build_address_context` output for the moment it
                was said in, which is what resolves "the other two".
            key: The cache key for `(text, context, recent)`.
            source: Provenance to stamp on the returned verdict.
            store: Whether to cache the result. True for speculation only.
            recent: `prompts.build_address_recent`'s output — the actual
                words of Ricky's trailing run and the last agent's own last
                turns, which is what resolves a bare "sorry, go again?".

        Returns:
            An `AddressVerdict`, fail-closed on timeout or error.
        """
        started = time.perf_counter()
        try:
            response = await self._client.system_one(
                state=build_address_state(text, context, self._cast, recent=recent),
                questions=self._questions,
                model=self._model,
                retry=_RETRY,
                timeout=_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 — one dead call must not stop the panel
            log.warning("address: typesafe classification failed: %r", exc)
            return _unavailable(latency_ms=(time.perf_counter() - started) * 1000.0)

        latency_ms = (time.perf_counter() - started) * 1000.0
        mode_answer = response.choices.get("mode")
        if mode_answer is None:
            log.warning("address: typesafe response carried no 'mode' answer")
            return _unavailable(latency_ms=latency_ms)

        def noul(name: str) -> float:
            answer = response.nouls.get(name)
            return answer.noul if answer is not None else 0.0

        # `.probabilities`, never `.confidence` — the latter is a distribution
        # spread statistic and reads well below p(top answer) on a four-option
        # Choice, so a confidence floor rejects answers the distribution
        # supports.
        token = compose_address_verdict(
            mode_probabilities=dict(mode_answer.probabilities),
            agent_probabilities={
                agent_id: response.nouls[name].noul
                for agent_id in self._cast.personas
                if (name := _addressed_question_name(agent_id)) in response.nouls
            },
            joint_request=noul(JOINT_REQUEST_QUESTION),
            unidentifiable=noul(UNIDENTIFIABLE_QUESTION),
            cast=self._cast,
        )
        if token is None:
            # An unconvincing vector. Fail closed to the regex rather than
            # guessing which panellist was meant.
            return _unavailable(latency_ms=latency_ms)

        verdict = AddressVerdict(
            verdict=token,
            agents=resolve_address_verdict(token, self._verdicts),
            # TypeSafe returns typed answers, not prose; there is no model
            # justification to carry to the operator console.
            reason="",
            latency_ms=latency_ms,
            source=source,
        )
        if store:
            self._store(key, verdict)
        return verdict
