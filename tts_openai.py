"""TTS via OpenAI gpt-4o-mini-tts — cloud-side, zero local inference.

Default voice for skelly_speak. Set SKELLY_TTS_VOICE to change.
"""
import os
import subprocess
import tempfile
from pathlib import Path

API_URL = "https://api.openai.com/v1/audio/speech"
MODEL = "gpt-4o-mini-tts"
VOICE = os.environ.get("SKELLY_TTS_VOICE", "onyx")


def synth_mp3(text: str, voice: str = VOICE) -> bytes:
    """Return Skelly-ready MP3 bytes (64 kbps mono CBR @ 44.1 kHz)."""
    key = os.environ["OPENAI_API_KEY"]
    raw = tempfile.mktemp(suffix=".mp3")
    subprocess.run(
        ["curl", "-sf", API_URL,
         "-H", f"Authorization: Bearer {key}",
         "-H", "Content-Type: application/json",
         "-d", __import__("json").dumps(
             {"model": MODEL, "voice": voice, "input": text,
              "response_format": "mp3"}),
         "-o", raw],
        check=True,
    )
    # device wants 64k mono CBR; API returns 24k mono — transcode to spec
    dev = tempfile.mktemp(suffix=".mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", raw, "-ac", "1", "-ar", "44100",
         "-b:a", "64k", "-c:a", "libmp3lame", "-f", "mp3", dev],
        check=True,
    )
    data = Path(dev).read_bytes()
    Path(raw).unlink(missing_ok=True)
    Path(dev).unlink(missing_ok=True)
    return data
