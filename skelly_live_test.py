#!/usr/bin/env python3
"""ENABLE_LIVE (0xFD) test: baseline BLE scan -> auth + send 0xFD variants -> re-scan, diff names."""
import asyncio, sys, time
sys.path.insert(0, "/Users/rich/Projects/skellybot")
from skelly import Skelly, Cmd, ADDR_DEFAULT

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

async def scan(seconds, label):
    from bleak import BleakScanner
    log(f"scan start: {label} ({seconds}s)")
    devs = await BleakScanner.discover(timeout=seconds, return_adv=True)
    out = {}
    for a, (d, adv) in devs.items():
        name = d.name or adv.local_name or ""
        rssi = adv.rssi
        if name:
            out[a] = (name, rssi)
        if "skell" in name.lower() or "live" in name.lower():
            log(f"  HIT {a} name={name!r} rssi={rssi}")
    log(f"scan done: {label}, {len(out)} named devices")
    return out

async def main():
    base = await scan(10, "baseline")
    log(f"baseline names: {[v[0] for v in base.values()]}")

    variants = [
        ("bare", b""),
        ("on-01", bytes([0x01])),
        ("on-1-01", bytes([0x01, 0x01])),
    ]
    for label, payload in variants:
        log(f"--- connect + send ENABLE_LIVE ({label}) ---")
        try:
            async with Skelly(ADDR_DEFAULT) as sk:
                ok = await sk.auth()
                log(f"auth: {ok}")
                if not ok:
                    continue
                await sk.cmd(Cmd.ENABLE_LIVE, payload)
                reply = await sk.wait(timeout=5.0)
                log(f"0xFD reply: {reply.hex(' ') if reply else 'NONE'}")
                await asyncio.sleep(3)
        except Exception as e:
            log(f"connect/tx error: {e!r}")
        await asyncio.sleep(2)
        post = await scan(12, f"post-{label}")
        new = {a: v for a, v in post.items() if a not in base}
        changed = {a: (base.get(a), v) for a, v in post.items() if a in base and base[a] != v}
        log(f"NEW devices after {label}: {new}")
        log(f"CHANGED after {label}: {changed}")

asyncio.run(main())
