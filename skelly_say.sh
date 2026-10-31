#!/usr/bin/env bash
# skelly_say.sh — text → Ming TTS on the DGX Spark → Raspberry Pi → Skelly (live A2DP).
# Usage: ./skelly_say.sh "Happy Halloween, mortals." [--tempo 0.65]
# Runs on the Mac (the orchestrating host): Spark is reachable on the lab LAN,
# the Pi over Tailscale. Requires ming_server.py running on the Spark.
set -euo pipefail
KEY=~/.ssh/id_ed25519_glados
SPARK=${SKELLY_SPARK:-rich@192.168.100.10}
PI=${SKELLY_PI:-rich@100.84.5.92}
TEXT="${1:?usage: skelly_say.sh \"text\" [--tempo T]}"; shift || true
TEMPO_ARGS=()
if [[ "${1:-}" == "--tempo" ]]; then TEMPO_ARGS=(--tempo "$2"); fi
TMP=$(mktemp -t skelly).wav
NAME="say_$(date +%H%M%S).wav"
t0=$(date +%s)
# shellcheck disable=SC2029
ssh -i "$KEY" "$SPARK" "~/ming-venv/bin/python ~/skelly-spark/ming_client.py $(printf '%q' "$TEXT") ${TEMPO_ARGS[*]:-}" > "$TMP"
echo "[skelly_say] TTS $(ffprobe -v error -show_entries format=duration -of csv=p=0 "$TMP")s audio in $(( $(date +%s) - t0 ))s"
scp -q -i "$KEY" "$TMP" "$PI:/tmp/$NAME"
rm -f "$TMP"
ssh -i "$KEY" "$PI" "timeout 300 ~/skelly-venv/bin/python ~/skelly_orchestrator.py --mode live /tmp/$NAME 2>&1 | grep -v -i userwarn | grep -v warnings.warn; rm -f /tmp/$NAME"
echo "[skelly_say] total $(( $(date +%s) - t0 ))s"
