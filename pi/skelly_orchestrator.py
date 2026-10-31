#!/usr/bin/env python3
"""skelly_orchestrator — Raspberry Pi runtime for the 12ft Skelly Halloween prop.

Combines BLE GATT control (JieLi auth + AA/CRC8 command protocol) with
classic Bluetooth A2DP pairing and audio streaming via PipeWire.

Usage:
  # Full porch session (live A2DP mode):
  python3 skelly_orchestrator.py --mode live /path/to/audio.wav

  # Upload mode — send audio to skull speaker, no A2DP needed:
  python3 skelly_orchestrator.py --mode upload /path/to/audio.wav

  # Hybrid mode — try live A2DP first; auto-fallback to upload on failure:
  python3 skelly_orchestrator.py --mode hybrid /path/to/audio.wav

  # Test head motion only (no audio, no A2DP):
  python3 skelly_orchestrator.py --test-head 5

  # Query device storage capacity:
  python3 skelly_orchestrator.py --capacity

  # Specify classic BT address (normally auto-discovered):
  python3 skelly_orchestrator.py --classic-addr D3:A6:5D:78:BD:19 audio.wav

Dependencies: bleak, bluez, pipewire, pipewire-pulse, dbus-next (for BT agent)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

# ── BLE UUIDs (JieLi AE00 service) ──────────────────────────────────────────
SERVICE_UUID = "0000ae00-0000-1000-8000-00805f9b34fb"
WRITE_UUID   = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID  = "0000ae02-0000-1000-8000-00805f9b34fb"

# ── Protocol constants (verified from existing skellybot code) ───────────────
AA_HEADER    = 0xAA
ACK_PREFIX   = 0xBB
MIN_PAYLOAD  = 8

# Core opcodes (from skelly.py Cmd class)
ENABLE_LIVE       = 0xFD
SET_MOVEMENT      = 0xCA
MOVEMENT_HEAD     = 0x02
QUERY_PARAMS      = 0xE0
QUERY_CAPACITY    = 0xD2
QUERY_FILES       = 0xD0
PLAY_FILE         = 0xC6
DELETE_FILE       = 0xC7
START_TRANSFER    = 0xC0
CHUNK             = 0xC1
END_TRANSFER      = 0xC2
CONFIRM_TRANSFER  = 0xC3
SET_VOLUME        = 0xFA

# NEW opcodes (TODO-verify: inferred from upstream sequence, need on-hardware validation)
SELECT_CLASSIC_SOURCE = 0xFE  # Select classic BT audio source after A2DP connect
ENTER_LIVE_ANIM       = 0xFC  # Enter live animation mode (head motion driven by audio)

# Classic BT pairing
CLASSIC_LIVE_NAME = "12ft Skelly(Live)"
CLASSIC_DEFAULT_ADDR = "D3:A6:5D:78:BD:19"  # known stable A2DP address
A2DP_PIN = "1234"

# BLE scan — always by name, never hardcode RPA
SKELLY_BLE_NAME = "12ft Skelly"

# Upload constants
UPLOAD_SETTLE_S = 8.0   # firmware needs time to flush after C3
VOCAB = "skelly"        # non-colliding prefix for uploaded clips
MAX_CUSTOM_FILES = 25   # recycle before firmware corrupts near 30

# Hybrid mode: max classic BT failure attempts before upload fallback
HYBRID_MAX_LIVE_FAILS = 2


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ═══════════════════════════════════════════════════════════════════════════
# CRC8 + Framing (port of skelly.py — polynomial 0x1D / reverse 0x8C)
# ═══════════════════════════════════════════════════════════════════════════

def crc8(data: bytes) -> int:
    """CRC-8, poly 0x1D (reversed 0x8C), init 0."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


def build_packet(cmd: int, payload: bytes = b"") -> bytes:
    """Build an AA-framed command packet with CRC8 trailer.

    Frame: AA <cmd> <payload padded to 8 bytes> <crc8>
    """
    if len(payload) < MIN_PAYLOAD:
        payload = payload + bytes(MIN_PAYLOAD - len(payload))
    frame = bytes([AA_HEADER, cmd]) + payload
    return frame + bytes([crc8(frame)])


# ═══════════════════════════════════════════════════════════════════════════
# JieLi BLE Auth
# ═══════════════════════════════════════════════════════════════════════════

_vendor = str(Path(__file__).parent / "vendor")
sys.path.insert(0, _vendor)
from jli_auth import (          # noqa: E402
    JliCipher,
    STEP_CHALLENGE,
    STEP_ENCRYPTED,
    STEP_PASS_LITERAL,
    get_random_auth_data,
)

