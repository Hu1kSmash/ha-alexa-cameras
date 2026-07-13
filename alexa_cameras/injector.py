#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Tom Hirt
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Birdseye audio injector (experimental).

Plays announcement audio THROUGH a camera's stream instead of as a separate Alexa
announcement. A spoken Alexa announcement is a foreground interrupt that tears the
Echo Show's live camera view down; but a camera's *own* audio track is something the
Echo already plays. So for a silent source (Frigate birdseye is a video-only mosaic)
we synthesise an audio track and splice announcement audio into it on demand — the
camera view never drops.

For each camera with `audio_source: inject` in /data/config.yaml this:
  - creates a FIFO at /tmp/inject/<cam>.pcm
  - runs a feeder thread that writes 48 kHz s16le stereo SILENCE continuously and
    splices queued PCM (an announcement) in when asked.
run.sh points that camera's ffmpeg audio input at the FIFO, so whatever the feeder
writes becomes the stream's audio.

Control (LAN, port 8790). If `inject_token` is set in config, requests must carry it
(header `X-Inject-Token`, JSON field `token`, or `?token=`):
  POST /say  {"cam":"birdseye","text":"A vehicle is approaching"}  add-on makes the TTS
             (via HA tts_get_url using `tts_engine` from config, or a per-request `engine`)
             then fetches+decodes+injects it — automation stays trivial.
  POST /say  {"cam":"birdseye","url":"http://.../clip.mp3"}        fetch any audio URL+inject
  POST /say  {"cam":"birdseye","test":true}                        built-in test beep
  GET  /health                                                     {"ok":true,"cams":[...]}

