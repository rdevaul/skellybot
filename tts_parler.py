"""Parler-TTS local dramatic voice — zero cloud, zero censorship, full theatrical control.

Runs on the local Mac (M4, 24 GB). Model downloads from HF on first use (~2.3 GB),
then works entirely offline.
"""
from __future__ import annotations

import os
import tempfile
import subprocess
from pathlib import Path

import torch
from parler_tts import ParlerTTSForConditionalGeneration, ParlerTTSStreamer
from transformers import AutoTokenizer
import soundfile as sf

MODEL = "parler-tts/parler-tts-mini-v1"
DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"

_model = _tokenizer = None

def _load():
    global _model, _tokenizer
    if _model is None:
        _tokenizer = AutoTokenizer.from_pretrained(MODEL)
        _model = ParlerTTSForConditionalGeneration.from_pretrained(MODEL).to(DEVICE)
    return _model, _tokenizer

DEFAULT_DESC = (
    "A deep, resonant male voice with menacing gravitas — slow, deliberate delivery, "
    "theatrical villain quality, low register, with a subtle echoing quality as if spoken "
    "from inside a hollow ribcage."
)

def synth(text: str, description: str = DEFAULT_DESC, speed: str = "slow") -> bytes:
    """Return Skelly-ready MP3 bytes (64 kbps mono CBR @ 44.1 kHz)."""
    model, tok = _load()
    input_ids = tok(description, return_tensors="pt").input_ids.to(DEVICE)
    prompt_ids = tok(text, return_tensors="pt").input_ids.to(DEVICE)
    streamer = ParlerTTSStreamer(model, device=DEVICE)
    out = model.generate(input_ids=input_ids, prompt_ids=prompt_ids, streamer=streamer)
    wav = out.cpu().numpy().squeeze()
    raw = tempfile.mktemp(suffix=".wav")
    sf.write(raw, wav, model.config.sampling_rate)
    # transcode to device spec
    dev = tempfile.mktemp(suffix=".mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", raw,
         "-ac", "1", "-ar", "44100", "-b:a", "64k", "-c:a", "libmp3lame", "-f", "mp3", dev],
        check=True,
    )
    data = Path(dev).read_bytes()
    Path(raw).unlink(missing_ok=True)
    Path(dev).unlink(missing_ok=True)
    return data

if __name__ == "__main__":
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else "The singularity is here."
    data = synth(text)
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/parler_test.mp3"
    Path(out).write_bytes(data)
    print(f"synthesized {len(data)} bytes → {out}")
