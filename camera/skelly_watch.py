#!/usr/bin/env python3
"""skelly_watch — person-triggered heckle loop for the 12ft Skelly.

Tapo C325WB ONVIF PeopleDetector event (local, no cloud)
  → RTSP frame grab → vision LLM (OpenRouter) writes a costume heckle
  → Ming TTS on the DGX Spark → skelly-daemon on the Pi (held-open A2DP link).

Runs on the Mac. Stdlib only. Secrets via env: TAPO_PASS, OPENROUTER_API_KEY.

  TAPO_PASS=... python3 camera/skelly_watch.py            # live
  TAPO_PASS=... python3 camera/skelly_watch.py --dry-run  # everything except playback
  python3 camera/skelly_watch.py --image some.jpg --dry-run   # one-shot, no camera events

Privacy: frames are sent to the vision API only, held in a temp dir and deleted
after each trigger unless --keep-frames. The log keeps text only.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CAM = os.environ.get("TAPO_HOST", "192.168.4.110")
CAM_USER = os.environ.get("TAPO_USER", "glados-ops")
ONVIF_PORT = 2020
KEY = os.path.expanduser("~/.ssh/id_ed25519_glados")
SPARK = os.environ.get("SKELLY_SPARK", "rich@192.168.100.10")
PI = os.environ.get("SKELLY_PI", "rich@100.84.5.92")
MODEL = os.environ.get("SKELLY_VISION_MODEL", "anthropic/claude-sonnet-5.5")
FALLBACK_MODEL = os.environ.get("SKELLY_VISION_FALLBACK", "anthropic/claude-haiku-5.5")
LOG = Path(os.environ.get("SKELLY_WATCH_LOG", os.path.expanduser("~/Projects/skellybot/camera/watch_log.jsonl")))
CTL_DIR = os.path.expanduser("~/.ssh/skelly-ctl")

SYSTEM_PROMPT = """You are the voice of a 12-foot animatronic skeleton on a Halloween porch in San Pedro, California. \
Persona: theatrical, dry, sardonic, ancient, delighted by good costumes, mock-offended by lazy ones. \
You will see one frame from a porch camera. Write ONE short line for the skeleton to say out loud to the people in view.

Hard rules:
- Audience is mostly children and families. Keep it PG: spooky-fun, never scary-mean, no profanity, no innuendo.
- Roast or praise the COSTUME, props, candy bag, or group theme only. Never comment on bodies, weight, height, skin, race, \
age, disability, gender, faces, hair (unless it is clearly part of a costume/wig), or clothing that is not a costume.
- If someone has no costume, tease gently about that (e.g. "disguised as a regular human — bold"), never their person.
- No names, no locations, no guesses about identity. Never mention cameras or AI.
- 1–2 sentences, at most 30 words. Plain words only, no emoji, no stage directions, no asterisks.
- Do not repeat these recent lines: {recent}

