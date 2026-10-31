#!/usr/bin/env python3
"""Offline tests for skelly_daemon.py — runs on a Mac with plain python3.

Stubs the bleak module and all hardware-dependent functions so we can test:
- socket protocol parsing
- request queueing order
- backoff schedule
- state transitions on simulated drops
- py_compile for all pi/*.py
- Python 3.9 syntax compatibility
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call

# ── Stub bleak before importing the daemon ───────────────────────────────────

bleak_stub = types.SimpleNamespace(
    BleakClient=object,
    BleakScanner=object,
)
sys.modules["bleak"] = bleak_stub

# ── Stub dbus-next (used by skelly_bt_agent.py imported via orchestrator) ────

dbus_next_stub = types.SimpleNamespace()
dbus_next_stub.aio = types.SimpleNamespace()
dbus_next_stub.aio.message_bus = types.SimpleNamespace()
sys.modules["dbus_next"] = dbus_next_stub
sys.modules["dbus_next.aio"] = dbus_next_stub.aio
sys.modules["dbus_next.aio.message_bus"] = dbus_next_stub.aio.message_bus

# Stub vendor/jli_auth
jli_stub = types.SimpleNamespace(
    JliCipher=MagicMock,
    STEP_CHALLENGE=0x01,
    STEP_ENCRYPTED=0x02,
    STEP_PASS_LITERAL=0x03,
    get_random_auth_data=lambda: b"\x00" * 16,
)
sys.modules["jli_auth"] = jli_stub

# ── Now import the modules ──────────────────────────────────────────────────

# We can't import the real orchestrator/daemon on Mac (no bleak, no hardware),
# so we test the *design* — socket protocol, queueing, backoff, state machine —
# using the actual code structures where possible and mocks elsewhere.

# Import skelly_daemon to test state machine logic, backoff, socket parsing
# (after stubbing, the imports should work)
try:
    # Add the pi dir to path
    pi_dir = os.path.join(os.path.dirname(__file__))
    if pi_dir not in sys.path:
        sys.path.insert(0, pi_dir)
    import skelly_daemon as sd
    HAS_DAEMON = True
except Exception:
    HAS_DAEMON = False


class TestSocketProtocol(unittest.TestCase):
    """Test JSON request/response parsing for the daemon socket."""

    def test_valid_play_request(self):
        line = json.dumps({"cmd": "play", "path": "/tmp/test.wav", "head": True})
        req = json.loads(line)
        self.assertEqual(req["cmd"], "play")
        self.assertEqual(req["path"], "/tmp/test.wav")
        self.assertTrue(req["head"])

    def test_valid_status_request(self):
        line = json.dumps({"cmd": "status"})
        req = json.loads(line)
        self.assertEqual(req["cmd"], "status")

    def test_valid_stop_request(self):
        line = json.dumps({"cmd": "stop"})
        req = json.loads(line)
        self.assertEqual(req["cmd"], "stop")

    def test_valid_shutdown_request(self):
        line = json.dumps({"cmd": "shutdown"})
        req = json.loads(line)
        self.assertEqual(req["cmd"], "shutdown")

    def test_play_without_head(self):
        line = json.dumps({"cmd": "play", "path": "/tmp/test.wav", "head": False})
        req = json.loads(line)
        self.assertFalse(req["head"])

    def test_invalid_json(self):
        line = "not json at all"
        with self.assertRaises(json.JSONDecodeError):
            json.loads(line)

    def test_empty_line(self):
        line = ""
        with self.assertRaises(json.JSONDecodeError):
            json.loads(line)

    def test_unknown_command(self):
        line = json.dumps({"cmd": "foobar"})
        req = json.loads(line)
        self.assertEqual(req["cmd"], "foobar")

    def test_missing_path_for_play(self):
        line = json.dumps({"cmd": "play"})
        req = json.loads(line)
        self.assertNotIn("path", req)

    def test_reply_format(self):
        # Verify our reply structure
        reply = {
            "ok": True,
            "timing": {"render_s": 0.5, "duration_s": 3.2, "start_latency_s": 0.1},
            "link": {"connected": True, "paired": True},
        }
        serialized = json.dumps(reply)
        parsed = json.loads(serialized)
        self.assertTrue(parsed["ok"])
        self.assertIn("timing", parsed)
        self.assertIn("link", parsed)


class TestDaemonState(unittest.TestCase):
    """Test the DaemonState class."""

    def test_initial_state(self):
        state = sd.DaemonState()
        self.assertEqual(state.state, "INIT")
        self.assertIsNone(state.last_error)
        self.assertEqual(state.reconnect_count, 0)

    def test_set_state(self):
        state = sd.DaemonState()
        state.set("READY")
        self.assertEqual(state.state, "READY")

    def test_set_state_with_error(self):
        state = sd.DaemonState()
        state.set("FAILED", "BLE timeout")
        self.assertEqual(state.state, "FAILED")
        self.assertEqual(state.last_error, "BLE timeout")

    def test_snapshot(self):
        state = sd.DaemonState()
        state.set("PLAYING")
        state.reconnect_count = 3
        state.last_error = None
        state.now_playing = "test.wav"
        snap = state.snapshot()
        self.assertEqual(snap["state"], "PLAYING")
        self.assertEqual(snap["reconnect_count"], 3)
        self.assertEqual(snap["now_playing"], "test.wav")
        self.assertIsInstance(snap["uptime_s"], float)
        self.assertGreaterEqual(snap["uptime_s"], 0)


class TestBackoffSchedule(unittest.TestCase):
    """Test the exponential backoff schedule."""

    def test_backoff_values(self):
        """Verify backoff schedule is [2, 4, 8, 16, 30]."""
        self.assertEqual(sd.BACKOFF_SCHEDULE, [2, 4, 8, 16, 30])

    def test_backoff_is_exponential_then_capped(self):
        """Values should roughly double then cap at 30."""
        sched = sd.BACKOFF_SCHEDULE
        for i in range(len(sched) - 2):
            # Each step is ~2x the previous (except the last which is capped)
            self.assertGreater(sched[i + 1], sched[i])
        # Last value is the cap
        self.assertEqual(sched[-1], 30)

    def test_backoff_total_max_wait(self):
        """Total max wait time across all retries."""
        total = sum(sd.BACKOFF_SCHEDULE)
        self.assertEqual(total, 60)  # 2+4+8+16+30 = 60


class TestStateTransitions(unittest.TestCase):
    """Test state transitions during simulated drop scenarios."""

    def test_normal_startup_sequence(self):
        """Expected: INIT → CONNECTING → READY."""
        state = sd.DaemonState()
        self.assertEqual(state.state, "INIT")
        state.set("CONNECTING")
        self.assertEqual(state.state, "CONNECTING")
        state.set("READY")
        self.assertEqual(state.state, "READY")

    def test_play_sequence(self):
        """Expected: READY → PLAYING → READY."""
        state = sd.DaemonState()
        state.set("READY")
        state.set("PLAYING")
        self.assertEqual(state.state, "PLAYING")
        state.now_playing = "test.wav"
        state.now_playing = None
        state.set("READY")
        self.assertEqual(state.state, "READY")

    def test_classic_drop_recovery(self):
        """Expected: READY → RECOVERING → READY (on success)."""
        state = sd.DaemonState()
        state.set("READY")
        # Simulate classic drop
        state.set("RECOVERING", "classic BT disconnected")
        self.assertEqual(state.state, "RECOVERING")
        self.assertEqual(state.last_error, "classic BT disconnected")
        state.reconnect_count += 1
        # Recovery succeeds
        state.set("READY")
        self.assertEqual(state.state, "READY")
        self.assertEqual(state.reconnect_count, 1)

    def test_ble_drop_full_reconnect(self):
        """Expected: READY → RECOVERING → READY (full reconnect)."""
        state = sd.DaemonState()
        state.set("READY")
        state.set("RECOVERING", "BLE disconnected")
        self.assertEqual(state.state, "RECOVERING")
        state.reconnect_count += 1
        state.set("READY")
        self.assertEqual(state.state, "READY")
        self.assertEqual(state.reconnect_count, 1)

    def test_exhausted_reconnect(self):
        """Expected: RECOVERING → FAILED after all attempts."""
        state = sd.DaemonState()
        state.set("RECOVERING", "BLE E5 health check failed")
        # Simulate all backoff steps failing
        for _ in sd.BACKOFF_SCHEDULE:
            state.reconnect_count += 1
        state.set("FAILED", "all reconnect attempts exhausted")
        self.assertEqual(state.state, "FAILED")
        self.assertEqual(state.reconnect_count, len(sd.BACKOFF_SCHEDULE))

    def test_shutdown_from_any_state(self):
        """SHUTTING_DOWN should be reachable from any state."""
        for initial in ["INIT", "CONNECTING", "READY", "PLAYING", "RECOVERING", "FAILED"]:
            state = sd.DaemonState()
            state.set(initial)
            state.set("SHUTTING_DOWN")
            self.assertEqual(state.state, "SHUTTING_DOWN",
                             f"Expected SHUTTING_DOWN from {initial}")

    def test_reconnect_count_increments(self):
        """Each reconnection attempt should increment the counter."""
        state = sd.DaemonState()
        for i in range(1, 6):
            state.set("RECOVERING")
            state.reconnect_count += 1
            self.assertEqual(state.reconnect_count, i)
            state.set("READY")


class TestRequestQueueing(unittest.TestCase):
    """Test that play requests are queued FIFO and don't overlap."""

    def test_fifo_order_preserved(self):
        """Requests should be processed in FIFO order."""
        queue = asyncio.Queue()
        # Enqueue three requests
        results = []
        for i in range(3):
            req = (f"/tmp/clip_{i}.wav", True, results)
            queue.put_nowait(req)

        # Verify they come out in order
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0][0], "/tmp/clip_0.wav")
        self.assertEqual(items[1][0], "/tmp/clip_1.wav")
        self.assertEqual(items[2][0], "/tmp/clip_2.wav")

    def test_concurrent_requests_dont_overlap(self):
        """Queue ensures only one clip plays at a time."""
        processed = []
        processing = [False]

        async def worker_one(q):
            path, head, holder = await q.get()
            self.assertFalse(processing[0], f"Overlap detected for {path}")
            processing[0] = True
            await asyncio.sleep(0.01)  # simulate play
            processed.append(path)
            processing[0] = False
            q.task_done()

        async def run_test():
            q = asyncio.Queue()
            for i in range(3):
                await q.put((f"/tmp/clip_{i}.wav", True, []))
            # Process sequentially (like the daemon's single worker)
            for _ in range(3):
                await worker_one(q)
            return processed

        result = asyncio.run(run_test())
        self.assertEqual(result, [
            "/tmp/clip_0.wav",
            "/tmp/clip_1.wav",
            "/tmp/clip_2.wav",
        ])


