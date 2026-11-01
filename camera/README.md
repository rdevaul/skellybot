# Camera → heckle pipeline

`skelly_watch.py` runs on the Pi as `skelly-watch.service` (it can also run on the Mac) and chains:

1. **Trigger:** the Tapo C325WB's built-in person detector, read locally over ONVIF
   PullPoint (`tns1:RuleEngine/PeopleDetector/People`, `IsPeople=true/false`, ~1 event/s
   while someone is in view). No cloud. We use raw SOAP (zeep/onvif-zeep-async choke on
   the Tapo's long-poll). The Tapo hangs up on most PullMessages when it has nothing to
   report, sometimes within milliseconds. That's normal, and events still arrive on the
   pulls it answers. The camera has only ~10 subscription slots, so the watcher keeps
   **one** subscription and renews it instead of recreating it. It uses Renew as the
   liveness check, and unsubscribes before resubscribing and on SIGTERM. Never resubscribe
   because of dropped pulls: leaked slots make every new subscription fail with a 400
   until their 5-minute leases expire.
2. A **new visit** starts when a person appears after being absent for ≥4s (`--min-gap`).
   After a 0.7s settle, one RTSP frame is grabbed (1600px wide).
3. A **vision LLM** (OpenRouter, default `anthropic/claude-sonnet-5.5`, falls back to
   haiku-5.5) returns JSON `{people, costumes, skip, line}`. The prompt keeps it PG and
   roasts costumes only, never bodies, faces, race, age or identity. It skips delivery
   drivers and empty frames, and avoids repeating recent lines.
4. **Ming TTS** on the Spark (`ming-tts.service`), then **skelly-daemon** on the Pi
   (held-open A2DP, `--prenorm`).
5. **Cooldown:** 20s after each line (`--cooldown`). Triggers while busy are dropped.

Privacy: frames go only to the vision API and are deleted after each trigger
(`--keep-frames` to keep them). `watch_log.jsonl` stores text and timings only, and
is gitignored.

Logs redact `TAPO_PASS` and `OPENROUTER_API_KEY`.

```bash
export OPENROUTER_API_KEY=...  TAPO_PASS=...
python3 camera/skelly_watch.py --dry-run                 # everything except playback
python3 camera/skelly_watch.py --image test.jpg --dry-run
python3 camera/skelly_watch.py                           # live
```

## Configuration (env, on the Pi in `~/.skelly_env`)

| Variable | Pi value | Meaning |
|---|---|---|
| `SKELLY_PI` | `local` | Talk to `skelly_client.py` directly. Default is the Pi's Tailscale address, used when running on the Mac |
| `SKELLY_SPARK` | `spark-via-lan` | ssh target for Ming TTS. Default `rich@100.88.44.116` (Tailscale, relayed and slow from the Pi). See `spark/README.md` |
| `SKELLY_CANDY_GONE` | `1` when out | End-of-night mode: lines say the candy is gone and never invite anyone up |
| `SKELLY_VISION_MODEL` / `SKELLY_VISION_FALLBACK` | default | OpenRouter model ids |

## Latency

Live, 2026-10-31, after the network fixes: detection → first sound **~12s**
(frame grab 3.5s, vision 2.8s, TTS + transfer 4.7s, daemon start 0.5s). It was ~21s while
the Pi fetched audio through the Tailscale relay. Next wins: keep a persistent RTSP
reader (saves ~3s), send a smaller image and ask for terser JSON (saves ~1–2s), and
send Opus instead of WAV.
