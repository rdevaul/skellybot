# 12ft Skelly — Raspberry Pi Runtime

Pi OS Bookworm setup + orchestrator for the 12-foot Halloween skeleton prop.
Handles **BLE GATT control** (JieLi auth, AA/CRC8 commands) and **classic Bluetooth A2DP** audio streaming.

## Quick Start

```bash
# 1. Flash Pi OS Bookworm to SD card (use Raspberry Pi Imager)
#    Settings: enable SSH, set hostname, WiFi, user `pi`

# 2. SSH in and run setup
ssh pi@raspberrypi.local
cd /home/pi
git clone <your-repo> skellybot && cd skellybot/pi
sudo bash setup.sh

# 3. Add pi user to bluetooth group (then log out/in)
sudo usermod -aG bluetooth pi

# 4. Reboot (or restart services)
sudo reboot

# 5. After reboot, start PipeWire user services
systemctl --user start pipewire pipewire-pulse wireplumber

# 6. Test head motion first (no audio)
.venv/bin/python3 skelly_orchestrator.py --test-head 5

# 7. Full session with audio
.venv/bin/python3 skelly_orchestrator.py /path/to/audio.wav
```

## Architecture

```
┌──────────────────────────────────────────────────┐
│                   Raspberry Pi                    │
│                                                   │
│  BLE (bleak)              Classic BT (bluez)      │
│  ┌─────────────┐          ┌─────────────────┐     │
│  │ JieLi Auth  │          │ D-Bus Agent     │     │
│  │ 0xAE00/AE01 │          │ (PIN: 1234)     │     │
│  │ AA/CRC8 cmd │          │ bluetoothctl    │     │
│  └──────┬──────┘          └────────┬────────┘     │
│         │                          │              │
│  ┌──────▼──────┐          ┌────────▼────────┐     │
│  │ ENABLE_LIVE │──keepalive→│ A2DP Connect  │     │
│  │  (0xFD 01)  │  (every 4s)│ bluez_output.*│     │
│  └──────┬──────┘          └────────┬────────┘     │
│         │                          │              │
│  ┌──────▼──────┐          ┌────────▼────────┐     │
│  │ SET_MOVEMENT│          │ PipeWire pw-play│     │
│  │ head start  │          │ → BT speaker    │     │
│  └─────────────┘          └─────────────────┘     │
└──────────────────────────────────────────────────┘
```

### Two planes, separate connections

| Plane | Protocol | Purpose |
|-------|----------|---------|
| BLE GATT | BLE (0xAE00 service, char AE01) | Auth, commands, head motion, live-mode trigger |
| Classic BT | Bluetooth BR/EDR A2DP | Audio streaming to the Skelly's built-in speaker |

The BLE address is a **rotating RPA** — always scan by name (`12ft Skelly`), never hardcode.
The classic BT address (`D3:A6:5D:78:BD:19`) is stable and advertised only briefly after each `0xFD 01`.

## Current Pi state (2026-10-31)

The deployed Pi (`skelly-pi`, tailnet 100.84.5.92) runs **Raspbian 11 Bullseye
(32-bit userland) with PulseAudio 14 + pulseaudio-module-bluetooth**, not the
Bookworm/PipeWire stack `setup.sh` assumes. The orchestrator handles both:
the A2DP sink is `bluez_sink.<MAC>.a2dp_sink` on PulseAudio and
`bluez_output.<MAC>.1` on PipeWire. PulseAudio there defaults to 44.1 kHz.
Run `--preflight` first; it checks tools, audio server, bluez module and bond
state without sending anything to the skull.

### First end-to-end live audio: 2026-10-31 08:59 ✅

Skull speaker played the clip with jaw lip-sync and head motion. Notes:
- BLE connect often fails first with HCI `0x3e` (*Connection Failed to be
  Established*), which BlueZ reports as "Software caused connection abort".
  No packets reach the skull, so up to 3 retries are allowed **for that error
  only**. If all 3 fail, wait ~30s before running again. That worked at 09:02;
  don't power-cycle first.
- Loudness: `FA` gets no ACK, but `E5` QUERY_VOLUME confirms it (`bb e5 64` =
  100). The orchestrator now defaults to `FA 100`, forces the BT sink to 100%,
  and applies loudnorm(-10 LUFS) + `SKELLY_GAIN_DB` (default +4 dB) with a limiter.
