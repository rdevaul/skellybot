# Skellybot

Direct computer control of the Home Depot 12-ft animatronic Skelly skeleton via Bluetooth, ultimately driven by an AI agent with camera input.

## Project Overview

This project reverse-engineers the BLE protocol used by the **Decore Pro** companion app for the Home Depot 12-ft Skelly animatronic, then replaces the phone app with a computer-controlled daemon. End goal: the skeleton has a POV camera, watches trick-or-treaters, and an AI agent generates spooky/snarky commentary — with real jaw, eye, and voice control from a local machine.

## Phase Plan

### Phase 0 — Recon & Range Test
- Power on the skeleton, explore the Decore Pro app features
- BLE range test from the Mac (skeleton is set up in San Pedro yard)
- **Fallback:** Raspberry Pi Zero 2 W as Wi-Fi/BLE proxy if Mac-to-skeleton BLE range is insufficient

### Phase 1 — BLE Protocol Reverse-Engineering
- `bleak` BLE scan and GATT characteristic enumeration
- nRF Connect capture of Decore Pro app traffic (jaw, eyes, voice triggers)
- Search for prior public reverse-engineering of Home Depot animatronics
- Document the full command/characteristic map

### Phase 2 — Control Daemon + CLI
- Python daemon that connects via BLE and sends protocol commands
- CLI: `skelly speak <text>`, `skelly jaw <angle>`, `skelly eyes <mode>`, `skelly dance`
- Connection management, retries, and BLE reconnection

### Phase 3 — TTS Voice Pipeline
- Local Piper TTS or cloud ElevenLabs for custom spooky voices
- Upload custom audio via the app's discovered upload path
- Voice caching and pre-generation for low-latency playback

### Phase 4 — Vision + Agent Loop
- Webcam or ESP32-S3-CAM POV camera at the skeleton's eye level
- AI agent generates snarky commentary about costumes
- Latency budget: 5–15s from motion detection to jaw movement + audio

## Hardware
- Home Depot 12-ft Skelly animatronic
- MacBook (BLE controller / AI agent host)
- Raspberry Pi Zero 2 W (optional BLE proxy)
- Webcam or ESP32-S3-CAM (Phase 4)

## Stack
- Python (bleak, asyncio)
- Piper TTS (local) / ElevenLabs (optional cloud)
- OpenAI or local LLM for agent commentary