_mac_skelly = "/Users/rich/Projects/skellybot"
if os.path.isdir(_mac_skelly):
    sys.path.insert(0, _mac_skelly)
    try:
        from skelly import (    # noqa: E402,F811
            STEP_CHALLENGE as _MC,
            STEP_ENCRYPTED as _ME,
            STEP_PASS_LITERAL as _MP,
        )
        assert STEP_CHALLENGE == _MC, "STEP_CHALLENGE mismatch"
        assert STEP_ENCRYPTED == _ME, "STEP_ENCRYPTED mismatch"
        assert STEP_PASS_LITERAL == _MP, "STEP_PASS_LITERAL mismatch"
    except (ImportError, AssertionError):
        pass  # fine on Pi where Mac path doesn't exist


async def jli_auth(client: BleakClient, rx_queue: asyncio.Queue) -> bool:
    """3-step (6-exchange) JieLi BLE authentication over raw AE01/AE02."""
    cipher = JliCipher()

    our_random = get_random_auth_data()
    await client.write_gatt_char(WRITE_UUID, bytes([STEP_CHALLENGE]) + our_random, response=False)

    resp = await asyncio.wait_for(rx_queue.get(), timeout=8.0)
    if resp[0] != STEP_ENCRYPTED or len(resp) < 17:
        log(f"auth step2: bad marker/len: {resp.hex()}")
        return False
    if resp[1:17] != cipher.encrypt(our_random):
        log("auth step2: cipher mismatch")
        return False

    await client.write_gatt_char(WRITE_UUID, STEP_PASS_LITERAL, response=False)

    resp = await asyncio.wait_for(rx_queue.get(), timeout=8.0)
    if resp[0] != STEP_CHALLENGE or len(resp) < 17:
        log(f"auth step4: bad marker/len: {resp.hex()}")
        return False
    encrypted = cipher.encrypt(resp[1:17])
    await client.write_gatt_char(WRITE_UUID, bytes([STEP_ENCRYPTED]) + encrypted, response=False)

    resp = await asyncio.wait_for(rx_queue.get(), timeout=8.0)
    success = resp == STEP_PASS_LITERAL
    log(f"auth: {'SUCCESS ✅' if success else 'FAILED ❌'} (step6={resp.hex()})")
    return success


async def scan_for_skelly(timeout: float = 10.0) -> str | None:
    """Scan for the Skelly BLE device by name (RPA rotates; never hardcode)."""
    log(f"scanning for BLE device '{SKELLY_BLE_NAME}' ({timeout}s)...")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for addr, (dev, adv) in devices.items():
        name = dev.name or adv.local_name or ""
        if "skell" in name.lower():
            log(f"  found: {addr} name={name!r} rssi={adv.rssi}")
            return addr
    log("  no Skelly device found!")
    return None


async def ble_connect_auth():
    """Scan + connect + auth. Returns (client, rx_queue) or (None, None).

    Pi BlueZ workaround: hold the discovered BLEDevice object and connect
    while discovery is still active; the BlueZ device object is removed
    the moment discovery stops, so scanning-then-connecting-by-address fails.
    """
    held = []
    def _cb(dev, adv):
        name = dev.name or adv.local_name or ""
        if "skell" in name.lower():
            held.append(dev)
    scanner = BleakScanner(detection_callback=_cb)
    await scanner.start()
    for _ in range(15):
        await asyncio.sleep(1)
        if len(held) >= 3:
            break
    if not held:
        log("FATAL: Skelly BLE device not found. Check power and proximity.")
        await scanner.stop()
        return None, None
    ble_addr = held[-1].address
    log(f"connecting to BLE {ble_addr} (discovery held open)...")
    try:
        client = BleakClient(held[-1], timeout=30.0)
        await client.connect()
    except Exception as e:
        log(f"BLE connect FAILED: {e}")
        log("ADVICE: power-cycle the Skelly and try again. Do NOT retry — stacked retries cause chirping.")
        await scanner.stop()
        return None, None
    log("BLE connected ✅")

    rx_queue: asyncio.Queue = asyncio.Queue()
    await client.start_notify(NOTIFY_UUID, lambda _, d: rx_queue.put_nowait(bytes(d)))
    await asyncio.sleep(0.3)
    try:
        ok = await jli_auth(client, rx_queue)
    except asyncio.TimeoutError:
        log("auth timed out")
        ok = False
    if not ok:
        log("FATAL: JieLi auth failed")
        await ble_close(client)
        return None, None
    drain(rx_queue)
    return client, rx_queue


async def ble_close(client: BleakClient):
    try:
        await client.stop_notify(NOTIFY_UUID)
    except Exception:
        pass
    try:
        await client.disconnect()
        log("BLE disconnected ✅")
    except Exception:
        pass


def drain(q: asyncio.Queue) -> list[bytes]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Classic BT pairing via bluetoothctl + D-Bus agent
# ═══════════════════════════════════════════════════════════════════════════

def _bt_mac(addr: str) -> str:
    return addr.replace("-", ":").upper()


async def run_btctl_batch(cmds: list[str], timeout: float = 10.0) -> str:
    script = "\n".join(cmds) + "\nquit\n"
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(script.encode()), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    return out.decode(errors="replace")


