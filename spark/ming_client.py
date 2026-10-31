#!/usr/bin/env python3
"""ming_client — talk to ming_server over its UNIX socket; write WAV to stdout or a file.

Usage: ming_client.py "text" [--tempo 0.65] [-o out.wav]
"""
import argparse
import json
import os
import socket
import struct
import sys

p = argparse.ArgumentParser()
p.add_argument("text")
p.add_argument("--tempo", type=float, default=None)
p.add_argument("--prompt", default=None)
p.add_argument("-o", "--out", default="-")
p.add_argument("--sock", default=os.path.expanduser(os.environ.get("MING_SOCK", "~/ming.sock")))
a = p.parse_args()

req = {"text": a.text}
if a.tempo is not None:
    req["tempo"] = a.tempo
if a.prompt:
    req["prompt"] = a.prompt

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(180)
s.connect(a.sock)
s.sendall(json.dumps(req).encode() + b"\n")


def recv_exact(n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(min(65536, n - len(buf)))
        if not chunk:
            raise SystemExit("ming_client: connection closed early")
        buf += chunk
    return buf


(n,) = struct.unpack(">Q", recv_exact(8))
if n == 0:
    err = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        err += chunk
    sys.exit(f"ming_server error: {err.decode(errors='replace')}")
wav = recv_exact(n)
if a.out == "-":
    sys.stdout.buffer.write(wav)
else:
    open(a.out, "wb").write(wav)
