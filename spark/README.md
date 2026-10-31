# Ming TTS on the DGX Spark

`ming_server.py` keeps the Ming (BailingMM) model loaded (~13s load, ~4 GB) and serves
synthesis over a **UNIX socket** (`~/ming.sock`, mode 0600). It opens no TCP port, so the
Spark's network listeners don't change. Clients reach it via SSH using `ming_client.py`.

Voice is Rich's 2026-10-28 pick: zero speaker embedding, the prompt "deep, low-pitched
male voice with a dry, sardonic delivery", then a pitch-preserving `atempo` slowdown
(default 0.65, override with `MING_TEMPO` or `--tempo`). Output is 44.1 kHz mono WAV.

Generation runs at about RTF 0.2: 162 chars → 18.7s of audio in 3.4s.

```bash
# on spark
nohup ~/ming-venv/bin/python ~/skelly-spark/ming_server.py > ~/skelly-spark/ming_server.log 2>&1 &
# from anywhere with the key
ssh rich@192.168.100.10 '~/ming-venv/bin/python ~/skelly-spark/ming_client.py "text"' > out.wav
```

End to end, from the Mac: `./skelly_say.sh "text"` runs Spark TTS → scp to the Pi →
`skelly_orchestrator.py --mode live`. First run took 46s total: TTS 2s, BLE+classic
setup ~8s, Pi pre-render ~4s, then playback.
Not yet a systemd unit: the server has to be restarted by hand after a Spark reboot.
