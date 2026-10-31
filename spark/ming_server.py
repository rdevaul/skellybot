#!/usr/bin/env python3
"""ming_server — persistent Ming (BailingMM) TTS worker for Skelly, runs on the DGX Spark.

Loads the model once (~30s) and serves requests over a UNIX socket (no TCP
listener → no new network surface). Clients reach it via SSH:

    ssh spark '~/ming-venv/bin/python ~/skelly-spark/ming_client.py "text"' > out.wav

Protocol: client sends one JSON line {"text": str, "tempo": float?, "prompt": str?};
server replies with an 8-byte big-endian length followed by WAV bytes
(length 0 => error; error text follows as UTF-8 until EOF).

Voice = Rich's pick (2026-10-28): zero speaker embedding + text-described
"deep, low-pitched male voice, dry sardonic delivery", slowed with
pitch-preserving atempo (~0.65).
"""
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

SRC = os.path.expanduser("~/ming-tts-src")
MODEL = os.path.expanduser("~/ming-tts-model")
SOCK = os.path.expanduser(os.environ.get("MING_SOCK", "~/ming.sock"))
DEFAULT_PROMPT = ("Please generate speech with a deep, low-pitched male voice "
                  "with a dry, sardonic delivery.\n")
DEFAULT_TEMPO = float(os.environ.get("MING_TEMPO", "0.65"))
MAX_TEXT = 600

sys.path.insert(0, SRC)
sys.path.insert(0, os.path.join(SRC, "cookbooks"))


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    t0 = time.time()
    os.chdir(SRC)  # cookbook loads sentence_manager/... relative to repo root
    from test import MingAudio  # noqa: E402  (Ming cookbook wrapper)
    model = MingAudio(MODEL)
    log(f"model loaded in {time.time() - t0:.1f}s")
    lock = threading.Lock()

    def synth(req: dict) -> bytes:
        text = str(req.get("text", "")).strip()[:MAX_TEXT]
        if not text:
            raise ValueError("empty text")
        tempo = float(req.get("tempo", DEFAULT_TEMPO))
        tempo = min(2.0, max(0.5, tempo))
        prompt = str(req.get("prompt") or DEFAULT_PROMPT)
        with tempfile.TemporaryDirectory(prefix="ming-") as td:
            raw = os.path.join(td, "raw.wav")
            out = os.path.join(td, "out.wav")
            steps = min(1200, 120 + 4 * len(text))
            t = time.time()
            with lock:
                model.speech_generation(
                    prompt=prompt, text=text,
                    use_zero_spk_emb=True, use_spk_emb=False,
                    prompt_wav_path=None, prompt_text=None, instruction=None,
                    max_decode_steps=steps, output_wav_path=raw)
            gen = time.time() - t
            af = f"atempo={tempo}" if abs(tempo - 1.0) > 1e-3 else "anull"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", raw, "-af", af,
                            "-ar", "44100", "-ac", "1", out], check=True, timeout=60)
            data = open(out, "rb").read()
            log(f"synth {len(text)} chars -> {len(data)} B in {gen:.2f}s (tempo {tempo})")
            return data

    def handle(conn):
        with conn:
            try:
                buf = b""
                while not buf.endswith(b"\n") and len(buf) < 16384:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                wav = synth(json.loads(buf.decode()))
                conn.sendall(struct.pack(">Q", len(wav)) + wav)
            except Exception as e:  # report, don't die
                log(f"error: {e}")
                try:
                    conn.sendall(struct.pack(">Q", 0) + str(e).encode())
                except OSError:
                    pass

    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)
    srv.listen(4)
    log(f"listening on {SOCK}")
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