- `C6` stop and `FC 00` get no ACK (harmless). `FC 01` replies with a
  `fe dc ba …` status frame rather than `bb fc`.

### Dropouts / choppy audio (09:05 investigation)

- Classic link RF looked clean during a 22s stream: `hcitool rssi` 0 the whole
  time (inside the golden receive range), link quality 255/255. SBC negotiated
  Joint Stereo, bitpool 2–53, 44.1k.
- Software causes we fixed: BLE discovery used to stay **on** for the whole
  session (now stopped right after connect; SkellyUltra v1.6.2 saw the same
  dropouts from scanning during audio), and loudnorm used to run live in the
  stream pipe (now pre-rendered to a file before streaming). pacat now uses a
  300 ms buffer (`SKELLY_LATENCY_MS`).
- Prime suspect if it still stutters: **Pi 4 Wi-Fi/BT coexistence.** The two
  radios share one antenna, and the Pi is on 2.4 GHz ch 1 at −70 dBm with a
  big retry count. Use ethernet, 5 GHz, or `sudo rfkill block wifi` while
  testing.
- SkellyUltra also notes cold-start clipping: the skull mutes during silence.
  Fix is a short faint-noise pre-roll.

### Pre-roll

Rich heard the start of the clip lag and clip. The stream now opens with
`SKELLY_PREROLL_S` (default 0.6s) of faint pink noise (~-60 dBFS), not digital
silence, so the skull's amp is already unmuted when speech starts.

### Persistent daemon results (2026-10-31 09:45)

- Installed as `systemctl --user` unit `skelly-daemon` (linger on, so it starts at boot
  without anyone logged in). `StartLimitBurst=6/600s` plus `RestartSec=30` keep a
  powered-off skull from being hammered with BLE connects.
- Per-line start latency went from ~25s to **0.48s** (`--prenorm`: Spark normalizes
  the audio, Pi just adds pre-roll). Ming "Now I am always listening…": 3s TTS, 0.5s
  to first sound, 18s wall clock for an 8s clip, the rest being SSH/scp overhead.
- Review fixes applied on top of the generated daemon: FC 01/FA now go out *during*
  playback; the E5 health check uses the persistent notify queue (start/stop_notify
  would have killed the main callback); request handlers use the reconnect holders
  (not stale captured client/sink); the old BLE client and noise bed are torn down
  before reconnecting; `shutdown` actually stops the daemon.

### Bond reuse (do NOT re-pair every session)

The classic `12ft Skelly(Live)` device only needs to be paired **once**
(legacy PIN `1234`, answered by `skelly_bt_agent.py`). After that each session
is just `FD 01` → `bluetoothctl connect` (≤4 tries, `FD 01` re-sent between
tries), matching OpenSkelly `connect_live()`. Earlier code ran
`bluetoothctl remove` at the start of every session, destroying the bond.
Use `--repair` only if the bond is genuinely broken.
Back up `/var/lib/bluetooth/` before reflashing the Pi to keep the bond.

## ⚠️ REQUIRED: 44.1 kHz SBC (silent-failure gotcha)

The Skelly hardware produces **no audio at 48 kHz**, even though the A2DP transport
looks active (sink RUNNING, no errors). PipeWire defaults to 48 kHz. `setup.sh`
installs WirePlumber (0.5 `.conf.d` and 0.4 Lua) + PipeWire overrides pinning the
bluez output to SBC / S16LE / 44100 Hz. **Do not skip it**; after setup restart
`systemctl --user restart pipewire pipewire-pulse wireplumber` and verify with
`pactl list sinks | grep -i "sample spec"` → `s16le 2ch 44100Hz`.
The orchestrator logs a loud warning if the BT sink is not 44.1 kHz.

## Modes

| Mode | Command | Behavior |
|------|---------|----------|
| `live` (default) | `--mode live audio.wav` | Classic A2DP stream (sequence below), single handshake attempt |
| `upload` | `--mode upload audio.wav` | Upload via C0–C3, play via C6 on the skull's own speaker; jaw auto-lip-syncs. Non-mp3 input is transcoded with ffmpeg (64k mono) |
| `hybrid` | `--mode hybrid audio.wav` | Try live; if the classic handshake (pair/A2DP) fails twice, fall back to upload+play. Log ends with `RESULT: path=LIVE`, `path=UPLOAD-FALLBACK`, etc. |
| capacity | `--capacity` | Query `0xD2` QUERY_CAPACITY and `0xE0` QUERY_PARAMS; logs raw replies |
| head test | `--test-head N` | Head motion only |

