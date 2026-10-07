"""One-time generator for `feedback_test.py`'s committed clips.

    uv run generate-feedback-assets

Synthesises every persona's `introduction` in their real ElevenLabs voice and
writes it to `assets/feedback/<persona_id>.wav`, committed to the repo so
`uv run feedback-test` needs no network and no `ELEVENLABS_API_KEY` on a
fresh checkout. Run this by hand only when a persona's voice or introduction
changes — not part of any regular workflow, and not run by `uv run panel`.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

from panel_core import PanelCast, sanitise

from .config import PIPELINE_SAMPLE_RATE
from .feedback_test import ASSETS_DIR
from .tts import ElevenLabsTTS, TTSConfig

PERSONA_DIR = Path("personas")


async def _generate_one(tts: ElevenLabsTTS, persona) -> None:
    path = ASSETS_DIR / f"{persona.id}.wav"
    print(f"synthesising {persona.name} ({persona.voice_id})…")
    stream = await tts.synthesise(sanitise(persona.introduction), voice_id=persona.voice_id)
    chunks = bytearray()
    async for chunk in stream.chunks():
        chunks.extend(chunk)

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(PIPELINE_SAMPLE_RATE)
        w.writeframes(bytes(chunks))
    print(f"  {len(chunks) / 2 / PIPELINE_SAMPLE_RATE:.1f}s -> {path}")


async def _run() -> None:
    cast = PanelCast.from_dir(PERSONA_DIR)
    # Same accent_tags/pace_tags construction as `PanelRuntime.__init__` —
    # without it, a persona with a standing `accent` or `pace`
    # (`panel_core.personas.ACCENT_TAGS`, `PACE_TAGS`) ships a feedback clip
    # that doesn't carry the tags the real show prepends to every push, so the
    # committed asset is quieter on this than the mic will ever hear on stage.
    accent_tags = {p.voice_id: p.accent for p in cast.personas.values() if p.accent}
    pace_tags = {p.voice_id: p.pace for p in cast.personas.values() if p.pace}
    tts = ElevenLabsTTS(TTSConfig(), accent_tags=accent_tags, pace_tags=pace_tags)
    try:
        for persona in cast.personas.values():
            await _generate_one(tts, persona)
    finally:
        await tts.aclose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
