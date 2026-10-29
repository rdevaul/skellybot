#!/usr/bin/env python3
"""Enable Live Mode (0xFD 01) then scan CLASSIC Bluetooth via blueutil before/after."""
import asyncio, subprocess, sys, time
sys.path.insert(0, "/Users/rich/Projects/skellybot")
from skelly import Skelly, Cmd, ADDR_DEFAULT

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

def classic_scan():
    out = subprocess.run(["blueutil", "--inquiry", "20"], capture_output=True, text=True, timeout=60)
    return out.stdout + out.stderr

async def main():
    log("baseline classic scan")
    base = classic_scan()
    print(base)

    async with Skelly(ADDR_DEFAULT) as sk:
        ok = await sk.auth()
        log(f"auth: {ok}")
        await sk.cmd(Cmd.ENABLE_LIVE, bytes([0x01]))
        r = await sk.wait(timeout=5.0)
        log(f"0xFD(01): {r.hex(' ') if r else 'NONE'}")

    log("waiting 5s for live-mode advertising...")
    await asyncio.sleep(5)
    post = classic_scan()
    log("post-live classic scan:")
    print(post)

    # leave it ON for manual pairing test; comment out to disable
    # async with Skelly(ADDR_DEFAULT) as sk:
    #     await sk.auth()
    #     await sk.cmd(Cmd.ENABLE_LIVE, bytes([0x00]))

asyncio.run(main())