async def _btctl(*args: str, timeout: float = 15.0) -> str:
    """Run one non-interactive bluetoothctl command; return combined output."""
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return "(timeout)"
    return out.decode(errors="replace")


async def classic_info(bt_mac: str) -> dict:
    out = await _btctl("info", bt_mac, timeout=5.0)
    return {
        "known": "not available" not in out and "Device " in out,
        "paired": "Paired: yes" in out,
        "trusted": "Trusted: yes" in out,
        "connected": "Connected: yes" in out,
    }


async def connect_classic_bt(classic_addr: str, keepalive_fn, repair: bool = False) -> bool:
    """Bring up the classic A2DP link to the skull.

    Mirrors OpenSkelly connect_live(): the bond is made ONCE and reused;
    each session is just FD 01 → `bluetoothctl connect` (≤4 tries, FD 01
    re-sent between tries). We only pair if BlueZ has no bond (or --repair).
    NEVER `remove` the device by default — that destroys the bond.
    """
    bt_mac = _bt_mac(classic_addr)
    info = await classic_info(bt_mac)
    log(f"classic {bt_mac}: {info}")
    if info["connected"]:
        log("classic already connected ✅")
        return True
    if repair or not info["paired"]:
        log("no usable bond → pairing flow" + (" (--repair)" if repair else ""))
        if not await pair_classic_bt(classic_addr, keepalive_fn, remove_first=repair):
            return False
        info = await classic_info(bt_mac)
        if info["connected"]:
            return True
    if not info["trusted"]:
        await _btctl("trust", bt_mac, timeout=5.0)

    last = ""
    for attempt in range(1, 5):
        log(f"bluetoothctl connect {bt_mac} (try {attempt}/4)")
        last = (await _btctl("connect", bt_mac, timeout=15.0)).strip()
        log(f"  {last.splitlines()[-1] if last else '(no output)'}")
        if (await classic_info(bt_mac))["connected"]:
            log("classic A2DP link UP ✅")
            return True
        await asyncio.sleep(1.0)
        await keepalive_fn()
    log(f"classic connect FAILED: {last[-200:]}")
    return False


async def pair_classic_bt(classic_addr: str, keepalive_fn, remove_first: bool = False) -> bool:
    """One-time legacy-PIN pairing (agent answers PIN). Only used when no bond exists.

    Returns True only if pairing succeeded or `info` reports Connected: yes.
    keepalive_fn re-sends FD 01 to hold the classic advertising window open.
    """
    bt_mac = _bt_mac(classic_addr)
    agent_script = str(Path(__file__).parent / "skelly_bt_agent.py")

    log(f"starting BT agent (PIN={A2DP_PIN})...")
    agent_proc = await asyncio.create_subprocess_exec(
        sys.executable, agent_script, A2DP_PIN,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    await asyncio.sleep(1.5)

    proc = None
    try:
        if remove_first:
            log(f"--repair: removing old pairing for {bt_mac}...")
            out = await run_btctl_batch([f"remove {bt_mac}"], timeout=5.0)
            log(f"  {out.strip()[:120]}")
            await asyncio.sleep(0.5)
        await run_btctl_batch(["power on"], timeout=5.0)
        log("  adapter ready")

        log("starting classic BT discovery...")
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdin and proc.stdout
        proc.stdin.write(b"power on\nscan on\n")
        await proc.stdin.drain()

        mac_norm = bt_mac.replace(":", "").upper()
        found = False
        scan_start = time.time()
        ka_counter = 0
        while time.time() - scan_start < 20:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
                line = line.decode(errors="replace").strip()
                if not line:
                    continue
                log(f"bt: {line}")
                if mac_norm in line.replace(":", "").upper():
                    log("device detected in discovery!")
                    found = True
                ka_counter += 1
                if ka_counter % 4 == 0:
                    await keepalive_fn()
                if found:
                    break
            except asyncio.TimeoutError:
                await keepalive_fn()

        proc.stdin.write(b"scan off\n")
        await proc.stdin.drain()
        await asyncio.sleep(0.5)
        if not found:
            log("device not seen in scan — attempting direct pair anyway")

        log(f"pairing with {bt_mac} (agent provides PIN={A2DP_PIN})...")
        proc.stdin.write(f"pair {bt_mac}\n".encode())
        await proc.stdin.drain()

        pair_start = time.time()
        paired = False
        last_ka = time.time()
        while time.time() - pair_start < 25:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=3.0)
                line = line.decode(errors="replace").strip()
                if line:
                    log(f"bt: {line}")
                if "Pairing successful" in line or "AlreadyExists" in line:
                    paired = True
                    break
                if "failed" in line.lower() and "pair" in line.lower():
                    log("pairing failed")
                    break
            except asyncio.TimeoutError:
                pass
            if time.time() - last_ka > 3.5:
                await keepalive_fn()
                last_ka = time.time()

        proc.stdin.write(f"trust {bt_mac}\nquit\n".encode())
        await proc.stdin.drain()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
        log("pairing successful ✅" if paired else "WARNING: pairing outcome unclear")
        return paired or (await classic_info(bt_mac))["paired"]

    except Exception as e:
        log(f"classic BT pairing error: {e}")
        return False
    finally:
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            agent_proc.terminate()
            await asyncio.wait_for(agent_proc.wait(), timeout=3.0)
        except Exception:
            try:
                agent_proc.kill()
            except ProcessLookupError:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# Audio streaming via PipeWire / PulseAudio (pacat preferred)
