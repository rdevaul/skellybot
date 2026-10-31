#!/usr/bin/env python3
"""skelly_daemon — persistent "hold the channel open" daemon for the 12ft Skelly.

Imports and reuses functions from skelly_orchestrator.py; keeps its CLI
behavior unchanged.  Run this on the Pi so the BLE + classic links stay
up between utterances, cutting per-clip latency from ~25 s to ~1-2 s.

Usage (on Pi):
  # Foreground (use for testing):
  ~/skelly-venv/bin/python ~/skelly_daemon.py

  # Via systemd --user (recommended):
  cp skelly-daemon.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now skelly-daemon
  loginctl enable-linger rich

  # Client commands (on Pi):
  ~/skelly-venv/bin/python ~/skelly_client.py play /tmp/say_12345.wav
  ~/skelly-venv/bin/python ~/skelly_client.py status

  # Client commands (on Mac, via skelly_say.sh fast path):
  ssh pi '~/skelly-venv/bin/python ~/skelly_client.py status'

Dependencies: bleak, bluez, pulseaudio/pipewire, ffmpeg (same as orchestrator).
Python ≥ 3.9 (Raspbian Bullseye).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── Import everything from the orchestrator (reuses its functions unchanged) ─
sys.path.insert(0, str(Path(__file__).resolve().parent))
import skelly_orchestrator as orc  # noqa: E402

# ── Constants ────────────────────────────────────────────────────────────────

SOCKET_PATH = os.environ.get("SKELLY_SOCKET", "/tmp/skelly.sock")

# Backoff schedule: seconds between reconnection attempts
BACKOFF_SCHEDULE = [2, 4, 8, 16, 30]

# Idle noise: very-faint pink noise (~-60 dBFS) to keep the skull's amp awake
NOISE_AMPLITUDE = 0.001  # matches orchestrator PREROLL pink-noise level

# Health-check interval (E5 QUERY_VOLUME every N seconds)
HEALTH_INTERVAL = 15.0

# Default: don't keep sending FD 01 periodically — we rely on E5 health checks
# Set --fd01-keepalive to re-enable (for hardware that needs it)
DEFAULT_FD01_KEEPALIVE = False

# Default noise-bed preroll (seconds of pink noise at the tail of each pre-rendered
# clip so the amp stays awake during the gap between clips)
DAEMON_PREROLL_S = float(os.environ.get("SKELLY_DAEMON_PREROLL_S", "0.2"))


class DaemonState:
    """Daemon status bag (only touched from the main event-loop)."""

    def __init__(self) -> None:
        self.state = "INIT"
        self.uptime_start = time.time()
        self.reconnect_count = 0
        self.last_error: str | None = None
        self.now_playing: str | None = None

    def set(self, s: str, error: str | None = None) -> None:
        self.state = s
        if error is not None:
            self.last_error = error

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "uptime_s": round(time.time() - self.uptime_start, 1),
            "reconnect_count": self.reconnect_count,
            "last_error": self.last_error,
            "now_playing": self.now_playing,
        }


# ── No-op keepalive (for when fd01 keepalive is disabled) ───────────────────

async def _no_op_keepalive() -> None:
    pass


# ── Startup / teardown ──────────────────────────────────────────────────────

async def daemon_startup(
    state: DaemonState,
    classic_addr: str | None = None,
    volume: int = 100,
    fd01_keepalive: bool = DEFAULT_FD01_KEEPALIVE,
):
    """Bring up BLE + auth + classic. Returns (client, rx_queue, sink, ka_stop_event|None)."""
    classic_addr = classic_addr or orc.CLASSIC_DEFAULT_ADDR
    state.set("CONNECTING")

    # 1. BLE connect + auth
    client, rx = await orc.ble_connect_auth()
    if client is None:
        state.set("FAILED", "BLE connect/auth failed")
        raise RuntimeError("BLE connect/auth failed")

    # 2. FD 01 (enable classic radio)
    await orc.tx(client, orc.ENABLE_LIVE, bytes([0x01]), "daemon FD 01", rx)

    # Optional: keepalive stop-event for FD 01 periodic resend (default off)
    ka_stop_event: asyncio.Event | None = None
    if fd01_keepalive:
        ka = orc.LiveKeepalive(client)
        ka_stop_event = asyncio.Event()
        asyncio.create_task(ka.run_loop(ka_stop_event))
        orc.log("daemon: FD 01 keepalive ENABLED")
    else:
        orc.log("daemon: FD 01 keepalive OFF (E5 health checks only)")

    # 3. Connect classic BT (reuse bond)
    orc.log("daemon: connecting classic BT …")
    # During connect_classic_bt retry loops, re-send FD 01 to keep the classic
    # advertising window open (the orchestrator's keepalive_fn pattern).
    async def _fd01_retry() -> None:
        try:
            await orc.tx(client, orc.ENABLE_LIVE, bytes([0x01]), "FD 01 retry", rx)
        except Exception:
            pass

    ok = await orc.connect_classic_bt(
        classic_addr,
        _fd01_retry,
        repair=False,
    )
    if not ok:
        state.set("FAILED", "classic BT connect failed")
        raise RuntimeError("classic BT connect failed")

    # 4. FE 01 — select classic source
    orc.drain(rx)
    await orc.select_classic_source(client, rx)

    # 5. Wait for A2DP sink
    bt_mac = orc._bt_mac(classic_addr)
    orc.log("daemon: waiting for A2DP sink …")
    sink = await asyncio.get_running_loop().run_in_executor(
        None, orc.wait_for_bt_sink, bt_mac, 12.0,
    )
    if sink is None:
        state.set("FAILED", "A2DP sink never appeared")
        raise RuntimeError("A2DP sink never appeared")
    orc.log(f"daemon: A2DP sink ready: {sink}")

    # 6. Set sink volume 100%
    orc.max_sink_volume(sink)

    # 7. FA 100 (BLE volume)
    orc.drain(rx)
    await orc.apply_volume(client, rx, volume)

    state.set("READY")
    orc.log("daemon: READY ✅")
    return client, rx, sink, ka_stop_event


async def daemon_shutdown(
    client, rx, ka_stop_event: asyncio.Event | None, bed_proc,
    classic_addr: str | None = None,
) -> None:
    """Graceful shutdown: stop bed, FC 00, FD 00, disconnect classic + BLE."""
    orc.log("daemon: shutting down …")

    # Stop idle noise bed
    if bed_proc and bed_proc.poll() is None:
        bed_proc.terminate()
        try:
            bed_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            bed_proc.kill()

    # Stop FD 01 keepalive if running
    if ka_stop_event is not None:
        ka_stop_event.set()

    # FC 00 (exit live animation)
    await orc.tx(client, orc.ENTER_LIVE_ANIM, bytes([0x00]), "daemon FC 00", rx)

    # FD 00 (disable classic radio)
    await orc.tx(client, orc.ENABLE_LIVE, bytes([0x00]), "daemon FD 00", rx)

    # Disconnect classic
    bt_mac = orc._bt_mac(classic_addr or orc.CLASSIC_DEFAULT_ADDR)
    await orc._btctl("disconnect", bt_mac, timeout=10.0)

    # Disconnect BLE
    await orc.ble_close(client)


# ── Idle noise bed ──────────────────────────────────────────────────────────

def start_noise_bed(sink: str) -> subprocess.Popen:
    """Start a continuous very-faint pink-noise stream to the BT sink.

    The skull's amplifier goes to sleep between clips (SkellyUltra finding).
    This keeps it awake with ~-60 dBFS pink noise.
    """
    # Generate infinite pink noise via ffmpeg, decode to s16le, pipe to pacat
    cmd = [
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i",
        f"anoisesrc=color=pink:amplitude={NOISE_AMPLITUDE}:duration=0:sample_rate=44100",
        "-f", "s16le", "-ar", "44100", "-ac", "2", "-",
    ]
    pacat_cmd = [
        "pacat", "--playback", "--format=s16le", "--rate=44100", "--channels=2",
        f"--device={sink}",
    ]
    # Pipe ffmpeg stdout → pacat stdin
    ffmpeg_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pacat_proc = subprocess.Popen(pacat_cmd, stdin=ffmpeg_proc.stdout, stderr=subprocess.PIPE)
    # Close the stdout pipe in this process (pacat owns it now)
    if ffmpeg_proc.stdout:
        ffmpeg_proc.stdout.close()
    orc.log(f"daemon: noise bed started (ffmpeg → pacat → {sink})")
    return pacat_proc  # track pacat; if it dies the bed is broken


# ── Audio clip pre-render (fast path) ───────────────────────────────────────

def pre_render_clip(
    audio_path: str, tmpdir: str,
    preroll_s: float = DAEMON_PREROLL_S,
    gain_db: float = orc.GAIN_DB,
    noise_amplitude: float = NOISE_AMPLITUDE,
    prenorm: bool = False,
) -> str:
    """Pre-render an audio clip for playback through the BT sink.

    Similar to the orchestrator's ffmpeg chain but uses a shorter preroll
    (the idle noise bed keeps the amp awake, so we don't need 0.6 s).
    Returns path to the raw s16le file.
    """
    raw = os.path.join(tmpdir, "clip.s16")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", audio_path,
         "-f", "lavfi", "-i",
         f"anoisesrc=color=pink:amplitude={noise_amplitude}:duration={preroll_s}:sample_rate=44100",
         "-filter_complex",
         f"[0:a]aresample=44100,aformat=channel_layouts=stereo,"
         + ("apad=pad_dur=0.25[sp];" if prenorm else
            f"loudnorm=I=-10:TP=-1:LRA=11,volume={gain_db}dB,alimiter=limit=0.97,apad=pad_dur=0.25[sp];") +
         f"[1:a]aformat=channel_layouts=stereo[pr];[pr][sp]concat=n=2:v=0:a=1[out]",
         "-map", "[out]",
         "-f", "s16le", "-ar", "44100", "-ac", "2", raw],
        check=True, timeout=120,
    )
    return raw


# ── Clip playback ───────────────────────────────────────────────────────────

async def play_clip(
    client, rx, sink: str, audio_path: str,
    head_motion: bool = True, volume: int = 100, prenorm: bool = False,
) -> dict:
    """Play one audio clip: pre-render, pause bed, head START, pacat, FC 01 + FA, head STOP.

    Returns timing/stats dict.
    """
    timings: dict = {}
    t_start = time.time()

    # Pre-render
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="skelly-clip-") as td:
        raw = pre_render_clip(audio_path, td, prenorm=prenorm)
        render_s = time.time() - t0
        size_bytes = os.path.getsize(raw)
        orc.log(f"daemon: pre-rendered {size_bytes / 176400:.2f}s in {render_s:.2f}s")
        timings["render_s"] = round(render_s, 3)

        # Start head motion
        if head_motion:
            await orc.head_start(client)
            await asyncio.sleep(0.15)

        # Stream via pacat
        pa_cmd = [
            "pacat", "--playback", "--format=s16le", "--rate=44100", "--channels=2",
            f"--latency-msec={orc.PACAT_LATENCY_MS}", f"--device={sink}", raw,
        ]
        orc.log("daemon: streaming clip via pacat …")
        t_stream = time.time()
        loop = asyncio.get_running_loop()
        pa_fut = loop.run_in_executor(None, subprocess.run, pa_cmd)
        timings["start_latency_s"] = round(t_stream - t0, 3)
        # FC 01 + FA while audio is flowing (same order as the verified one-shot path)
        await asyncio.sleep(0.3)
        orc.drain(rx)
        await orc.enter_live_animation(client, rx)
        await asyncio.sleep(0.08)
        await orc.apply_volume(client, rx, volume)
        pa_result = await pa_fut
        timings["duration_s"] = round(time.time() - t_stream, 3)
        if pa_result.returncode != 0:
            raise RuntimeError(f"pacat exited {pa_result.returncode}")

    # Stop head motion
    if head_motion:
        await orc.head_stop(client)

    # Exit live animation
    await orc.tx(client, orc.ENTER_LIVE_ANIM, bytes([0x00]), "daemon FC 00 (post-clip)")

    timings["total_s"] = round(time.time() - t_start, 3)
    return timings


# ── UNIX socket server ──────────────────────────────────────────────────────

async def handle_client_request(
    line: str,
    client, rx, sink: str, state: DaemonState,
    queue: asyncio.Queue,
    bed_proc_holder: list,  # mutable [proc] — updated when bed starts/stops
    classic_addr: str,
    head_motion: bool,
    volume: int,
    ka_stop_event: asyncio.Event | None,
) -> dict:
    """Handle one JSON request from the socket.

    Play requests are queued (FIFO) so clips don't overlap.
    """
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid JSON"}

    cmd = req.get("cmd")
    if cmd == "status":
        snap = state.snapshot()
        # Add link stats
        try:
            info = await orc.classic_info(orc._bt_mac(classic_addr))
            snap["classic_info"] = info
        except Exception:
            snap["classic_info"] = None
        return snap

    if cmd == "stop":
        # Signal stop to current clip playback (best-effort; pacat will be killed)
        # For simplicity we just note it; the FIFO queue handles ordering.
        state.now_playing = None
        return {"ok": True, "message": "stop requested (current clip will finish)"}

    if cmd == "shutdown":
        # Same path as SIGTERM: main loop is waiting on the signal handler.
        asyncio.get_running_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGTERM)
        return {"ok": True, "message": "shutdown requested"}

    if cmd == "play":
        path = req.get("path", "")
        if not path or not os.path.exists(path):
            return {"ok": False, "error": f"file not found: {path}"}

        # Queue the request
        result_holder: list = []
        await queue.put((path, (req.get("head", True), bool(req.get("prenorm", False))), result_holder))

        # Wait for it to complete (FIFO ensures no overlap)
        while not result_holder:
            await asyncio.sleep(0.05)
        return result_holder[0]

    return {"ok": False, "error": f"unknown cmd: {cmd}"}


async def socket_server(
    client_holder: list, rx_holder: list, sink_holder: list, state: DaemonState,
    classic_addr: str, head_motion: bool, volume: int,
    ka_stop_event: asyncio.Event | None,
    bed_proc_holder: list,
) -> asyncio.Server:
    """Listen on the UNIX socket; dispatch requests.

    Uses the mutable holders (not captured objects) so requests after a
    supervisor reconnect go to the *new* BLE client / sink.
    """

    # FIFO queue for play requests
    play_queue: asyncio.Queue = asyncio.Queue()

    # Background worker: drain the queue and play clips one at a time
    async def queue_worker():
        while True:
            path, opts, result_holder = await play_queue.get()
            do_head, prenorm = opts if isinstance(opts, tuple) else (opts, False)
            if state.state in ("SHUTTING_DOWN", "FAILED"):
                result_holder.append({"ok": False, "error": "daemon not ready"})
                play_queue.task_done()
                continue

            if state.state != "READY":
                result_holder.append({"ok": False, "error": f"daemon state {state.state}"})
                play_queue.task_done()
                continue
            client, rx, sink = client_holder[0], rx_holder[0], sink_holder[0]
            state.set("PLAYING")
            state.now_playing = os.path.basename(path)
            orc.log(f"daemon: playing {path} …")

            try:
                # Pause the idle noise bed before playing a clip
                if bed_proc_holder and bed_proc_holder[0] and bed_proc_holder[0].poll() is None:
                    bed_proc_holder[0].terminate()
                    try:
                        bed_proc_holder[0].wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        bed_proc_holder[0].kill()
                    bed_proc_holder[0] = None

                timings = await play_clip(client, rx, sink, path, do_head, volume, prenorm)

                # Get link stats
                link_stats: dict = {}
                try:
                    info = await orc.classic_info(orc._bt_mac(classic_addr))
                    link_stats = info
                except Exception:
                    pass

                result_holder.append({
                    "ok": True,
                    "timing": timings,
                    "link": link_stats,
                })
                orc.log(f"daemon: clip done — {timings}")
            except Exception as e:
                result_holder.append({"ok": False, "error": str(e)})
                orc.log(f"daemon: clip error: {e}")
            finally:
                state.now_playing = None
                if state.state == "PLAYING":
                    state.set("READY")

                # Resume the idle noise bed
                sink = sink_holder[0]
                if bed_proc_holder[0] is None and sink and state.state == "READY":
                    try:
                        bed_proc_holder[0] = start_noise_bed(sink)
                    except Exception as e:
                        orc.log(f"daemon: failed to restart noise bed: {e}")

                play_queue.task_done()

    # Start the worker
    worker_task = asyncio.create_task(queue_worker())

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            line = data.decode().strip()
            if not line:
                writer.close()
                return

            resp = await handle_client_request(
                line, client_holder[0], rx_holder[0], sink_holder[0], state, play_queue,
                bed_proc_holder, classic_addr, head_motion, volume, ka_stop_event,
            )
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        except Exception as e:
            orc.log(f"daemon: socket handler error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_unix_server(_handle, path=SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)
    orc.log(f"daemon: listening on {SOCKET_PATH}")
    return server


# ── Health check + recovery ─────────────────────────────────────────────────

async def health_check_ble(client, rx) -> bool:
    """Cheap BLE health: send E5 QUERY_VOLUME on the persistent notify queue, expect BB E5 xx."""
    try:
        if not getattr(client, "is_connected", True):
            return False
        orc.drain(rx)
        await client.write_gatt_char(orc.WRITE_UUID, orc.build_packet(0xE5), response=False)
        end = time.time() + 3.0
        while time.time() < end:
            try:
                reply = await asyncio.wait_for(rx.get(), timeout=max(0.05, end - time.time()))
            except asyncio.TimeoutError:
                break
            if len(reply) >= 2 and reply[0] == orc.ACK_PREFIX and reply[1] == 0xE5:
                return True
        return False
    except Exception:
        return False


async def health_check_classic(bt_mac: str) -> bool:
    """Check if classic BT is still connected."""
    try:
        info = await orc.classic_info(bt_mac)
        return info.get("connected", False)
    except Exception:
        return False


def health_check_bed(bed_proc) -> bool:
    """Check if the noise bed process is still running."""
    return bed_proc is not None and bed_proc.poll() is None


async def attempt_reconnect_classic(
    client, rx, classic_addr: str, ka_stop_event: asyncio.Event | None,
) -> bool:
    """Reconnect classic BT only (BLE still alive).
    Sends FD 01, bluetoothctl connect, FE 01, waits for sink.
    """
    bt_mac = orc._bt_mac(classic_addr)

    # FD 01
    await orc.tx(client, orc.ENABLE_LIVE, bytes([0x01]), "recon FD 01", rx)
    await asyncio.sleep(0.3)

    # connect_classic_bt
    ok = await orc.connect_classic_bt(classic_addr, _no_op_keepalive, repair=False)
    if not ok:
        return False

    # FE 01
    orc.drain(rx)
    await orc.select_classic_source(client, rx)

    # Wait for sink
    sink = await asyncio.get_running_loop().run_in_executor(
        None, orc.wait_for_bt_sink, bt_mac, 12.0,
    )
    return sink is not None


async def attempt_full_reconnect(
    classic_addr: str, volume: int, fd01_keepalive: bool,
) -> tuple | None:
    """Full reconnect: BLE + auth + classic.
    Returns (client, rx, sink, ka_stop_event) or None.
    """
    state = DaemonState()
    try:
        result = await daemon_startup(state, classic_addr, volume, fd01_keepalive)
        return result
    except Exception as e:
        orc.log(f"full reconnect failed: {e}")
        return None


# ── Supervisor loop ─────────────────────────────────────────────────────────

async def supervisor_loop(
    client_holder: list,
    rx_holder: list,
    sink_holder: list,
    ka_stop_holder: list,
    bed_proc_holder: list,
    state: DaemonState,
    classic_addr: str,
    volume: int,
    fd01_keepalive: bool,
    server_holder: list,
):
    """Monitor health and reconnect as needed. Runs alongside the socket server."""
    bt_mac = orc._bt_mac(classic_addr)

    while state.state != "SHUTTING_DOWN":
        await asyncio.sleep(HEALTH_INTERVAL)

        if state.state == "PLAYING":
            continue  # don't interrupt active playback

        # Check BLE
        client = client_holder[0]
        if client is None:
            orc.log("supervisor: client is None — triggering full reconnect")
            state.set("RECOVERING", "BLE disconnected")
            await _do_reconnect(
                client_holder, rx_holder, sink_holder, ka_stop_holder,
                bed_proc_holder, state, classic_addr, volume, fd01_keepalive,
                server_holder, bt_mac, full=True,
            )
            continue

        ble_ok = client.is_connected if hasattr(client, 'is_connected') else True
        if not ble_ok:
            orc.log("supervisor: BLE disconnected — triggering full reconnect")
            state.set("RECOVERING", "BLE disconnected")
            await _do_reconnect(
                client_holder, rx_holder, sink_holder, ka_stop_holder,
                bed_proc_holder, state, classic_addr, volume, fd01_keepalive,
                server_holder, bt_mac, full=True,
            )
            continue

        # E5 health check
        e5_ok = await health_check_ble(client, rx_holder[0])
        if not e5_ok:
            orc.log("supervisor: E5 health check failed — triggering full reconnect")
            state.set("RECOVERING", "BLE E5 health check failed")
            await _do_reconnect(
                client_holder, rx_holder, sink_holder, ka_stop_holder,
                bed_proc_holder, state, classic_addr, volume, fd01_keepalive,
                server_holder, bt_mac, full=True,
            )
            continue

        # Check classic
        classic_ok = await health_check_classic(bt_mac)
        if not classic_ok:
            orc.log("supervisor: classic disconnected — attempting classic-only reconnect")
            state.set("RECOVERING", "classic BT disconnected")
            await _do_reconnect(
                client_holder, rx_holder, sink_holder, ka_stop_holder,
                bed_proc_holder, state, classic_addr, volume, fd01_keepalive,
                server_holder, bt_mac, full=False,
            )
            continue

        # Check bed
        if not health_check_bed(bed_proc_holder[0]):
            orc.log("supervisor: noise bed died — restarting")
            try:
                bed_proc_holder[0] = start_noise_bed(sink_holder[0])
            except Exception as e:
                orc.log(f"supervisor: noise bed restart failed: {e}")
                state.set("FAILED", f"noise bed restart failed: {e}")

    orc.log("supervisor: shutting down")


async def _do_reconnect(
    client_holder, rx_holder, sink_holder, ka_stop_holder,
    bed_proc_holder, state, classic_addr, volume, fd01_keepalive,
    server_holder, bt_mac, full: bool,
):
    """Execute reconnection with exponential backoff."""
    state.reconnect_count += 1

    for i, delay in enumerate(BACKOFF_SCHEDULE):
        if state.state == "SHUTTING_DOWN":
            return

        orc.log(f"supervisor: reconnect attempt {i + 1}/{len(BACKOFF_SCHEDULE)} "
                f"{'(full)' if full else '(classic-only)'}, waiting {delay}s …")
        await asyncio.sleep(delay)

        if state.state == "SHUTTING_DOWN":
            return

        try:
            old_bed = bed_proc_holder[0]
            if old_bed is not None and old_bed.poll() is None:
                old_bed.terminate()
            bed_proc_holder[0] = None
            if full and client_holder[0] is not None:
                try:
                    await client_holder[0].disconnect()
                except Exception:
                    pass
                client_holder[0] = None
            if full:
                result = await attempt_full_reconnect(
                    classic_addr, volume, fd01_keepalive,
                )
                if result:
                    client, rx, sink, ka_stop_event = result
                    # Update holders
                    client_holder[0] = client
                    rx_holder[0] = rx
                    sink_holder[0] = sink
                    ka_stop_holder[0] = ka_stop_event
                    bed_proc_holder[0] = start_noise_bed(sink)
                    state.set("READY")
                    orc.log("supervisor: full reconnect SUCCESS ✅")
                    return
            else:
                # Classic-only reconnect (BLE still alive)
                ok = await attempt_reconnect_classic(
                    client_holder[0], rx_holder[0], classic_addr, ka_stop_holder[0],
                )
                if ok:
                    bt_mac_norm = orc._bt_mac(classic_addr)
                    sink = await asyncio.get_running_loop().run_in_executor(
                        None, orc.wait_for_bt_sink, bt_mac_norm, 12.0,
                    )
                    if sink:
                        sink_holder[0] = sink
                        bed_proc_holder[0] = start_noise_bed(sink)
                        state.set("READY")
                        orc.log("supervisor: classic reconnect SUCCESS ✅")
                        return
        except Exception as e:
            orc.log(f"supervisor: reconnect attempt {i + 1} failed: {e}")

    state.set("FAILED", "all reconnect attempts exhausted")
    orc.log("supervisor: FAILED — all reconnect attempts exhausted ❌")


# ── Main ────────────────────────────────────────────────────────────────────

async def daemon_main(
    classic_addr: str | None = None,
    volume: int = 100,
    head_motion: bool = True,
    fd01_keepalive: bool = DEFAULT_FD01_KEEPALIVE,
    socket_path: str = SOCKET_PATH,
):
    """Main daemon entry point."""
    global SOCKET_PATH
    if socket_path != SOCKET_PATH:
        SOCKET_PATH = socket_path

    state = DaemonState()

    # Mutable holders so supervisor can update references
    client_holder: list = [None]
    rx_holder: list = [None]
    sink_holder: list = [None]
    ka_stop_holder: list = [None]
    bed_proc_holder: list = [None]
    server_holder: list = [None]

    classic_addr = classic_addr or orc.CLASSIC_DEFAULT_ADDR

    # Startup
    try:
        client, rx, sink, ka_stop_event = await daemon_startup(
            state, classic_addr, volume, fd01_keepalive,
        )
    except Exception as e:
        orc.log(f"daemon: startup failed: {e}")
        state.set("FAILED", str(e))
        return 1

    client_holder[0] = client
    rx_holder[0] = rx
    sink_holder[0] = sink
    ka_stop_holder[0] = ka_stop_event

    # Start noise bed
    try:
        bed_proc_holder[0] = start_noise_bed(sink)
    except Exception as e:
        orc.log(f"daemon: noise bed failed to start: {e}")
        state.set("FAILED", f"noise bed: {e}")
        await daemon_shutdown(client, rx, ka_stop_event, None, classic_addr)
        return 1

    # Start socket server
    try:
        server = await socket_server(
            client_holder, rx_holder, sink_holder, state, classic_addr, head_motion, volume,
            ka_stop_event, bed_proc_holder,
        )
        server_holder[0] = server
    except Exception as e:
        orc.log(f"daemon: socket server failed: {e}")
        state.set("FAILED", f"socket: {e}")
        await daemon_shutdown(client, rx, ka_stop_event, bed_proc_holder[0], classic_addr)
        return 1

    # Shutdown signal handler
    shutdown_event = asyncio.Event()

    def _signal_handler():
        orc.log("daemon: received shutdown signal")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    # Start supervisor (runs in background)
    sup_task = asyncio.create_task(
        supervisor_loop(
            client_holder, rx_holder, sink_holder, ka_stop_holder,
            bed_proc_holder, state, classic_addr, volume, fd01_keepalive,
            server_holder,
        )
    )

    # Wait for shutdown signal
    orc.log("daemon: running (send SIGTERM/SIGINT or 'shutdown' to stop)")
    await shutdown_event.wait()

    # Graceful shutdown
    state.set("SHUTTING_DOWN")
    orc.log("daemon: graceful shutdown …")

    # Cancel supervisor
    sup_task.cancel()
    try:
        await sup_task
    except asyncio.CancelledError:
        pass

    # Close socket server
    server.close()
    await server.wait_closed()

    # Clean up socket file
    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)

    # Shutdown BLE + classic
    await daemon_shutdown(
        client, rx, ka_stop_event, bed_proc_holder[0], classic_addr,
    )

    orc.log("daemon: stopped ✅")
    return 0


def main():
    p = argparse.ArgumentParser(description="12ft Skelly persistent daemon")
    p.add_argument("--classic-addr", default=None,
                   help=f"Classic BT MAC (default {orc.CLASSIC_DEFAULT_ADDR})")
    p.add_argument("--volume", type=int, default=100, help="FA volume 0-100 (default 100)")
    p.add_argument("--no-head", action="store_true", help="Skip head motion during clips")
    p.add_argument("--fd01-keepalive", action="store_true",
                   help="Enable periodic FD 01 resend (default: off, E5 health checks only)")
    p.add_argument("--socket", default=SOCKET_PATH, help=f"UNIX socket path (default {SOCKET_PATH})")
    a = p.parse_args()

    orc.log("=== SKELLY DAEMON ===")
    rc = asyncio.run(daemon_main(
        classic_addr=a.classic_addr,
        volume=a.volume,
        head_motion=not a.no_head,
        fd01_keepalive=a.fd01_keepalive,
        socket_path=a.socket,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