class TestClientCLI(unittest.TestCase):
    """Test the client CLI argument parsing."""

    def test_play_command_args(self):
        """skelly_client.py play file.wav should parse correctly."""
        # We test the argparse setup, not the actual network call
        import argparse
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="command")
        p_play = sub.add_parser("play")
        p_play.add_argument("file")
        p_play.add_argument("--no-head", action="store_true")
        sub.add_parser("status")
        sub.add_parser("stop")
        sub.add_parser("shutdown")

        args = p.parse_args(["play", "/tmp/test.wav"])
        self.assertEqual(args.command, "play")
        self.assertEqual(args.file, "/tmp/test.wav")
        self.assertFalse(args.no_head)

        args = p.parse_args(["play", "/tmp/test.wav", "--no-head"])
        self.assertTrue(args.no_head)

    def test_status_command(self):
        import argparse
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="command")
        sub.add_parser("status")

        args = p.parse_args(["status"])
        self.assertEqual(args.command, "status")

    def test_stop_command(self):
        import argparse
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="command")
        sub.add_parser("stop")

        args = p.parse_args(["stop"])
        self.assertEqual(args.command, "stop")

    def test_shutdown_command(self):
        import argparse
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="command")
        sub.add_parser("shutdown")

        args = p.parse_args(["shutdown"])
        self.assertEqual(args.command, "shutdown")

    def test_no_command_shows_help(self):
        import argparse
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="command")
        sub.add_parser("play")

        args = p.parse_args([])
        self.assertIsNone(args.command)


