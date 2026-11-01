#!/usr/bin/env bash
# skelly_say.sh — text → Ming TTS on the DGX Spark → Raspberry Pi → Skelly (live A2DP).
# Usage: ./skelly_say.sh "Happy Halloween, mortals." [--tempo 0.65]
# Runs on the Mac: Spark and Pi both over Tailscale (192.168.100.10 is the Spark's
# wired link to old-glados only; see spark/README.md for the faster jump-host path).
# Requires ming-tts.service running on the Spark.
set -euo pipefail
KEY=~/.ssh/id_ed25519_glados
SPARK=${SKELLY_SPARK:-rich@100.88.44.116}
PI=${SKELLY_PI:-rich@100.84.5.92}
TEXT="${1:?usage: skelly_say.sh \"text\" [--tempo T]}"; shift || true
TEMPO_ARGS=()
if [[ "${1:-}" == "--tempo" ]]; then TEMPO_ARGS=(--tempo "$2"); fi
TMP=$(mktemp -t skelly).wav
# Keep the WAV until exit: the daemon-failure fallback re-sends it. ${TMP%.wav} is mktemp's own (empty) file.
trap 'rm -f "$TMP" "${TMP%.wav}"' EXIT
NAME="say_$(date +%H%M%S).wav"
t0=$(date +%s)
# shellcheck disable=SC2029
ssh -i "$KEY" "$SPARK" "~/ming-venv/bin/python ~/skelly-spark/ming_client.py $(printf '%q' "$TEXT") ${TEMPO_ARGS[*]:-}" > "$TMP"
echo "[skelly_say] TTS $(ffprobe -v error -show_entries format=duration -of csv=p=0 "$TMP")s audio in $(( $(date +%s) - t0 ))s"
# Fast path: use the persistent daemon if the socket exists
if ssh -i "$KEY" -o ConnectTimeout=5 "$PI" 'test -S /tmp/skelly.sock' 2>/dev/null; then
  echo "[skelly_say] daemon socket detected — fast path"
  scp -q -i "$KEY" "$TMP" "$PI:/tmp/$NAME"
  RESP=$(ssh -i "$KEY" -o ConnectTimeout=5 "$PI" \
    "~/skelly-venv/bin/python ~/skelly_client.py play --prenorm /tmp/$NAME 2>&1")
  echo "$RESP"
  ssh -i "$KEY" "$PI" "rm -f /tmp/$NAME"
  # Check for ok=false in response
  if echo "$RESP" | grep -q '"ok": false'; then
    echo "[skelly_say] daemon play FAILED — falling back to orchestrator"
    scp -q -i "$KEY" "$TMP" "$PI:/tmp/$NAME"
    ssh -i "$KEY" "$PI" "timeout 300 ~/skelly-venv/bin/python ~/skelly_orchestrator.py --mode live /tmp/$NAME 2>&1 | grep -v -i userwarn | grep -v warnings.warn; rm -f /tmp/$NAME"
    echo "[skelly_say] total $(( $(date +%s) - t0 ))s"
    exit 0
  fi
else
  echo "[skelly_say] no daemon socket — one-shot orchestrator"
  scp -q -i "$KEY" "$TMP" "$PI:/tmp/$NAME"
  ssh -i "$KEY" "$PI" "timeout 300 ~/skelly-venv/bin/python ~/skelly_orchestrator.py --mode live /tmp/$NAME 2>&1 | grep -v -i userwarn | grep -v warnings.warn; rm -f /tmp/$NAME"
fi
echo "[skelly_say] total $(( $(date +%s) - t0 ))s"