Reply with JSON only: {{"people": <int>, "costumes": "<short description>", "skip": <bool>, "line": "<what the skeleton says>"}}.
Set skip=true (line empty) if no person is clearly visible, or if the person looks like a delivery driver, mail carrier, \
or someone not trick-or-treating (just passing or working)."""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def jlog(rec: dict) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")


# ── ONVIF PullPoint (raw SOAP; zeep chokes on the Tapo's long-poll) ─────────

def _wsse(password: str) -> str:
    nonce = os.urandom(16)
    created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return ('<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
            'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
            f'<wsse:UsernameToken><wsse:Username>{CAM_USER}</wsse:Username>'
            '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
            f'{digest}</wsse:Password><wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
            f'{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>')


def _soap(url: str, action: str, body: str, password: str, to: str | None = None, timeout: float = 15) -> str:
    hdr = _wsse(password) + (f"<wsa:Action>{action}</wsa:Action><wsa:To>{to}</wsa:To>" if to else "")
    env = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
           'xmlns:wsa="http://www.w3.org/2005/08/addressing" xmlns:tev="http://www.onvif.org/ver10/events/wsdl">'
           f"<s:Header>{hdr}</s:Header><s:Body>{body}</s:Body></s:Envelope>")
    req = urllib.request.Request(url, env.encode(), {"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'})
    return urllib.request.urlopen(req, timeout=timeout).read().decode()


class PeopleWatcher(threading.Thread):
    """Keeps a PullPoint subscription alive; tracks IsPeople and fires on rising edges."""

    def __init__(self, password: str, on_rising, min_gap_s: float = 4.0):
        super().__init__(daemon=True)
        self.password, self.on_rising, self.min_gap_s = password, on_rising, min_gap_s
        self.present = False
        self.last_true = 0.0
        self.last_false = time.time()

    def _subscribe(self) -> str:
        r = _soap(f"http://{CAM}:{ONVIF_PORT}/onvif/service",
                  "http://www.onvif.org/ver10/events/wsdl/EventPortType/CreatePullPointSubscriptionRequest",
                  "<tev:CreatePullPointSubscription><tev:InitialTerminationTime>PT900S</tev:InitialTerminationTime>"
                  "</tev:CreatePullPointSubscription>", self.password)
        m = re.search(r"Address>([^<]+)<", r)
        if not m:
            raise RuntimeError("no subscription address")
        return m.group(1)

    def run(self):
        while True:
            try:
                addr = self._subscribe()
                born = time.time()
                log(f"onvif: subscribed {addr}")
                fails = 0
                while time.time() - born < 840 and fails < 30:   # resubscribe before PT900S expiry
                    try:
                        r = _soap(addr, "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest",
                                  "<tev:PullMessages><tev:Timeout>PT5S</tev:Timeout><tev:MessageLimit>20</tev:MessageLimit>"
                                  "</tev:PullMessages>", self.password, to=addr, timeout=10)
                        fails = 0
                    except Exception:
                        # Tapo drops the long-poll when it has nothing to say; that's normal.
                        fails += 1
                        time.sleep(0.5)
                        self._decay()
                        continue
                    for m in re.findall(r"<wsnt:NotificationMessage>(.*?)</wsnt:NotificationMessage>", r, re.S):
                        if "PeopleDetector" not in m:
                            continue
                        v = re.search(r'Value="(true|false)" Name="IsPeople"', m)
                        if v:
                            self._update(v.group(1) == "true")
                    self._decay()
            except Exception as e:
                log(f"onvif: subscription error {e!r}; retry in 5s")
                time.sleep(5)

    def _update(self, people: bool):
        now = time.time()
        if people:
            self.last_true = now
            if not self.present:
                self.present = True
                gap = now - self.last_false
                log(f"onvif: PERSON (absent {gap:.0f}s)")
                if gap >= self.min_gap_s:
                    self.on_rising()
        else:
            if self.present:
                self.present = False
                self.last_false = now
                log("onvif: person gone")

    def _decay(self):
        # Treat a stale "true" (no events for 4s) as gone, in case a false was missed.
        if self.present and time.time() - self.last_true > 4.0:
            self.present = False
            self.last_false = time.time()


# ── Pipeline stages ─────────────────────────────────────────────────────────

def ssh_base(host: str) -> list[str]:
    os.makedirs(CTL_DIR, mode=0o700, exist_ok=True)
    return ["ssh", "-i", KEY, "-o", "ControlMaster=auto", "-o", f"ControlPath={CTL_DIR}/%r@%h:%p",
            "-o", "ControlPersist=600", "-o", "ConnectTimeout=8", host]


def grab_frame(password: str, out: str) -> None:
    url = f"rtsp://{CAM_USER}:{password}@{CAM}:554/stream1"
    subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-rtsp_transport", "tcp", "-i", url, "-frames:v", "1",
                    "-vf", "scale=1600:-2", "-q:v", "4", out], check=True, timeout=15)


def vision_line(jpg: str, recent: list[str], model: str) -> dict:
    b64 = base64.b64encode(Path(jpg).read_bytes()).decode()
    body = {
        "model": model,
        "max_tokens": 200,
        "temperature": 0.9,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.format(recent=json.dumps(recent[-8:]))},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": "Porch camera frame. JSON only."},
            ]},
        ],
    }
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", json.dumps(body).encode(), {
        "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
        "Content-Type": "application/json",
        "X-Title": "skellybot",
    })
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
    text = resp["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in model reply: {text[:200]}")
    out = json.loads(m.group(0))
    out["line"] = sanitize_line(str(out.get("line", "")))
    return out


def sanitize_line(line: str) -> str:
    line = re.sub(r"[*_#`~\[\]<>]", "", line).strip().strip('"')
    words = line.split()
    if len(words) > 40:
        line = " ".join(words[:40]).rstrip(",;:") + "."
    return line


def tts(line: str, out_wav: str) -> None:
    cmd = ssh_base(SPARK) + [f"~/ming-venv/bin/python ~/skelly-spark/ming_client.py {shlex.quote(line)}"]
    with open(out_wav, "wb") as f:
        subprocess.run(cmd, stdout=f, check=True, timeout=120)


def play(wav: str) -> dict:
    name = f"/tmp/watch_{int(time.time() * 1000)}.wav"
    subprocess.run(["scp", "-q", "-i", KEY, "-o", f"ControlPath={CTL_DIR}/%r@%h:%p", wav, f"{PI}:{name}"],
                   check=True, timeout=30)
    r = subprocess.run(ssh_base(PI) + [f"~/skelly-venv/bin/python ~/skelly_client.py play --prenorm {name}; rm -f {name}"],
                       capture_output=True, text=True, timeout=240)
    try:
        return json.loads(r.stdout[r.stdout.index("{"):])
    except Exception:
        return {"ok": False, "error": (r.stdout + r.stderr)[-300:]}


def daemon_ready() -> bool:
    r = subprocess.run(ssh_base(PI) + ["~/skelly-venv/bin/python ~/skelly_client.py status"],
                       capture_output=True, text=True, timeout=20)
    return '"READY"' in r.stdout


# ── Orchestration ───────────────────────────────────────────────────────────

class Heckler:
    def __init__(self, args, password: str | None):
        self.a, self.password = args, password
        self.busy = threading.Lock()
        self.cooldown_until = 0.0
        self.recent: list[str] = []

    def trigger(self, image: str | None = None):
        if time.time() < self.cooldown_until:
            log(f"trigger ignored (cooldown {self.cooldown_until - time.time():.0f}s)")
            return
        if not self.busy.acquire(blocking=False):
            log("trigger ignored (busy)")
            return
        threading.Thread(target=self._run, args=(image,), daemon=True).start()

    def _run(self, image: str | None):
        rec: dict = {"dry_run": self.a.dry_run}
        t0 = time.time()
        td = tempfile.mkdtemp(prefix="skelly-watch-")
        try:
            jpg = image or os.path.join(td, "frame.jpg")
            if not image:
                if self.a.settle > 0:
                    time.sleep(self.a.settle)        # let them walk into the frame
                grab_frame(self.password, jpg)
            rec["grab_s"] = round(time.time() - t0, 2)
            t1 = time.time()
            try:
                v = vision_line(jpg, self.recent, MODEL)
            except Exception as e:
                log(f"vision {MODEL} failed ({e!r}); trying {FALLBACK_MODEL}")
                v = vision_line(jpg, self.recent, FALLBACK_MODEL)
            rec["vision_s"] = round(time.time() - t1, 2)
            rec.update({k: v.get(k) for k in ("people", "costumes", "skip", "line")})
            log(f"vision: people={v.get('people')} costumes={v.get('costumes')!r} skip={v.get('skip')}")
            if v.get("skip") or not v.get("line"):
                log("skip (no trick-or-treater)")
                return
            line = v["line"]
            log(f"LINE: {line}")
            self.recent.append(line)
            t2 = time.time()
            wav = os.path.join(td, "line.wav")
            tts(line, wav)
            rec["tts_s"] = round(time.time() - t2, 2)
            if self.a.dry_run:
                keep = LOG.parent / "dryrun_last.wav"
                Path(wav).replace(keep)
                log(f"dry-run: not playing (audio at {keep}); to-sound would be "
                    f"{time.time() - t0 + 1.5:.1f}s")
            else:
                t3 = time.time()
                res = play(wav)
                rec["play"] = res
                rec["first_sound_s"] = round(t3 - t0 + (res.get("timing", {}) or {}).get("start_latency_s", 0) + 0.5, 2)
                log(f"played ok={res.get('ok')} first sound ≈{rec['first_sound_s']}s after trigger")
            if self.a.keep_frames and not image:
                Path(jpg).replace(LOG.parent / f"frame_{time.strftime('%H%M%S')}.jpg")
        except Exception as e:
            rec["error"] = repr(e)
            log(f"pipeline error: {e!r}")
        finally:
            rec["total_s"] = round(time.time() - t0, 2)
            jlog(rec)
            subprocess.run(["rm", "-rf", td])
            self.cooldown_until = time.time() + self.a.cooldown
            self.busy.release()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="vision + TTS, no playback")
    p.add_argument("--image", help="one-shot: use this image instead of camera events")
    p.add_argument("--cooldown", type=float, default=20.0, help="seconds after a line before the next (default 20)")
    p.add_argument("--settle", type=float, default=0.7, help="delay after detection before frame grab (s)")
    p.add_argument("--min-gap", type=float, default=4.0, help="person must be absent this long to count as a new visit")
    p.add_argument("--keep-frames", action="store_true", help="keep triggering frames next to the log (default: delete)")
    a = p.parse_args()

    if "OPENROUTER_API_KEY" not in os.environ:
        sys.exit("OPENROUTER_API_KEY not set")
    pw = os.environ.get("TAPO_PASS")
    h = Heckler(a, pw)

    if a.image:
        h.trigger(a.image)
        time.sleep(0.2)
        h.busy.acquire()
        return
    if not pw:
        sys.exit("TAPO_PASS not set")
    if not a.dry_run and not daemon_ready():
        log("WARNING: skelly-daemon not READY on the Pi — lines will fail until it is")
    PeopleWatcher(pw, h.trigger, a.min_gap).start()
    log(f"watching (model={MODEL}, cooldown={a.cooldown}s, dry_run={a.dry_run}) — Ctrl-C to stop")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