Isolated from the :8888 HLS file server on purpose: if this process misbehaves it can
only affect injected audio, not camera serving (and run.sh restarts it).
"""
import json
import math
import os
import queue
import struct
import subprocess
import threading
import time
import urllib.request

import yaml
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RATE = 48000            # Hz  — must match run.sh's ffmpeg FIFO input (-ar)
CH = 2                  # stereo
SAMPLE_BYTES = 2        # s16le
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000          # 960
FRAME_BYTES = FRAME_SAMPLES * CH * SAMPLE_BYTES  # 3840
SILENCE = b"\x00" * FRAME_BYTES
BYTES_PER_SEC = RATE * CH * SAMPLE_BYTES

INJECT_DIR = "/tmp/inject"
CONFIG = "/data/config.yaml"
PORT = 8790
MAX_CLIP_SEC = 20       # cap a single injected clip so a bad URL can't flood the queue

# s6-overlay strips SUPERVISOR_TOKEN from the service env but persists it as a file.
S6_TOKEN_FILE = "/run/s6/container_environment/SUPERVISOR_TOKEN"

feeders = {}            # cam -> Feeder
# Runtime settings from /data/config.yaml (loaded at startup):
#   inject_token : shared secret required on /say (empty = no auth)
#   tts_engine   : default HA TTS entity for the {"text":...} convenience mode
#   ha_base      : base URL the add-on fetches HA audio from (internal, avoids hairpin NAT)
SETTINGS = {"token": "", "tts_engine": "", "ha_base": "http://homeassistant:8123"}


def load_settings():
    try:
        cfg = yaml.safe_load(open(CONFIG)) or {}
    except Exception:
        cfg = {}
    SETTINGS["token"] = str(cfg.get("inject_token", "")).strip()
    SETTINGS["tts_engine"] = str(cfg.get("tts_engine", "")).strip()
    SETTINGS["ha_base"] = (str(cfg.get("ha_base", "")).strip() or "http://homeassistant:8123").rstrip("/")


def _supervisor_token():
    try:
        return open(S6_TOKEN_FILE).read().strip()
    except Exception:
        return os.environ.get("SUPERVISOR_TOKEN", "")


def tts_url_for(text, engine=""):
    """Ask HA to render `text` and return a URL the add-on can fetch. Uses the s6 token
    to reach the core API via the Supervisor; fetches the audio from ha_base (internal
    hostname) to dodge the hairpin-NAT flakiness of HA's external LAN URL."""
    eng = (engine or SETTINGS["tts_engine"]).strip()
    if not eng:
        raise RuntimeError("no TTS engine — set 'tts_engine' in config or pass 'engine'")
    body = json.dumps({"engine_id": eng, "message": text}).encode()
    req = urllib.request.Request(
        "http://supervisor/core/api/tts_get_url", data=body,
        headers={"Authorization": "Bearer " + _supervisor_token(),
                 "Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=15))
    path = r.get("path") or ""
    if not path:
        raise RuntimeError("tts_get_url returned no path")
    return SETTINGS["ha_base"] + path


def _frames(pcm):
    """Yield fixed-size frames from a PCM blob, zero-padding the last one."""
    for i in range(0, len(pcm), FRAME_BYTES):
        frame = pcm[i:i + FRAME_BYTES]
        if len(frame) < FRAME_BYTES:
            frame = frame + SILENCE[len(frame):]
        yield frame


class Feeder(threading.Thread):
    """Writes real-time silence to <cam>'s FIFO, splicing queued announcement PCM."""

    def __init__(self, cam):
        super().__init__(daemon=True)
        self.cam = cam
        self.path = os.path.join(INJECT_DIR, cam + ".pcm")
        self.q = queue.Queue()
        try:
            os.mkfifo(self.path)
        except FileExistsError:
            pass

    def enqueue(self, pcm):
        # Cap the clip length, then split into frames the feeder loop drains.
        pcm = pcm[:MAX_CLIP_SEC * BYTES_PER_SEC]
        for frame in _frames(pcm):
            self.q.put(frame)
        return len(pcm)

    def _write_all(self, fd, buf):
        while buf:
            n = os.write(fd, buf)   # blocking write; paces us to ffmpeg's real-time read
            buf = buf[n:]

    def run(self):
        while True:
            try:
                fd = os.open(self.path, os.O_WRONLY)   # blocks until ffmpeg opens read
            except OSError:
                time.sleep(1)
                continue
            try:
                try:
                    import fcntl
                    fcntl.fcntl(fd, 1031, 1 << 18)     # F_SETPIPE_SZ 256KB — jitter cushion
                except Exception:
                    pass
                next_t = time.monotonic()
                while True:
                    try:
                        frame = self.q.get_nowait()
                    except queue.Empty:
                        frame = SILENCE
                    self._write_all(fd, frame)
                    # Pace to real time: blocking write handles the case where ffmpeg is
                    # slower, this sleep handles the case where it has buffered ahead.
                    next_t += FRAME_MS / 1000.0
                    dt = next_t - time.monotonic()
                    if dt > 0:
                        time.sleep(dt)
                    else:
                        next_t = time.monotonic()
            except BrokenPipeError:
                pass   # ffmpeg (reader) went away — drop and reopen
            except OSError:
                pass
            finally:
                try:
                    os.close(fd)
                except Exception:
                    pass
            time.sleep(0.2)


def beep_pcm(freq=660, ms=600, vol=0.25):
    """A short sine beep (built-in test source, no TTS needed)."""
    n = RATE * ms // 1000
    out = bytearray()
    amp = int(vol * 32767)
    for i in range(n):
        s = int(amp * math.sin(2 * math.pi * freq * i / RATE))
        out += struct.pack("<hh", s, s)   # stereo
    return bytes(out)


def decode_to_pcm(src, attempts=6, delay=1.0):
    """Fetch (if URL) and decode any audio source to 48k s16le stereo PCM via ffmpeg.

    Retries briefly: a freshly-minted TTS URL (e.g. Google Translate via tts_get_url) can
    404 for a moment while the engine renders and caches the audio. The first attempt
    tends to trigger that render, so a short retry then succeeds.
    """
    last = "decode failed"
    for i in range(attempts):
        p = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", src,
             "-f", "s16le", "-ar", str(RATE), "-ac", str(CH), "-t", str(MAX_CLIP_SEC), "-"],
            capture_output=True,
        )
        if p.returncode == 0 and p.stdout:
            return p.stdout
        last = (p.stderr or b"").decode("utf-8", "replace").strip()[:300] or "empty output"
        if i < attempts - 1:
            time.sleep(delay)
    raise RuntimeError(last)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            self._json(200, {"ok": True, "cams": list(feeders)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/say":
            self._json(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        try:
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            req = {}
        if SETTINGS["token"]:
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            given = self.headers.get("X-Inject-Token") or req.get("token") or (q.get("token") or [""])[0]
            if given != SETTINGS["token"]:
                self._json(403, {"error": "missing or bad token"})
                return
        cam = req.get("cam") or (next(iter(feeders), None))
        f = feeders.get(cam)
        if not f:
            self._json(400, {"error": "no inject camera '%s'" % cam, "cams": list(feeders)})
            return
        try:
            if req.get("test"):
                pcm = beep_pcm()
            elif req.get("text"):
                pcm = decode_to_pcm(tts_url_for(req["text"], req.get("engine", "")))
            elif req.get("url") or req.get("file"):
                pcm = decode_to_pcm(req.get("url") or req.get("file"))
            else:
                self._json(400, {"error": "provide 'text', 'url', or 'test'"})
                return
            written = f.enqueue(pcm)
            self._json(200, {"ok": True, "cam": cam, "ms": written * 1000 // BYTES_PER_SEC})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def log_message(self, *a):
        pass


def main():
    os.makedirs(INJECT_DIR, exist_ok=True)
    load_settings()
    try:
        cfg = yaml.safe_load(open(CONFIG)) or {}
    except Exception:
        cfg = {}
    for c in (cfg.get("cameras") or []):
        if str(c.get("audio_source", "")).strip() in ("inject", "inject_mix"):
            name = str(c.get("name", "")).strip()
            if name and name not in feeders:
                fdr = Feeder(name)
                feeders[name] = fdr
                fdr.start()
                print("[injector] feeding '%s' at %s" % (name, fdr.path), flush=True)
    print("[injector] control on :%d  cams=%s" % (PORT, list(feeders)), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
