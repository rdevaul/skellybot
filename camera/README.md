# Camera → heckle pipeline

`skelly_watch.py` runs on the Mac and chains:

1. **Trigger:** the Tapo C325WB's built-in person detector, read locally over ONVIF
   PullPoint (`tns1:RuleEngine/PeopleDetector/People`, `IsPeople=true/false`, ~1 event/s
   while someone is in view). No cloud. The Tapo drops long-polls when idle, which is
   normal, so we use raw SOAP (zeep/onvif-zeep-async choke on it). The watcher
   resubscribes every 14 min.
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

```bash
export OPENROUTER_API_KEY=...  TAPO_PASS=...
python3 camera/skelly_watch.py --dry-run                 # everything except playback
python3 camera/skelly_watch.py --image test.jpg --dry-run
python3 camera/skelly_watch.py                           # live
```

Dry-run timing on a test image (2026-10-31): vision 2.4s + TTS 2.8s, so about
**6–7s from detection to first sound** including frame grab and daemon start.
