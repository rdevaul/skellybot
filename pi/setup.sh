#!/usr/bin/env bash
# setup.sh — Idempotent setup for Raspberry Pi OS Bookworm (12ft Skelly prop)
# Run as root or with sudo.
#
# Installs: bluez, pipewire, pipewire-pulse, libspa-0.2-bluetooth, wireplumber,
#           python3-venv, bleak, dbus-next
# Disables PulseAudio if present. Ensures bluetooth service is on.
# Creates a Python venv in the project directory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

log() { echo "[$(date +%H:%M:%S)] $*"; }

# ── Check root ──
if [[ $EUID -ne 0 ]]; then
    log "This script needs root. Run with sudo."
    exit 1
fi

# ── Detect OS ──
if ! grep -q "bookworm" /etc/os-release 2>/dev/null; then
    log "WARNING: This script targets Raspberry Pi OS Bookworm. You're on:"
    grep PRETTY_NAME /etc/os-release || true
    log "Continuing anyway, but package names may differ."
fi

# ── Install system packages ──
log "Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    bluez \
    pipewire \
    pipewire-pulse \
    pipewire-alsa \
    libspa-0.2-bluetooth \
    wireplumber \
    python3-venv \
    python3-pip \
    ffmpeg \
    pulseaudio-utils \
    bluetooth \
    bluez-tools \
    alsa-utils \
    || true

# ── Ensure bluetooth service is enabled and running ──
log "Ensuring bluetooth service is running..."
systemctl enable bluetooth
systemctl start bluetooth

# ── Disable PulseAudio (conflicts with PipeWire) ──
log "Disabling PulseAudio (if present)..."
systemctl disable pulseaudio 2>/dev/null || true
systemctl stop pulseaudio 2>/dev/null || true

# Mask pulseaudio socket units so they can't auto-start
systemctl --global mask pulseaudio.socket 2>/dev/null || true
systemctl mask pulseaudio.socket 2>/dev/null || true

# ── Ensure PipeWire + WirePlumber are running ──
log "Checking PipeWire BT backend..."

# For user services (the pi user runs these)
# These will be started when the user logs in; ensure the config is correct.
# If running as a specific user, switch to them for systemctl --user commands
PI_USER="${PI_USER:-pi}"

# Check if PipeWire bluetooth support files exist
if [ -f "/usr/share/pipewire/bluez-monitor.conf" ]; then
    log "PipeWire bluez-monitor.conf found ✅"
elif [ -f "/usr/share/wireplumber/bluez-monitor.conf" ]; then
    log "wireplumber bluez-monitor.conf found ✅"
else
    log "WARNING: PipeWire bluez-monitor.conf not found — BT audio may not work"
    log "  Check: apt list --installed | grep libspa-0.2-bluetooth"
fi

# ── REQUIRED: pin Bluetooth audio to 44.1 kHz SBC ──
# The Skelly produces NO audio at 48 kHz even though the A2DP transport looks
# active (silent failure). PipeWire defaults to 48k, so we override it.
# Installs for both WirePlumber 0.5+ (SPA-JSON .conf.d) and 0.4.x (Lua).
log "Installing WirePlumber 44.1 kHz SBC override (REQUIRED for Skelly)..."
PI_HOME="$(getent passwd "${PI_USER}" | cut -d: -f6 || true)"
PI_HOME="${PI_HOME:-/home/${PI_USER}}"

WP_CONF_DIR="${PI_HOME}/.config/wireplumber/wireplumber.conf.d"
mkdir -p "${WP_CONF_DIR}"
cat > "${WP_CONF_DIR}/51-skelly-44k.conf" <<'WPEOF'
# Skelly: force 44.1 kHz SBC A2DP (hardware is silent at 48 kHz)
monitor.bluez.rules = [
  {
    matches = [
      { "device.name" = "~bluez_card.*" }
    ]
    actions = {
      update-props = {
        bluez5.auto-connect = [ a2dp_sink ]
        bluez5.codecs = [ sbc ]
        bluez5.default.rate = 44100
        bluez5.default.channels = 2
      }
    }
  }
  {
    matches = [
      { "node.name" = "~bluez_output.*" }
    ]
    actions = {
      update-props = {
        audio.format = "S16LE"
        audio.rate = 44100
        audio.allowed-rates = [ 44100 ]
        resample.quality = 7
      }
    }
  }
]
WPEOF

