#!/usr/bin/env python3
"""skelly_jaw — drive the jaw via BLE while audio plays out the JBL (or any speaker).

Usage:
  python skelly_jaw.py "text to speak"     # TTS -> wav, play on speaker, jaw-sync via BLE
  python skelly_jaw.py --wav file.wav      # jaw-sync a local audio file
  python skelly_jaw.py --test              # 3s open/close chattering test, no audio

Jaw model: SET_MOVEMENT (0xCA) with head-movement payload; bit 0x02 = head/jaw per
hardware-verified profile. We pulse open/close at a syllable-ish rate while audio plays.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from skelly import ADDR_DEFAULT, Cmd, Skelly, build, log

# --- tunables ---------------------------------------------------------------
JAW_HZ = 6.0          # chatter rate, open/close cycles per second at "full" speech
OPEN_MS = 70          # open pulse width
CLOSE_MS = 55         # close pulse width
# ----------------------------------------------------------------------------

def make_tts_wav(text: str, out: str) -> float:
    """Synthesize via macOS `say` to a wav; returns duration seconds."""
    subprocess.run(["say", "-o", out, "--data-format=LEF32@22050", text], check=True)
    with wave.open(out, "rb") as w:
        return w.getnframes() / w.getframerate()

def play_wav(path: str) -> subprocess.Popen:
    """Play to default output (JBL when connected). Returns process handle."""
    return subprocess.Popen(["afplay", path])

async def jaw_loop(sk: Skelly, duration: float, stop: asyncio.Event) -> None:
    period = 1.0 / JAW_HZ
    t0 = asyncio.get_event_loop().time()
    while not stop.is_set() and (asyncio.get_event_loop().time() - t0) < duration:
        await sk.cmd(Cmd.SET_MOVEMENT, bytes([0x02, 0x01]))   # jaw/head actuate
        await asyncio.sleep(OPEN_MS / 1000)
        await sk.cmd(Cmd.SET_MOVEMENT, bytes([0x02, 0x00]))
        await asyncio.sleep(max(0.0, period - OPEN_MS / 1000))

async def run(text: str | None, wav: str | None) -> None:
    with tempfile.TemporaryDirectory() as td:
        if wav:
            path, dur = wav, 0.0
            with wave.open(wav, "rb") as w:
                dur = w.getnframes() / w.getframerate()
        else:
            path = str(Path(td) / "tts.wav")
            dur = make_tts_wav(text or "hello", path)

    log(f"audio duration: {dur:.2f}s")
    async with Skelly(ADDR_DEFAULT) as sk:
        ok = await sk.auth()
        log(f"auth: {'OK' if ok else 'FAILED'}")
        if not ok:
            return
        stop = asyncio.Event()
        task = asyncio.create_task(jaw_loop(sk, dur + 0.5, stop))
        proc = play_wav(path)
        await asyncio.to_thread(proc.wait)
        await asyncio.sleep(0.5)
        stop.set()
        await task
        log("done — jaw released")

if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--test":
        asyncio.run(run(None, None)) if False else None
        async def _test():
            async with Skelly(ADDR_DEFAULT) as sk:
                assert await sk.auth()
                for _ in range(9):
                    await sk.cmd(Cmd.SET_MOVEMENT, bytes([0x02, 0x01]))
                    await asyncio.sleep(0.07)
                    await sk.cmd(Cmd.SET_MOVEMENT, bytes([0x02, 0x00]))
                    await asyncio.sleep(0.08)
                log("test done")
        asyncio.run(_test())
    elif args and args[0] == "--wav":
        asyncio.run(run(None, args[1]))
    else:
        asyncio.run(run(" ".join(args), None))
