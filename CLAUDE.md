# CLAUDE.md — voice_agent_panel

AI voice panel for Boost Camp Oslo, **21 Oct 2026**. Three AI agents plus a human
moderator (Ricky) on a live stage in front of ~400 prospects and customers.

**Read before proposing architecture:** `FEASIBILITY.md` (risks, phases, AV
requirements) · `docs/adr/` (settled decisions and the reasoning behind them).

## Commands

```bash
uv sync
uv run pytest            # floor + mixer + chunker acceptance criteria, <1s
uv run panel-sim         # text-mode panel, offline stub brains
uv run panel-sim --live  # real model behind the personas
uv run panel             # the live pipeline: mic -> STT -> floor -> TTS
uv run panel --no-tts    # same, printed rather than spoken
uv run barge-in          # interrupt reflex on a live mic
uv run ruff check .
```

## Layout

| Path | What |
|---|---|
| `packages/panel_core` | **Pure** floor logic — `reduce(state, event) -> (state, commands)` |
| `packages/panel_sim` | Text-mode harness for tuning personas without audio |
| `personas/*.yaml` | The cast, as structured data. Source of truth for prompts. |

**Not built yet:** operator console, video wall, mid-turn steering (`InjectDirective` is a no-op).

## Settled — do not re-litigate

| Decision | Note |
|---|---|
| LiveKit as a **library**, not a framework | No `AgentSession` for the agent loop. See ADR 0001. |
| STT is **`speechmatics-rt`** direct, one client per human mic | Not `speechmatics-voice`. Not the LiveKit STT plugin. |
| Speechmatics **Flow is deprecated** | Confirmed by the user, 3 Sept 2026. Never propose it. |
| Floor arbitration lives in `panel_core` | Deterministic. **No LLM in the floor path** — it must resolve in <50ms. |
| Video wall is **Phase 1**, not Phase 3 | Comprehension infrastructure, not decoration. |
| All persona employers are **fictional** | Melia's was deliberately changed off a real NGO. Do not "fix" it back. |
| Speechmatics product claims belong to **Ricky**, not the agents | Agents have no basis to say anything about Speechmatics. Structural, not prompt-level. |
| **The floor is closed by default** | Agents propose constantly but may only *take* the floor on an invitation. A question invites; a statement invites nobody. Confirmed by the user, 3 Sept 2026. |
| **Sentences stream to TTS, never whole turns** | Generation is ~40 tok/s, so waiting for the last token costs 4-6s of dead air. Measured: streaming saves ~1.4s per turn. See spike S0.7. |

**Cost is not a constraint** — the user works at Speechmatics; STT/LLM spend is
not a valid argument. Argue from architecture.

## Invariants

- **`panel_core` stays pure.** No I/O, no `await`, no clock reads — timestamps
  arrive on events. This is what makes rehearsals replayable and keeps the suite
  under a second. I/O belongs in a runtime adapter.
- **Never send raw model output to TTS.** Always `sanitise()` first. A leaked
  `<thinking>` tag read aloud over a PA is the worst-case failure mode.
- **Agent speech never enters the STT path.** That is the feedback loop that ends
  the show. Agent turns enter conversation state as text — we generated them, so
  we already know them verbatim.
- **VAD owns stopping; STT owns understanding.** Never put a transcription
  round-trip in the barge-in path.
- **Personas are data.** `prompts.py` renders the YAML. Editing a persona must
  never mean editing a prompt string.
- **Wanting the floor is not taking it.** A proposal is a raised hand. It goes
  to the operator console and video wall as `HandsRaised`; Ricky decides. Never
  let a scoring result alone put an agent on the PA.
- New floor behaviour lands with a test in `packages/panel_core/tests/`. Those
  tests are the scoping doc's acceptance criteria — keep them readable as such.

## Deployment

**The panel runs on a different machine at the venue, not this one.** Audio
devices, sample rates, channel counts and buffer sizes are deployment
configuration — never hardcode them, never assume a device present here exists
there (in particular, do not build on virtual loopback devices). Every latency
figure must be re-measured on the target rig; desk measurements are budgets, not
results.

## Known gaps to plan around

- **Backchannel discrimination is ours to build.** Going Tier B means we forgo
  LiveKit's `min_duration` / `backchannel_boundary` tuning. Ricky saying "mm-hm"
  must not stop an agent. Phase 0 deliverable, not a Phase 2 discovery.
- End-of-turn is silence-threshold tuning on `speechmatics-rt` — no SMART_TURN,
  since that ships with the wrapper we rejected.

## Working style

- **Verify, don't assert.** Package versions, API shapes and integration claims
  get checked against PyPI/docs before they land in a document. Reasoning from
  memory produced a wrong recommendation on LiveKit once already.
- Corrections from the user are accepted and moved on from, not re-argued.
- Don't commit or push unless asked.
