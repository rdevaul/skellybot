# Phase 1 Recon — 2026-10-18

## Device discovery (confirmed)
- Name: `12ft Skelly`
- Address (macOS): `B3C1F919-364D-6A68-2C9A-0286CB3F5507`
- RSSI from office: −75 dBm (marginal; Pi Zero 2 W proxy likely needed)
- Manufacturer ID: `1494` (0x05D6) in advertisement

## GATT map (enumerated live)
| Handle | UUID | Properties |
|---|---|---|
| Service | `0000ae00-0000-1000-8000-00805f9b34fb` | — |
| Command | `0000ae01-0000-1000-8000-00805f9b34fb` | write-without-response |
| Response | `0000ae02-0000-1000-8000-00805f9b34fb` | notify (CCCD 0x2902) |

- Device connects easily (no pairing/bonding gate observed).
- AE02 is silent at idle (15 s passive listen, zero notifications) — device is purely write-driven until queried.
- AE00/AE01/AE02 is the signature of JieLi (杰理) audio SoCs running RCSP — but the RCSP auth handshake may or may not be enforced here. No prior public RE found for the Decore line specifically.

## Next steps
1. **Passive capture (preferred):** iPhone + nRF Connect → connect to Skelly, mirror Decore Pro app traffic, export .cfa/log. Yields exact command frames for jaw/eyes/voice + the audio upload path.
2. **Active probing (fallback):** benign writes on AE01 while logging AE02; observe physical reactions (jaw/eyes/sound) to map opcodes.
3. Check whether RCSP `GET_DEV_INFO`-style queries get responses (distinguishes stock JieLi stack vs custom firmware).

## Environment notes
- macOS BLE from daemon context requires Bluetooth TCC permission for `node` (granted 2026-10-18 16:21 PT).
- Scan/enumeration scripts: `/tmp/skelly_scan.py`, `/tmp/skelly_gatt.py`, `/tmp/skelly_listen.py` (move into `0-recon/`).

## Active probing results (2026-10-18 16:27)
- Benign writes (RCSP-style FD frames, short pings) → no response; `00 ff` → 17-byte reply on AE02: `01` + 16 bytes (AES block size).
- Reply is nonce-like: identical for #0/#1, changed on #2; zero traffic in control window → handshake challenge, deterministic per connection.
- Conclusion: control is gated behind an encrypted auth handshake (JieLi RCSP-style, AES).
- **Decisive next step:** nRF Connect capture of Decore Pro app pairing flow (iPhone), export handshake bytes → replay from bleak.
- Parallel offline work: audit JieLi SDK default key/handshake for exploitable defaults.

## 🔓 AUTH BREAKTHROUGH (2026-10-18 16:30)
- Device uses the stock **JieLi raw auth handshake** on AE01/AE02 with the **default static key** (`06775F87918DD423005DF1D8CF0C142B`).
- 6-step handshake completed: `02 70 61 73 73` ("\x02pass") received on step 6 → **AUTH SUCCESS**.
- Cipher ported from ElectronicCats/jieli-ble-badge-research `qix_ble/auth.py` (verified byte-exact vs device).
- Auth script: `1-recon/skelly_auth.py` (to be moved into repo); handshake replayable on every connection.
- Implication: full JieLi stack available — RCSP commands, filesystem, and **audio file upload** are all reachable with the stock framing (FE/fe-dc-ba obfuscated RCSP frames).

## 🔓🔓 PROTOCOL FULLY DECODED via community RE (2026-10-18 16:33)
Credit: WJDDesigns/supreme-skelly (protocol.md + profiles.py), martinecker/SkellyUltraWebController (original RE), ElectronicCats/jieli-ble-badge-research (auth cipher).

- Frame: `AA <cmd> <payload ≥8B>` + CRC-8/MAXIM; replies `BB <cmd>`; 40 ms write spacing.
- JieLi raw auth (default static key) still required/accepted; vendored in `vendor/jli_auth.py`.
- 12ft Skelly profile (hardware-verified by supreme-skelly): head movement bit 0x02, single chest light channel, 20 eye icons (official app labels wrong).
- Live query results: PIN "1234", volume 100, fw v14, ~8 file slots, one stock file "Default Sound 1.mp3".
- `skelly.py`: auth + query client. Next: control ops (eyes/volume/movement), audio upload (C0–C5, 8kHz mono MP3, ≤20-char names).

## ✅ Custom audio upload verified (2026-10-18 16:49)
- `upload.py` pushes any MP3 via C0→C1→C2→C3 with JieLi auth and CRC-8/MAXIM framing.
- Test file `GLaDOS1.mp3` (62.7 KB, 263 chunks @ 239 B, ~25 s) uploaded and confirmed; appears in D0 list as serial 9.
- File-list reply layout corrected: per-file record, UTF-16LE name starts at byte 60 (59 was one short).
- Device holds 8 stock sounds + uploads (30-file firmware corruption limit per community).
- Audio prep for Skelly playback: 44.1 kHz mono 64 kbps CBR MP3 (supreme-skelly audio.py recipe), wake-tone prefix recommended so the amp is awake.
- Next: jaw-synced playback (C6), TTS pipeline (Phase 3), POV camera + agent loop (Phase 4).

## skelly_speak wrapper (2026-10-18 17:53)
- `skelly_speak.py "<text>"`: say → 64k mono MP3 → C0-C3 upload → C6 play, one command.
- Transient-playback root cause: playback too soon after C3 — firmware needs ~8 s to flush; wrapper settles before playing.
- Clip auto-recycling: `skellyHHMMSS.mp3` names (no prefix collisions), oldest deleted at 25 clips (30-file corruption limit).
- Verified live: serial 11 played. Phase 3 ✅ (TTS pipeline); Phase 2 daemon = trivial wrapper around this.
