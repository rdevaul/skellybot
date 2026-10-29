#!/usr/bin/env python3
"""Stream live audio to the existing AE01 endpoint after ENABLE_LIVE (0xFD 01).
Tries two formats: raw 16k mono PCM and MP3, chunked ~20ms. Logs all notify traffic.
Rich: listen for audio from the skull during the run."""
import asyncio, sys, time
sys.path.insert(0, "/Users/rich/Projects/skellybot")
from skelly import Skelly, Cmd, ADDR_DEFAULT

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

async def stream(sk, data, chunk, pacing, label):
    log(f"streaming {label}: {len(data)} bytes, chunk={chunk}, {pacing*1000:.0f}ms pacing")
    t0 = time.time()
    for i in range(0, len(data), chunk):
        await sk.raw(data[i:i+chunk])
        await asyncio.sleep(pacing)
        if sk.rx.qsize():
            while not sk.rx.empty():
                log(f"  notify: {sk.rx.get_nowait().hex(' ')}")
        if i > 0 and (i // chunk) % 50 == 0:
            log(f"  ...{i}/{len(data)} bytes, {time.time()-t0:.1f}s")
    log(f"stream done {label} in {time.time()-t0:.1f}s")

async def main():
    wav = open("/Users/rich/.sybilclaw/workspace/deep_male_fresh.wav", "rb").read()
    # strip 44-byte wav header -> raw PCM s16le 16k mono; take 6s
    pcm = wav[44:44 + 16000*2*6]
    import subprocess
    mp3 = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "/Users/rich/.sybilclaw/workspace/deep_male_fresh.wav", "-t", "6", "-b:a", "96k", "-f", "mp3", "pipe:1"], capture_output=True).stdout
    log(f"pcm {len(pcm)}B, mp3 {len(mp3)}B")

    async with Skelly(ADDR_DEFAULT) as sk:
        log(f"auth: {'OK' if await sk.auth() else 'FAILED'}")
        await sk.cmd(Cmd.ENABLE_LIVE, bytes([0x01]))
        r = await sk.wait(timeout=5.0)
        log(f"0xFD reply: {r.hex(' ') if r else 'NONE'}")
        await asyncio.sleep(1.0)

        try:
            await stream(sk, pcm, 200, 0.02, "PCM-200B")
        except Exception as e:
            log(f"PCM stream error: {e!r}")
        await asyncio.sleep(2)
        # drain
        while not sk.rx.empty(): log(f"  drain: {sk.rx.get_nowait().hex(' ')}")

        try:
            await stream(sk, mp3, 160, 0.02, "MP3-160B")
        except Exception as e:
            log(f"MP3 stream error: {e!r}")
        await asyncio.sleep(2)
        while not sk.rx.empty(): log(f"  drain: {sk.rx.get_nowait().hex(' ')}")

        # disable live
        await sk.cmd(Cmd.ENABLE_LIVE, bytes([0x00]))
        r = await sk.wait(timeout=5.0)
        log(f"0xFD(00) reply: {r.hex(' ') if r else 'NONE'}")
    log("done — did the skull speak?")

asyncio.run(main())
