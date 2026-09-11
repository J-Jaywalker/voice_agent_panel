# CLAUDE.md — voice_agent_panel

AI voice panel, 3 agents + human moderator, live on stage. 21 Oct 2026.

## Commands

```bash
uv sync
uv run pytest            # floor + mixer + chunker, <1s
uv run panel-sim          # text-mode, offline stub brains
uv run panel-sim --live   # text-mode, real model
uv run panel              # live pipeline: mic -> STT -> floor -> TTS
uv run panel --no-tts     # same, printed not spoken
uv run barge-in           # interrupt reflex on live mic
uv run ruff check .
```

## Layout

| Path | What |
|---|---|
| `packages/panel_core` | Pure floor logic — `reduce(state, event) -> (state, commands)` |
| `packages/panel_runtime` | STT, TTS, VAD, mixer, chunking, sanitisation |
| `packages/panel_sim` | Text-mode persona harness, no audio |
| `personas/*.yaml` | Cast data. Source of truth for prompts. |

Not built: operator console, video wall, mid-turn steering (`InjectDirective` is a no-op). Phase status: FEASIBILITY.md §8.

## Settled — do not re-litigate

| Decision | Note |
|---|---|
| LiveKit as library, not framework | No `AgentSession`. ADR 0001. |
| STT: Agent STT (Speechmatics preview API), raw `websockets`, one client per mic | Not `speechmatics-voice`/`speechmatics-rt`/LiveKit STT plugin. No local end-of-turn tuning — native `EndOfTurn`. |
| TTS: ElevenLabs, hand-rolled over raw `websockets` | Not a LiveKit plugin — cancellation latency must be our code's property. |
| Model: Claude Opus 5, `effort: "low"` | Chosen for mid-turn `role: "system"` messages surviving prompt cache, not speed. |
| Speechmatics Flow is deprecated | Never propose it. |
| Floor arbitration in `panel_core` | Deterministic, no LLM, <50ms. |
| Video wall is Phase 1 | Not decoration. |
| All persona employers fictional | Do not "fix" back to real ones. |
| Speechmatics product claims belong to Ricky | Agents never make them. Structural, not prompt-level. |
| Floor closed by default | Agents propose; only take floor on invitation. Question invites, statement doesn't. |
| Sentences stream to TTS, never whole turns | Saves ~1.4s/turn (S0.7). |
| Backchannel: duck-first-classify-after | Built in `panel_core` (S0.2). "mm-hm" must not stop an agent. |

Cost is not a constraint — Speechmatics employee, STT/LLM spend is not a valid objection. Argue architecture only.

## Invariants

- `panel_core`: no I/O, no `await`, no clock reads. Timestamps arrive on events.
- Never send raw model output to TTS — always `sanitise()` first.
- Agent speech never enters the STT path — text only, verbatim.
- VAD owns stopping; STT owns understanding. No transcription round-trip in barge-in.
- Personas are data. `prompts.py` renders YAML — never edit a prompt string directly.
- A proposal is not a floor claim. Scoring alone never puts an agent on the PA.
- New floor behaviour needs a test in `packages/panel_core/tests/`.

## Deployment

Runs on a different machine at the venue. Audio devices/rates/channels/buffers are deployment config — never hardcode, never assume a device here exists there, no virtual loopback devices. Re-measure every latency figure on the target rig.

## Working style

- Verify package versions/API shapes/integration claims against source before writing them down.
- Accept corrections, don't re-argue them.
- Don't commit or push unless asked.