# ═══════════════════════════════════════════════════════════════════════════

def find_pipewire_bt_sink(mac: str | None = None) -> str | None:
    """Find the Bluetooth A2DP sink.

    PipeWire names it bluez_output.<MAC>.1; PulseAudio (Bullseye Pi) names it
    bluez_sink.<MAC>.a2dp_sink. Accept both; prefer one matching `mac`.
    """
    want = mac.replace(":", "_").upper() if mac else None
    try:
        result = subprocess.run(["pactl", "list", "short", "sinks"],
                                capture_output=True, text=True, timeout=5)
        cands = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1].lower().startswith(("bluez_output", "bluez_sink")):
                cands.append(parts[1])
        for c in cands:
            if want and want in c.upper():
                return c
        return cands[0] if cands else None
    except Exception as e:
        log(f"pactl error: {e}")
    return None


def wait_for_bt_sink(mac: str, timeout: float = 12.0) -> str | None:
    """Poll for the A2DP sink; on PulseAudio, nudge the card into a2dp_sink."""
    end = time.time() + timeout
    card = f"bluez_card.{mac.replace(':', '_').upper()}"
    nudged = False
    while time.time() < end:
        sink = find_pipewire_bt_sink(mac)
        if sink:
            return sink
        if not nudged and time.time() > end - timeout / 2:
            subprocess.run(["pactl", "set-card-profile", card, "a2dp_sink"],
                           capture_output=True, timeout=5)
            nudged = True
        time.sleep(0.5)
    return None


def log_sink_rate(sink: str | None) -> None:
    """Warn loudly if the BT sink is not 44.1 kHz (48k = silent skull)."""
    try:
        out = subprocess.run(["pactl", "list", "sinks"], capture_output=True,
                             text=True, timeout=5).stdout
        cur = None
        for line in out.splitlines():
            if line.strip().startswith("Name:"):
                cur = line.split("Name:")[1].strip()
            if cur == sink and "Sample Specification" in line:
                spec = line.split(":", 1)[1].strip()
                log(f"BT sink sample spec: {spec}")
                if "44100" not in spec:
                    log("WARNING: BT sink is NOT 44100 Hz — Skelly is silent at 48k! "
                        "Check WirePlumber override from setup.sh (README: REQUIRED).")
                return
    except Exception:
        pass


def stream_audio_pipewire(audio_path: str, timeout: float = 120.0, sink: str | None = None) -> bool:
    """Stream audio to the BT sink. Order: pacat (via ffmpeg decode) → pw-play → paplay.

    Refuses to play if no BT sink exists (otherwise audio silently goes to the
    Pi's analog jack and the run looks like a success).
    """
    sink = sink or find_pipewire_bt_sink()
    if not sink:
        log("FAILED: no Bluetooth sink — refusing to play to the analog jack")
        return False
    log(f"using BT sink: {sink}")
    log_sink_rate(sink)

    # (e) pacat: decode to raw s16le 44.1k stereo and pipe in
    try:
        log("streaming via pacat (ffmpeg → s16le 44100 stereo)")
        ff = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-i", audio_path,
             "-af", "loudnorm=I=-10:TP=-1:LRA=11,apad=pad_dur=0.75",
             "-f", "s16le", "-ar", "44100", "-ac", "2", "-"],
            stdout=subprocess.PIPE)
        pa_cmd = ["pacat", "--playback", "--format=s16le", "--rate=44100", "--channels=2"]
        if sink:
            pa_cmd.append(f"--device={sink}")
        pa = subprocess.Popen(pa_cmd, stdin=ff.stdout)
        ff.stdout.close()
        try:
            pa.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log("WARNING: pacat timed out")
            pa.kill(); ff.kill()
            return False
        if pa.returncode == 0:
            log("audio playback complete (pacat) ✅")
            return True
        log(f"pacat exited {pa.returncode}; falling back")
    except FileNotFoundError:
        log("ffmpeg/pacat not found; falling back to pw-play")

    for tool in (["pw-play"], ["paplay"]):
        cmd = list(tool)
        if sink:
            cmd += ["--target", sink] if tool[0] == "pw-play" else ["-d", sink]
        cmd.append(audio_path)
        try:
            log(f"fallback: {' '.join(cmd)}")
            p = subprocess.Popen(cmd)
            p.wait(timeout=timeout)
            log("audio playback complete ✅")
            return p.returncode == 0
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            p.kill()
            return False
    log("FAILED: no usable audio player")
    return False


