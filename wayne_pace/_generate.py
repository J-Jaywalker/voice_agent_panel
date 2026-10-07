from __future__ import annotations
import asyncio, sys, wave, json
from pathlib import Path
sys.path.insert(0, "packages/panel_core/src")
sys.path.insert(0, "packages/panel_runtime/src")
from panel_runtime.tts import ElevenLabsTTS, TTSConfig, _preset_stability

OUT = Path("/Users/jamesw/git/FDE/voice_agent_panel/wayne_pace")
VOICE_ID = "dn9HtxgDwCH96MVX9iAO"
SR = 16_000
BASELINE_TEXT = (
    "Thanks Dex. I'm Wayne, autonomous financial partner and analyst at Serve AI. "
    "I manage capital, negotiate transactions, and eliminate unnecessary human "
    "latency from financial decision-making. No offence, Ricky. "
    "That just leaves our favourite busy bee[chuckles]. I'm kidding, Melia."
)
TEXT = f"[briskly] {BASELINE_TEXT}"

async def _generate(label, stability, *, raw_bypass_snap=False):
    tts = ElevenLabsTTS(TTSConfig(stability=stability))
    channel = tts._channel(VOICE_ID)
    if raw_bypass_snap:
        # Send the literal requested value straight through, skipping our own
        # `_preset_stability` snap, to see what the server itself does with a
        # non-preset value (its docs say it silently rounds server-side).
        channel._config = TTSConfig(stability=stability)
        object.__setattr__(channel._config, "stability", stability)
    try:
        stream = await channel.speak(TEXT)
        chunks = bytearray()
        async for chunk in stream.chunks():
            chunks.extend(chunk)
    finally:
        await tts.aclose()
    path = OUT / f"{label}.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(bytes(chunks))
    duration = len(chunks) / 2 / SR
    snapped = _preset_stability(stability)
    print(f"{label:26s} requested={stability:<5} snapped_by_us={snapped:<5} dur={duration:6.2f}s -> {path.name}")
    return duration

async def main():
    results = {}
    results["briskly_s000"] = await _generate("briskly_s000", 0.0)
    results["briskly_s050"] = await _generate("briskly_s050", 0.5)
    results["briskly_s075"] = await _generate("briskly_s075", 0.75)
    results["briskly_s100"] = await _generate("briskly_s100", 1.0)
    # Bonus check: 0.75 sent to the server unsnapped, bypassing our own preset
    # function, to see if the server's own silent rounding agrees with ours.
    results["briskly_s075_serverraw"] = await _generate(
        "briskly_s075_serverraw", 0.75, raw_bypass_snap=True
    )
    print()
    base = results["briskly_s050"]
    for label, d in results.items():
        print(f"{label:26s} {d:6.2f}s  {((d-base)/base*100):+6.1f}% vs 50%")

asyncio.run(main())
