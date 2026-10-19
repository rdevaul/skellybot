"""TTS via Kokoro-82M on old GLaDOS (192.168.4.241) — keeps the Spark's RAM free.

Usage: python tts_glados.py "text" > out.wav
Requires: SSH key auth to old GLaDOS (id_ed25519_glados).
"""
import subprocess, sys, tempfile
from pathlib import Path

GLADOS = "rich@192.168.4.241"
KEY = str(Path.home() / ".ssh/id_ed25519_glados")
VOICE = "af_heart"

def synth(text: str, voice: str = VOICE) -> bytes:
    """Synthesize on old GLaDOS, return WAV bytes."""
    remote = f"/tmp/tts_{abs(hash(text))}.wav"
    b64 = __import__("base64").b64encode(text.encode()).decode()
    cmd = f"""cd ~/tts && ./venv/bin/python - <<PYEOF
import base64, kokoro_onnx, soundfile as sf
text = base64.b64decode("{b64}").decode()
k = kokoro_onnx.Kokoro("kokoro-v1.0.onnx", "voices.bin")
samples, sr = k.create(text, voice="{voice}")
sf.write("{remote}", samples, sr)
PYEOF"""
    subprocess.run(["ssh", "-i", KEY, "-o", "BatchMode=yes", GLADOS, cmd], check=True)
    wav = subprocess.run(
        ["ssh", "-i", KEY, "-o", "BatchMode=yes", GLADOS, f"base64 {remote}"],
        check=True, capture_output=True,
    ).stdout
    subprocess.run(["ssh", "-i", KEY, GLADOS, f"rm -f {remote}"], check=False)
    import base64
    return base64.b64decode(wav)

if __name__ == "__main__":
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/tts_out.wav"
    Path(out).write_bytes(synth(" ".join(sys.argv[1:])))
    print(out)
