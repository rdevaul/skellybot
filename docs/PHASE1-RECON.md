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