# ═══════════════════════════════════════════════════════════════════════════
# BLE command helpers
# ═══════════════════════════════════════════════════════════════════════════

class LiveKeepalive:
    """Re-send ENABLE_LIVE (FD 01) so classic advertising stays open."""

    def __init__(self, client: BleakClient, interval: float = 4.0):
        self.client = client
        self.interval = interval
        self._packet = build_packet(ENABLE_LIVE, bytes([0x01]))

    async def send_once(self):
        try:
            await self.client.write_gatt_char(WRITE_UUID, self._packet, response=False)
        except Exception as e:
            log(f"keepalive write error: {e}")

    async def run_loop(self, stop_event: asyncio.Event):
        log(f"keepalive: sending every {self.interval}s")
        while not stop_event.is_set():
            await self.send_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass


async def tx(client: BleakClient, cmd: int, payload: bytes, label: str,
             rx_queue: asyncio.Queue | None = None, ack_timeout: float = 3.0):
    """Send one command; log TX and any ACK. Returns the reply or None."""
    pkt = build_packet(cmd, payload)
    log(f"{label}: TX {pkt.hex(' ')}")
    await client.write_gatt_char(WRITE_UUID, pkt, response=False)
    if rx_queue is None:
        return None
    try:
        reply = await asyncio.wait_for(rx_queue.get(), timeout=ack_timeout)
        log(f"{label}: RX {reply.hex(' ')}")
        return reply
    except asyncio.TimeoutError:
        log(f"{label}: no ACK")
        return None


async def head_start(client: BleakClient):
    """Head/jaw start via SET_MOVEMENT (0xCA) [0x02, 0x01]."""
    await tx(client, SET_MOVEMENT, bytes([MOVEMENT_HEAD, 0x01]), "head START")


async def head_stop(client: BleakClient):
    """Head/jaw stop via SET_MOVEMENT (0xCA) [0x02, 0x00]."""
    await tx(client, SET_MOVEMENT, bytes([MOVEMENT_HEAD, 0x00]), "head STOP")


async def stop_onboard_playback(client, rx):
    """(a) Stop onboard-file playback via C6. TODO-verify payload: we send
    PLAY_FILE with serial 0x0000 and action 0x00 (play uses action 0x01)."""
    await tx(client, PLAY_FILE, bytes([0x00, 0x00, 0x00]), "(a) stop playback C6 [TODO-verify payload]", rx)
    drain(rx)


async def select_classic_source(client, rx):
    """(d) FE 01 — select classic BT audio source. TODO-verify on hardware."""
    await tx(client, SELECT_CLASSIC_SOURCE, bytes([0x01]), "(d) select classic source FE 01 [TODO-verify]", rx)


async def enter_live_animation(client, rx):
    """(f) FC 01 — enter live animation mode. TODO-verify whether it subsumes 0xCA."""
    await tx(client, ENTER_LIVE_ANIM, bytes([0x01]), "(f) live animation FC 01 [TODO-verify]", rx)


async def apply_volume(client, rx, volume: int):
    """(g) FA — SET_VOLUME (opcode from skelly.py Cmd.SET_VOLUME). Range assumed 0-100 [TODO-verify]."""
    v = max(0, min(100, int(volume)))
    await tx(client, SET_VOLUME, bytes([v]), f"(g) volume FA {v}", rx)


# ═══════════════════════════════════════════════════════════════════════════
# Upload + play path (ported from skelly_speak.py / upload.py)
# ═══════════════════════════════════════════════════════════════════════════

def _filename_ref(name: str) -> bytes:
    return bytes.fromhex("5C55") + name.encode("utf-16le")


async def _event(rx: asyncio.Queue, cmd: int, timeout: float) -> bytes | None:
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            return None
        try:
            pkt = await asyncio.wait_for(rx.get(), timeout=left)
        except asyncio.TimeoutError:
            return None
        if len(pkt) >= 2 and pkt[0] == ACK_PREFIX and pkt[1] == cmd:
            return pkt


async def _read_file_table(client, rx) -> list[tuple[int, int, str]]:
    drain(rx)
    await tx(client, QUERY_FILES, b"", "QUERY_FILES")
    table = []
    while True:
        try:
            pkt = await asyncio.wait_for(rx.get(), timeout=3.0)
        except asyncio.TimeoutError:
            break
        if len(pkt) >= 60 and pkt[0] == ACK_PREFIX and pkt[1] == QUERY_FILES:
            name = pkt[60:].decode("utf-16le", errors="ignore").strip("\x00 ")
            if name:
                table.append((int.from_bytes(pkt[2:4], "big"),
                              int.from_bytes(pkt[4:8], "big"), name))
    return table


