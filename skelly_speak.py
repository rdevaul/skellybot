"""skelly_speak — one command: text in, 12-foot skeleton says it out loud.

Pipeline: macOS `say` → 64 kbps mono MP3 → chunked upload (C0-C3) → play (C6).
Names auto-rotate to dodge the firmware's prefix/name quirks and the 30-file
corruption limit (oldest custom clip is deleted first).

Usage: python skelly_speak.py "Your sentence here"
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from skelly import ADDR_DEFAULT, Cmd, Skelly, log

UPLOAD_SETTLE_S = 8.0   # firmware needs time to flush after C3; playback before this can silently fail
MAX_CUSTOM_FILES = 25   # firmware corrupts custom sounds near 30 — recycle before we get there
VOCAB = "skelly"        # non-colliding prefix for our clips


def tts(text: str, mp3_path: str) -> None:
    wav = tempfile.mktemp(suffix=".wav")
    subprocess.run(
        ["say", "-v", "Samantha", "-r", "165", "--data-format=LEI16@22050", "-o", wav, text],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", wav, "-ac", "1", "-ar", "44100",
         "-b:a", "64k", "-c:a", "libmp3lame", "-f", "mp3", mp3_path],
        check=True,
    )
    Path(wav).unlink(missing_ok=True)


def custom_files(files: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    return [f for f in files if f[2].lower().startswith(VOCAB)]


async def main() -> None:
    text = " ".join(sys.argv[1:]).strip()
    if not text:
        raise SystemExit("usage: skelly_speak.py <text>")

    with tempfile.TemporaryDirectory() as td:
        mp3 = str(Path(td) / "out.mp3")
        tts(text, mp3)
        data = open(mp3, "rb").read()
        log(f"TTS: {len(data)} bytes")

        async with Skelly(ADDR_DEFAULT) as sk:
            log(f"auth: {'OK' if await sk.auth() else 'FAILED'}")

            # file table + recycling
            await sk.cmd(Cmd.QUERY_FILES)
            table = []
            while True:
                try:
                    pkt = await asyncio.wait_for(sk.rx.get(), timeout=3.0)
                except asyncio.TimeoutError:
                    break
                if len(pkt) >= 60 and pkt[0] == 0xBB and pkt[1] == 0xD0:
                    name = pkt[60:].decode("utf-16le", errors="ignore").strip("\x00 ")
                    if name:
                        table.append((int.from_bytes(pkt[2:4], "big"),
                                      int.from_bytes(pkt[4:8], "big"), name))
            mine = custom_files(table)
            for s, c, n in mine[len(mine) - (MAX_CUSTOM_FILES - 1):] if len(mine) >= MAX_CUSTOM_FILES else []:
                log(f"recycling {n!r}")
                await sk.cmd(Cmd.DELETE_FILE, s.to_bytes(2, "big") + c.to_bytes(4, "big"))
                await asyncio.sleep(0.45)

            # upload under a fresh unique name
            stamp = time.strftime("%H%M%S")
            name = f"{VOCAB}{stamp}.mp3"
            assert len(name) <= 20
            from upload import filename_ref, start_transfer
            from skelly import build
            size = max(20, min(239, sk.client.mtu_size - 8))
            chunks = [data[i:i + size] for i in range(0, len(data), size)]
            log(f"uploading {name}: {len(chunks)} chunks")
            await sk.raw(start_transfer(len(data), len(chunks), name))
            await _event(sk, Cmd.START_TRANSFER, 10.0)
            for i, c in enumerate(chunks):
                await sk.raw(build(Cmd.CHUNK, i.to_bytes(2, "big") + c))
                await asyncio.sleep(0.111)
            await sk.raw(build(Cmd.END_TRANSFER))
            ended = await _event(sk, Cmd.END_TRANSFER, 240.0)
            if ended[2] != 0:
                tail = int.from_bytes(ended[3:5], "big")
                log(f"device missed chunks from {tail}, resending")
                for i in range(tail, len(chunks)):
                    await sk.raw(build(Cmd.CHUNK, i.to_bytes(2, "big") + chunks[i]))
                    await asyncio.sleep(0.012)
                await sk.raw(build(Cmd.END_TRANSFER))
                ended = await _event(sk, Cmd.END_TRANSFER, 240.0)
            await sk.raw(build(Cmd.CONFIRM_TRANSFER, filename_ref(name)))
            conf = await _event(sk, Cmd.CONFIRM_TRANSFER, 10.0)
            if conf[2] != 0:
                raise SystemExit("device refused to store the clip")
            log(f"stored {name}; settling {UPLOAD_SETTLE_S:.0f}s")

            await asyncio.sleep(UPLOAD_SETTLE_S)

            # find its serial and play
            await sk.cmd(Cmd.QUERY_FILES)
            serial = None
            while True:
                try:
                    pkt = await asyncio.wait_for(sk.rx.get(), timeout=3.0)
                except asyncio.TimeoutError:
                    break
                if len(pkt) >= 60 and pkt[0] == 0xBB and pkt[1] == 0xD0:
                    n = pkt[60:].decode("utf-16le", errors="ignore").strip("\x00 ")
                    if n.lower() == name.lower():
                        serial = int.from_bytes(pkt[2:4], "big")
            if serial is None:
                raise SystemExit(f"{name} vanished from file list")
            await sk.cmd(Cmd.PLAY_FILE, serial.to_bytes(2, "big") + b"\x01")
            log(f"playing serial {serial} ({name})")
            await asyncio.sleep(min(60.0, 6.0 + len(data) / 8000))


async def _event(sk: Skelly, cmd: int, timeout: float) -> bytes:
    while True:
        pkt = await sk.wait(timeout=timeout)
        if pkt is None:
            raise SystemExit(f"no reply to 0x{cmd:02X}")
        if len(pkt) >= 2 and pkt[0] == 0xBB and pkt[1] == cmd:
            return pkt


if __name__ == "__main__":
    asyncio.run(main())