## Live sequence (corrected)

| Step | Action | Opcode / tool | Status |
|------|--------|---------------|--------|
| a | Stop onboard-file playback + reset anim | BLE `C6` (zero payload), `FC 00` | frames byte-identical to OpenSkelly |
| b | Enable classic radio | BLE `FD 01` (kept alive every 4s during pairing) | verified |
| c | Connect A2DP (pair only if no bond) | `bluetoothctl connect`; agent for one-time PIN | pairing verified 10-30 |
| d | Select classic BT audio source | BLE `FE 01`, then wait for A2DP sink | OpenSkelly-verified |
| e | Stream audio | ffmpeg loudnorm → `pacat` s16le 44100 stereo to the BT sink (refuses to fall back to the analog jack) | |
| f | Enter live animation mode | BLE `FC 01` (~0.3s after stream starts) | OpenSkelly-verified |
| g | Volume | BLE `FA <0-100>` right after `FC 01` (`--volume`, default 80) | OpenSkelly sends `FA 64` |

`0xCA` head start/stop is still sent alongside `FC 01` (disable with `--no-head`).
Whether `FC 01` subsumes head motion is **unverified** — A/B on hardware with and
without `--no-head`. On exit: `FC 00`, `FD 00`.

## Usage

### Full session (porch test)

```bash
.venv/bin/python3 skelly_orchestrator.py audio.wav
```

Sequence:
1. BLE scan by name → connect
2. JieLi 3-step auth
3. Send `ENABLE_LIVE (0xFD 01)` — triggers classic BT advertising
4. Start keepalive thread (re-sends `0xFD 01` every 4s to keep classic advertising alive)
5. Concurrent classic BT discovery + pairing (PIN: `1234`)
6. Head movement start (`SET_MOVEMENT 0xCA [0x02, 0x01]`)
7. Stream audio via `pw-play` / `paplay` to PipeWire BT sink
8. Head movement stop (`SET_MOVEMENT 0xCA [0x02, 0x00]`)
9. Disable live mode (`0xFD 00`)
10. BLE disconnect

### Test head motion only

```bash
.venv/bin/python3 skelly_orchestrator.py --test-head 5
```

BLE connect + auth → head start → wait 5s → head stop → disconnect.
No audio, no A2DP pairing. Good for verifying BLE link and movement.

### Options

```bash
.venv/bin/python3 skelly_orchestrator.py audio.wav \
  --classic-addr D3:A6:5D:78:BD:19 \
  --no-head
```

| Flag | Description |
|------|-------------|
| `--test-head <sec>` | Head motion test only, no audio |
| `--classic-addr <MAC>` | Override classic BT MAC (default: `D3:A6:5D:78:BD:19`) |
| `--no-head` | Skip 0xCA head movement during live playback |
| `--mode live\|upload\|hybrid` | Playback path (see Modes) |
| `--volume <0-100>` | FA volume (default 80) |
| `--capacity` | Storage capacity probe |
| `--preflight` | Local checks only, no radio traffic |
| `--repair` | Remove + re-pair the Live device (default reuses bond) |

## Operator Sequence (Porch Test Tonight)

1. **Power on the Skelly** — wait for the startup chime
2. **SSH to Pi** — `ssh pi@raspberrypi.local` (or local terminal)
3. **Verify BT is up** — `bluetoothctl show | grep Powered` should say `yes`
4. **Run test first** — `.venv/bin/python3 skelly_orchestrator.py --test-head 3`
   - Should see: scan → connect → auth → head START → wait 3s → head STOP
   - If head moves, BLE is good ✅
5. **Run full session** — `.venv/bin/python3 skelly_orchestrator.py halloween.wav`
   - Watch for: ENABLE_LIVE ACK → pairing → `audio playback complete ✅`
6. **If pairing fails** — check the classic address is still `D3:A6:5D:78:BD:19` (query via BLE 0xE0)
7. **If no audio** — run `pactl list sinks` and look for `bluez_output.*`