def prepare_upload_audio(audio_path: str, tmpdir: str) -> bytes:
    """Skull firmware stores MP3. Pass .mp3 through; transcode anything else (64k mono)."""
    if audio_path.lower().endswith(".mp3"):
        return Path(audio_path).read_bytes()
    out = str(Path(tmpdir) / "upload.mp3")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", audio_path, "-ac", "1",
                    "-b:a", "64k", out], check=True)
    return Path(out).read_bytes()


async def upload_and_play(client: BleakClient, rx: asyncio.Queue, audio_path: str) -> bool:
    """Upload via C0-C3, settle, play via C6. Jaw auto-lip-syncs on onboard playback."""
    log("PATH: UPLOAD+PLAY (skull's own speaker)")
    with tempfile.TemporaryDirectory() as td:
        try:
            data = prepare_upload_audio(audio_path, td)
        except Exception as e:
            log(f"transcode failed: {e}")
            return False
    log(f"upload payload: {len(data)} bytes")

    table = await _read_file_table(client, rx)
    mine = [f for f in table if f[2].lower().startswith(VOCAB)]
    if len(mine) >= MAX_CUSTOM_FILES:
        for s, c, n in mine[: len(mine) - (MAX_CUSTOM_FILES - 1)]:
            log(f"recycling {n!r}")
            await tx(client, DELETE_FILE, s.to_bytes(2, "big") + c.to_bytes(4, "big"), "DELETE")
            await asyncio.sleep(0.45)

    name = f"{VOCAB}{time.strftime('%H%M%S')}.mp3"
    size = max(20, min(239, client.mtu_size - 8))
    chunks = [data[i:i + size] for i in range(0, len(data), size)]
    log(f"uploading {name}: {len(chunks)} chunks of {size}")

    drain(rx)
    start = build_packet(START_TRANSFER,
                         len(data).to_bytes(4, "big") + len(chunks).to_bytes(2, "big") + _filename_ref(name))
    await client.write_gatt_char(WRITE_UUID, start, response=False)
    ev = await _event(rx, START_TRANSFER, 10.0)
    if not ev or ev[2] != 0:
        log(f"C0 not accepted: {ev.hex(' ') if ev else 'no reply'}")
        return False
    for i, c in enumerate(chunks):
        await client.write_gatt_char(WRITE_UUID, build_packet(CHUNK, i.to_bytes(2, "big") + c), response=False)
        await asyncio.sleep(0.111)
    await client.write_gatt_char(WRITE_UUID, build_packet(END_TRANSFER), response=False)
    ended = await _event(rx, END_TRANSFER, 240.0)
    if not ended:
        log("no C2 reply")
        return False
    if ended[2] != 0:
        tail = int.from_bytes(ended[3:5], "big")
        log(f"device missed chunks from {tail}, resending (single pass)")
        for i in range(tail, len(chunks)):
            await client.write_gatt_char(WRITE_UUID, build_packet(CHUNK, i.to_bytes(2, "big") + chunks[i]), response=False)
            await asyncio.sleep(0.012)
        await client.write_gatt_char(WRITE_UUID, build_packet(END_TRANSFER), response=False)
        ended = await _event(rx, END_TRANSFER, 240.0)
        if not ended or ended[2] != 0:
            log("C2 still failing after resend")
            return False
    await client.write_gatt_char(WRITE_UUID, build_packet(CONFIRM_TRANSFER, _filename_ref(name)), response=False)
    conf = await _event(rx, CONFIRM_TRANSFER, 10.0)
    if not conf or conf[2] != 0:
        log("device refused to store clip")
        return False
    log(f"stored {name}; settling {UPLOAD_SETTLE_S:.0f}s")
    await asyncio.sleep(UPLOAD_SETTLE_S)

    serial = None
    for s, _c, n in await _read_file_table(client, rx):
        if n.lower() == name.lower():
            serial = s
    if serial is None:
        log(f"{name} vanished from file list")
        return False
    await tx(client, PLAY_FILE, serial.to_bytes(2, "big") + b"\x01", f"PLAY serial {serial}")
    await asyncio.sleep(min(60.0, 6.0 + len(data) / 8000))
    log("upload+play complete ✅")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# Capacity probe
# ═══════════════════════════════════════════════════════════════════════════

