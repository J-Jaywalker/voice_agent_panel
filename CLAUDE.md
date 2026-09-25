# CLAUDE.md — voice_agent_panel

AI voice panel, 3 agents + human moderator, live on stage. 21 Oct 2026.

## Commands

```bash
uv sync
uv run pytest                     # everything — 287 tests, ~3min
uv run pytest packages/panel_core # floor logic only — 226 tests, ~2s. The tight loop.
uv run panel-sim          # text-mode, offline stub brains
uv run panel-sim --live   # text-mode, real model
uv run panel              # live pipeline: mic -> STT -> floor -> TTS
uv run panel --no-tts     # same, printed not spoken
uv run panel --llm-address # resolve the addressee with Haiku, not the regex
uv run panel --display    # + the 12m video wall, on http://localhost:8765
uv run barge-in           # interrupt reflex on live mic
uv run panel-display --demo # video wall alone, synthetic panel, no mic or keys
uv run ruff check .
```

## Layout

| Path | What |
|---|---|
| `packages/panel_core` | Pure floor logic — `reduce(state, event) -> (state, commands)` |
| `packages/panel_runtime` | STT, TTS, VAD, mixer, chunking, sanitisation |
| `packages/panel_sim` | Text-mode persona harness, no audio |
| `packages/panel_display` | The video wall. `wall.py` is pure, `server.py` is aiohttp, `static/` has no build step. |
| `personas/*.yaml` | Cast data. Source of truth for prompts. |

Video wall built 25 Sept: 12.00m x 4.50m, 8:3, four 3m lanes — transcript, then one per agent. Laid out at a fixed 3840x1440 (320px = 1m) and scaled to fit, so the stylesheet's dimensions stay physical whatever the LED processor reports. Orbs are driven by real post-gain audio off `Mixer.take_levels()`, never by a timer. The transcript lane is still a **stub**: moderator only. The event it was waiting for now exists — `AgentUtteranceProgress`, one per sentence off the `speak()` loop — so wiring agent turns into the lane is a display job, not a plumbing one.

Not built: operator console. Mid-turn steering was cut (11 Sept) — turn length is a prompt instruction, no orchestrator-enforced ceiling; moderator handles the rest live. Phase status: FEASIBILITY.md §8.

Agents already pass turns to each other without Ricky (`_maybe_rearbitrate`), bounded by `open_invitation_turns` / `max_consecutive_agent_turns`. Proposal rounds now open *during* an agent's turn, off `AgentUtteranceProgress` at `agent_turn_speculation_interval_s` (2.5s), so the next speaker has a line written against most of what they just heard and the handover costs no generation. Before 25 Sept every handover paid one cold round trip — measured at 2.43s — because nothing was ever asked between `AgentSpeechStarted` and `AgentSpeechEnded`. Wanted, not built: exchanges that end when the agents are done rather than when a counter expires — needs a termination signal on `Signals`. FEASIBILITY.md §3.8.

## Settled — do not re-litigate

| Decision | Note |
|---|---|
| LiveKit as library, not framework | No `AgentSession`. ADR 0001. In practice this is now **Silero VAD only** — `silero.VAD`, `rtc.AudioFrame`, `lkvad.VADEventType`, nothing else. Audio I/O is `sounddevice`, the mixer is ours. Re-litigating this means rewriting the audio path, not changing a config. |
| STT: Agent STT (Speechmatics preview API), raw `websockets`, one client per mic | Not `speechmatics-voice`/`speechmatics-rt`/LiveKit STT plugin. No local end-of-turn tuning — native `EndOfTurn`. |
| TTS: ElevenLabs, hand-rolled over raw `websockets` | Not a LiveKit plugin — cancellation latency must be our code's property. |
| Model: Claude Sonnet 5, `effort: "low"` | Chosen for speed. (Opus 5 was used briefly for mid-turn `role: "system"` support; reverted when mid-turn steering was cut.) |
| Speechmatics Flow is deprecated | Never propose it. |
| Scoring an *open* floor stays in `panel_core` | Deterministic, no LLM, <50ms. `scoring.py`. This is who wins an open floor — not whether it opens. |
| Who Ricky invited: Haiku classifier | `claude-haiku-4-5` in `panel_runtime.address`, behind `--llm-address` (off by default). Verdict arrives as an `AddressDetected` event, so `panel_core` stays pure and logs still replay. Fails closed to the regex. ADR 0002, FEASIBILITY §3.7. |
| Agents never interrupt agents | Removed 21 Sept, not disabled — the config, scoring, `StopReason.AGENT_INTERRUPT` and both CLI flags are gone. Agents pass turns. Don't reintroduce it. FEASIBILITY §3.6. |
| Controlled overlap: gone, and never existed | `StopSpeech.overlap_ms` was set and asserted but the runtime never read it. Removed with the above. `StopSpeech` is always an immediate stop. |
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
