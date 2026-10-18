"""Skellybot BLE client — JieLi auth + AA/CRC8 command protocol.

Protocol facts cribbed (with credit) from:
- WJDDesigns/supreme-skelly (docs/protocol.md, server/skelly/protocol.py, profiles.py)
- martinecker/SkellyUltraWebController (original community RE)
- ElectronicCats/jieli-ble-badge-research (JieLi raw auth cipher)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time

from bleak import BleakClient

SERVICE = "0000ae00-0000-1000-8000-00805f9b34fb"
WRITE = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY = "0000ae02-0000-1000-8000-00805f9b34fb"

# ── JieLi raw auth (ported from jieli-ble-badge-research qix_ble/auth.py) ────
sys.path.insert(0, str(Path(__file__).parent / "vendor"))
from jli_auth import (  # noqa: E402
    JliCipher,
    STEP_CHALLENGE,
    STEP_ENCRYPTED,
    STEP_PASS_LITERAL,
    get_random_auth_data,
)

# ── AA/CRC8 command protocol (cribbed from supreme-skelly) ───────────────────
MIN_PAYLOAD = 8


def crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


def build(cmd: int, payload: bytes = b"") -> bytes:
    if len(payload) < MIN_PAYLOAD:
        payload = payload + bytes(MIN_PAYLOAD - len(payload))
    frame = bytes([0xAA, cmd]) + payload
    return frame + bytes([crc8(frame)])


class Cmd:
    QUERY_PARAMS = 0xE0
    QUERY_LIVE = 0xE1
    QUERY_VOLUME = 0xE5
    QUERY_BT_NAME = 0xE6
    QUERY_VERSION = 0xEE
    QUERY_FILES = 0xD0
    QUERY_ORDER = 0xD1
    QUERY_CAPACITY = 0xD2
    PLAY_FILE = 0xC6
    DELETE_FILE = 0xC7
    START_TRANSFER = 0xC0
    CHUNK = 0xC1
    END_TRANSFER = 0xC2
    CONFIRM_TRANSFER = 0xC3
    CANCEL_TRANSFER = 0xC4
    RESUME_TRANSFER = 0xC5
    SET_MOVEMENT = 0xCA
    SET_LIGHT_MODE = 0xF2
    SET_BRIGHTNESS = 0xF3
    SET_RGB = 0xF4
    SET_SPEED = 0xF6
    SET_EYE = 0xF9
    SET_VOLUME = 0xFA
    ENABLE_LIVE = 0xFD


# 12ft Skelly profile (verified on hardware by supreme-skelly)
MOVEMENT_HEAD = 0x02

QUERIES = {
    "params": Cmd.QUERY_PARAMS,
    "live": Cmd.QUERY_LIVE,
    "volume": Cmd.QUERY_VOLUME,
    "btname": Cmd.QUERY_BT_NAME,
    "version": Cmd.QUERY_VERSION,
    "capacity": Cmd.QUERY_CAPACITY,
    "files": Cmd.QUERY_FILES,
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Skelly:
    def __init__(self, address: str) -> None:
        self.address = address
        self.client: BleakClient | None = None
        self.rx: asyncio.Queue[bytes] = asyncio.Queue()

    async def __aenter__(self) -> "Skelly":
        self.client = BleakClient(self.address, timeout=30.0)
        await self.client.connect()
        await self.client.start_notify(NOTIFY, lambda _, d: self.rx.put_nowait(bytes(d)))
        await asyncio.sleep(0.3)
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self.client and self.client.is_connected:
                await self.client.stop_notify(NOTIFY)
                await self.client.disconnect()
        except Exception:
            pass

    async def raw(self, data: bytes) -> None:
        assert self.client
        await self.client.write_gatt_char(WRITE, data, response=False)

    async def cmd(self, cmd: int, payload: bytes = b"") -> None:
        await self.raw(build(cmd, payload))
        await asyncio.sleep(0.04)  # spacing per community notes

    async def wait(self, timeout: float = 5.0) -> bytes | None:
        try:
            return await asyncio.wait_for(self.rx.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def auth(self) -> bool:
        cipher = JliCipher()
        r = get_random_auth_data()
        await self.raw(bytes([STEP_CHALLENGE]) + r)
        resp = await self.wait()
        if not resp or resp[0] != STEP_ENCRYPTED or resp[1:] != cipher.encrypt(r):
            return False
        await self.raw(STEP_PASS_LITERAL)
        resp = await self.wait()
        if not resp or resp[0] != STEP_CHALLENGE:
            return False
        await self.raw(bytes([STEP_ENCRYPTED]) + cipher.encrypt(resp[1:]))
        resp = await self.wait(timeout=8.0)
        return resp == STEP_PASS_LITERAL


async def main() -> None:
    addr = "B3C1F919-364D-6A68-2C9A-0286CB3F5507"
    async with Skelly(addr) as sk:
        log(f"connected; auth: {'OK' if await sk.auth() else 'FAILED'}")
        for name, cmd in QUERIES.items():
            await sk.cmd(cmd)
            reply = await sk.wait(timeout=3.0)
            if reply is None:
                log(f"{name:8s} (0x{cmd:02X}): no reply")
            else:
                log(f"{name:8s} (0x{cmd:02X}): {reply.hex(' ')}")
            # drain stragglers
            while not sk.rx.empty():
                extra = sk.rx.get_nowait()
                log(f"  +extra: {extra.hex(' ')}")


if __name__ == "__main__":
    asyncio.run(main())