async def run_capacity() -> bool:
    """Query storage capacity. QUERY_CAPACITY=0xD2 is from skelly.py Cmd (reference code);
    reply layout is NOT documented there → TODO-verify decoding. Also logs raw E0 reply."""
    client, rx = await ble_connect_auth()
    if not client:
        return False
    try:
        for label, op in (("QUERY_CAPACITY 0xD2", QUERY_CAPACITY), ("QUERY_PARAMS 0xE0", QUERY_PARAMS)):
            drain(rx)
            await tx(client, op, b"", label)
            await asyncio.sleep(0.5)
            got = False
            while True:
                try:
                    pkt = await asyncio.wait_for(rx.get(), timeout=3.0)
                except asyncio.TimeoutError:
                    break
                got = True
                log(f"{label} RAW ({len(pkt)}B): {pkt.hex(' ')}")
                if op == QUERY_CAPACITY and len(pkt) >= 6 and pkt[0] == ACK_PREFIX and pkt[1] == op:
                    # TODO-verify: guess big-endian u32 fields after the opcode
                    for off in range(2, min(len(pkt) - 3, 14), 4):
                        v = int.from_bytes(pkt[off:off + 4], "big")
                        log(f"  u32@{off} = {v} ({v / 1048576:.1f} MiB if bytes)")
            if not got:
                log(f"{label}: no reply")
        return True
    finally:
        await ble_close(client)


# ═══════════════════════════════════════════════════════════════════════════
# Sessions
# ═══════════════════════════════════════════════════════════════════════════

async def live_session(client, rx, audio_path: str, classic_addr: str,
                       head_motion: bool, volume: int, max_attempts: int = 1,
                       repair: bool = False) -> bool:
    """Live path on an existing authed BLE link (OpenSkelly connect_live order).

    (a) C6 stop file + FC 00 → (b) FD 01 (wait ACK) → (c) bluetoothctl connect
    (bond reused; pair only if missing) → (d) FE 01 → wait sink → (e) pacat →
    (f) FC 01 → (g) FA volume.
    """
    log("PATH: LIVE (A2DP)")
    await stop_onboard_playback(client, rx)                       # (a)
    await tx(client, ENTER_LIVE_ANIM, bytes([0x00]), "(a) FC 00 (reset live anim)", rx)

    keepalive = LiveKeepalive(client)
    classic_ok = False
    for attempt in range(1, max_attempts + 1):                    # bounded; classic only, never BLE connect
        log(f"(b) enable classic radio FD 01 — handshake attempt {attempt}/{max_attempts}")
        await tx(client, ENABLE_LIVE, bytes([0x01]), "(b) FD 01", rx)
        stop = asyncio.Event()
        ka = asyncio.create_task(keepalive.run_loop(stop))
        await asyncio.sleep(0.5)
        log(f"(c) connect classic {classic_addr}")
        classic_ok = await connect_classic_bt(classic_addr, keepalive.send_once,
                                              repair=repair and attempt == 1)
        stop.set()
        try:
            await asyncio.wait_for(ka, timeout=5.0)
        except asyncio.TimeoutError:
            ka.cancel()
        if classic_ok:
            break
        log(f"classic handshake FAILED (attempt {attempt}/{max_attempts})")
    if not classic_ok:
        await tx(client, ENABLE_LIVE, bytes([0x00]), "FD 00 (abort live)")
        return False

    drain(rx)
    await select_classic_source(client, rx)                       # (d) right after connect
    log("waiting for A2DP sink...")
    sink = await asyncio.get_running_loop().run_in_executor(
        None, wait_for_bt_sink, _bt_mac(classic_addr), 12.0)
    if not sink:
        log("FAILED: classic link up but no A2DP sink appeared (pactl list sinks)")
        await tx(client, ENABLE_LIVE, bytes([0x00]), "FD 00 (abort live)")
        return False
    log(f"A2DP sink ready: {sink}")

    if head_motion:
        await head_start(client)                                  # 0xCA alongside FC 01
        await asyncio.sleep(0.3)

    stream_task = asyncio.get_running_loop().run_in_executor(     # (e) start streaming
        None, stream_audio_pipewire, audio_path, 120.0, sink)
    await asyncio.sleep(0.3)                                      # let pacat open the transport
    await enter_live_animation(client, rx)                        # (f) FC 01 while audio flows
    await asyncio.sleep(0.08)
    await apply_volume(client, rx, volume)                        # (g) volume after FC 01 (OpenSkelly order)
    audio_ok = await stream_task

    if head_motion:
        await head_stop(client)
    await tx(client, ENTER_LIVE_ANIM, bytes([0x00]), "FC 00 (exit live animation)")
    await tx(client, ENABLE_LIVE, bytes([0x00]), "FD 00 (disable live)")
    await _btctl("disconnect", _bt_mac(classic_addr), timeout=10.0)
    log(f"LIVE path executed — audio {'OK' if audio_ok else 'FAILED'}")
    return audio_ok