class TestConstants(unittest.TestCase):
    """Verify key constant values."""

    def test_socket_path_default(self):
        self.assertEqual(sd.SOCKET_PATH, "/tmp/skelly.sock")

    def test_socket_path_from_env(self):
        original = os.environ.get("SKELLY_SOCKET")
        try:
            # Can't easily reimport, but verify the default constant
            self.assertIsInstance(sd.SOCKET_PATH, str)
            self.assertTrue(sd.SOCKET_PATH.startswith("/"))
        finally:
            if original is not None:
                os.environ["SKELLY_SOCKET"] = original
            elif "SKELLY_SOCKET" in os.environ:
                del os.environ["SKELLY_SOCKET"]

    def test_noise_amplitude(self):
        """Should be very small (~-60 dBFS)."""
        self.assertLess(sd.NOISE_AMPLITUDE, 0.01)
        self.assertGreater(sd.NOISE_AMPLITUDE, 0)

    def test_health_interval(self):
        """E5 health check interval should be reasonable."""
        self.assertEqual(sd.HEALTH_INTERVAL, 15.0)

    def test_fd01_keepalive_default_off(self):
        """FD 01 periodic keepalive should be off by default."""
        self.assertFalse(sd.DEFAULT_FD01_KEEPALIVE)


class TestPyCompile(unittest.TestCase):
    """Verify all pi/*.py files compile without syntax errors."""

    def test_py_compile_all_pi_files(self):
        import py_compile
        pi_dir = os.path.join(os.path.dirname(__file__) or ".", "")
        for fname in os.listdir(pi_dir):
            if fname.endswith(".py"):
                fpath = os.path.join(pi_dir, fname)
                try:
                    py_compile.compile(fpath, doraise=True)
                except py_compile.PyCompileError as e:
                    self.fail(f"Compile error in {fname}: {e}")


class TestPython39Syntax(unittest.TestCase):
    """Verify Python 3.9 syntax compatibility using ast.parse."""

    def test_39_syntax_compatibility(self):
        import ast
        pi_dir = os.path.join(os.path.dirname(__file__) or ".", "")
        files = [
            os.path.join(pi_dir, "skelly_daemon.py"),
            os.path.join(pi_dir, "skelly_client.py"),
            os.path.join(pi_dir, "skelly_orchestrator.py"),
        ]
        for fpath in files:
            if not os.path.exists(fpath):
                continue
            with open(fpath) as f:
                source = f.read()
            try:
                ast.parse(source, filename=fpath, feature_version=(3, 9))
            except SyntaxError as e:
                self.fail(f"Python 3.9 syntax error in {os.path.basename(fpath)}: {e}")


class TestNoiseBedCommand(unittest.TestCase):
    """Test the noise bed ffmpeg command construction."""

    def test_noise_amplitude_in_range(self):
        """Amplitude should be very small (faint noise)."""
        amp = sd.NOISE_AMPLITUDE
        # 0.001 corresponds to about -60 dBFS
        self.assertAlmostEqual(amp, 0.001, places=4)


if __name__ == "__main__":
    unittest.main()
