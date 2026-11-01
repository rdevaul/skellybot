# Ming TTS on the DGX Spark

`ming_server.py` keeps the Ming (BailingMM) model loaded (~13s load, ~4 GB) and serves
synthesis over a **UNIX socket** (`~/ming.sock`, mode 0600). It opens no TCP port, so the
Spark's network listeners don't change. Clients reach it via SSH using `ming_client.py`.

Voice is Rich's 2026-10-28 pick: zero speaker embedding, the prompt "deep, low-pitched
male voice with a dry, sardonic delivery", then a pitch-preserving `atempo` slowdown
(default 0.65, override with `MING_TEMPO` or `--tempo`). Output is 44.1 kHz mono WAV,
already loudness-normalized (`loudnorm I=-10`, then `MING_GAIN_DB`, then a limiter), so the
Pi plays it with `--prenorm`.

Generation runs at about RTF 0.1: 160 chars → ~17s of audio in ~1.9s.

## Service

`ming-tts.service` is a system unit (`sudo systemctl restart ming-tts`); the model loads in
~13s. Gain is `MING_GAIN_DB=7` in the unit file (the code default is 4). On the Spark it is
set via a drop-in, `/etc/systemd/system/ming-tts.service.d/gain.conf`.

```bash
# from anywhere with the key
ssh rich@100.88.44.116 '~/ming-venv/bin/python ~/skelly-spark/ming_client.py "text"' > out.wav
```

## Network paths

The Spark (`zgx-adfd`) has only a wired link, `192.168.100.10`, shared with old-glados
(`192.168.100.11`). Nothing else can reach that address directly.

- **Tailscale** `100.88.44.116` works from anywhere, but Tailscale can't make a direct
  connection to it, so traffic goes through the DERP relay (LA). That's fine from the Mac,
  but from the Pi it was ~10s per 3 MB clip.
- **Jump via old-glados** (wired to the Spark; on Wi-Fi at `192.168.4.241`). This is what
  the Pi uses (`SKELLY_SPARK=spark-via-lan`): ~0.8s per 3 MB clip. old-glados has system
  sleep disabled. The Pi's `~/.ssh/config`:

  ```
  Host old-glados-lan
      HostName 192.168.4.241
      User rich
      IdentityFile ~/.ssh/id_ed25519_glados
  Host spark-via-lan
      HostName 192.168.100.10
      User rich
      IdentityFile ~/.ssh/id_ed25519_glados
      ProxyJump old-glados-lan
  ```

End to end, from the Mac: `./skelly_say.sh "text"` runs Spark TTS, copies the WAV to the
Pi, and plays it through `skelly-daemon` (or falls back to `skelly_orchestrator.py --mode live`).