async def run_session(mode: str, audio_path: str, classic_addr: str | None,
                      head_motion: bool, volume: int, repair: bool = False) -> bool:
    classic_addr = classic_addr or CLASSIC_DEFAULT_ADDR
    client, rx = await ble_connect_auth()
    if not client:
        return False
    try:
        if mode == "upload":
            ok = await upload_and_play(client, rx, audio_path)
            log(f"RESULT: path=UPLOAD ok={ok}")
            return ok
        if mode == "live":
            ok = await live_session(client, rx, audio_path, classic_addr, head_motion, volume, 1, repair)
            log(f"RESULT: path=LIVE ok={ok}")
            return ok
        # hybrid
        ok = await live_session(client, rx, audio_path, classic_addr, head_motion, volume,
                                HYBRID_MAX_LIVE_FAILS, repair)
        if ok:
            log("RESULT: path=LIVE (hybrid, no fallback needed)")
            return True
        log(f"HYBRID: live handshake failed {HYBRID_MAX_LIVE_FAILS}x (or audio failed) → FALLBACK to upload+play")
        ok = await upload_and_play(client, rx, audio_path)
        log(f"RESULT: path=UPLOAD-FALLBACK ok={ok}")
        return ok
    finally:
        await ble_close(client)


async def run_test_head(seconds: float) -> bool:
    client, rx = await ble_connect_auth()
    if not client:
        return False
    try:
        await head_start(client)
        log(f"head running for {seconds}s...")
        await asyncio.sleep(seconds)
        await head_stop(client)
        log("test complete ✅")
        return True
    finally:
        await ble_close(client)


def run_preflight(classic_addr: str | None) -> bool:
    """No-radio-traffic checks: tools, audio server, BT module, bond state."""
    import shutil
    mac = _bt_mac(classic_addr or CLASSIC_DEFAULT_ADDR)
    ok = True
    for t in ("bluetoothctl", "pactl", "pacat", "ffmpeg"):
        found = shutil.which(t)
        log(f"tool {t}: {'ok' if found else 'MISSING'}")
        ok &= bool(found)
    info = subprocess.run(["pactl", "info"], capture_output=True, text=True).stdout
    server = next((l.split(":", 1)[1].strip() for l in info.splitlines() if l.startswith("Server Name")), "?")
    rate = next((l.split(":", 1)[1].strip() for l in info.splitlines() if l.startswith("Default Sample")), "?")
    log(f"audio server: {server}  default spec: {rate}")
    mods = subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True).stdout
    if "pulseaudio" in server.lower():
        has = "module-bluez5-discover" in mods
        log(f"PulseAudio bluez5 module: {'loaded' if has else 'MISSING (apt install pulseaudio-module-bluetooth)'}")
        ok &= has
        if "44100" not in rate:
            log("WARNING: PulseAudio default-sample-rate is not 44100 — set it in /etc/pulse/daemon.conf")
    bt = subprocess.run(["bluetoothctl", "info", mac], capture_output=True, text=True).stdout
    paired = "Paired: yes" in bt
    log(f"classic {mac}: paired={paired} trusted={'Trusted: yes' in bt} "
        f"a2dp_uuid={'0000110b' in bt} connected={'Connected: yes' in bt}")
    if not paired:
        log("NOTE: no bond yet — first live run will pair (agent answers PIN 1234)")
    log(f"PREFLIGHT {'OK ✅' if ok else 'FAILED ❌'}")
    return ok


def main():
    p = argparse.ArgumentParser(description="12ft Skelly Pi Orchestrator")
    p.add_argument("audio", nargs="?", default=None, help="Audio file (wav/mp3)")
    p.add_argument("--mode", choices=["live", "upload", "hybrid"], default="live",
                   help="live=A2DP stream; upload=C0-C6 onboard playback; hybrid=live, fall back to upload after 2 failed handshakes")
    p.add_argument("--test-head", type=float, metavar="SECONDS", help="Head motion test only")
    p.add_argument("--preflight", action="store_true", help="Local checks only; no radio traffic to the skull")
    p.add_argument("--capacity", action="store_true", help="Query device storage capacity (0xD2) + raw 0xE0")
    p.add_argument("--classic-addr", default=None, help=f"Classic BT MAC (default {CLASSIC_DEFAULT_ADDR})")
    p.add_argument("--no-head", action="store_true", help="Skip 0xCA head start/stop during live playback")
    p.add_argument("--repair", action="store_true",
                   help="Remove + re-pair the classic Live device (default: reuse existing bond)")
    p.add_argument("--volume", type=int, default=80, help="FA volume 0-100 (default 80)")
    a = p.parse_args()

    if a.preflight:
        sys.exit(0 if run_preflight(a.classic_addr) else 1)
    if a.capacity:
        sys.exit(0 if asyncio.run(run_capacity()) else 1)
    if a.test_head is not None:
        log("=== SKELLY HEAD MOTION TEST ===")
        sys.exit(0 if asyncio.run(run_test_head(a.test_head)) else 1)
    if a.audio is None:
        p.error("audio file required (or use --test-head / --capacity)")
    path = str(Path(a.audio).resolve())
    if not os.path.exists(path):
        log(f"FATAL: audio file not found: {path}")
        sys.exit(1)
    log(f"=== SKELLY SESSION mode={a.mode} ===")
    ok = asyncio.run(run_session(a.mode, path, a.classic_addr, not a.no_head, a.volume, a.repair))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