## Known Failure Modes

### `br-connection-page-timeout`
**Classic BT page timeout** — the Skelly's classic advertising window closed before pairing completed.
- **Fix**: The keepalive thread should prevent this. If it still happens, reduce the keepalive interval (change `interval=4.0` in `LiveKeepalive` to `3.0`).
- **Also**: Move the Pi closer to the Skelly (within 3m).

### `br-connection-profile-unavailable`
**PipeWire doesn't have the BT audio profile** — means `libspa-0.2-bluetooth` isn't loaded.
- **Fix**: `sudo apt install libspa-0.2-bluetooth && systemctl --user restart pipewire`
- **Verify**: `pactl list sinks` should show a `bluez_output.*` sink.

### BLE connect fails / timeout
**Firmware issue** — stacked BLE retries cause connect timeouts and constant chirping.
- **Fix**: **DO NOT retry.** Power-cycle the Skelly (unplug/replug) and try again once.

### No ACK after 0xFD 01
**Live mode didn't enable** — the Skelly may not be in the right state.
- **Fix**: Power-cycle, wait for chime, try again.

### macOS cannot do A2DP with the Skelly
**mac limitation** — macOS only supports AVRCP (media remote) for this device, not A2DP audio streaming. The Pi (or any Linux machine) is required for live audio. This is why this Pi runtime exists.

## Audio Format

- **WAV**: Preferred (no codec overhead). 16-bit PCM, mono or stereo.
- **MP3**: Works via PipeWire's built-in decoder.
- **Duration**: PipeWire handles any length. The Skelly's firmware buffer is small — keep clips under 60s for best results, or split into chunks.

## Protocol Reference

### BLE GATT (AE00 service)
- **Write char**: `0000ae01-0000-1000-8000-00805f9b34fb`
- **Notify char**: `0000ae02-0000-1000-8000-00805f9b34fb`
- **Auth**: 3-step challenge-response (JieLi cipher, XOR key)
- **Frame**: `AA <cmd> <8-byte payload> <crc8>`
- **ACK**: `BB <cmd> <response data>`

### Key Commands
| Opcode | Name | Payload | Description |
|--------|------|---------|-------------|
| `0xFD` | ENABLE_LIVE | `01`=on, `00`=off | Triggers classic BT A2DP advertising |
| `0xCA` | SET_MOVEMENT | `[0x02, 0x01]`=start, `[0x02, 0x00]`=stop | Head/jaw movement |
| `0xE0` | QUERY_PARAMS | — | Device params (includes PIN) |
| `0xD0` | QUERY_FILES | — | File listing |
| `0xC6` | PLAY_FILE | `<serial><0x01>` | Play stored clip (stop payload TODO-verify) |
| `0xC0`–`0xC3` | START/CHUNK/END/CONFIRM_TRANSFER | see skelly_speak.py | Upload |
| `0xD2` | QUERY_CAPACITY | — | Storage capacity (reply layout TODO-verify) |
| `0xFA` | SET_VOLUME | `<0-100>` | Volume |
| `0xFE` | SELECT_CLASSIC_SOURCE | `01` | Select classic BT audio source (TODO-verify; not in Mac ref code) |
| `0xFC` | ENTER_LIVE_ANIM | `01`/`00` | Live animation mode (TODO-verify; not in Mac ref code) |

### CRC8
Polynomial 0x1D (reversed 0x8C), init 0. Computed over `[AA, cmd, payload_padded]`.
Verified against existing Mac reference code (`skelly.py`). Flag: needs on-hardware verification against a captured ACK packet.

## Persistent Daemon

The one-shot orchestrator costs ~25 s of setup (BLE scan/auth + classic connect) per
utterance. The **persistent daemon** keeps BLE + A2DP alive between clips so each
utterance starts in ~1-2 s.

### Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    skelly_daemon.py                       │
│                                                           │
│  Startup:  BLE connect → auth → FD 01 → classic → FE 01  │
│            → wait sink → volume 100% → FA 100            │
│                                                           │
│  Idle:     faint pink-noise bed (~-60 dBFS) via pacat     │
│            → keeps skull amp awake (no sleep between clips)│
│            E5 QUERY_VOLUME health check every 15s         │
│                                                           │
│  Play:     UNIX socket → queue → pre-render → pacat clip  │
│            → FC 01 + FA → head start/stop → resume bed    │
│                                                           │
│  Recovery: exponential backoff (2,4,8,16,30s cap)        │
│            classic-only drop → FD 01 + btctl connect      │
│            BLE drop → full reconnect                      │
└──────────────────────────────────────────────────────────┘
```

### Running

```bash
# Foreground (testing):
~/skelly-venv/bin/python ~/skelly_daemon.py

# Via systemd --user (recommended):
cp skelly-daemon.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now skelly-daemon
loginctl enable-linger rich  # survive logout
```

### Client

```bash
# On the Pi:
~/skelly-venv/bin/python ~/skelly_client.py play /tmp/say_12345.wav
~/skelly-venv/bin/python ~/skelly_client.py status
~/skelly-venv/bin/python ~/skelly_client.py stop
~/skelly-venv/bin/python ~/skelly_client.py shutdown
```

### Socket protocol

UNIX socket at `/tmp/skelly.sock` (mode 0600). One JSON line per request:

| Request | Reply |
|---------|-------|
| `{"cmd":"play","path":"/abs/file.wav","head":true}` | `{"ok":true,"timing":{"render_s":0.5,...},"link":{...}}` |
| `{"cmd":"status"}` | `{"state":"READY","uptime_s":3600,"reconnect_count":0,...}` |
| `{"cmd":"stop"}` | `{"ok":true,"message":"stop requested"}` |
| `{"cmd":"shutdown"}` | `{"ok":true,"message":"shutdown requested"}` |

### Recovery behavior

| Failure | Detection | Action |
|---------|-----------|--------|
| Classic BT disconnect | `bluetoothctl info` shows `Connected: no` | FD 01 over BLE + `bluetoothctl connect` (≤4 tries) + FE 01 |
| BLE disconnect | bleak `is_connected` false / E5 health check fails | Full reconnect (BLE + auth + classic) |
| Noise bed process dies | `bed_proc.poll()` returns non-None | Restart noise bed |
| All reconnect attempts exhausted | Backoff schedule drained | State → FAILED, log last error |

Backoff schedule: 2, 4, 8, 16, 30 s (cap). The daemon **never** tight-loops BLE
connects — firmware latches if hammered. Max one full BLE connect attempt per
backoff step.

### State values

| State | Meaning |
|-------|---------|
| `INIT` | Daemon starting |
| `CONNECTING` | BLE + classic setup in progress |
| `READY` | Links up, noise bed streaming, waiting for play requests |
| `PLAYING` | Currently streaming a clip |
| `RECOVERING` | Reconnection in progress (backoff active) |
| `FAILED` | All reconnect attempts exhausted |
| `SHUTTING_DOWN` | Graceful shutdown in progress |

### skelly_say.sh fast path

`skelly_say.sh` now checks for the daemon socket before falling back to the
one-shot orchestrator. If `/tmp/skelly.sock` exists on the Pi, it SCPs the WAV
and uses the client to play it (~1-2 s latency). Otherwise it falls back to the
existing `skelly_orchestrator.py --mode live` command (~25 s).

## File Listing

| File | Purpose |
|------|---------|
| `setup.sh` | Idempotent Pi OS Bookworm setup |
| `skelly_orchestrator.py` | Main orchestrator — full session or head test |
| `skelly_bt_agent.py` | D-Bus Bluetooth agent for PIN pairing |
| `skelly_daemon.py` | Persistent daemon — keeps BT links alive between clips |
| `skelly_client.py` | CLI client for the daemon socket |
| `skelly-daemon.service` | systemd --user unit template |
| `README.md` | This file |

## Source / Reference

Ported from `/Users/rich/Projects/skellybot/` (macOS reference):
- `skelly.py` — BLE client, CRC8, auth, commands
- `skelly_speak.py` — TTS + upload + play pipeline
- `skelly_live_test.py` — ENABLE_LIVE testing
- `skelly_live_classic.py` — Classic BT discovery test
- `skelly_live_stream.py` — Audio streaming over BLE (experimental)
- `skelly_jaw.py` — Jaw sync during playback
- `1-recon-skelly_auth.py` — JieLi raw auth research