# Legacy WirePlumber 0.4.x (Bookworm ships 0.4.13)
WP_LUA_DIR="${PI_HOME}/.config/wireplumber/bluetooth.lua.d"
mkdir -p "${WP_LUA_DIR}"
cat > "${WP_LUA_DIR}/51-skelly-44k.lua" <<'WPEOF'
-- Skelly: force 44.1 kHz SBC A2DP (hardware is silent at 48 kHz)
bluez_monitor.properties["bluez5.codecs"] = "[ sbc ]"
bluez_monitor.properties["bluez5.default.rate"] = 44100
bluez_monitor.properties["bluez5.default.channels"] = 2
table.insert(bluez_monitor.rules, {
  matches = { { { "node.name", "matches", "bluez_output.*" } } },
  apply_properties = {
    ["audio.format"] = "S16LE",
    ["audio.rate"] = 44100,
    ["audio.allowed-rates"] = "[ 44100 ]",
  },
})
WPEOF

# Pin PipeWire's global default clock to 44.1k as belt-and-braces
PW_CONF_DIR="${PI_HOME}/.config/pipewire/pipewire.conf.d"
mkdir -p "${PW_CONF_DIR}"
cat > "${PW_CONF_DIR}/51-skelly-44k.conf" <<'PWEOF'
context.properties = {
  default.clock.rate          = 44100
  default.clock.allowed-rates = [ 44100 ]
}
PWEOF
chown -R "${PI_USER}:${PI_USER}" "${PI_HOME}/.config/wireplumber" "${PI_HOME}/.config/pipewire" 2>/dev/null || true
log "44.1 kHz override installed ✅ (restart: systemctl --user restart wireplumber pipewire pipewire-pulse)"

# ── Create Python venv ──
log "Setting up Python venv at ${VENV_DIR}..."
if [ ! -d "${VENV_DIR}" ]; then
    python3 -m venv "${VENV_DIR}"
    log "venv created ✅"
else
    log "venv already exists, skipping"
fi

# ── Install Python packages ──
log "Installing Python packages into venv..."
"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
"${VENV_DIR}/bin/pip" install bleak dbus-next --quiet
log "Python packages installed ✅"

# ── Final checks ──
log ""
log "=== Setup Summary ==="
log "BlueZ version: $(bluetoothd -v 2>/dev/null || echo 'unknown')"
log "PipeWire: $(pipewire --version 2>/dev/null || echo 'not found in PATH')"
log "Python venv: ${VENV_DIR}"
log "bleak: $("${VENV_DIR}/bin/python3" -c 'import bleak; print(bleak.__version__)' 2>/dev/null || echo 'unknown')"
log "dbus-next: $("${VENV_DIR}/bin/python3" -c 'import dbus_next; print("ok")' 2>/dev/null || echo 'unknown')"

log ""
log "=== Post-Setup Notes ==="
log "1. Add your user to the bluetooth group:"
log "   sudo usermod -aG bluetooth ${PI_USER}"
log ""
log "2. Reboot or restart bluetooth:"
log "   sudo systemctl restart bluetooth"
log ""
log "3. (Re)start PipeWire services as the pi user (REQUIRED to load 44.1k override):"
log "   systemctl --user restart pipewire pipewire-pulse wireplumber"
log "   verify after connect: pactl list sinks | grep -A20 bluez_output | grep -i sample  # must say 44100Hz"
log ""
log "4. Run the orchestrator:"
log "   ${VENV_DIR}/bin/python3 ${SCRIPT_DIR}/skelly_orchestrator.py --test-head 5"
log ""
log "5. Full session with audio:"
log "   ${VENV_DIR}/bin/python3 ${SCRIPT_DIR}/skelly_orchestrator.py /path/to/audio.wav"
log ""
log "Setup complete ✅"
