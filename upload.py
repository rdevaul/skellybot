"""Upload an MP3 to Skelly via the C0-C3 transfer protocol.

Framing, pacing, and event parsing cribbed (with credit) from
WJDDesigns/supreme-skelly (server/skelly/protocol.py, service.py).
Usage: python upload.py <file.mp3> <name>   (name <= 16 chars, .mp3 appended)
"""
from __future__ import annotations

import asyncio
import sys

from skelly import Cmd, Skelly, build, crc8, log

ADDR = "B3C1F919-364D-6A68-2C9A-0286CB3F5507"
CHUNK_GAP_S = 0.111
FILENAME_MARKER = bytes.fromhex("5C55")


def filename_ref(name: str) -> bytes:
    return FILENAME_MARKER + name.encode("utf-16le")


def start_transfer(size: int, nchunks: int, name: str) -> bytes:
    payload = size.to_bytes(4, "big") + nchunks.to_bytes(2, "big")
    return build(Cmd.START_TRANSFER, payload + filename_ref(name))


def chunk(index: int, data: bytes) -> bytes:
    return build(Cmd.CHUNK, index.to_bytes(2, "big") + data)


async def wait_event(sk: Skelly, cmd: int, timeout: float) -> bytes | None:
    while True:
        pkt = await sk.wait(timeout=timeout)
        if pkt is None:
            return None
        if len(pkt) >= 2 and pkt[0] == 0xBB and pkt[1] == cmd:
            return pkt
        log(f"  (unrelated: {pkt.hex(' ')})")


async def main() -> None:
    src, raw_name = sys.argv[1], sys.argv[2]
    name = raw_name if raw_name.lower().endswith(".mp3") else raw_name + ".mp3"
    assert len(name) <= 20, f"device filename too long: {name}"
    mp3 = open(src, "rb").read()

    async with Skelly(ADDR) as sk:
        log(f"auth: {'OK' if await sk.auth() else 'FAILED'}")
        mtu = sk.client.mtu_size
        size = max(20, min(239, mtu - 8))
        chunks = [mp3[i:i + size] for i in range(0, len(mp3), size)]
        log(f"uploading {name}: {len(mp3)} bytes, {len(chunks)} chunks of {size} (mtu {mtu})")

        await sk.raw(start_transfer(len(mp3), len(chunks), name))
        started = await wait_event(sk, Cmd.START_TRANSFER, 10.0)
        if not started or started[2] != 0:
            raise SystemExit(f"transfer not accepted: {started and started.hex(' ')}")
        log("C0 accepted")

        for i, c in enumerate(chunks):
            await sk.raw(chunk(i, c))
            await asyncio.sleep(CHUNK_GAP_S)
            if i % 32 == 0 or i == len(chunks) - 1:
                log(f"  chunk {i + 1}/{len(chunks)}")

        await sk.raw(build(Cmd.END_TRANSFER))
        ended = await wait_event(sk, Cmd.END_TRANSFER, 240.0)
        if not ended:
            raise SystemExit("no C2 reply")
        if ended[2] != 0:
            tail = int.from_bytes(ended[3:5], "big")
            log(f"device missed chunks from {tail}, resending")
            for i in range(tail, len(chunks)):
                await sk.raw(chunk(i, chunks[i]))
                await asyncio.sleep(0.012)
            await sk.raw(build(Cmd.END_TRANSFER))
            ended = await wait_event(sk, Cmd.END_TRANSFER, 240.0)
        log(f"C2 done: {ended.hex(' ')}")

        await sk.raw(build(Cmd.CONFIRM_TRANSFER, filename_ref(name)))
        conf = await wait_event(sk, Cmd.CONFIRM_TRANSFER, 10.0)
        if not conf or conf[2] != 0:
            raise SystemExit(f"confirm failed: {conf and conf.hex(' ')}")
        log(f"C3 confirmed: {name} stored ✅")

        # verify in file list
        await sk.cmd(Cmd.QUERY_FILES)
        reply = await sk.wait(timeout=3.0)
        if reply:
            names = reply.decode("utf-16le", errors="ignore")
            log(f"file list mentions {name}: {name.lower() in names.lower()}")


if __name__ == "__main__":
    asyncio.run(main())
