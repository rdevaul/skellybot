#!/usr/bin/env python3
"""skelly_client — tiny CLI for talking to the skelly_daemon UNIX socket.

Usage:
  skelly_client.py play /path/to/audio.wav [--no-head]
  skelly_client.py status
  skelly_client.py stop
  skelly_client.py shutdown

Prints JSON reply to stdout. Exits nonzero if ok=false.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile

SOCKET_PATH = os.environ.get("SKELLY_SOCKET", "/tmp/skelly.sock")


async def send_request(cmd: str, path: str | None = None, head: bool = True) -> dict:
    """Connect to the daemon socket, send one JSON request, return the reply."""
    if not os.path.exists(SOCKET_PATH):
        return {"ok": False, "error": f"socket not found: {SOCKET_PATH} (is the daemon running?)"}

    req = {"cmd": cmd}
    if cmd == "play":
        req["path"] = path
        req["head"] = head

    reader, writer = await asyncio.open_unix_connection(SOCKET_PATH)
    try:
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()

        data = await asyncio.wait_for(reader.read(4096), timeout=60.0 if cmd == "play" else 5.0)
        reply = data.decode().strip()
        return json.loads(reply) if reply else {"ok": False, "error": "empty reply"}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def play_file(audio_path: str, head: bool = True) -> int:
    """Play a file: if remote (scp needed), copy first, then request play."""
    abs_path = os.path.abspath(audio_path)
    if not os.path.exists(abs_path):
        print(json.dumps({"ok": False, "error": f"file not found: {abs_path}"}))
        return 1

    resp = await send_request("play", abs_path, head)
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


async def status() -> int:
    resp = await send_request("status")
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


async def stop() -> int:
    resp = await send_request("stop")
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


async def shutdown() -> int:
    resp = await send_request("shutdown")
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


def main():
    p = argparse.ArgumentParser(description="Skelly daemon client")
    sub = p.add_subparsers(dest="command")

    p_play = sub.add_parser("play", help="Play an audio file")
    p_play.add_argument("file", help="Path to audio file")
    p_play.add_argument("--no-head", action="store_true", help="Skip head motion")

    sub.add_parser("status", help="Get daemon status")
    sub.add_parser("stop", help="Stop current playback")
    sub.add_parser("shutdown", help="Shut down the daemon")

    a = p.parse_args()

    if a.command == "play":
        sys.exit(asyncio.run(play_file(a.file, head=not a.no_head)))
    elif a.command == "status":
        sys.exit(asyncio.run(status()))
    elif a.command == "stop":
        sys.exit(asyncio.run(stop()))
    elif a.command == "shutdown":
        sys.exit(asyncio.run(shutdown()))
    else:
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
