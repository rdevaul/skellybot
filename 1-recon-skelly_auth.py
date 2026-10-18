import asyncio, sys, time
sys.path.insert(0, "/tmp/jieli-ble-badge-research/tools/qix-ble")
from bleak import BleakClient
from qix_ble.auth import JliCipher, get_random_auth_data, STEP_CHALLENGE, STEP_ENCRYPTED, STEP_PASS_LITERAL

ADDR = "B3C1F919-364D-6A68-2C9A-0286CB3F5507"
W = "0000ae01-0000-1000-8000-00805f9b34fb"
N = "0000ae02-0000-1000-8000-00805f9b34fb"

def log(s): print(f"[{time.strftime('%H:%M:%S')}] {s}", flush=True)

class Shim:
    def __init__(self, client):
        self.c = client
        self.q = asyncio.Queue()
    def cb(self, _, d): self.q.put_nowait(bytes(d))
    async def send(self, data): await self.c.write_gatt_char(W, data, response=False)
    async def recv(self, timeout=5.0): return await asyncio.wait_for(self.q.get(), timeout=timeout)

async def main():
    cipher = JliCipher()
    async with BleakClient(ADDR, timeout=30.0) as c:
        sh = Shim(c)
        await c.start_notify(N, sh.cb)
        await asyncio.sleep(0.5)

        r = get_random_auth_data()
        log(f"step1 tx: 00 {r.hex()}")
        await sh.send(bytes([STEP_CHALLENGE]) + r)
        resp2 = await sh.recv()
        log(f"step2 rx: {resp2.hex()}")
        exp = cipher.encrypt(r)
        log(f"step2 expected cipher: {exp.hex()} match={resp2[1:]==exp}")

        await sh.send(STEP_PASS_LITERAL)
        log("step3 tx: 02 pass")
        resp4 = await sh.recv()
        log(f"step4 rx: {resp4.hex()}")

        enc = cipher.encrypt(resp4[1:])
        await sh.send(bytes([STEP_ENCRYPTED]) + enc)
        log(f"step5 tx: 01 {enc.hex()}")
        resp6 = await sh.recv(timeout=8.0)
        log(f"step6 rx: {resp6.hex()} -> AUTH {'SUCCESS ✅' if resp6==STEP_PASS_LITERAL else 'FAILED ❌'}")

        await asyncio.sleep(2)
        await c.stop_notify(N)

asyncio.run(main())
