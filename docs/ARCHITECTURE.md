# Skellybot Architecture

## System Overview

```
┌──────────────────────────────────────────────────────────────┐
│                        Skellybot System                       │
├────────────┐   BLE    ┌─────────────┐   LAN/USB   ┌──────────┤
│  MacBook   │◄────────►│  Decore Pro │             │ 12-ft    │
│  (Agent +  │  (Phase1│  BLE Protocol│             │ Skelly   │
│   Daemon)  │  Phase2)│             │             │Animatronic│
└─────┬──────┘         └─────────────┘             └──────────┘
      │
      │ USB / LAN
┌─────▼──────┐                         ┌──────────────┐
│ ESP32-S3-  │   or                    │  Raspberry   │
│ CAM (POV)  │◄───────────────────────►│  Pi Zero 2 W │
│ (Phase 4)  │                         │  (BLE Proxy) │
└────────────┘                         │  (Phase 0)   │
                                       └──────────────┘
```

## Phase 0 — Recon & Range Test

**Goal:** Understand what the skeleton does, verify BLE reachability.

```
[MacBook] ──BLE scan (bleak)──► [Skelly]
     │                                │
     └── Decore Pro app exploration ──┘
     └── BLE range test from yard ────┘
     └── Pi Zero 2 W fallback eval ───┘
```

**Deliverables:**
- BLE device name, MAC/service UUIDs
- Range measurement (Mac ↔ skeleton in San Pedro yard)
- Pi Zero 2 W bridge feasibility decision

---

## Phase 1 — BLE Protocol Reverse-Engineering

**Goal:** Map every GATT characteristic to a skeleton action.

```
[nRF Connect on Phone]
      │
      │ Decore Pro app traffic capture
      ▼
[GATT Characteristic Map]
      │
      ├── Jaw open/close
      ├── Eye LEDs on/off/pattern
      ├── Voice trigger IDs
      ├── Audio upload endpoint
      └── Dance/routine commands
      │
      ▼
[Protocol Document] ──► prior-public-RE search
```

**Tools:** `bleak`, `nRF Connect`, `nRF Sniffer`, Wireshark

**Deliverables:**
- Full GATT service/characteristic tree
- Write value → action mapping table
- Audio upload protocol (format, endpoint, auth if any)

---

## Phase 2 — Control Daemon + CLI

**Goal:** Replace the phone app with a CLI on the Mac.

```
┌─────────────────────────────────────────┐
│              skellyd (daemon)            │
│                                         │
│  ┌─────┐ ┌─────┐ ┌─────┐ ┌───────────┐ │
│  │ BLE │ │Cmd  │ │Conn │ │Reconnect  │ │
│  │ I/O │ │Queue│ │Mgr  │ │+ Heartbeat│ │ │
│  └──┬──┘ └──┬──┘ └──┬──┘ └─────┬─────┘ │
│     │       │       │          │        │
└─────┼───────┼───────┼──────────┼────────┘
      │       │       │          │
   bleak   asyncio  bleak    periodic
  GATT wr  queue   connect   BLE ping
      │       │       │          │
      ▼       ▼       ▼          ▼
┌──────────────────────────────────────┐
│              CLI                      │
│  skelly speak <text>                  │
│  skelly jaw <angle|open|close>        │
│  skelly eyes <on|off|blink|strobe>    │
│  skelly dance                         │
│  skelly status                        │
└──────────────────────────────────────┘
```

---

## Phase 3 — TTS Voice Pipeline

**Goal:** Generate and play custom spooky audio through the skeleton.

```
┌──────────┐    ┌──────────────┐    ┌──────────────┐
│ Text in  │───►│ TTS Engine   │───►│ Audio Cache  │
│ (agent   │    │ Piper (local)│    │ (WAV/OGG)    │
│  or CLI) │    │ or ElevenLabs│    └──────┬───────┘
└──────────┘    └──────────────┘           │
                                           ▼
                                    ┌──────────────┐
                                    │ Upload to    │
                                    │ Skelly via   │
                                    │ BLE audio    │
                                    │ endpoint     │
                                    └──────────────┘
```

---

## Phase 4 — Vision + Agent Loop

**Goal:** Full autonomous spooky commentary.

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│ ESP32-CAM   │────►│ Frame Grab   │────►│ Motion Detect│
│ or Webcam   │     │ (opencv)     │     │ + Costumer   │
└─────────────┘     └──────────────┘     │ Classification│
                                         └──────┬───────┘
                                                ▼
                                         ┌──────────────┐
                                         │ LLM Agent    │
                                         │ (snark gen)  │
                                         └──────┬───────┘
                                                ▼
                                         ┌──────────────┐
                                         │ TTS → Jaw →  │
                                         │ Audio out    │
                                         └──────────────┘

Latency budget: 5-15s total
  Motion detect:  <1s
  LLM generate:   2-8s
  TTS:            1-3s
  BLE upload+play: 1-3s
```
