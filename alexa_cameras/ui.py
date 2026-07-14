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
"""Ingress Web UI for the Alexa Cameras (HLS) add-on.

Self-managed config: the add-on's real configuration lives in /data/config.yaml,
edited from this UI (form or raw YAML) — NOT the Home Assistant add-on options —
so it needs no Supervisor API access. Saving writes the file and touches /tmp/reload,
which run.sh watches to restart the camera workers in-place.

Tabs: Overview, Configuration, Validate streams, Public URL check, Logs,
+ a hidden Advanced diagnostics page (deep stream probes; reached via the small
icon on Overview) that uses the /api/validate/{rate|keyframe|firstframe} endpoints.

JSON API (same origin, served through Home Assistant ingress):
  GET  /api/config      -> {data, yaml, error}        the current config
  GET  /api/cameras     -> [{name, mode, source}]     (source has the password masked)
  GET  /api/logs        -> {text}                      tail of the add-on log
  GET  /api/validate/{source|output|internal|public}?cam=&base=  -> {status, detail, msg}
  GET  /api/validate/{rate|keyframe|firstframe}?cam=  -> {status, detail, msg}   (Deep check)
  POST /api/config      {data|yaml} -> {ok, error}     validate + save + trigger reload
  POST /api/to-yaml     {data}      -> {yaml}          form  -> YAML (server-side)
  POST /api/from-yaml   {yaml}      -> {data, error}   YAML  -> form

Standard library + PyYAML (py3-yaml); ffmpeg/ffprobe from the image.
"""
import json
import os
import ipaddress
import re
import ssl
import socket
import subprocess
import time
import urllib.request
import urllib.error
import yaml
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HLS_PORT = 8888
INGRESS_PORT = 8099
CONFIG = "/data/config.yaml"
RELOAD = "/tmp/reload"
LOG = "/tmp/addon.log"
REPO = "https://github.com/Hu1kSmash/ha-alexa-cameras"
# The add-on manifest is copied to /manifest.yaml at build time; read the version from it.
try:
    VERSION = str((yaml.safe_load(open("/manifest.yaml")) or {}).get("version", "")) or "unknown"
except Exception:
    VERSION = "unknown"

# s6-overlay strips SUPERVISOR_TOKEN from the service env but persists it as a file.
S6_TOKEN_FILE = "/run/s6/container_environment/SUPERVISOR_TOKEN"


def tts_engines():
    """List HA TTS engine entities (tts.*) so the config form can offer them as a dropdown
    instead of a free-text guess. Uses the s6-persisted Supervisor token to reach HA's
    states API (needs homeassistant_api in the manifest)."""
    try:
        tok = open(S6_TOKEN_FILE).read().strip()
    except Exception:
        tok = os.environ.get("SUPERVISOR_TOKEN", "")
    try:
        req = urllib.request.Request("http://supervisor/core/api/states",
                                     headers={"Authorization": "Bearer " + tok})
        states = json.load(urllib.request.urlopen(req, timeout=8))
    except Exception as e:
        return {"engines": [], "error": str(e)[:200]}
    out = [{"id": s.get("entity_id", ""),
            "name": (s.get("attributes") or {}).get("friendly_name") or s.get("entity_id", "")}
           for s in states if str(s.get("entity_id", "")).startswith("tts.")]
    out.sort(key=lambda x: x["id"])
    return {"engines": out, "error": None}

COPY_OK_PROFILES = {"Constrained Baseline", "Baseline", "Main"}


# --------------------------------------------------------------------------- #
# Config file (the add-on's own source of truth)
# --------------------------------------------------------------------------- #
def load_config():
    """Return (data_dict, error_or_None)."""
    try:
        with open(CONFIG) as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return {}, "config root must be a mapping (key: value)"
        data.setdefault("cameras", [])
        return data, None
    except FileNotFoundError:
        return {"cameras": []}, None
    except yaml.YAMLError as e:
        return {"cameras": []}, str(e)
    except Exception as e:
        return {"cameras": []}, str(e)


def dump_yaml(data):
    return yaml.safe_dump(data or {}, sort_keys=False, default_flow_style=False,
                          allow_unicode=True)


def validate_config(data):
    if not isinstance(data, dict):
        return "config must be a mapping (key: value)"
    ip = str(data.get("lan_ip", "")).strip()
    if not ip:
        return ("Home Assistant IP is required — enter the Home Assistant server's "
                "internal (private) IPv4 address (e.g. 192.168.1.100), not a hostname.")
    try:
        addr = ipaddress.IPv4Address(ip)
    except (ipaddress.AddressValueError, ValueError):
        return (f"'{ip}' is not a valid IPv4 address. Enter the HA server's internal "
                "(private) IPv4 address (e.g. 192.168.1.100), not a hostname.")
    if not addr.is_private:
        return (f"'{ip}' is not a private/internal IPv4 address. Use the HA server's "
                "LAN IP (e.g. 192.168.x.x, 10.x.x.x, or 172.16-31.x.x).")
    cams = data.get("cameras")
    if cams is None:
        return "missing 'cameras:' list"
    if not isinstance(cams, list):
        return "'cameras' must be a list"
    seen = set()
    for i, c in enumerate(cams):
        if not isinstance(c, dict):
            return f"camera #{i + 1} must be a mapping"
        name = str(c.get("name", "")).strip()
        if not name:
            return f"camera #{i + 1} is missing a name"
        if not re.match(r"^[a-z0-9_]+$", name):
            return f"camera '{name}': use lowercase letters/numbers/underscore only"
        if name in seen:
            return f"duplicate camera name '{name}'"
        seen.add(name)
        if not str(c.get("host", "")).strip() and not str(c.get("url", "")).strip():
            return f"camera '{name}' needs either a host or a url"
        if str(c.get("mode", "")).strip() not in ("copy", "transcode"):
            return f"camera '{name}' mode must be 'copy' or 'transcode'"
    return None


def save_config(data):
    """Validate, write config.yaml atomically, trigger a reload. Returns error/None."""
    err = validate_config(data)
    if err:
        return err
    # A full `url` already includes its path, so a per-camera `path` is ignored alongside it —
    # drop it to keep the stored config honest (covers YAML-mode edits too, not just the form).
    for c in data.get("cameras") or []:
        if isinstance(c, dict) and str(c.get("url", "")).strip() and "path" in c:
            c.pop("path", None)
    try:
        tmp = CONFIG + ".tmp"
        with open(tmp, "w") as f:
            f.write(dump_yaml(data))
        os.replace(tmp, CONFIG)
        open(RELOAD, "w").close()
    except Exception as e:
        return f"could not write config: {e}"
    return None


def read_logs(max_bytes=200000):
    try:
        with open(LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Stream helpers / probes (source of cameras is now config.yaml)
# --------------------------------------------------------------------------- #
def mask(url):
    return re.sub(r"(://[^:/@]+:)[^@]*(@)", r"\1***\2", url or "")


def camera_source(cam, cfg):
    url = (cam.get("url") or "").strip()
    if url:
        return url
    user = str(cfg.get("rtsp_user", "") or "")
    pw = str(cfg.get("rtsp_password", "") or "")
    port = str(cfg.get("rtsp_port", 554) or 554)
    host = str(cam.get("host", "") or "").strip()
    path = cam.get("path") or cfg.get("default_path", "") or ""
    if pw:
        creds = f"{user}:{pw}@"
    elif user:
        creds = f"{user}@"
    else:
        creds = ""
    return f"rtsp://{creds}{host}:{port}{path}"


def ffprobe_streams(url, rtsp=False, timeout=15):
    cmd = ["ffprobe", "-v", "error"]
    if rtsp:
        cmd += ["-rtsp_transport", "tcp"]
    cmd += ["-rw_timeout", "8000000", "-print_format", "json", "-show_streams", url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timed out connecting to the stream"
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return None, (err[-1] if err else "ffprobe failed")
    try:
        streams = json.loads(p.stdout).get("streams", [])
    except Exception:
        return None, "could not parse ffprobe output"
    for s in streams:
        if s.get("codec_type") == "video":
            return s, None
    return None, "no video stream found"


def classify(stream):
    codec = (stream.get("codec_name") or "").lower()
    profile = stream.get("profile") or "?"
    w, h = stream.get("width"), stream.get("height")
    fps = stream.get("avg_frame_rate", "")
    label = f"{codec} {profile} {w}x{h}".strip()
    if fps and fps not in ("0/0", "0"):
        try:
            n, d = fps.split("/")
            label += f" @{round(int(n) / int(d))}fps"
        except Exception:
            pass
    if codec == "h264":
        return (profile in COPY_OK_PROFILES), label
    return False, label


def is_on_demand(cam):
    return str(cam.get("on_demand", "")).strip().lower() in ("true", "1", "yes", "on")


def source_kind(cam, cfg):
    """Best-effort classification of how a camera is sourced, for the Validate page:
      - direct   : built from a Host (IP) + the shared RTSP defaults
      - restream : a URL pointing at a *local* restreamer (go2rtc/Frigate/mediamtx) — one camera
                   stream is fanned out, so the add-on adds no extra load on the camera itself
      - url      : a full RTSP URL that isn't recognizably a local restream (pulled as-is)
    Restream detection keys on things a real camera never is: the HA host's own IP (lan_ip),
    localhost, a Frigate/go2rtc hostname, or port 8554 (the go2rtc/mediamtx restream port)."""
    url = str(cam.get("url", "")).strip()
    host = str(cam.get("host", "")).strip()
    if url:
        u = urlparse(url)
        h = (u.hostname or "").lower()
        try:
            port = u.port
        except ValueError:
            port = None
        lan = str(cfg.get("lan_ip", "")).strip().lower()
        if lan and h == lan:
            return {"label": "Restream", "why": "URL points at your HA host (lan_ip) — a local restream"}
        if h in ("127.0.0.1", "localhost", "::1"):
            return {"label": "Restream", "why": "URL points at localhost — a local restream"}
        if any(k in h for k in ("frigate", "go2rtc", "mediamtx", "restream")):
            return {"label": "Restream", "why": f"URL host '{h}' looks like a go2rtc/Frigate restreamer"}
        if port == 8554:
            return {"label": "Restream", "why": "URL uses port 8554 (go2rtc/mediamtx restream port)"}
        return {"label": "Direct URL", "why": "full RTSP URL used as-is (not a recognized local restream)"}
    if host:
        return {"label": "Direct", "why": "connects straight to the camera's IP using the RTSP defaults"}
    return {"label": "—", "why": ""}


def path_kind(cam, cfg):
    """For the Validate page: is the RTSP path the shared default, a per-camera override, or in a URL?"""
    if str(cam.get("url", "")).strip():
        return {"label": "in URL", "why": "the RTSP path is part of the full URL"}
    p = str(cam.get("path", "")).strip()
    if p:
        return {"label": "override", "why": "per-camera Path override: " + p}
    dp = str(cfg.get("default_path", "")).strip()
    return {"label": "default", "why": "shared Default RTSP path: " + (dp or "(none set)")}


def check_source(cam, cfg):
    src = camera_source(cam, cfg)
    mode = str(cam.get("mode", "transcode"))
    stream, err = ffprobe_streams(src, rtsp=src.startswith("rtsp"), timeout=18)
    if err:
        if is_on_demand(cam):
            return {"status": "idle", "detail": "idle",
                    "msg": "On-demand source isn't producing right now — expected when idle "
                           "(e.g. Frigate birdseye). It validates once it's active."}
        return {"status": "error", "detail": mask(src),
                "msg": f"Could not read the source: {err}."}
    copy_ok, label = classify(stream)
    if not copy_ok and mode == "copy":
        return {"status": "error", "detail": label,
                "msg": "Source is NOT Alexa-decodable in copy mode. Set mode: "
                       "transcode (or set the camera's sub stream to H.264 Baseline)."}
    if copy_ok and mode == "transcode":
        return {"status": "warn", "detail": label,
                "msg": "Source is already Alexa-ready H.264 — you could switch to "
                       "mode: copy to save CPU."}
    if not copy_ok and mode == "transcode":
        return {"status": "ok", "detail": label,
                "msg": "Source needs transcoding; mode: transcode converts it to "
                       "H.264 Baseline. Good."}
    return {"status": "ok", "detail": label,
            "msg": "Source is H.264 Baseline/Main and mode: copy — ideal."}


def _get_playlist(url, timeout=6):
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout, context=ctx) as r:
        return r.status, r.read().decode("utf-8", "replace")


def check_output(cam):
    name = cam.get("name", "")
    playlist = f"http://127.0.0.1:{HLS_PORT}/{name}/stream.m3u8"
    try:
        _, body1 = _get_playlist(playlist)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            if is_on_demand(cam):
                return {"status": "idle", "detail": "idle",
                        "msg": "No output yet — the on-demand source is idle (expected). "
                               "It'll serve once the source is active."}
            return {"status": "error", "detail": "404",
                    "msg": "No stream yet — ffmpeg hasn't produced a playlist. "
                           "Check the Logs tab for this camera."}
        return {"status": "error", "detail": str(e.code), "msg": "Playlist error."}
    except Exception as e:
        return {"status": "error", "detail": type(e).__name__,
                "msg": f"Could not fetch the local playlist: {e}."}

    def seq(txt):
        m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", txt)
        return int(m.group(1)) if m else None

    if ".ts" not in body1:
        return {"status": "warn", "detail": "empty",
                "msg": "Playlist exists but has no segments yet — starting up. Retry."}
    time.sleep(1.3)
    try:
        _, body2 = _get_playlist(playlist)
    except Exception:
        body2 = body1
    advancing = seq(body2) is not None and seq(body1) is not None and seq(body2) > seq(body1)
    stream, err = ffprobe_streams(playlist, rtsp=False, timeout=12)
    if err:
        return {"status": "warn", "detail": "live" if advancing else "stale",
                "msg": f"Serving, but a segment didn't probe cleanly: {err}."}
    copy_ok, label = classify(stream)
    if not advancing:
        if is_on_demand(cam):
            return {"status": "idle", "detail": "idle",
                    "msg": "Playlist isn't advancing — the on-demand source has gone idle "
                           "(expected). It resumes when the source is active again."}
        return {"status": "warn", "detail": label,
                "msg": "Serving, but the playlist is not advancing — ffmpeg may be "
                       "restarting. Check the Logs tab."}
    if not copy_ok:
        return {"status": "warn", "detail": label,
                "msg": "Live, but OUTPUT is not H.264 Baseline/Main — Alexa may show black."}
    return {"status": "ok", "detail": label, "msg": "Output is live and Alexa-decodable H.264."}


def check_internal(cam):
    """Quick LAN check: is the add-on serving this camera's playlist on :8888?"""
    name = cam.get("name", "")
    url = f"http://127.0.0.1:{HLS_PORT}/{name}/stream.m3u8"
    try:
        _, body = _get_playlist(url, timeout=5)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "error", "detail": "404", "msg": "not serving yet (check the Logs tab)"}
        return {"status": "error", "detail": str(e.code), "msg": "playlist error"}
    except Exception as e:
        return {"status": "error", "detail": type(e).__name__, "msg": "unreachable on :8888"}
    if ".ts" not in body:
        return {"status": "warn", "detail": "empty", "msg": "no segments yet"}
    return {"status": "ok", "detail": "live", "msg": "serving on :8888"}


def check_public(base, name):
    base = (base or "").strip().rstrip("/")
    if not base.startswith("http"):
        return {"status": "error", "detail": "", "msg": "Enter a full https:// URL."}
    url = f"{base}/{name}/stream.m3u8"
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "alexa-cameras-validate"})
        with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
            code = r.status
        return {"status": "warn", "detail": str(code),
                "msg": "Reachable (200) — the stream is NOT locked to Amazon's ASNs. "
                       "Anyone with the URL can view it; consider the WAF lockdown."}
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return {"status": "ok", "detail": "403",
                    "msg": "403 — DNS, tunnel and TLS work; the WAF lockdown is blocking "
                           "non-Amazon IPs (expected). Amazon's fetchers get through."}
        if e.code == 404:
            return {"status": "error", "detail": "404",
                    "msg": "Host reached but 404 — wrong camera name, or tunnel points "
                           "at the wrong service/port."}
        return {"status": "warn", "detail": str(e.code), "msg": f"Host returned HTTP {e.code}."}
    except ssl.SSLError as e:
        return {"status": "error", "detail": "TLS",
                "msg": f"TLS/certificate error: {e}. Alexa requires a valid cert."}
    except (urllib.error.URLError, socket.timeout, socket.error) as e:
        return {"status": "error", "detail": "unreachable",
                "msg": f"Could not reach the host: {e}. Check DNS / the tunnel."}


# --------------------------------------------------------------------------- #
# Deep diagnostics (opt-in "Deep check"): real-time rate, keyframe spacing,
# time-to-first-frame. Each samples a live stream for several seconds, so they
# are NOT part of the fast Validate — they answer "why is this stream slow to
# open / choppy?" (e.g. an idle Frigate birdseye that runs below real-time).
# --------------------------------------------------------------------------- #
def _playlist_seq_seg(txt):
    m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", txt)
    seq = int(m.group(1)) if m else None
    infs = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", txt)]
    if infs:
        seg = sum(infs) / len(infs)
    else:
        td = re.search(r"#EXT-X-TARGETDURATION:(\d+)", txt)
        seg = float(td.group(1)) if td else 1.0
    return seq, seg


def check_rate(cam, window=8.0):
    """Real-time factor of the add-on's OUTPUT: content-seconds produced per
    wall-second. ~1.0x is healthy; well under 1x means the stream is starved /
    time-dilated (e.g. an idle birdseye) and Alexa will open slowly or stutter."""
    name = cam.get("name", "")
    playlist = f"http://127.0.0.1:{HLS_PORT}/{name}/stream.m3u8"
    try:
        _, b1 = _get_playlist(playlist)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "error", "detail": "not serving",
                    "msg": "The output playlist isn't being served right now (404) — the transcode has "
                           "stalled or is restarting. For a deep-idle on-demand source (Frigate birdseye) "
                           "that lines up with the source probes below; otherwise check Output / the Logs tab."}
        return {"status": "warn", "detail": str(e.code),
                "msg": f"Output playlist returned HTTP {e.code}. See Output."}
    except Exception as e:
        return {"status": "warn", "detail": "n/a",
                "msg": f"Couldn't sample the output ({type(e).__name__}) — is it serving? See Output."}
    if ".ts" not in b1:
        return {"status": "warn", "detail": "empty", "msg": "No segments yet — starting up. Retry."}
    s1, seg1 = _playlist_seq_seg(b1)
    t0 = time.time()
    time.sleep(window)
    try:
        _, b2 = _get_playlist(playlist)
    except Exception:
        b2 = b1
    s2, seg2 = _playlist_seq_seg(b2)
    elapsed = time.time() - t0
    seg = seg2 or seg1 or 1.0
    if s1 is None or s2 is None:
        return {"status": "warn", "detail": "n/a", "msg": "Couldn't read the media sequence."}
    dseq = s2 - s1
    if dseq <= 0:
        return {"status": "error", "detail": "<0.2x",
                "msg": f"Produced <1 segment in {elapsed:.0f}s — far below real-time (starved). "
                       "Alexa will open slowly / stutter. On-demand source (e.g. Frigate birdseye)? "
                       "Raise its idle_heartbeat_fps / keyframe rate at the source."}
    factor = (dseq * seg) / elapsed
    det = f"{factor:.2f}x"
    if factor > 1.7:
        return {"status": "warn", "detail": det,
                "msg": "Running faster than real-time — catching up after a stall; the stream may be dropping/restarting."}
    if factor >= 0.7:
        return {"status": "ok", "detail": det, "msg": "Output keeps up with real-time."}
    if factor >= 0.4:
        return {"status": "warn", "detail": det,
                "msg": "Producing slower than real-time — the view may lag/stutter and open slowly."}
    return {"status": "error", "detail": det,
            "msg": "Far below real-time (starved). Alexa will open slowly / stutter. "
                   "On-demand source? For a Frigate birdseye, raise idle_heartbeat_fps at the source."}


def _probe_keyframe_gap(url, rtsp=False, read_sec=8, timeout=22):
    cmd = ["ffprobe", "-v", "error"]
    if rtsp:
        cmd += ["-rtsp_transport", "tcp"]
    cmd += ["-rw_timeout", "10000000", "-select_streams", "v:0",
            "-read_intervals", f"%+{read_sec}",
            "-show_entries", "frame=key_frame,best_effort_timestamp_time",
            "-print_format", "json", url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return None, (err[-1] if err else "ffprobe failed")
    try:
        frames = json.loads(p.stdout).get("frames", [])
    except Exception:
        return None, "could not parse frames"
    kts = []
    for f in frames:
        if str(f.get("key_frame")) == "1":
            try:
                kts.append(float(f.get("best_effort_timestamp_time")))
            except (TypeError, ValueError):
                pass
    if len(kts) < 2:
        return float(read_sec), None  # <2 keyframes in the window => gap >= read_sec
    kts.sort()
    return max(b - a for a, b in zip(kts, kts[1:])), None


def check_keyframe(cam, cfg):
    """Max keyframe spacing on the SOURCE. A fresh viewer can't render until it
    receives a keyframe, so a large gap = a slow first frame on Alexa."""
    src = camera_source(cam, cfg)
    gap, err = _probe_keyframe_gap(src, rtsp=src.startswith("rtsp"))
    if err == "TIMEOUT":
        return {"status": "error", "detail": ">22s",
                "msg": "The source didn't deliver two keyframes within the probe window — it's "
                       "severely keyframe-starved or stalled, so a fresh view is very slow to appear. "
                       "Classic idle on-demand source (a Frigate birdseye at deep idle): raise "
                       "idle_heartbeat_fps, or check that the source is actually alive."}
    if err:
        return {"status": "warn", "detail": "n/a", "msg": f"Couldn't measure keyframe spacing: {err}."}
    det = f"~{gap:.1f}s"
    if gap <= 3.0:
        return {"status": "ok", "detail": det, "msg": "Normal keyframe spacing — a fresh view opens promptly."}
    if gap <= 6.0:
        return {"status": "warn", "detail": det,
                "msg": f"Sparse keyframes ({det}) — Alexa may lag a few seconds opening a fresh view."}
    return {"status": "error", "detail": (f">={gap:.0f}s" if gap >= 8 else det),
            "msg": f"Very sparse keyframes ({det}) — Alexa can take that long to show a fresh view. "
                   "On-demand source? For a Frigate birdseye, raise idle_heartbeat_fps."}


def _time_to_first_frame(url, rtsp=False, timeout=22):
    cmd = ["ffmpeg", "-v", "error"]
    if rtsp:
        cmd += ["-rtsp_transport", "tcp"]
    cmd += ["-i", url, "-frames:v", "1", "-an", "-f", "null", "-"]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    dt = time.time() - t0
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return None, (err[-1] if err else "ffmpeg failed")
    return dt, None


def check_firstframe(cam):
    """Wall time to open the add-on's HLS OUTPUT and decode the first frame — what
    Alexa actually does. Reflects the *served* keyframe/segment cadence, unlike a raw
    source RTSP connect (which is dominated by RTSP negotiation overhead)."""
    name = cam.get("name", "")
    playlist = f"http://127.0.0.1:{HLS_PORT}/{name}/stream.m3u8"
    try:
        _, body = _get_playlist(playlist)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "error", "detail": "not serving",
                    "msg": "The output playlist isn't being served (404) — the transcode has stalled or "
                           "is restarting, so Alexa would get nothing right now. See Output / the Logs tab."}
        return {"status": "warn", "detail": str(e.code), "msg": f"Output returned HTTP {e.code}. See Output."}
    except Exception as e:
        return {"status": "warn", "detail": "n/a", "msg": f"Couldn't reach the output ({type(e).__name__}). See Output."}
    if ".ts" not in body:
        return {"status": "warn", "detail": "empty", "msg": "No segments yet — starting up. Retry."}
    dt, err = _time_to_first_frame(playlist, rtsp=False)
    if err == "TIMEOUT":
        return {"status": "error", "detail": ">22s",
                "msg": "Alexa's stream didn't decode a frame within 22s — the output is stalled. "
                       "Deep-idle on-demand source (a Frigate birdseye)? Raise idle_heartbeat_fps; "
                       "otherwise check Output / the Logs tab."}
    if err:
        return {"status": "error", "detail": "n/a", "msg": f"Couldn't decode a first frame from the output: {err}."}
    det = f"{dt:.1f}s"
    if dt <= 2.5:
        return {"status": "ok", "detail": det, "msg": "Opens quickly — Alexa shows the picture promptly."}
    if dt <= 5.0:
        return {"status": "warn", "detail": det, "msg": f"{det} to first frame — a noticeable open delay."}
    return {"status": "error", "detail": det,
            "msg": f"{det} to first frame — slow open. Usually a keyframe-sparse or stalled/on-demand source."}


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        # Silence this server's per-request logging — the :8888 HLS file server does
        # the access logging that the Logs tab surfaces; this UI server would just spam it.
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.strip("/")
        q = parse_qs(u.query)
        cfg, cfg_err = load_config()
        cams = cfg.get("cameras", []) or []

        if path in ("", "index.html"):
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        if path == "api/config":
            return self._send(200, json.dumps({
                "data": cfg, "yaml": dump_yaml(cfg), "error": cfg_err}))
        if path == "api/logs":
            return self._send(200, json.dumps({"text": read_logs()}))
        if path == "api/tts_engines":
            return self._send(200, json.dumps(tts_engines()))
        if path == "api/cameras":
            def cam_info(c):
                sk = source_kind(c, cfg)
                pk = path_kind(c, cfg)
                return {
                    "name": c.get("name", ""), "mode": c.get("mode", ""),
                    "audio": str(c.get("audio_source", "")).strip(),
                    "on_demand": is_on_demand(c),
                    "source_label": sk["label"], "source_why": sk["why"],
                    "path_label": pk["label"], "path_why": pk["why"],
                    "source": mask(camera_source(c, cfg))}
            return self._send(200, json.dumps([cam_info(c) for c in cams]))
        if path.startswith("api/validate/"):
            name = q.get("cam", [""])[0]
            cam = next((c for c in cams if c.get("name") == name), None)
            kind = path.rsplit("/", 1)[-1]
            force = q.get("force", ["0"])[0] in ("1", "true", "yes")
            if not cam and kind != "public":
                return self._send(404, json.dumps({"status": "error", "msg": f"No camera '{name}'."}))
            # On-demand cameras are skipped during a normal validation — probing them would
            # wake the source (e.g. Frigate birdseye). The card's "Check on-demand stream"
            # button passes force=1 to run the live check anyway.
            if cam and is_on_demand(cam) and not force and kind in ("source", "output"):
                return self._send(200, json.dumps({
                    "status": "idle", "detail": "on-demand",
                    "msg": "On-demand camera — not queried, so its source isn't woken. "
                           "Use “Check on-demand stream” on this card to test it live."}))
            if kind == "source":
                return self._send(200, json.dumps(check_source(cam, cfg)))
            if kind == "output":
                return self._send(200, json.dumps(check_output(cam)))
            if kind == "internal":
                return self._send(200, json.dumps(check_internal(cam)))
            if kind == "rate":
                return self._send(200, json.dumps(check_rate(cam)))
            if kind == "keyframe":
                return self._send(200, json.dumps(check_keyframe(cam, cfg)))
            if kind == "firstframe":
                return self._send(200, json.dumps(check_firstframe(cam)))
            if kind == "public":
                return self._send(200, json.dumps(check_public(q.get("base", [""])[0], name)))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = urlparse(self.path).path.strip("/")
        body = self._body()
        if path == "api/config":
            if "yaml" in body:
                try:
                    data = yaml.safe_load(body["yaml"]) or {}
                except yaml.YAMLError as e:
                    return self._send(200, json.dumps({"ok": False, "error": "YAML: " + str(e)}))
            else:
                data = body.get("data") or {}
            err = save_config(data)
            return self._send(200, json.dumps({"ok": err is None, "error": err}))
        if path == "api/to-yaml":
            return self._send(200, json.dumps({"yaml": dump_yaml(body.get("data") or {})}))
        if path == "api/from-yaml":
            try:
                return self._send(200, json.dumps({"data": yaml.safe_load(body.get("yaml", "")) or {}, "error": None}))
            except yaml.YAMLError as e:
                return self._send(200, json.dumps({"data": None, "error": str(e)}))
        if path == "api/say":
            # Quick audio-injection test from the Validate page: inject a fixed spoken message into
            # this camera's stream via the local injector (:8790). (Automations still use the /say
            # control API directly; this is just a one-click check.)
            cfg, _ = load_config()
            cams = cfg.get("cameras", []) or []
            name = parse_qs(urlparse(self.path).query).get("cam", [""])[0]
            cam = next((c for c in cams if c.get("name") == name), None)
            if not cam:
                return self._send(200, json.dumps({"ok": False, "error": f"No camera '{name}'."}))
            if str(cam.get("audio_source", "")).strip() not in ("inject", "inject_mix"):
                return self._send(200, json.dumps({"ok": False, "error": "This camera has no audio injection set."}))
            payload = {"cam": name, "text": f"Audio injection test on the {name} camera."}
            tok = str(cfg.get("inject_token", "")).strip()
            if tok:
                payload["token"] = tok
            try:
                req = urllib.request.Request("http://127.0.0.1:8790/say",
                    data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    return self._send(200, json.dumps({"ok": True, "resp": json.load(r)}))
            except urllib.error.HTTPError as e:
                return self._send(200, json.dumps({"ok": False,
                    "error": f"injector {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return self._send(404, json.dumps({"error": "not found"}))


INDEX_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alexa Cameras</title>
<style>
  :root { color-scheme: light dark; --line: rgba(128,128,128,.32); --dim: rgba(128,128,128,.12); }
  * { box-sizing: border-box; }
  body { font-family: system-ui,-apple-system,Segoe UI,Roboto,sans-serif; margin:0; padding:0 16px 24px; line-height:1.45; }
  a { color: inherit; }
  .hdr { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:14px 0; border-bottom:1px solid var(--line);
         position:sticky; top:0; background:Canvas; z-index:5; }
  .brand { font-weight:700; font-size:1.05rem; margin-right:auto; }
  .links { display:flex; flex-wrap:wrap; gap:6px; }
  .links a { text-decoration:none; font-size:.82rem; padding:5px 10px; border-radius:8px; border:1px solid var(--line); white-space:nowrap; }
  .links a:hover, button:hover { background:var(--dim); }
  .tabs { display:flex; flex-wrap:wrap; gap:4px; margin:14px 0 16px; }
  .tab { font:inherit; font-size:.92rem; padding:7px 14px; cursor:pointer; background:transparent; border:1px solid var(--line); border-radius:999px; color:inherit; }
  .tab.active { background:#3b82f6; border-color:#3b82f6; color:#fff; }
  h2 { font-size:.9rem; margin:0 0 12px; color:#3b82f6; text-transform:uppercase; letter-spacing:.05em; }
  .panel { border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin-bottom:14px; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  @media (max-width:640px){ .grid2 { grid-template-columns:1fr; } }
  .kv { display:grid; grid-template-columns:auto 1fr; gap:4px 14px; font-size:.9rem; }
  .kv .k { opacity:.6; }
  code, code.inline { font-family:ui-monospace,monospace; font-size:.82rem; background:var(--dim); padding:1px 6px; border-radius:6px; }
  .quick { display:flex; flex-direction:column; gap:8px; }
  .quick a, .quick button { text-decoration:none; padding:9px 12px; border:1px solid var(--line); border-radius:9px; font-size:.9rem; text-align:left; background:transparent; color:inherit; cursor:pointer; transition:background .12s, border-color .12s; }
  .quick a:hover, .quick button:hover { background:rgba(59,130,246,.09); border-color:rgba(59,130,246,.5); }
  table.cams { width:100%; border-collapse:collapse; font-size:.88rem; }
  table.cams th, table.cams td { text-align:left; padding:6px 6px; border-bottom:1px solid var(--line); }
  table.cams th { opacity:.6; font-weight:600; }
  table.cams td.src { font-family:ui-monospace,monospace; font-size:.78rem; opacity:.75; word-break:break-all; }
  table.cams input, table.cams select { width:100%; font:inherit; font-size:.82rem; padding:4px 6px; border:1px solid var(--line); border-radius:6px; background:transparent; color:inherit; }
  .mode { font-size:.72rem; padding:1px 8px; border-radius:999px; border:1px solid var(--line); }
  .cfg { display:grid; grid-template-columns:repeat(5,84px); gap:6px; align-items:center; }
  .cfg .c { font-size:.72rem; padding:1px 6px; border-radius:999px; border:1px solid var(--line); text-align:center; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; cursor:help; }
  .cfg .c.rst { border-color:#2e9d6e; color:#2e9d6e; }
  .cfg .c.off { opacity:.4; border-style:dashed; cursor:default; }
  .cfg .c.aud { cursor:pointer; border-color:#7c5cff; color:#7c5cff; }
  .cfg .c.aud:hover { background:rgba(124,92,255,.14); }
  .vhead { display:flex; align-items:center; gap:10px; padding:2px 15px 8px; }  /* right pad matches .card (14px + 1px border) so header columns align over the pills */
  .vhead .cfg .c { border:none; opacity:.55; font-weight:600; padding:0 6px; cursor:default; }
  button.primary { font:inherit; font-weight:600; padding:8px 15px; border-radius:9px; cursor:pointer; border:1px solid #3b82f6; background:#3b82f6; color:#fff; }
  button.primary:hover { background:#2563eb; border-color:#2563eb; }
  button { font:inherit; padding:6px 12px; border-radius:8px; cursor:pointer; border:1px solid var(--line); background:transparent; color:inherit; }
  .vbtn { font-size:.78rem; padding:4px 11px; border-radius:7px; white-space:nowrap; }
  p.sub { margin:0 0 12px; opacity:.75; font-size:.9rem; }
  .cfg-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px 16px; }
  @media (max-width:640px){ .cfg-grid { grid-template-columns:1fr; } }
  .cfg-grid label { display:flex; flex-direction:column; gap:4px; font-size:.85rem; opacity:.9; }
  .cfg-grid input, .cfg-grid select { font:inherit; padding:6px 10px; border:1px solid var(--line); border-radius:8px; background:transparent; color:inherit; }
  .cfg-grid select option { background:Canvas; color:CanvasText; }
  label.single { display:flex; flex-direction:column; gap:6px; font-size:.85rem; opacity:.9; }
  label.single input { font:inherit; padding:6px 10px; border:1px solid var(--line); border-radius:8px; background:transparent; color:inherit; width:100%; }
  .iprow { display:flex; align-items:center; gap:8px; margin-top:6px; }
  .octet { width:64px; text-align:center; font:inherit; font-size:.9rem; padding:6px 6px; border:1px solid var(--line); border-radius:8px; background:transparent; color:inherit; }
  .ipdot { font-weight:700; font-size:1.15rem; opacity:.55; }
  #yamlbox, #logbox { width:100%; font-family:ui-monospace,monospace; font-size:.8rem; border:1px solid var(--line); border-radius:10px; background:var(--dim); color:inherit; padding:12px; }
  #yamlbox { height:calc(100vh - 240px); min-height:320px; resize:vertical; }
  #logbox { min-height:460px; max-height:70vh; overflow:auto; white-space:pre; margin:0; }
  .row { display:flex; flex-wrap:wrap; align-items:center; gap:10px; }
  .publicbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:4px 0 14px; }
  input[type=text].pub { font:inherit; padding:6px 10px; border-radius:8px; min-width:240px; border:1px solid var(--line); background:transparent; color:inherit; }
  .card { border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin-bottom:12px; }
  .name { font-weight:600; font-size:1.05rem; }
  .src2 { font-family:ui-monospace,monospace; font-size:.8rem; opacity:.7; word-break:break-all; margin:4px 0 10px; }
  .results { margin-top:10px; display:grid; gap:6px; }
  .res { display:grid; grid-template-columns:80px 1fr; gap:8px; align-items:start; font-size:.88rem; }
  .res .k { font-weight:600; opacity:.8; }
  .res .v { display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; }
  .badge { font-size:.72rem; font-weight:700; padding:1px 8px; border-radius:999px; white-space:nowrap; }
  .ok { background:rgba(34,197,94,.18); color:#16a34a; }
  .warn { background:rgba(234,179,8,.20); color:#b45309; }
  .error { background:rgba(239,68,68,.18); color:#dc2626; }
  .idle { background:rgba(59,130,246,.16); color:#2563eb; }
  .pending { opacity:.6; }
  .detail { font-family:ui-monospace,monospace; font-size:.8rem; opacity:.85; }
  .pline { margin-bottom:12px; }
  .prow { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .plabel { font-size:.68rem; font-weight:700; letter-spacing:.05em; text-transform:uppercase; padding:2px 9px; border-radius:999px; border:1px solid var(--line); }
  .plabel.int, .plabel.ext { color:#2563eb; border-color:rgba(59,130,246,.45); background:rgba(59,130,246,.06); }
  .purl { display:block; margin-top:3px; word-break:break-all; text-decoration:none; color:#2563eb; opacity:1; }
  .purl:hover { text-decoration:underline; }
  /* focus + field polish */
  input:focus, select:focus, textarea:focus { outline:none; border-color:#3b82f6; box-shadow:0 0 0 2px rgba(59,130,246,.22); }
  /* cards stand out: white in light mode, an elevated surface in dark (keeps text readable) */
  .panel, .card { background:#fff; box-shadow:0 1px 3px rgba(0,0,0,.07); }
  @media (prefers-color-scheme: dark){ .panel, .card { background:rgba(255,255,255,.05); box-shadow:none; } }
  :root[data-theme="light"] .panel, :root[data-theme="light"] .card { background:#fff; box-shadow:0 1px 3px rgba(0,0,0,.07); }
  :root[data-theme="dark"] .panel, :root[data-theme="dark"] .card { background:rgba(255,255,255,.05); box-shadow:none; }
  #f-port { max-width:150px; }
  .pwwrap { display:flex; gap:6px; }
  .pwwrap input { flex:1; min-width:0; }
  .pwtoggle { border:1px solid var(--line); background:transparent; color:inherit; border-radius:8px; padding:0 12px; cursor:pointer; font-size:.82rem; }
  .pwtoggle:hover { background:var(--dim); }
  /* cameras table proportions + interactions */
  #camrows { table-layout:fixed; width:100%; min-width:760px; }
  #camrows th:nth-child(1),#camrows td:nth-child(1){ width:14%; }
  #camrows th:nth-child(2),#camrows td:nth-child(2){ width:13%; }
  #camrows th:nth-child(3),#camrows td:nth-child(3){ width:20%; }
  #camrows th:nth-child(4),#camrows td:nth-child(4){ width:16%; }
  #camrows th:nth-child(5),#camrows td:nth-child(5){ width:10%; }
  #camrows th:nth-child(6),#camrows td:nth-child(6){ width:13%; }
  #camrows th:nth-child(7),#camrows td:nth-child(7){ width:10%; text-align:center; }
  #camrows th:nth-child(8),#camrows td:nth-child(8){ width:4%; text-align:right; padding-right:0; }
  table.cams tr:hover td { background:rgba(59,130,246,.06); }
  .card { transition:border-color .12s, background .12s; }
  .card:hover { border-color:rgba(59,130,246,.5); background:rgba(59,130,246,.045); }
  .btn-del { border:none; background:transparent; color:#dc2626; font-size:1.05rem; line-height:1; padding:5px 9px; border-radius:8px; cursor:pointer; opacity:.5; }
  .btn-del:hover { background:rgba(239,68,68,.15); opacity:1; }
  .btn-add { border:1px dashed #3b82f6; color:#3b82f6; font-weight:600; }
  .btn-add:hover { background:rgba(59,130,246,.10); }
  [hidden] { display:none !important; }
  /* Advanced diagnostics page */
  .dbg-dot { cursor:pointer; opacity:.28; font-size:1.15rem; transition:opacity .15s; user-select:none; }
  .dbg-dot:hover { opacity:.75; }
  .dbg-head { display:flex; gap:14px; align-items:flex-start; justify-content:space-between; margin-bottom:16px; flex-wrap:wrap; }
  .dbg-actions { display:flex; gap:8px; align-items:center; }
  .dbg-cam { margin-bottom:18px; }
  .dbg-cam-head { display:flex; align-items:center; gap:10px; margin-bottom:9px; }
  .dbg-cam-head .name { font-weight:700; font-size:1.05rem; }
  .dbg-metrics { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
  @media (max-width:760px){ .dbg-metrics { grid-template-columns:1fr; } }
  .dbg-metric { border:1px solid rgba(0,0,0,.09); border-left:4px solid #cbd5e1; border-radius:12px; padding:14px; background:rgba(0,0,0,.015); }
  .dbg-metric.ok { border-left-color:#16a34a; }
  .dbg-metric.warn { border-left-color:#d97706; }
  .dbg-metric.error { border-left-color:#dc2626; }
  .dbg-metric.pending { border-left-color:#94a3b8; opacity:.7; }
  .dbg-metric-head { display:flex; align-items:center; justify-content:space-between; gap:8px; }
  .dbg-metric-title { font-weight:600; font-size:.9rem; }
  .dbg-badge { font-size:.66rem; font-weight:700; letter-spacing:.04em; padding:2px 7px; border-radius:999px; white-space:nowrap; }
  .dbg-badge.ok { background:#16a34a; color:#fff; } .dbg-badge.warn { background:#d97706; color:#fff; } .dbg-badge.error { background:#dc2626; color:#fff; }
  .dbg-value { font-size:1.7rem; font-weight:700; margin:8px 0 6px; font-variant-numeric:tabular-nums; }
  .dbg-desc { font-size:.78rem; opacity:.7; line-height:1.4; }
  .dbg-verdict { font-size:.83rem; margin-top:8px; line-height:1.4; }
  .dbg-value .dots { font-size:.9rem; font-weight:400; opacity:.6; }
  @media (prefers-color-scheme: dark){ .dbg-metric { border-color:rgba(255,255,255,.1); background:rgba(255,255,255,.03); } }
  :root[data-theme="dark"] .dbg-metric { border-color:rgba(255,255,255,.1); background:rgba(255,255,255,.03); }
</style></head>
<body>
  <header class="hdr">
    <div class="brand"><img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAj0AAABICAYAAAAd4URaAACIGElEQVR4nOx9d3gdxfX2e2Z2by/qsmy5G9x7wRgbGTC9YyRKCBBIAoQACekkxJiShIQkEEoSQu9YNgFCN8WyCZhiwN0Y965ebr+7M+f7Y6/kpivLYBvy+/Q+z2KhezU7u3Nm5swp7wG60IUudKELXehCF7rQhS50oQtd6EIXutCFLnShC13oQhe60IUudKELXehCF7rQhS50oQtd6EIXutCFLnShC134HwNlri50oQtd6EIXutCF/xMgAAKABMoMlJUZmMECQgLUpfN0oQtd6EIXutCF/11Qm3LDGeWmfQXHABD6GvrXhS50oQtd6MJ+oeuI3oXOwg2gWAL93L7wAKOo71Ajp3t/V8lhg9M1aym96KWJUaI6MAsA+uvubBe60IUudKELe8L4Gu65awyIAMYShiQIGAqkI87vXUEGlgMrvAwsYuzcRDlzdeHggQBwMBjM1zJwgVHQa5CZ12OQzC3tY+SW9HAV9vG4ivrAyC2B8IZgFuSidtYdVsOil/wgUQdWX3f/u9CFLnShC11oF4dS6cnEhAyRKB0qEYLEjo0CDZ8LrGgmYIXATmUoo+iEGXkTNLr11ghBoWW5wooVyvmsS/k5OCiTQJWdTuP0Hlfedbd36DEQLjfI5QORBLNigBVsC9pOQceVYJVOocu604WvF4TdDlRle1ixixioBHYenLrWjy504f9DHGylp3URkiidaMCTY2LNawa2rHADCBjB4jzXYUcVyPze+TKYFyRXwAOhwXY8qWJ1EVW/viG9aXGtveLDBgBRAClMOMmGaLKwcKENQOH/1gLWGizcWRxUs4pZ3M+W4QKlI42GtpsJrIlARIIMgADWDBLUFcncha8JAmVlAkVXM+ZckFkLGGAGuGrvbxMBtMv0mvIbA1XzAFRpdCntXejC/xc4mEqPk+lTfLwL7h0ubFroAVDoHX7hQF/fY0ebRQNHiGBxP+ELFMJ0haRpuGBKQDKIbGi20oR0i0611KlY7dp09cqliWVvfxpbWLkKQC16DU8ip1saS+am4Wz+/xcWLQaRAqNz0VbMhIOo8HEqbnA6TQBLIrFzw9jVHteFLhxaEFAmIRbYADSqqjRQBQASQHc4cWfFpvDksOn2AABpy9JWPKaYGxSrGgA7ANSjaqYNwAnQVzaBpkqgqvUg1YUudOH/IA6G0uNYdkrGuiBND7bM9RmBnn0Cx/11qrtP2fFGbu9R0pcTIpcAE0CwwWQBUI7lgBkQBgnTdAkzVGDkFRfAGDzIO3DCqYEJZ7Tkn/2Tz+Kr5r/Z9NLd79iblm4IDZkYb2lBAlsWZhr5n1ywWtUIn2QuA0AdPAUDMAhotIkWgA/i41KXEacL3xgQyssF5jynoKvszBFnpDdYdIznsBGT3T0GDDeLSnsY+d39RjgfwhcCmS4nQE3Z0KkEVKwZdlMN7MbqOmvHpg3pjas/TX6+aH5SW/8F0XoANoQEpp8jUVnZ5ULvQhf+D+JAKz0CgIF+07xY96YPRqhn+Lj7TvX1P+FcI6fXEHKZYG2B0wmtbdYQGmQQSLJgAaLWHZY1Q2vWtqWhNUgCZJCQ3kDIzBtztKvv4KMDR5y6Irb4jTm1j/z2ZSQimzB2WhyL3kwAcE6A/0soK5Oomm8Huw/8beHZM39O3iCgVfsKB2uQO4D4yndQ8/wtk2zmhSASOMiuri504WuEhJAKlZUKQJHXm3O+f/wJF/qHHTnBN/wocnXvB+EPg6QAgxnsXJxRWghMIAEQEREJEArYSheo5vpxqY2rvhdf/kE8sfKj9yMfvTErZcWfQ2VlHYQAtD6oltQudKELhx4HUumRQG8T3Qf4se7NXN+IK8pCY374fSN/yARhCGg7zjqR1pAMIUgIw2uQW4IkwNBgTgE6DQgGpEnkdpFwGQJSA5wC6ySznVYqngIZJFzd+w4xu18+xDd68klN//7H/c2vPjgvOP64xkjNmhg2bmy1+vwvgDBvngJRkX/kqd8PTalQKmkzQVD7Li4GmdL29Bnlii5782ctJM5B+SygsuJQ97sLXTjYcKw7s2craJXvzS25NufY87+fc8y53TwDRoFcHmhl22wlScWbCZoJBCIC7eof5rZ/2/QXJiGYAjnaN/po8o89xqcTseOSX3x6XMv852c2v/Pco7G6jfcC2IxMNuMhfeoudKELBw0HSumRKBnrhrYC2PZWj9wTHr3Ee9hZl0tvKKCtOCubmQgg05TC7QJrCzpWV6/qq9ep+LZ1Olq9xU7UNbIdjUNoSE/AJ0N5uTKnpIeZ362fzC3oL8N5+cLrkWxHwZzWKhlhkkyevsPHF155y2DvqCMe2vH77z/q7ztsa6xkbATbF6XwP6H4lEkIYftySq8IH3VRjoqlbE5GDOeFtf8XrJWQ4SIOTZh+Wsua94dhVvlyECT+J563C13oFASE0KisVAK4sOiU796Wd9YP+ngGjARrW+lElDgZFyTIcGzEwonq6cAbS23/ySRYWCmh00kwaybD1N5hE+EfMalb3hnf/UX9v//xvYaX778glUrNBbrmVhe68H8FB0LpkcAID+xE0Ig0HhY+p+rn3p5HnwatoBNx7YQzG0J43FCp5mRq2/IPk9venZde+8anifVvb4YVbwKQwu5uqUy5A3hg+sLeIWW9fKOPH+0bPHGqq3u/8TI316vTEYBsrWKNLDzuQPjEC641u5X2337LZX+E370axSMiqF6SxDd7sSLwPAWicHDs6Ve5ew5nFW8WJI3WT9v/IxLE6YQKTTjXDFY9dl2ExPdQXg5UVh66nnehCwcPjjtLq0CgdPDdxZf85tLQ1AowK9tubpAkSEIIkJRf7S6ZmDWCIDBLjrXAVjZ7Bg6xgkeckFfzwj89IOKDGjfXhS504ZDiqyo9EiUlbjTbQRmPDco786VbXCVjJutUUoPYWU5Mr9Aqbic3L3wrvvzR2dHP/rEIQA0CJSn0nGTBk6vQmNBwRXaPwykICiS8AsnGusTazzYlFr/6UT3wfPjE748NHXfeue7Dhh8nfQFDpyKaVZqRTHPgiGNPLb1jdnjzr7/1W7eneWW8pISxffs32OLjWHncgZxvhY68oIS1VuQoex2DCDoVk2ZBbw6OP+P8yNYlN2PWrC2Z2J7/rXimLnRhd7QqPD3zxp44p+SHfxnv6jNEqZZ6IoZBUh48HnkhADJYR5Ou5nlz1iqVfhVCAqy65lQXuvB/BF9F6RFAbxM6HDB07WF5Z7x8i6t49GSdTCnnM2LyeoTVvGVtbOWD/4y8d9MbgK8Gvc9MIK3S2L7ORvTNVn6MvY9SGwHsJDQUKOluhItKY82v37+j+fX7P8r/1i9OCJ18yRXunn3760STBjGpaIPyDR8zuefvH7tly/Xn3eAPFa2K9XZpbNy4K6vzNwWtVh5XaPhp13r7HcEqFSWiztH0EAnSdsoOHXl+oHnBY1fGSPzaCYiu+qY9Zxe60FmIjMLTt+Doc+f2+PF9/ckftlVTrUHSOPhFc5gh3D5tbd8oYkve+zcAC1MmG6iqsg/ynbvQhS4cIuwPEd6uIAAGug/wo3pJj/CJz/7C1W30ZJ1KKmgIQIBMj0ht/uDNhhemXxd576ZnUXrCZpSOaMLGF6LY/lISWJHGTpcWZ7m0850VaWxflGxe/EIUpaVNoYknbK5/8vZnt8688NrEsnfnCr9fwCQQsVCRRuUbMf6o7r978BexdUt6BIsG+OEod9+03GsJIdglXWeGJlYMhDA0ad358SABnYxKV/fBHBhz+mUAhzFvnsI37zm70IXOQICZoVVhwVFnvdrjp/f3J0/A1vEWo83d21kwA1o7GZBaZX7WGdLCDrggtIYwXTKy6G2Obl39LIQAqqq6fFtd6ML/IXxZpUci7yQPtr2Vk3vso5d6uk85VSdSGpoESDAJg1LrX36qtnLyb9Jo+QT5kxqx5Y0ItizcM3YH2MnaLIAZmQtil9+3wlGAtmxJtSx8I4KBkxqNdPqTjVcf/5vI+68+KdwugikYAkLHm1RgwqRTut/69+9EPnorN3fsNC864zY6lJjBGswIDDv+R74hx0CnIg5J2n6AQMRa69DE87q5vbkXggQ7ZSS60IX/KRDKZxGIKGfwpGe6X3fPQHJ5bU7FDOrsnNAarBRDCEVuny38ISUCOdq5wor8IZvcPhuGaYNZs7IZeg+jqJCarTTFP52/GMAnUIrwjXWNd6ELXfgy+DLuLYHSiSa2zPf7Bl9Z5ulz9uWsVGs2KJNhiuSGV56uf/m0O/x9j90WS66MYPuq1tpMbaem8vJyOWvWLBimqVhrMDMzzwQAJ+eUCCQEbMuSFRUVqHQ4OtDWzufvxeMlJcp/5PEbtv78vDt63vEUBaaecqFOxTSgie00B489+fLcikuXNL4062WMHWth0aJvCt28xM1SGcDk4LizJ5E7oHW0XnZ6gW+FEOBUhLz9J3Bw5ElXpxY+/a+My+zg9PrQI6MMlxHKABRdvfupu+Zecsh4q3a1Fv7/hMzBoJxQVuMM+p7vqBU19zqfO5aLb5a7t7xcYPb5yhsuuanbFbcfKwtKLNVcb3bKwsMaAGnyh7SU0tDxiLR2bIDdsAM61uIEKhsuCH8IMpgLI5wHEQiDDBOsbKVTCbCVFkREwhfQqQ0rRHRx1SwAGlOnGnAOaZ3FLvW/MjKLqUDRUEb5Lt+qxC7jARxC+d31ILlLH7Pgy8mKM2fL9qx99pXaPFBwDtNlZYSiq3cfk3u/1vnRjtzsgaqvXDtu5z3axmYqcPXQvdvZ+10cQtksE21rffke37h3OQHzWudM6xjtd7/2d3d03FqlE4NG/eoBeafPv88sGDqWk0kNAoTHI5LbPnir7t+Tb/AXHb0xVrO6BdjSWiYiG1wAcjNX6ypnA2gE0AQnsysbJEpLXf4eh4diH8zv3efhl27zjT9qmoo2aSKGDIdFYtXSTzZddsYPfN0O/6JlxcIoAAtf9+ZYPkuiskLlDDhiVo/rXignt19BW3tHaBLQWk6oXUWGAGgF4c/V0Y+eE1v+dv5padIvg/krpNiWGUCV7XYHL+39q1cedg84QnEyInetWdTaFdaKjUAe1T3/h9iWZ24YDCE3Q6sDEUwtUF5OmPOcclwS+2hOZAyDDpPu/i9YM2YI3HRT5757E4CZhP2+h+O+6eQ9bgJmzmxvoRHOojAVWHBrZjPOuHI6i9b6U8omTJ0qUfW1l12QEEJB65Gll97ycdElvyG7qVaQNLKvTa3ypxSEL6CIhEx+vggt778SSa755P3kps8XquqtayydbABAEtJN7kCemVvYzSgqHewu6TPU1WfwIP+wI12e/sMh/CGoRNyWXo+oe/ov9qZ//GI4hFgNx93c0cvducHPm6chDee7rDt0o+2F3eUXODDWpd03kV3711afbB993FVWOi7R4dRAmzdPQRrc4fPvLX8H+yDqlC3heQqGwdCqY2kncsbDtgVoqjgIZUlaFQ+BoquZ5lygnGJCnRgTITLZhgDbtsDUqaKD97e7bBqZsdf7KZutGZLnfMm1dd9wZGfBu/Z+rWWZdwEGMnF3nVaC9lfpESg+3ovqubnhyXd/PzD0qhvZVhrQINMjVGTz+trXz71GoeUT1DU3A+1mThEA2bPn4dNPm37OeUNHDB+Sk1fcLRDM8Xk9hgCAZDKtI5GWeFNj9Y7li5eu+M+/58zasvGL2Wi/zIRESYnbZ4bDtt8zutdfH7nb3bNXP5WIahIE8rpF49OP3Lr95p//EyOOb8SSuQl8vadcJ3aBqH+Pc29bln/GDS4VrQcJufdYEANCgkiAbWtvxaftzCYUWMutd577RuOKt07EDBaYSV/yGb9WpUdgBgO3SJ0Rfh+AEd5A7mizqP9hRiC3G6ThY61tTqdaVKRuW7p23bpUKr4YwHIASQgB3KjEl1RMOgei/Vs4DhTBncMSvOtvCgD0ckH2N8OFPWVet27CG8oTLncQJF0gELRKaSsZVZGmarth25Z0tH6VAlYB2Oa0+TWXXWAWINI5w45+s/etzx0Hw1RQtuzQWtmq7AdzdWrdMlH33L3rW95+5p5kMlIJh1Aw873Ma9/7qQjAYW4jcExg1JSzfKOPPiZ09Flud88B2PiLs+bXf/ByWWu/Oux720Rou4EbQE8J9Hb7wv1ETnGJDOcVCbc3B6bLQ4IM0lppy0roRLTRaqjZatVuXm9BrwCwGkAKQgBKOabuLye/ErNmAedfoNrZRAwAxXDqk5UY7kA3wxMKi1AwLFxuNwvDhGalE5GIaqyvSyca12RkxXmnQgL6HAm0Wd13Hk502zJfIoFBrkD+YCO/uIRcHh+0betYrMVqqau1483rlPOsm9raPDjyt0vZkra+9XYBY13dBww1i0r6CI83hyEElJVQ0ZZ6q2brxlT9tuUKWAJga5Zn/nJ9AQTKZ8EpjLvXuBQB6CaB7qbwFJA3mEse0y1cbpfWWnMs2qKiTTVpqC0AquHM3VRby+2qoWJPJcoLRzb7uLy5/YyCohIjJ6+QvN4wScNDQkjWWrFtxVUkWm/VVm+z6zetTQOtsqkO8NpKmDGDcOutGkoBjmwO8sAc6SrtO0AUFpZKry9MQrpYa8XKTnIs3mg11FRbO3ZsTun4agDrAWx3WsuUTOoEi/r+Kj0mwsMDhooNzT9t7r+M3H6DOJ3UEJLAtmr59PZfRxfNfAb5kxpR/14ceyg85eXlcvbs2SqvqOiYB5584e2zjjsC733egm5FIeSEgFTa+Z7LBTRHgO3VLThqYAgvvvMhLjv/9GkNtbVvnXvuuXIXV1crZP6kSb76997Lzb/qpxWF3//R74jIANssg0GR2rBm9frvnPddmwLLsGlpq7Xn60FZmYGqKjtY1P/20utf+rlR1MfmVMLYa5FnBrncsGo3QCea4e49GpyO714luk35sCEDBdz4+p168+M/HgMhlnTilJqtg1+P0lNeLjFnjsosBhPCfUZe4h085RRP3zF93D2HwcgpgvAEHLcEa7CVgk5GoRp3ILllOVIblqyLrXrvtciGTx8D8EEnFivKcLC4w/3G/svsPrCI0zENakf5BABokDShIvV2bOnbv0iDVgDcmWeVIFJeT/gM39DJV8H0aCBLwLrWIMMFHW2kyIr3fmpZsWVwTPI68+8wnzsw0dV3xBFmyYARruLefc2invmuot6QucWQviDI5QUZrgyHDYHZBltpcCoO1dKAdPV6pDZ/3pxYu/iz+OJ3XkjEGp4BsB0kAP6yMvOlIUFCSdbTev/8kbnhky5RqqlWktGxW4tZwQjm6aa3nhY1D/72nuiOdTcCaMoUDs2cgIGMCRxAOYCanS5SpyI7dtkMhwR7DrnEN/zI78QXvf3LSPX6h+AswvtybeVK4Ahfj4GTXL0GjnV17z3ELO7Zw1XSxzSLesAI5oE8PgiXBzAN5/DCGrCdWmA6HoFVtx2pjZ8jtWHl2vjyj95qXv3RUwCqHAV3+lfZbIMADnebvhHuXgNHunr0GmwW9OhvFJYUmfndgjK3EEZOAaQ/4PRPSpCQmbmVho62IF2zBakNn0eTKz/9pPnd155J2dGnATSBBHD0FAMLFtiZ+drfFy482z968imew0aO9vQblOPq0RcylAMyDLB22lSRZlg1W5DauCaWWrNsaeSjqv/EG7Y+BWBDRqE/UPLnEFs6fctzG4HzglOOr/ANGz/BN3iUz1XaGzIYBrk9ICKwbUMn41AtTUhvXo/E6qUt8WUffxh5941ZKZ2aBaD5K/SPIOWuFiY3gKHeQP5Ez4ChR7p69x3u7tG7l1FQnGsUlcAIhiGCYZBpwgl3YOhEAnZzPey6aqRrtqfs6u3bre1bvoiv+PS99Pb1L1jAZ5l77brZF0vgCF+foUe6+x421t2z9yCze8/u7tI+0tWtB0Q4B8Lrg3C5d5NNtixwMgEVbYG1YxuSaz9XqS9WrI5+snBuy5qlTwL4MPMuvgphZ6t1FwCGB3oP+nbwiCmneQYPG+QdNJTM4u6QgSDI43HWMdaZMUpCx2Ow62uR3rwByU3rm9Pr161Orl75QXTlx+8oYD6Aun0PSOdBKDnNi+0vhUNH/vXy4PAf3sJKMcAs3B6R3Lbwzbrnj/wpSk/YjC1vROAsGHtqXMTM6NYtUHjCGd/5yzHTph0fzisp6tmzF3Jy8yCEwQCgtU1NTQ3YsnkTmuq21857+625rz3/7PXV1dU1zgFo73YBGKGJJwRbFr7Rs+9TL//RP/GoE1SkWZMgIo+L6p94cMaOmb/+F8aObcaiRYl22jgUIDADRKFu0374edFFdxWrRJMmEnttgqwVjFA+Gl75CyfXf4zuVz9FKlK/e7Bz6+gxg1weWzVXG5v/dPrfI1uX/aDVhbb/XTzkSk/rO2EA43IHT/ltcFLF6cFxZ8DMLwUTmLVSrGzHJN46bEIyCQlISSQMCdawG7Yi+vF/0LTgmReaV707A8BizJghMHNm+/0pL5eorESo/7gXev6i8lQjtxvYSu2uWLZ1UjsxVFYKm/94/sLGJW9NxqxZQEVFRydUkXm2ouITv7+85Kq783Qqjr1oCdoMBhrC7cfWv17WVDvviWFg3oZxZGARrEBR3xvyTrvqNt/QyTCL+0AGc0GGC0zMTmCuYmjlbKqsqe2EJwggwUSydWMTRCQ4FUdy43K0VFXWNrz28F3JeMMfIYR1ADeefSNjTckbecxrPWfOPgGGoaCU0eGqpBVEToFqeulBufkvV/7I1vZdkBJQk439cEXsdP1wxu3jKED5AJrRsbLTqiyXFJad+3HuKZd0d/XoD7OwBMLnB0iAmTWz0rAVWOuMe1bvfCoS3EquSEISCSnBGlb9DkQXvoHG156c2/jpOzcA+Dgjo52ZxwSA/QWlF3j7j7jA7D9klKfngJ6evoNhlvSCDOdBeLwZ2eZWaEIms21Xi5igVguzICmETiaQWLEIja88vanxxQf/lNL2vZn3PCY8aML14amnnRU66mS/q+9ACI8PDNaslfNOmTNvWwBCgCgjf1YKqY2r0fRaZXP9c/+6M9Fce9sBkr/WDdXj8YSvzT3jomtzTq7o4R0yGsLrA2utoNPMSiETqA4IwSQEIA2QlIKEEDoeQ2LZIjS+8uzGhucevSOlU/dCCN7Pzb7V6tDNBKYGRhx5kmfwqKO8hw8b4B06Gq6efTKKoQkAYGgNrTlTP26X8RAgJ+pMOFoaQ8djUE312H7379LV/366H0RqK7SWIFJgHlh85iXv551xfq6rVz+YRcUQXh9ABGatWWsNnZFNnVkv0HavzLoqQNIgIiHBCultW9Hy1stofP7pfzcuef/XIFqJc8/trGzuPj5OH0tDh424Oe+cCy/KOfks0+zZB2RIsFKKle2Mz27rWGbOCAmSgkhKASLidBp2XQ2Sn69Ay5uv1tY+8+hl6XTkJaA864FhfwKZBVK2AdMs8PSYegIZBlhZGsIlVDKSjH/xxLPwFdRgS0sC2audc0ZpqXn8X/dc9Pi/7inu1+/ww/IK8vv+4Ppf//mUM08tBIDXXnq79u4/zfxpQ2P9+vVffLEajkkvY/FtFwxAtWxZkYDPV9P8wuxZniGDpwiv26ttS0mvW/qPmHgCTPN5SBmDc2r+GrIyyiRI2G5fuCI4fnoxcwdkhEJottMisfGz9+Ir5y236jZ/XwbzFVupvU3/RNCpuDQKesM/6qTzI1uX/RZzLqjDgXKrHDwIkNAgEt5w8cz8U677Ze7x3zNlsIB1Oq7seJMAsyAih6SFsFPjslVr7Dy0o1hoGSrQuSdfLUNHVZxZ//LdJ9S9cMcN6Zkz78y4+/aOkams1BCCW9Z+fFHNE7/9tPuPHi5lO82OxWNvVyLbFoxQgSosv2FiYu0n1yXPO/8vHU2uTEaSCvYeeWdhxa/yAE6DWTLtPSSsFBu5eap+9t9k7bwnzgKJrSCSKCtjJ3KPZGhKBczS3knVHDF0Ki44EXVqTYHkrjUWdvs5s3ju9q6YmQxTuweM5uKB4wtDU865teaxGSc3fPrmhRBi0yFSfASk1AAGB4449TgZzIHdXN9xerpWEMEcFf/4Tbn9vp/eaLN9F77/fRP3328D+8Wlk5GFKp2Rp4ybZk59p2IKSACs0u5+w/3h40/TVn1UsbaFHW2r/yUIJHabp2KXn1llOpHZ25gBSVoEc3Xu6ZeK0DFnHe9/5u6jax/7409SlZX3dihjmR5BONYE36gpt/Wc8VBfkhIO0bS22U6DbYva+oe2+mTCyd9tZ11lhnZeE5Nhat/IifCNmtQrZ9r0u3f867ZTUhtXrSy88Nrr8s64RBqFJdB22uZkguzmBtH2/ILab5OZSUp29R2ku113Szg89fQZ2+765dTGT6oqIETNl5a/8nLp1GnTRxZMPfu+ou9cP8o3ciKYldKJOOymekFEklr71To+qnUtSWb6BybD0L5xR8E3bnLvnBPOubv6n78/u2FR1WUQciO06oziIwGoYI8BPwqdeM6M4MSpOd7BI2DkFQJCZGrGpaHjMcGOS4Ygdim4uMccZsrICcAgYkihXP37k5lfUA0kmkCZbcRxj2rfsDGh0CknKbu2RbO2hd3SRGAmAMIpuptFNpUCZ8JeM7LJkMRGYZEuuPQqmXPauWf7/3HHcdse+stVenblU63P2ckRalV4Tik5//sPFl/zy26unr2gk0lbxVoENBMR7dzfdpVN1TpnnPeQyZxiEoJFTo4On3GKFl5fYc0T/ww44QfZqxPsR8r6EImG9wxv73MGSX/v0dp2ZFKYkqzmNR/HVz+8CAWDksDCzlQ5J2YWUsrqDRvWvPvxh+8/zuBGITIxbko1L/pw4WMb165ZIKWsZubWFPaOoLFli4Xx4xMNzz2xKL1p7cfk9znPn0rA1aN0ZO4ppw/GqlUGhgz5etK6eZ4CWASGTLvS0+8I5mS0XasCWEO4/Dq9bRUSX7w/J960/Y+xJa+CTJ/gLEG9RESsbBUaf06u1593oXNy/UanrzsmaNY54cOOfKnXz567sWD6rwyYbmVH6oitlEEkBAmJTIVs7CYCrT5cymj/JARbKcNuqSOYXlV04c3enj9+6q+Bor5/w0zSKC9vT4ack5uQTQ3zH7+i6ZX7DOEPEduWBOu9LiKSdnOt6Rt5rM478YqbwfowiOcyZJx7QWLO+UoCZxeec/15RnEfW8VaXAAktN7tYtuSwhtEYvkH7trZt98BoiocPcUAoJwAPUKiZt1nyfWLoZNpE3bKAEhkrASZoD6x853s9pi097uShlN2IR4x7JZ69gyeaJXe8PRRBVMq3obWPUFCZ3mmA4gyAa0RKOpVEZxwoqHTadVh9iIzYLqVaq6X1Q/NeDMZb7oVv51hOArPV1bsNSorVSYeYF9gx+KI+vTmL9bYjTEBKyWgtXQsI7uOyS7vflfsMh5tFh8SAlbKsJvrBYShir9/k6v0hvvv8QTyfwmarRzFZ9+QwXAzDEPZkSZlNzdCR1oMTqUMaN7ZPykdi/GecrPrJXaXFR2NSNXSxIEjj1e9bn7w5H53vXh90Xd/JUUgpOymOuZ41ACzzPr8e7ZJJDgRNeyGOvaOGG/1/sOTZflHnvQ6tM7NnO73L/SirMzA7EplMl9a+r3fzOv1x8dGeUceYdstjayjEen0zSAIufeY7DU/pPPMkRapIs3af9Rxdu87nzm2+NRvvwutRkAIhX1RoMyaBQAwu/cp6/6jmTnBshPT5A/YdrRZ200N4GjEQDptgKhtTNre257jInaREymJhBBEJOy6BiOxatkyADHYtsBOc+LG1PrVm1VTUupkUkKxJLHr2O+XbBKREJxKGnZDPZHfb3e/8U+hPjfe9aRJ7sshaN/vwoGEEEoyT+/9o5te6vn7e7oZBYW2XV/HnEwYrbK5W9/aXe9pt3cBIsHJpNRNaVd04bsJS1v/dRIDsq8JnV3YBEqHSqDF7Sk9YbTw5gSgLA0IwbaCVf3+O7DiNbByOlvygYlIK6WEUkrOmDHD2NWKQ4IwY8YMQyktlVKCnIDCzixsGtFoGpZVG//047eRToAkBKu0NvJzA/4pR49GS4sboZDcj2c/UJAAYACTg6NPG0MuL2dODHuBWYMMl4wseS0dq1nzMoC10SWvf8JWnEgY7b9fEuBUlDx9xsA//ITvATAcJesbCcfCo3Vu7vDj3yj98dMnewZOsuzmWkDZ0mHf3d9wM8CxeRiAsqTdXMfBSWdZJVc/cI0vv9efUVmpUNauEqgwZbKhiN6o/fcdf0198Ykh/Dn2LvEeu99CSNLxCBec9WN/zqCj/g6tgFmz9tQ0MkR7Oi9/6iV3h8rOZxVpEO1aMZhBpktxMmpUP3bjh4nGbTfi3Gdbs6oAgCEICthg1W7WQhiy3dDcLwPhmLBVS71J3oDV/dr7+ueOnjYbrF2Y8SU2nv2BI5vkHTLpVFeP/mArQR2NObOG9PhF48sPpZpWvHcthETGbXkgLZmda6uiUgCA3Vi9lpNxwDAOTDfa5NeWdkM1cs64yC654ubfSxZngOZ0bnNRWkLZkoTYfYP7qshsgHZjrTTyi5R74AjbbqxlttKOMtFJJvndQAJkGGQ31Zsyv9Aq/fW9o0KHj30QRBozZnS+02VlBubPt93Se3Xpr+59uNu1t5isLKUjTUbbRvplIARICKEa6w3hC9g9b76vtNvpl7wGrftlFJ99NywoZjc3smpulEinWzf3rzYuzCDTzXZ9LdIb1n0KAJg61fFb2jYBSNt1NRvYSoMM48BUjiMCGQaQTht2Y50u+N61uuSHN9wPzUd1QgkUrRma3b73sye6XX8j7EiL4mTCIMPocN53CtJgZoXUhnVrAGyF8w6+stIDpCABBIycwSNIEphYk+kilWxsSGx+5VMEuyexfV1nrDwAQOXl5bKsrEwAwMyZM3vatsrNLOasNefMnDmzNwCUlZWJ8vLyfdRPboNGImEj2D0Zmf/mp6q5vp58HmLBmtwm3IcfNhxAoNDtPvQWkPJZABH7+46/0j/8JOhUTLc/GRkkXUrHmxFf9tY8AKvBTNFVbz+Y2vgpyO1D1hRurQUMUwfGnD5MAmUQgvFNI2VEhoiOtZkz6KjZ3X/wwHiZ38NSLXUmSfOrTwCgdfMgu7HW9I8+1ir+9u+uN03PxZi/wG73xFxVpfDsszJev+lXNU/euBjphAFptp/bSgS2U1KEC+yCc395nNsd+D7OP18B5TsHs7ycIIQO9hj4x8LzftWDtdLkBD3vBWbN0hdC3bN/iDV+NvcSCGmjsmJXV1wmKAKbrbqtDa2/OZAgaYDjEVMEc62ib980wZNbfB1uFq3WsYMBAWkwgF6e/iOHk8sNtlX2e2Vi1uzGHdSy4N/PgWglpj8t8XVlYWY4dqwdmz5XzQ0gafIBHRMikGGSXV8n8s75HuefeOF9YB12ArY6sw4ePF2VpASn05LjUSNjsTkAbRpQzY2m2bOfXXz5L852SVc5br5ZO/GF+4TEggW2ZJ5e8qPf3ZN/0Q+U3VjLUFruL9lrR/3jZNzQYLv7r/9ckjfxxFnQ2t3OYac9ZCwY8sAonwDAGmSalN64Fqnt6xaBCKiqcj6bepMEgPS2zV/oSAQwjAN2RgLgyCYJYTc2cPGVPxEFx5zxd2jt6lA2HYZ0mX/k8f8oue4Gj93SrIlZfmlldI+2yeXSqrERqTWffwJAZw4lB8DSozYKeIvzyFfcrzU+gCRBpWrWpre9swmFQ2xgRacUHiEEV1ZWqqqqKpuI1PTzv3PVcSceX5hMQCUTUMcef1zB9Asv/wERqaqqKruyslIJZwPft9SsWKFzDy+xo++/v9lqrF9LHhMgBrMNWZTf3ygqyq9NJFpZnw8VRCZjpNQ/4uQzZKgbs5Vov3KidoJZk2vep+jn8x9unSipaNPsyCcvNZE0JLNuf0CFgE4ltH/YNAQOO/IKMDvK1jcJ5eUCs89TvsLef+p26Z3Hyvxelo427YOIjuEE3tnsBOIpZm3zvvh7SJqwG+tluOxbOv/47/0NrEvBs9pz3TAqKhhCpho+eeXSun/fkZKBEKBUu++ZhISONMjg+FN03rTv3A6te+7SrsSc55RkPj7/jB9dbvYcqDgRle2dhFkpyFCBirz3vKx/+Z7rIcQq6MkG9tzMHXdKk92wbRPbFkiKzBRkRwHWCqwVnABAm1kpbv25LZh0X5AGVKRBeoccyTmTzvkpmHMw57mDVdaEwBouKUd7+4/wMGtFlP0+ToC3V8SX/BfR1R89Cq0JlRUHoVudRGZ/SdZs/Nyq3QIyDGqTxUwJDGc8do7Dzp9Vp7lIiCE0syo496oe3nDRdw4443prSnNrQGtnOVxa3R8dtbs/7QEgw4Td3EChqadx6OjTbwRzZyzVrRaEw4svuO7hgouv1XZ9HZEQokMFI8PezUoxa61Z2cz7midCghMJQwaCVvcfzxzrLez5G5x3njooB4O2ed1++RRmMJGQyTUr7bRKL8uMRUao5gEArB2bVtl11U6QNGfOUK2yqXaVTbX/skkE2LaE26UKLvrecNP0nNPBAVtCCi0hT8y/8LKJIhBQbKU7pqRgdvrHWoNItV2A2mu8mCFcbqQ3rUd89cqFAIDKeztcszo5YGMJDZ8LV+GYQuEOFoIBCBAEoONb18GKNyGSq9CJWB4ArLXuVljYc/KQESMm9+jR54jSPr0GhMJetm2QbYNCIQ9Ke/fs36NHnyOGjBgxubCw52StdQkc7W2fsT2NgYBCPN5sV29fR05ICEFZMIKBAu/IkYX48EOBsWMP3nFoT5SVCWgFX7jkW8ExZ/pZpxXRnlF+GRBpgGTk05e3WVbyJZBo1d5rYkvnvqAat4FcviwMWwS2klKGijgw4qRTAfTKKFuH2pWXDRKz5yjJfFzhGT+/znPYOFtH6vYRvKoBYSgRyNVGsICkL0fIQC4ZwQIiT9CG1tzRYkUkhE4ldP4Z14cDvYbPhBCM8vL23r2GnmxAiM/qXrzr17FP5koRLFCcxc0FEqRScc6f/vOc0IBxd4OIUTajtX5UIPeoir/nTLuEdaSB0K5bS0N4fMqu3mDUPDnzuVQqfj+m3Gi0E5DLbe6U+m1rdbwFDNJg1jDdNnmDtgjmKhnIhREuICNcQEYoL/NvAYlAria312atOnxPAECAACsdmnxOkcf0n+YsgAchLqysjMAMV2Hfka6SfmDL4o43UTCBRHzVh7UKeD9jJfoaubaqGELAspJfpLdvBDQLZq2dEhheWwRCSgZy2AjnkZGTGZO2n/MggiHb2az3sacLAR2PkmfgKA6MP+47AMsD6rI2DE1en02BoC2CYVsEQzZ5vTbAurOK2U4wnIwb1uT22CIQskUwvH/tKSVhmpxz7JnDJTAZUnZsqZ41i6C1yB099YHiq24MqnhU7xWku1sXGaw1kz9gGzl5ZIRzyQiGhZGTTzKUq2EYHY4JSQm7qdHwjjxC5Z/17Z+CuT9mtXuI+nLQGo4MuW3yBWwKBG3yB5x36HLZIFJO9QPNrBRSm9ZtBrAhsyM6k7uqikGEZN22z9PbNgMAsdYa0lDk9dkiFFZGOIeN3DwycgvI+Tfzc06uI5vAPseLpISORRCYcBSHJ5R9N6OA7P1H5bMAzQiNmPCtwJFlrOIx3lfsHpmmNnLzSAZCQpguSUJKMl1SeH3SCIWFkZtPMpyryOOxmVmBIBIrl3Eq0fixYz3quOh2J7O3EgQ0kxHskwfpDjGrTF4/oGI7tgJIwkjsy78uZsyYgTvu+NvQH/96xhunnXlaSSqtAQbcHh8iEQaRE4IeiTAuuPi7Z55TfuGZIMDjEvjPCy/vuP22G46/4ac/XTFz5ksGsCibksWIRjWApKqt3QJlAwRibQNuM+Tp2T0/AggkEodO6Zk3T4HI9A+bdqm7dBh0KibaD2BmkMunrdp1IrZi7jMAog5x10wGM8WJHootf/OS0ORLhEonMlwsu8MJaE7bwTFn+Hxv3f+teNPW3wNlYl+CcAhAGVJGV86Es+4KH/sdqEgzdci8qxWEL6R1vEVGFr6O1JYVEZWINpMQphEuLPANnmJ4+o2BTsc1lGp/sSOCTsakUdxH50w+76LoU0v/gDnPfYGd/De7oEph+iyZrqz4c+3TM04q7TNiGnkCCnZa7qVqE4HTSWnkl9qF5/zszMQdF11gLbj1aYibESjsc0vReTf0ZyIFzRLtUf8QaZKGqHnit1tb1n92RYdkeK3ulNpNq3QqqczuPQXH4kJFG4XVsB1WwzbYDTugYs1pTifSWiuLACk8AY9Z3MvlHTBamCX9oFMJDTvdvuwBgJDQySR7Boxi76AJZyWXvvMEyq9mVFZlHaIvhaKrGaiCq7jX4TKcD3bmaFaQaWidisv01nVLAbRA2fsmDjy4aHU5brJqtzbLHHcQCAudisPasRF23TZYtdthN9cpHY+m2baclEDD5TbCeT53v6GG9/CRIH9Y61iz6NANo5Qkw2T/iEnDat98eiikXIJ2ZfdLPEQiLqwdm4SKR8FWGiCCkVMAV/c+YCE0x6MCspP7uZAw/GFmZQu7druwarY5fEq5TnsQQut4VHTk0iAhoK2U9o2aRP4+Q85s2bBiHsrKqM11szskzjtPud2+S4su++kUEc6xVVOD0d6a6DwsA0Kw4fNTctViI/rxfLbra2tZqZTwesOe/oNDgYnHQIZztI62ZB0TkpJ0Isr5537H0zz3+esjRFejvFxkmLS/EoTPD04lpWqoh2ppAqdTgJQQXj9kOAwZCIECIZA0bJ1Oamvz+mUA0g57dNt8YJCAYrU+vX2zZeR5pQYLHY/B2rYFVvU2pGu2w25ssDkeS2srnQYYwu1xGXkFPu/AYYZnyAiQYWgVi4p22FR2vlLLEjIvn3xjj5yE/75eCsPYAuyeMUzPXaAYMDxDR04wcvPJjjRnbzPDTaca60XdY/dzau3qVXZjQzUrO0VSuqTP7xe5eUVmUbcS78Ahbs/AITCKu8EIuZFc+8V2ACuJqH060l3QGaWHMKAXYc0KQf7CIEnDBSiAWEAraLuxAYCCrOvwRtdcc4156623po454dTx3/vBdSU+H+xkEoYQYK1B1i50gcyEHj16QDh8SeTywL7ih9d2++SD/x49c+bMZURO5q1TnLQdDhbTZAC23VjfqO1UJqFXQbgNUxQWBgEQ0ulWf+zBTul2otaBYwMjTx3kcILodicVs4YwPTK69DUV3f75YxACGepvBgAb+G906RvLQhPPH0YiS4YNCehUTLhKh8A//LhL4gseuwM8z87k+3+N6etlEkLYHo/vgrwTrhpKpktxKp7d764VhD9HJVb+V9ZW3vx2y7K37lTAxwAicOS2u8eXe1rOcZf/orD813kQksGq3RgDEpI4nVTBI6e7/G8+9J1YzbobUFbWSuG+KxiVFQxmaiK63DPrlk+Lv3dPjraSjL3ybzNurmi9CE6arnOPm3tXzdwHnjeAoXmnXXOtu99IpZpqZXtWHlY2y5xCbnzxHlH/zuOXQ8g6OAp/+8fMzJpv1Vd/Efvkdanei8USn3+wIr193afpHRuWpluq1yuHmbQBQCspqAAQkEB/b/GAk4KTz76s4Nzrc4UnwGyns8ZisEoLI5BL7r4jJmDpO17MuSCBAz1PZpUzCBDhgl7k8YPtNGXVepgB6WLd0girbtvnAICpNx1qEsW9epV5HTWpjSubm155Lhz5+O1N6S1rP7O2rVmS3LpuhQW9FUANgBYAaedv4AJQ4hbusYEJx16cP/0HkwMTjmediGQNBCYiaFbKN2Sc4Q3kT05E65d8pUMMM2CYWsdaxJabLlua3rj6WZVM1NvRlqiUQsiCHgP8Q8eeUXDeD0Z6h03QOh7pUFHJdBI6EUfLm/+m6IfvLE5+sWRecsuGpaQty2lvzBkFF1w90jt8gtaxDtojAtIpMou6k2fQ6MktG1ZQ5sC41zednYE8oWPOvjEwaRqrlibRocIjJYNB2++8saX+uYf+lGypewHAFjhjEwAwOm/s1OuKr7zhJP8RR2sdyaKMEkHH49JV2odDR598fmTDihsx57kGfJU5wszkclHNP++IRz+c/5bd0rRON9ZvU6lkVAjpEv5ArpGXX2qU9BrsLu1zmGfw8AIzpwCpDeuXAGgNYt6p9Djd2JxY/pnV+MyzZuS/b69Lb1z3aWrLhqWpbWtWWg7bdC2ctbS1LJMbQKnXHZoQmHLcpYWXXTvWP/ZIRwHMpvgSEWut/KPGeaV0HaG0tQW708EIdvpV7Crp0S3DEp097sc0tWqop43XfmdBw8fzrgGwEnsTCXvhsFcP9/cbNsU7bFRZYNwR45NLFy8HEGN73wei/eHpIWH43I5gMYOImFPgdHMcgIZ0ZxtwEkLw3XffnQLQw+v1TrCstI5EXKS1gmObaGVd34l0msHMBGZYtqRUKsmxWJMPbvQr7D6sfzL6xbqW2plr0Z6wud0MgFUiEc/EQDhWBlMS+bxuAIQBA4A1a/bj8b8kymcBlRUIHnbUd32Dj2GdinO2SU/CUJyKyfjSuQsBLM7Q0TsDSE7xw/jKd55MbV3xe1fpUM2paLundtJaQBo6MOqUgQ0LHjtWAW/ga+MmcrqUKYRKgZEnXeMdPIV1vIOq8lpD+EMq8fn7cstdF90bb9j8w7b6RDvRlIw3rtjxnzte1tGG17t97+7urHQWRZDA6YRwlfSHf2jZ2bGadb/NLKbtLVQaRBJCbqp/7e8/9A6c9FR46oW2aqnN5oYTbKdU0fm/LowuevUhb99RfXJPvVKoaKNGe4twRplLrl5k1M75058V0euZ2jEd8Mw4mVwynPf69nuuuTSpkwsArNv1+fZOVQcA1CrW66PVa96MzvnTP9JbVz/f4/oHhsJw6Yy1ZO9bMQsmgqtH/+4A+sEp73EglR7K1IAiGQjnkWFCpxO0F2Hjrn8gJOx4C6yG7ZkSE/MOUFe+NDiT3q6jH7z2g/o3n1YKWAAg1vYNylA9tdYHan3VzNtTOvVJauGr/4p9tuCG0l8/eFvomLO0ijaLds3+RGArDbNbL7h7HT4mseJ9oGwqslg/OgcpNLQSdkP1a9Gajbe1/trSALZ/gdj2L26OLn7/nr5/euYKz6BRSseiWYNOWSuWgRxU//PW1Lan/lIB4CXsKiuZ9mKL37+79x3PXukdnGkvy0bKthIUcMHda8BAAEUwjGrsbdmSEMI2heeMvFPP7wcpFVjLNq6adpolaWLb7360fcfzD5wC4LM95mYMzK81LJr3WvLnq/7a5y9P/cg3eqLWsfYtU5TZ7IOTj88zn7j3RItTTztu4P3iitoJITQRydhnH37c8OFbZ+z1eQOcQiCL3wOc0jODfMHCiTb4RQDYJdMT2CmbkeZX/n3xjjkPNgB4H0Byl/sBaOecwbwtkWr5MPHmv++LfbLwT33++sj1/iOP1ioaadc6QyTAlsWuPgPgK+kzOrJl9ZwslrmA8Ae8HVlzWWsYXh81vzibGj6e9xOY5pIZ6bQAIGYCmAFgJsAwjASA9Urr9S3rlr3Ysm4Zql98YqwLrmimU/tcp/bLF9ke9cI+QE7mrhZHHlV226OPPbX0vnvuvNLvMYVmSCEkRIZkcq8/JIIQAkJK2ErL4lw3XXjhxTMN34Rl0WjxGzaNXRIo6PMbIBNhlK0DHVASHAK0BjD39A2bdrIMFRLbyb2J7wAnIt/tR3LDIkQ/r3rUEcqpu8zMKg0hEW/cPiu6+LUUSdNgzhKkISR0OqF9Q45B4LBJl4GIv+aAZoc8DRjtH378GDK9YG1nO5YBhql0PCJrn/7N/HjD5h+CWUBrA1rRLpfAkCEuSGN5zTsPXdxc9QQLXw6yxeCw1gLCZM+AcYcDGJKJF8gmEQpTJhuW1k/XVt72RHrbF4bwBlS7gdMkwMm4NAp6cvHFvzs//+wfT3TSjtshOGQGDJfiVNyofuw3n8TrNt2AZ3dLT88GBoBY9bqapE4+CiHXgZlQVmagrMwAs4TWAlrteUkwGxgwwA3DXFO/8IULG994JC28fsrK95R5Wa7i3lICvTPPfDBmjkd4A759T0p2rJfxCDgRbTwI/fiyYACIRxpfVkSvQcgYZrBAWZkBlBlg7YyJyoyFylwOo68JZplMRn9X8+jtz6nGOkEuj2o35soJGiXhD8Es6tEfADDvpgNg5SKQy+vLyJDHyZQqMzB2rEmGacdrN15d+8Rdq8AsITo4OTNAQpCrpJdEOLwUpskYMsS1Z3ux2k1X1z1x10roTHsdhOGBmV2lfYMA+rfL2VM+i8GM4OijLvKNOpJ1PIpscSKsFEQohxtfeIJ3PP/AxTCMzzBkiCszHtR2aW2AWcQbd/x4x99mVulYVMA0s46JTifZO3A4B4aMPslJGLn6Kx8KZDgHYBb45z/Ntrndds0wwEyQsg5CvBuP1N6RjtStbn3MPR8bABKJhjkQ4h1ImcSsWdKRTRgOP9guMrmbbI41mZnjDdt/suO+Py3Q8bggIwtNCgBWNoycPJg9eh4GALg6y3vQ+15DGMwyGAKAQjBj5sknmzOdjVvPJNIg4sx4CTBLlJUZGW6jRWmkP8/yLvbC/ig9zOl4imEDggjETIYb5Ar7AAio1F4PxcxEROKUU89+fNazT99w8bcvyC0t6abzvYTuYaA4uO+rKMjoESZIncbTLy71efoP9/b72WU6Z/KxPm30vSWQ12M6nFPATqlPpQgASb/XRy4DIM2QRFAKnEykAPAhsfKgNYC5+PzgqNP8rCw7WwAzMzMJKaOLX21KxZufzwRk7SpszukcWJdYWfWOjrcwSVNnG2NOJ6UMFcE/bNopAHpklK9Dr/YBTiA3a/jySk/xDZ5MbKdUtpM9aw3hDVLkw+cRWVF1I2axBFFrRpPY5SKsWKFwztkuEL3d8v6sd3S8SZA0252gRATWlvb2GyXcvvARmSDd7PJfVaXALKJbVlxT8+SMDeTUcmh/8RcSOtZEoSnl2nv4BNaJKNo7IbJWLPwh1M2+PdH46WuXQMg0KnZLT98XCCgzoJUAEaOqSjkuujLKBFEDQupdLgUhbaxbn8LCtMnMS+NLF7zPqQSRzMb3RGCttREugNsX7u4s+mUHQ24MMlxmB0lbDhzVlNhKQ1tp57R6gEOMviKko3QqwkzSznhUMcrKMtQMzCSkJumMCQmpSEgLN82jsf/82IytWfxgfOn7EG5PVkUUWhOZJmQ4vxBOun8nU9f3AWadsTBmGK2rbCxaZPHIESaIVPSjqodT61aBvH6dLVPS2Qm08vQdbLqao+ORtggrVuh22tPRj995OLVu5T7ac+TP1a0nTJi9M/N012clPHeBApDvHTl+igzlEFtWlsWEQS6X0k0NovGlJ18H0ZuYdpUbK1a0roW7ricaNNTALJbNn1bdFpn/OqTXT9xeQK/jhhMyN588hw8dB0AekPVVKSeR5fvft1FVtcc1027b9LVuVWD2dT+HAFUpQkWFbnPnl5URZs0idpix9S6XIvmZNe7++42yd94xYp+993BixWIIn5/bDWwmApQi4fHAyC3oBgBcXt7ewMZ1Kp7oaJUjKaCjUQSmHMvFp194J2x7PF57LQUpdNsBb6fFWcMhbrVZq9aaaJ3WZTrzRYZrEwPQdrI2wmynIQkQrOGSMHzhPAASqmC3ASgvL5dCCD1oyJgr777nzgtLe5SkYymL04oFCDAl4DL2fRlgeE3C/Q+/hHmLP+dJT97MhSdNEsPv+5mdf9xx2rLCvy4vh1OVrBUFFgEwjMLcXOFxgcFMkqBV2tK1tRHnmVz7s9l8OTiZFoZv8LEXu0uHg7MFMINBpkeplmrEV77zMoDaDA/J7v3LZCZE1ix4KrX+IyK3L2uKoRPQbNnBMacFPMGi875WhuZ58zQAePuMOtos7A22kh3EMEgFOy3iKxd8bAs5HxeYCiTSTrkKofa6Zs9JA4TEhqUPpzevBLl9WSco2xYbhb3g6TZgJACgrMNes2NulE2N7z59ecMrfycZzHPSwNvtuACn4oJtq914GdYKMlSoYh+8JOtf/NvPIMQypyDqfsWmcJsJvbxcQkiGEM4G4yyKTr0jrUxo5YFWPmjlglaEcWQREVv12z9UkUZAmtmzuVhDeAOA258DYF/v6cui07bX1uQUJ0bxG4dMQkWZBDNBSCfNtqrKRmWFAhGzVpKVckErH2vlYa0MzDzGXnTFOEtBfZjatiFNJASysP0ws8NA6/aGAJgH/YkWLdLQmuLN2+cnVi+GkIbMxpIBEFhrluE8mDlFJSAwyvYQmNb2GqsXJD5fDCEMmc1IDYLDXRXOgyu3sATAnvInoDUkMMY3aHQOd8APw6whvH7El3yIxKrFd0II4I17UxBCtXNpiFVpXOhSSsq5saUfrwWzyGblYs1EhoTZo3cfAN0yPTgUh0pGZsPHvvcvRzbLymQmrkmD4ChRFRWKiJiVI5uslI+V8rBSctEVV1hVxxxjp1LxhenNG0BA9vFSisg0ITyeMAAIJ7Ny174CQK1dW1OPnTUW2wEBWguSEqW33XV475/97t3Q4aMehtITMwc8G1IyymdJ7J3Rp7Ef62jnYnpWeBkA25ENDYxUC8lQAXMaJAEKlvQA4EGuV2B7myZGs2bN0kTkueyyb1/Xr08vnUjbhss024SiM/QNDIAEQTFQWfkmBl7/bUrW1WP+UeUY+qcbjH4/vpDr33p9xKuvugYCtAIZJa7QGxC1gMcoLiol0wAYDMMAW1ZLcuPmegAaXu8hCGCWygAmBYafOAyGWyMZbT84TmsIr1dEFr+M6NoPHgdztkBTBRKwkvGXI0vnNniHHpenAW43ClQIcCpGrp4jEBh67MXJhc/clYmrOeAPug+0xnB4zW4DBpLbBx1vbj+GwyGhg1W3CdFFr26CVuPhBH925P4RAOxkS40vuXEpPAOPlBrczgshQNkkfWEYBT0GYN0iRxnr+H04bM3z579d+/yf/+gbctTP3f1G2xxvNtoPcsyWlaBBbq+y6zYb1U/c9GIqFbsXZTMMVM3c3xgAgRkM3CJ1JlvEA2CY15s7zlV62AijoHtfI5jfTbi8OTBcJgQJtm2brWRUJaL1dt3WNaqlfginEyCCyKK9OS4LaQLSdO1n//YHipWt9snqR06mMZlukOHyAnA2wW+MtadcQjynoKvsjCx1dwHjXKWHjzaLeg0z84q6C3+4gEyXlwzDYGaNdCqlk7FmHY9siS//qJGT8dbGsgR5aqeUl5QmANkaJnQQwRlagM3Wji1RgALYPTJpJ4gAVhBeHygQzEXT9vba0w5JHjakd2yOQQh/1vaczQ/C44UI5+WhcdvuH2diRtz5paPcfQ4HK1uTyGY2dvhsIgvfSqdSLV4AR8BZL7K/PiceRsWXfFin4tH+JGS2zYpYa3Z1K/UB6AXmre0/z9cGQnm5wHPPOQqSI5u9XcA4d58ho13dew4x8gpLRCBYIFwuN6RhMCuNdDqp4rEmFWneHP/sg6SOx7jd+JMMGHCYq81M1VTa42MnsDiZXPP5Ch2L9iTDaP9QCmQOpjZBSl189U9dedMvuLT5jZcubXn37f/Gqt56JpFqeR6VFVsy9cUIziay33GqnQxkXsTIm6DTOz6pJStSS8GiAlbMRIARLOkH05eDZGMddgackXCyiw6bNGlSP3aKRu7/PGWGSxK21jRhY10zDjtqFDb943FAAFsrX0JJ+ansP2ygbF6wZCCQXgGUE1BJdmNUwucLm9279csYMZlcBnQ0WpdYvKwWEwZofLjo4K4bmQBmX58xl/qGHuekVYv2owGZhAZrEVs6d6NC8bu4aV7rjrr3zjroHIkVlU2JlfNeUy07LiRPSEGljXbnm25Ndz1hZNPCZybaQv4X+1cg7kCgVRHuZuSWlBDJtkW8XWhbkjRR9O0/nAPQOXuF0Lab/U3QVgqe/mPA6Xj2oFitiLwByIBjikVn9o6qKoVZs2SiouLG6idunNbzF8+OgeFS0Hb75JLtgEFaSpeofvKmHS3rFn0/o9Tub1yGhJAKMwkAJgT7jLzEP/Sokzz9RvTz9BsJs6AUwh8GGS7sVsaDHCsT2xY4GZ9sN+6A8IfBttUxgRtl6h8ePFhsJdP7HoHMJugLQXoCeYjWH8Qu7Rcoc3JV0MhxG97zgkeeXO49fPR478BRIVdpfxi5RSCXB2S6QLsG7jI742Fbo+2GaoAZKh7d/Tvt3rH9aPWDgNZRaVKRpkYwBzIn9PbvrRnC7YEw3YF9tNuioy310MrfYXusQS43pNvr3/vDqQCq4CrpOdDILwLbVtZmSAjSiSiCk0909es3+HkyXZkcnI78LARWNoQ/CFYKJNsflIweoI2CYukCuqW1BlAO4Kunrh8AEITgTAX0Iq8/78Lg5OOne4eOHOMbNtrn7tkXRn4ByO0BmWYmWDfzl1qDbRucTo+1aneApAEdi+1bNrMtJjRVgKCjixf+J7Fq2Um+sRNYtzRnT2JxXGbCbmpkmZunCy65Quadd8lRyeWLj2p667VbIlVv/Kd52Uf/AtECCKFwoxKZNfEAW3oAjW69NVZ82KASO9aZRv/BsJysQekv6u/qc0yv9I5PNwBDBLAC5eXlVFlZiZKSXqH8/DxJREwdaIvZ0KpgNze1IGUaML1uxNZtguEPIFVdDzsWY3f3brBBmclRQxgyRDSuWG0EphzZ0yjK76/TKceQbhqw6+rW2jU19YWDB+tarDmYKa+U8fHm+4Yce6aR0wMq1iDbDbZjhnB5tVW3USTXLnwQVBPDzcdlt/yvnK0gBGKbltyb+GLh+YFxZwodTbYvRCTAVlL5h08zfL1HX9qy8dP/orwcB4JTYj9AmYEsNsLFpsMmnW2CEFjbEIE8hMsubv3tHi6+bHchYisBtlIdeE2coFjhCwYBuCGNFLCXWrX3HzlszenGz9641Dvnjx8VX/w7l2qpY4j2CHj2+GOt2MgpQOPL/6C6Nx/6LoSs7jA9vX041Ym16hceMO73Ocd+uyJ45BkwCkqdStpa2WxbYG0Rp2LEmcrqTuAjOTmiQjJcbja79xNspZ34n6/nXNr6rtM6GY0CjH3F9bBWkP4gjILuvVC38eD3cN9wiuUSwYC4Iv/0792Qe+IFvbyDxkB4fGCtNNu2ZmWD7TTpdIYTzFF22XHskcMbU9gd0FqySuObZCjITIq4SkQTQMeywmBAGoBhZHe9OaOe0slYLLurbJevCgEWItPeVLSZ9ubdpEEzIfMLuwuvH6zap6lwHoLAloXA+KNBhgvMmkEE0aHS4+hirDXpeLRjl4TWjlz6wvnpeDNQVkPfAAukI5tak8cVvD7/vMt/mnvmed28g4aB3B4wa8WWxWylidNJcCLuTL9W2URGNqVks3spYCvJyvoKWUBVCpopRfR0w7OP/NY/bmIRMzR1FFpDBJKSOJ2WdjIJMgzlHTUOvnETc+xLr/x289yXvt300vNz6997/U+YSXMhBDIJAp1aUzufst4CBSBqNaxa4u555KkkSTAnWAbz8vz9Txqd/uLlD1EyJoHtK9pM9slkRCv11Y0K0jBASoOZIT1uMDOEaUBIAzqdAtoW0loBr9fAiogn+JNjRxv5efmcSjEECZ1OI/n5mqUAorWp1MG2dEhobbvdwTOCI07JY61UK/HinmDWEIZHRhc9j+iWZSkXcJoCE7JrrgRmZp30tix8NhIYfWq4babu9U2CTiekkdsD3iHHntWy8dOfY85zjdj3Rn9gwQwXkCf9uU50e4dLKAHahoo17vKL3T/O+pf7KOKXmdEg0+OG4zZLde4BoDFlsoH5C5Y2vvz3n/sOn/hX/7iTmBORdstL7LyhhvAGdHz5f2Xtv/98hyJ62SGbrNwP+SuXoNlKMp9ZePoPHyis+GWBkdeDVSqmVLxZACyIyNgZIpOhf2h9Da0aBTOgLLCdzu6GO1TIkAvqaEs9WylAiOzxRUSAtkgEcmEUlAwE0Bm35MFE66YSCA0Y82i37950TnDSydBgmxNRspvrhcMKLITz5jNW+J3d3fmTZnAqk0l8UI1q+43WwWC27X27YBlOdXKIfe0nim1ltYbLdQyCaP9LDACGP5xLhgm2ku18ZddmCDoeA3PUaRSA7lDpafuz7NaI1o4wM7k9IE/Aj3hzx/04NGiVzeLcMVOeKPnxTdN8RxwNaNvWiRhxIi6ISO6aykwim2xqcPKAyCY7FCCise65x38SOGLKE/kXXWqnt+0gMveRx+AoPwCz1NGI876DQZ3/rctF7pkVx+e+/NzxdU88MLtpyQe/ANE68LkS2Pfa2nlLz5blCgilkhvmfuobdGZUeMIBVkklXKZ09TryGJi+f8NsigOwMrWy0NjYuGPTps3JoYMHujPp1fv19ogyzEZFuQgTI1HTiNwjx2DHS88jMOgISK9HxNdugCl1jaPGxEQ4UOhq9pmF/oljjxVeD+xUSguXS6rGpmjs7Xc/RSiUQktLZ0pmfHk4xFnwD5xysafvOHA6lnWjoUx1dO/hR6Hvj1+4naSJtqCxjt6WdkzAOhkDRHapJCcdQgVHn1zQ/M7fz0gmE4/CcXF9OU6JLwUGAI9wedr+p2NQ1hTUjifJvrrhGD1ISAP7Sx1fVaUwi2Wigu5rnPvQDf4xJxQja1xC6+0YwnBRbPHbKrZ9zSwIY3+tbBI0R5nM53W7+HdPF5z3S1KpuG231BokpLHHZtoJ7FzsvlZUVBIAqEj9Zp2Igbx+RoYRuD2wZQsZCsJV0n8IgBCk0YJDrbg7IMyYAcycGcwbdcwrPX7xz8lmr8Nsu6lWEMiAFOi4hlx7LX4DxqMDEPOBLaaaNSK2cyDpVA0n0+WFlEAqa/zjTrTqn61tdELp2XdHMv8ICRjGfg76QYHIMN4XFUw9482et907zCgusez6OkNIYUCIdhn8O8SBk02F6dOlNbvyyW23/2akDOf+LHTKmUo11oOVkp3qlzOGBMuSdqoR5DJV3oXfoeCxJ51b/bfbj61+7J7vK6qcAy7fp+KzHwv/CoUBk+zEF8+tsiObPhVuExAErVJs5PcfFxh18VjUfeJB6UQDAJRSAsCGV195fSGcvXe/rStEhLStkRPwYtSQflj35Cvoe3kFCqedhsN+cQU3vrcUiQ3rWoz8FoeZckRYNi//yJt34XljPf37jtMxJ0BQ+LxIb966uPH5/6zEpEF2JmXxYEFASg1gsG/IsUeRO8jspJpne0iwtuDuNQr+Uadp37ATlX/ESfu+Rp2sfYOn7lswSUCn4vD2G8++gVMvBhiYsY9KnQcWrR3MsC/vt+57AHvirHas1f4rvWVlEhWkAkX97iws/0UxCPtMUSUhoeMtyDvjGllw5DmPQ9s5mfo0nZl3AiQUWI8urPjlYwUX/Aoq2qBhJY3dYnb2AgPcVvASTnFWlSkmqJAtVfiQoq1K+cY1qrmuM4oCMaC9h40qksDYTAHWQ2+umsGEmTN1uN+oB3r8/O+TXaX90qqh2iAhO2YtZnaKwrYVemy79qsoZxd2QavF4utCZshYK6Az1rCDjRkMEInc0VOe6XnbfcNkXoFl19eZZBi0L9nkvWUTB1w2KysVzi2XsbqtP9/4k8tn1v79L1K43FLm5Npg1txZj1Cr9UdradfXCRkO26W3/TWv56//NNsk93cg5iiUl3eoRe3PwqERM2xYVl1q0/w3tE6DJAQ4xUYoxxMY863zEI8XIRTyApBEREII/vv9/7hp9nMvwm0IyQzbtp1CqZ2+lGalNF9x5TlcM3uu2vaf+Wp85V2alUiv/dMjJGXkpVgNqoHenlC3bm7UxYtyzz+3wsjP82rL0kQkdDKJ2LsL34Bl1cJQTkG1g4YyAa3hze99fmD4SSarlNp3PBOB03HoWKPQ8UapY525moRORjrVI1aWJG8IvqHHTAEwELfIA1ckrxO3z/xr7Qw63NdEYrRWUj+QF5Rmti3mdCIOh36+kygzsGCBbQrzkqJvzbzaO/hIna1y+l7QSpDLo7pd/qeBob6jHkJrYdJ9wam0THmjjv9bYcUvXSrWrAHKXjcLmQVYGIq8QVsGc7URyoMRzCMjmEcylAcRyNXk8X/9Wk+mKGKiZvOydPXGTJXy7DJBQoDTKe0bOhG+nkPPAQmHB+fQwsDNQkvI0wu/9dMKV5+Btt1U56IOwljAGk4BTq8tgjnKCOc5RUgzlwzlsgiGVYdtdGE3tPndlEp3uiiq1ti90v2BuBRDa+h4jDmZ6NxCfPBg4Gah3b7wd4p/+KtjjG4llmppNqkjA5TOyKbXZ8tQjjZy8mDk5JGRk0dGbh6McC6LQFDvt+WyI1RWKsyYIRKR+pvW3/aTkzb+8NJPovPfNoTPL2ROjoJTULXTihYZBjiVNOymRl38w5/qkmtveMDQ+ijMmaPQQaHa/XkixvaXLPQanox+ePc8z+BTVrkKDxvEVpTZirOrdNTU8PE3ntg895ZnMHCShc/fi0+fPl3Onl1ZdfUPr/meZVn3X3DedKNdav6OYDiL/IlHj8ZtN18lf/ubv2PrIy8jVdvktpu+qDV19Y0AZO7YAnfjG28Euv36+uO9o0ccoyJRJx4u4KfkF2tXN9z30DsYPjyJlxa11ho5GGgtt2AGBk89z9XtcOhUpMONaudftuOq2Lfbu3OdIgLbKRUYeZLpff2eCxL1G2/KUnvq4IAIirlZJ5qBfSpbDAgDhj+YJdi5o/vsox+spPBJ6GTcws54nn3JgoR414bWwwvO/unfQ8depOzmWkH7DF9o7ZMAxyPSKO5jd/vOH85O/+H8XyXn3/J7lJV1VHqile5gSviYCyeTN6x1S63scAFiZhnIgY61yNSaT5DesQGquRbaTqUAEtIbMGWoQJhFveDq3v/rdqu0FkVckly3LB084iQXdxTrRQSdikuzuDcCY4+riGxefiPmzWvBoawnl3FZh8cff31wymmsWhqpQ2VFa5DHq0kawtq+UaS2rIFVuxU6FrFZKyVMl0uG8sjILZSu3gMhw7lZObe6sAuUTSBinYg3s211HA8GOIki/gDIdGUUa/pq7q22LCcFEXQZIIIdb4iA4CjzXwecCuBG+OiTfhQ4cirbTQ1iXwoP+fyaAJHevEGkNqyBVbMNKha1mFlLl9stc/LILCgm94CBEMHggZPNmTM1yssl5sx5vWbunLea5/770pyzvvXjvDPPG+KfOAUyEGSdSimdiEsS1LGVCnDOgWBhNzao4h/+RMaXLLq37u0Xx4PZzrbG7a8ap5DqlrYb525IrXmt0l3Y90angrTNwuM3AkdcemXi81eWp+sbPgFKVGVlZaq8vFxWVlY+cOH5565cMP9HPz/++GMn9+hZGpJCgll3HNOKjPdHM1Xv2JG0GpZ/YGJbc3xdSy/oyHqy1sxsbo6tQ/EIfyoW8/lGDRuWc9F5V0m/z9SRqCYSgFKIvrVgVnL9+vU4/vg0li49uK4tp7jo5MDQaQMhTO1Uzz6Id+wMMsR5rpKBCA495oLE/Ed+h3nzrEO08bVucDvs5homIYizcQsxgwwX7IatqKu8NUpSJPfLFbbPBYsUSLhSGxd/BgC48TcCM9spWLtri46f3J8/7rSni877tVdHWxTRvrO2doM0oFrqpW/siXZhxa9u3fbILxaqBe++g2z0ARkuEl/pkHP8w49mTsc1iQ5zRplcHmp87UE0V816JbV28SvpWP1y5RS8TMJRNIMSKPR2H/jj3re9fJKRW6zZTnectn7woDOb14b0xhWr2LZGkBAd+j0JRFrbKufEi4qa3nr6Rwkpb8LYsSYWLdqzIOHBQCsLcm//6CkThTdAdnNDBwUuNcjr4/TmNaJu1t0bY5/Or0xvXf1BGlgLIApnzD0AQgIY0+P7t/616NJfmnZzA0jup2z9/4aKSgFAqcaaak6nAJeLkS1t3eHHQuMLT6jU2hWNDnGpog715E6/fWZyeZDevM5m07cWdgJfk5+yNZxiRGBi2RAyDEBpmdW4oDWEz8+JFUtE7aP3fR79aMGc5I4NHylgAxzZ1HCKeoYN4OheN9/3+4JvfZftpsYDJ5tOIoeEEHZK6weqn3/8iYbnHz83fOwZV4SOPWly+JgTDbNHTzArpaNRckgiO1j+HH4fScGQypt+4cjGt18+VgnxOrKsr/ur9DCq56ZROjEeeWvmS94BR5/iKh01VqeaNdtR7Srs1zfv7Dt/vOOuY27wDztax5ZJrqysTJeXl8vZs2f/9+/33Xnm3++7sxhA4X7d01m0m+CUXdsdxcV+/wi/PzZ3Sc/+b734Y0//fv3s5mYNZhg5QRFfvOyT6t//8aXQxInxlrlzD6aVB63p4P7eY77lGzQV2oprtFep7esAawFpav/waYfL+Y9MVkK+jQPF2dMxf0jr+95uN+6oY+547JmETYbbaJr/WGWiace1cLKsDqTPXMCZ3NiHwuPE8RDZ/u6H3VV86R+GwnTbSETaJyZ0UsDZycBuJ5FOGqQiDSL/zOtEasOyx2vmPT4GzLUZgq3d+5GpLO3uM2yszCmmjqqiQysWvjDq59wR2/zIDd8C8IJzQ9o9eJ4ApTXI7ZkExkkgcShdnHtj6lQJwE58/vFbdu3WETKvm+Z0MrsSJgR0PCI8A8fqvLN/8POtj818AZ8t/hQOQ/GBUnxExgKqsCc7FGtIyFHe/iM8rJXOSgbFDHJ5tbVtg9h008VzIusWXw6gufUZdpsmQkAHAytB4nY4ct4V3LMvtMaD1WxdYzfVwSzpBbasLDqPZunxUmrTGmvrE38tg1NdvKPM2P0FwVk/o5n/P/SmuozF3ufLHecdPIK01raTzbk3WGsIn08nViwW66/51v2x7et/BCDRls2169wjgi3tHWD+PQ5O0oByiCDLBcRzSYvVE3Vvv/hE3dsvHhnuP/zbgaOPOyfnlLOLfWPGAwSlIpEOA55JCKhkgv3jJ3Jg0Mizm1d98nqW4qf7veg5FNhAwqbk5sgH/7xPJxtiwnQTBEGnm7XvsCOPK75q9o9iy97u7h92eAglJe7Kykows2RmIaWsBrBsP67lAJZKITY7FvAZwjEUDHGhsDDgH9HPH5v7fvc+sx65LnDkhGl2c4smEITLTXZDY7z2/of/bkeSm1osKwln8zx4rq05zykAYf+gstNlTndwOrHvYDvWB+jaF7OtgE7HtG/wMfD3HXsRWONAFCFlAMLtcwNoh0gs8xUn8LQpXbtuDdtJkJBZC/lwOi6Nwp4ITzrvBPTubZPpboCQLQfwagJRZ5QoiQXv2qYQFxZX/PZyV6+hNsdbsis8UjJJkyBlVnM7MQutbVV0yW09QgPGZ4vvoQwbrilzinsKlwesVZa0JnaYnuu3UuMbjzwMohfwz49Np+Aly92Kjw4c6AKzZE2+Tjz7wUdVFYME4puW/zu2ZAHI7RHt1jnaBUSCdLwFhedf7y0oq6iEsntCGhbGjjXx5aPjCSjPlJAQul16/7IyAjNcwfw+ZkF3MOus5RNZaxZuj2h645lIZN3iq2C4mp0CnNizKKyBp9ISjY25znrWhU6hqgogILV57WfpzetBhknZ63gJ0lZa5Uw72+PNKZ5Gptk8g7mFpPzKF5x/myFEtN2bH2LIwu79jIJiwO6QU0cL0yUanntqW2z7+mvIdCUwdqzZVrB4Z/FRA089JZFCzgHu5p4dY6BSQSsCo3UOvt+8dukPtj5859A1F5xy7eafXbUmsWyJlKFwx3scEZBKCSO/kDwDh4wB4Bwe28GXOelpbFloofvRscj7/6iKLX/hQbhMIskgCdIqrn0jTrqg2zUv/DS2bH4fnxkOY+AkH+DQLiilnMyLGTOEo8B07lJak8PqPBMAmZiU40ZhYSD2zvt9+lQ+/tPQCdMu1LGEJu04bcntouYXX3uw8f7H5uVOmhTHokVpHFxNXELZZJqek/3DTihiZtUZ3x25A4o8QfsrXd6QTdLkjs225BQhzSmBb/DUMwDkdlQkrzN1jshx/yizsI+UQD/c+Jv2C785p3qkNyx9XzXuYJju7IoBEbFtqZxjv9PDF7X/xFYKuPFNA1rtWWV9X5ezsYweZQJO9kzbZ7zPTUaAWUOrPvkn/uDe0NSLtG6pk2gvpibDHqsaq2nLXy61VaSByHC1P0FJAKm4NAp62N2+fcupHn/uT7DgFjtTTG9P+ITH7wUI2fvLgGGSVbcFOlL3CZglrvgJZ+pztWaoOVdhoQag6JsTOKKgFdnA+5EPXl0OK02QsmOrY4atlYl09x/d3b9g6nlvQ9mjsWiRBSE5k7Uh0VqQtv1LAHAKNpbPcuqXoVKBiKH1GH+4ZGYgEChoveOutzfDOTnk8QIqe71PEoLZtmE31mzAjBn1sFKEFSss7DkegMb5hsKhZUf/vwANkkhDfZRY+WmShJBZk+qFgI7HyDNkNOecWPFbtqy+M01T88CBLlZKsFLUyUs4NapGmWzbjnKe+SxTtuJrxFQAgAyFwsLtRkfEjyQl60QCVkPt5yBKczolMu7hdmTz/AMvm0JwZq3bw+SZMaY4c1CgvFxCyvqUHbt7x3OPjl57yVl/aHz2MRZ+v+5o+eJMYV6juFsPAAFySp/sNT5fNjRbYc1rSQw6rqn+6csecRX06u8detypOtGsACWgkuwfffKFPW58q6j2yevvxOfvLQ0NOSHa0rIigS1bLAB6n66FveEsVqWlZmjIEG/LG28EXKOGDev5yks/Chw5/ngdjzHAxIA2wjky8va8VzZ/79qHg8eNb2x8880EDvbiUj6LQcSBwcd+y9PvCOZ0gjv2QwqwlUB6+6oMU/OXMEAR0EqbLkNFkKFCQNvIuiA7EVIqMPKkfM/ce09NWokngamyrYjlrkgnU9pOOUHQ2aItSEBbSfb0GQ1vj6HTozff8jK+/7HE/T8xgCqGM4G4NVsntnX5q4k1H10fnHSu0Kk40B5fIwlwMirdvYerbhf/+Yfb/nllbXLmMTdDSCeIcepU6bCe7hk0WEZthQnnzdOQhgazxqJFwM76XZ2RAadiOZGdM2jygwXn35ijklFF2dyUJDQJKWor/7ChfuFzFxmh/FklV97TnVW6ffeRyMT3jDtR5Z9x3e+3Pn1TFRa8+zH2djVmL9WxKzJ1qViaPsyYwaislFjRiaf8JoCmShDZ0UWv35dY9dG9niETmWMtQEfhSySAdFoIj0+X/uyfA9w9DltQ/8J9v0tGG/6JykqnRsWebqTdwI7CXVWFDMtvkRviFN/Yad/KmTp9qplfZGy89bKtAO53CvTuOjc6kXQHRoYTxYd5EKhsqwrdhQMDnSG33BRf/vEinUxMIsPQYG7X90GA0KmELv7Br/Ot6s3/qZv3/DlYsWI1pATOeVqi5l5qPwA5s55cfTXj/AuU48ha1Fq70AsgcRCf8SAhU7hWSD+IgMpDHG2qNTKFQ53tbspkI7OWO/sEMnPF4TIjoEwyz4sR0a+23vIr9o0a9yvXgIGK4zHZ/hrhzD3p9QcBeHmn23E3fFmlhwHYWLUmhuIRW2sf//btxVc+H3YPmDBZJxoVSAttRbV34FHTSn70bN+WBY/8s7Hy1jcA1IRHnploNrakkUjYWLGiVbPMtuM7J7MhQwS8XiMcCLiaqz7ytmx5o6jwZz85If+iC65w9+/TX7e0aICJBbSRlyNjHy7678ZLfnC7f8SIrZE1NTEcXLcWAIiM1aSnb1DZscKfQyqapewE4ET++0NoWvgUVz/+o5tFKH8HrFTHhfCywXRDN9eI4KQLftb9uw/01ikrO92pENCpGDz9xrFv4JRvJZfRE5jBOlO7JANHQbG01aDjEYBE9sA/IiCdkEZ+D51TdskF0ad+Pgv3j3ut7bOd1g4NrUkRvRtdOndDcOI5vZkoOxW5kNDRRhmcVK57+nNm1s753ZFNK+ffCqL/ojW+py2MqJUwo2pnEUrn8cMARgZLh5zo7j3svOQXi26P1qz9FxyZz+7eKi8XmHOL7fLnXl94wYxjZajI1tF6o91JpmyInEJufOEurn/r4SvJMP9b98a/rvQOGPti7slXsGquRXvWIZKSdKwZBdN/YibXLHq0/qP/jMOsWWlUVOw6EAmdiqUzz5OFcVuA00lylfSHu/vhpyZmzrwXhpkGs8DUqXt22EDZDGDTf75BrhSHoj5B9HjDyw/+sseQiaWa9kFRDzglCqyUgDB08WU3+UOTTr2t6c1nroktWfByYu0nb1paLwewDUAcaONTMuBsVvkS6OEJFo1wHTZ8im/whKMDY48r8AwaBxkMK04n7MCwieWpD1+9H7w787OKtUTZamW0znISYC1ISO3uO7QP/n31WLx72wdgFqioFK3xKACA2lqBe5drnJ37dQWT/++CpgoAOvbxgsrkF8uO8g4eBR2Lot1DJhFgWUJ4fbrnzf8c6vpnn/frKv91WzodexyVFbW7fa9tzeKd64kTEyIB9HdDHBU85ozzVKQ5t/Hjd6aAKJ2xwn6NsVjzAAA6Fo1qy4LRgYmebSUo4GbvYYOH4RXdD99yrZvBLGZWVFK7snmq/0DIpgSgggW9TnMPG3Fbcs2q2dEta14EsLQtezXDpE8AtG238ckzoMbdv8ggw7DSsfoPrNpquAcP23e0uJTtVWJvw1dJwtfARguiJGqbuV/s+HvFjd2umXWL97AJk1WiUQOadLJJmwWl/fNO//HvfGOmHRed99Ts5tfvXwSgBoGSFMZOsxDIVYgmNAKR3U9D0ahAICAQNSU+X2giGnU3A0W5F186Nve88nO9w4ceJwNew25s1kQgksRGTo6MffTJu5su/O5vXXnu1bHa2ii2b3csSwcVZQK6SntDeWf5h53gY21nDSYDAAihYVsivqpqcUqlbkLT9q8c+G+snF+Yrlk7wyzqqzgVN7JZCVilpRHIh3fI1KlYNrcPbjE2YGehWADgTIr5drtpuwZRx2k1QpKOtyDvxKs8JI2XIx/MeS6xcfFrKhX93AZWw8kganVxJSOfvf5o/paVM8weAxUno9nT+R3FR/hGTlM9+445KbxwzkmxlfM/Tm9YNj9VvXaZbcW3KXDMAAQDHgGR5woXlRgFvQ43C3sNMrv1G+zpObTYO3gSjGABNt122iTUrP0XymYAVTOzPg3mPKeg9eCCk35wq2/0NKWa6jKp4nuMD2uQL6RSaz+TtS/ceacier33URd6Ns5/7D+1z/3lX/5hZd8zinsrTsXb4fMhwLYlfF676Nu3DElsXPaH+HnnXYfycqdEhZPZlNLRhhq20z07onliOy2lL6QLpl9/crp645/j9Zv+DKJt2FvmbaAKdmEv6xtEhsegCgkSkcb5z80Il537UODIU5Rq6SAzqhUkAK2EHW1k9+GjdbfB47vZddsuT21YcXlq8+dsN9U16UQ0BlZpQAiSpoc8Xo8M5gRchaWGq3tfmCV9IYO5TpZIMg67frswcwvJN+KoyfUfvtoHMjM3Msp0url2m2qqBfUdTNmsnyQkVLwFOdMqZNGS/z5W8+ZTV4NoHtpTtI8hAIhwB2zUXWgPVQpCIhGpe6b5jedm+oaPDzFrpmz1EoQAp1ICLrfu/rPb83JOOPvPzW8+/4vEik+rUutWfWA17lifZm4Asy0BUwJBMgNFrm49exndegz2lPYZ4upz2ADf0NGuwMSpqH3kLm78+J1SCLEOTrjG1z6hVH31VtXUAKO4JJNU0d4ZSZBOxHRexcW++Gcfzqqt+s81M4neb7dBRzajTpD4V5DNTDCxyMk9vtftfx+hoi0jWqrm3pxYtnhpYsWSd+OrlvzX0tZSAFsZqM8UJncqBjktaABDcyZO+7l36EjoWKxjRYwZnEwmACSzDcxXZR5S2L49heLCiHL5V1Xfc9YNhVc8/nPf4CmngVPQnGKdjmgypeEbMulET5+BZeFTL/0wsWrhvMSncz+Nfly1GVa8CQ5nyq6kgY4rC/DA5wsHJk7uFZx67GjfhHFT3X37jDcKcr06HoWKxDSIQS43kcdFkberXt542VV/tN3u1TDCEWxfksKh8Jk73DzwDSg73106ApyMZ3dNsAa5Azq1fZVIrv7vbKBcoleNiY1fNkOpVmDGvTo285h/x5e9dWPOCT+UnIxl1VOIBLRtqcCIEzz+V+86Jxap+QtQJjImRgBt1QU3WrUb6gAuQtZj7c5mWdmcd8p1Iufoi85NbV99rlW7AQ0v3zmred3H56G8XKCyUoOZEkT/aHznoR8VX/rXoNaaSXZQ2EVI6GiThMurck64QuYce+k4u3H7OKthK1RLLXQ6AYAgTA+ELwwZyoMMFkAG80AuL8DM0HaatTLN/B79AQDzblKgdpUeQvksQmUFhYcdc3/+2T/x6lhEUZaiokxCC2ZRW/m7tfHaDb/GrFlyY0VFGswiRvTz2tm3n9z9mvt7aGSxaAkBHWuW7v4j7cKzr792yz+veVHNee4tABIVlQCgUts2rFCx5jHk8WuodLs1J0hI6EREBMafzL1v6n1984LKy5ObVn6s6rd9oZPxFhBpEsIkaXjJ5fHJ3OKpZJqAsveTMOtgoVLj3FnSqqx4pPbp2y/yDp5wLHn9NqxkVsW9Da2nnXhUajCLYJ7yjzuOAuOPlyDKBevc1uo3JHZaBhnsMJ5aSbJbGpx6REKADBc0Kzs47jiP99k7z0hE6v7WxmclBCxtrUhtWs3+MVMFM2d1IkMpQabJPX52z+GBccfOjS5esMLatvEzFWmoZs0pMAkyyC08QQ9D9zYLenjZtluzILuwbzCmnyMxe3Z189w5D+SdfelPzJ59bY7HjKwhBUIAti3sSDP7Rk7Q/tFHFtlNDeV29dZyq3Y7VLTFqapuGBA+P4xwHoycPMjcAgh/EGQY0FZawZDaLOlpSKCv0nodvnwA/YFBJnQg2VyzLL1pHTxDRgqdTTadAqxCBnO41x3/Ght8cdZ7sc8+/Cy9ddNS1dJYy1qniaQkKd3k87sBPtws6ga27c4US2sfrZmo/Q4bIUIhLQsKrMJ+V7vZtobbdTXDU+vXXJVcswrWjq11dl1drY5FG3U6HWNoLUy3TwaDRa7e/frnnnGuIfx+cDo7zUYmno7suuodACLZznYHgm5RoXpJEiVj2RbG59tvn/bb4u8/uNY34fTLjVBuQCeamNnSOt4I4XZ7PAPHHO0ZMPjo0DFn1Be01K6zm7avU03VW1RLfaO2nLoRwuf1mXn5uWa34h5Gt+J+sjCvv5mfly98buhkHHZTs3YycolkTg7ZzU3Rlueff2jzFdc86h82bKvtdkewaNGhUXgAAScbaah34NETyO1jHa2T2cjrmBlCumRsxVtWtGHzcxBbFTbqr+b3v2kqYSaWxD9f8En4mO+Og5DZGSlJgNMJuEqHwjuk7PzYB5V/bVXaWruIG22BmdSS3rpiMSdj00hm95nv2rKONTIMt/YNPcpSzYPdDS/9dTsAoKbGSe2sqJAg2tH07tO/C02cfrv38Em2jjUYHdYpFBJQllTRBpAQWuZ207KgJzIpw62dZjhVWNmpbG2RTidaPzcNf64w8nv2A+CFNBJoLwWzvFxgzvnK5c25tqj8hsnCH7Z1tLH9bC1tQ4YKufmNB0Xjfyt/CCHiqKiQADRoqgEhmhrnPfnT4PhTnwkedY5WkYZ2696QNEhHGkTuSd/l2PIF/6x7d9YozOA4Zo6TIFLJ9Z+9k9688tueoZOIY8nsBRBJQCei5Oo9WBX3vzWsk7HjdDxyHFtJJ4deSJBhgqQJMt2Zbb+D6tSHFozKCgYzmogucz/wm0Xdr78vX9upDMdVJ/rYVpcnZWgrASewlZ3AyVZodty0jvE8UxSUdhsXEgROxsndbyh8w48qT7z3wt9aF+2MqK1KrF68Dqz7gSh7yr/DG0IQQuecegnlnHTREB1vHqKTcbBtOywW0nDGxDABw4BORL8x7Bb/E6isZDBTlOj2msfuvLjnTX/PtxHbd/VukqSjEekUC3Vr14DB7D58WGuAOyCIM9lgmpUNtixS8ShBayKw1ILYXdqXXP68wYlYw1vZ0qIPITSEgFJqUXzJx/Xhk8/JR/YozIzikyaYLl1w8ZUi/4LvjlLR5lE6EQeUcuaSkZFNlwuQEjoeyxrSuA8QnGDikKtX3yHC4xV2U4MJEJMQLPPytL94MgWOKpNgLoDmArbSaGVlJlOCXG7ngJiI644UHjADpkurpgZKrlr+KQCFZ56WqKj4yjw92aCwfVEK6K2Dg45T1fdffk9gzXeX5p1y1fddPQ+fIFwGcTrCWluK4k0gASFz8vPNbiX5MEaMJ7bAOgXAAiRDuA2QxwVyS0AArJLQqRTrdEpDMEgKIYJ+grIRX7ryo7oH/nV/4yOPzQseN74xsqYmhmXLWiPSDz4yJ8FAQa9zA8OmGWynbOqArpeEodiKy8Tn734MYCWUoswC+uWR4TyJr14wO71t5ThX6VDmVBRZXVzalsIVZu/go8fig8oRkHIxdnVxzXR85vEvFr6U3rryeFef0czJSNb22iAkQVlSp9Kc2rSUkttWLgawM9amslKjfJZMVlb8tebZm87p+ZNZR8Dl2/ep3lmsAEBwOimABFrrIGamgOMGFq0/7ixYyloRE8HI614MoBTgL7C30iMwa5YGUZ/8Ey6/1TdqmlYtWRRXZpDbp+yaTbLuP3c/q0CvYfozEpWtk6vKxvRZ0qqseLb++b9c5ht+9Ank9iooS7YfA8KCieyiC37TP7n6o9uiN4vrMPZ7Ep9+hlQq8lLLBy81+UZMDdn7qk4vBDgZkzZHmQxDkz/MgnLa/oKZHdOvsiQ6aufrgYZjbdlY9+q/zjfyi18tvuwWqSINGqw7x2gOZBQaiV3e087nlHv9pl2wZQkZDrF/5FFH1L/3wkBI43MAAlMmS1RVpSKfvDU7tWXtL8xuvRWn4tn7RgSwFirSCCKh4fJo6dnJ6uDoZU6pESglv3Ej8s1Hq8zUNrzwyDWB0Uc9k3fuJZZVXU3kMjt+m61Ksm1Lti1nLWldDajVpuEoxc5yQnCCbxmsbBiFxXD16j8osbLhYD9jZ8A45xyJysrGyLtvv1T4ne2XUCiskE4bWRWETBak3dQIEkKT16cNf2CvtQL6K8smZQrMHubuOyDjMaBW1zVxKiU4mWytQcsgYpIyY6YisGWBk0kCM5EQHR6AWGtIn49aqt6i2PJPKkEEVFS0+90DebRQwMZ0ZNVbEYydVht9+4FXNt14zDXNcx+82dq+ZgW5XCQDYSlMt4QA63TSVtEm50rGNCubQcJZvG2bVTym7ZZmW7U02zqZtAGwcLmkDIUlmS5KrVu/ou7Rx25Zf/Ip1zQ+8tjLmDatNvLWRxFs3JjGoUwDdbgApOfwo8tdxYejw4WQNcjt4/TW5Yivftcpt01Tv7qbIWN+jzdu/3ds2VsWSZfsMHWRBNhOqsCwacJf3K8CWjvK284GW33ms5rff7ZFmKZgrTrlt2YAJKS06jYhlYqvdt5FW3aEc6oXwmpa9tYFOx79abWQhgFpKujOF5xz5EQ6ik3bJZzf7zExCCCw1q7i/oYE+mdWt92/VD6LQMShfuP+mn/m9UGdiHFW1mXWLFx+qn/pb5HIxsU/B2tCZcXu76ayAhACTZ+/f33jq/enpDdIWd8fCeh4i3T3Ha7yTrv6hwbzRHz6gIVBA10gqm15d/Y9yTWfCBHMs1ntwwNKAiQkQbOElTI4nWy7YKUMqLSx17N/c6CgtaGEeHPHE7eeX/PwDC38IQHTve/nPoAgIUjblgqOPdb0hAvPdOQy4+Jipti2tffU//sfEeHxCdb7Sv/PKN9EAsrafTzSKQNW2vjmuBn/J6EwZYphsfXs9rt/+9fo+/NMs6jYdmr8dQK7riVy57VzPWmz8O38vmWRDOXCVdJjMAAnU/TrRmUlwEzNny+6o+7ph2wjGCK27Y7X61YrJ5GAlTY4lTQ46VxIpQykD4RsOnUo3e7AKM/hg6GV0rt5yYhaMx1BUjqKDbOEZgmtJZglCSFIyo6t0swgQyqk0rL+2UeXpFX6tQyVQLtjc6DtqRqAhUVvxlAytjnUd8ia2gd+9K8tM065ovHlf96Y/OKT+SoRaSGPVxihHEMGQoZwew0yTOHYlpnBzCwkCcMUwu0xpD9oGKFcgzxeoSKxlsRni+fXPfLQbzedNf2K7b/4xf2+oUO/wMSJTXjzzRgcZtZDKYQZng+M9w+aOgTSYGad9Z0yaxbSMKLL30wlIjUvOO6KA1L/SuNGJQCsjq9+9wNOxYikkV2LIIJOJ4RZ3B/+gUdPB2Biwbu75roz9DkZV9STt8WXvi2M3EKL7c7U6GQmISldvT4GYF1GWHd9Rg2tJYRcXzvv4dN3PHRdAwkpyRM4OJubU8dEm4U94Ql3Gwxm7FGsUmL2+coFnJp/+rVnyfzuitPJ9kklneBlnfj8fdH49hO/B4lNqKjYm1EZUJg+XYJoecMbD96b2rBMCE9AZyVRkwapWDNyT/6eCI4+4W/QWmLoTQrnPitjtRv/UP3obxZzvMUUvqDV6Xe0K8tqG9vqN1XfaYMNrQ1FYs6Wx28+dftd19YhlTRkKN9m7RR4PPgg6HiEXP2GqNDEU6YDMMDzHB4TIgEhttT954EfN73+pDALihTbVse1n3Zp9390TL7ZqKpSOLdcxms3Xb955lUPR99/xzQKihVr3SGny5cFK0Xk9sAs7tkXgDtTnuTrHkSFigoBEstqn7xvRstbr0izWzebrU7KZntyeSBkM0Mh4uk/ZJyrtDdgW3zAXepag1lrIz8fNf/4K9e99eIPQZQGtaVY7oWD4UR20tm3L0q2rFgYwYjjG/3enOV1D/ziwU3XTLqm+p7rv9v06uO/j302/+X05jUr7Zb6Op2MpaFtQAgCCWLbhkrE06qxoS65fs2q2ML/vtw468k/7Jjxy++tOemYa3b89jcPuAtylmPEiMaWhQsjWLiwNRD60EbRl5URWCPQffB5/kFTie2UysZODwAkTa2TUSS+eO89AOug7PY2zC8HxyWFxOr/zk5tWQ5y+zjbJgsA0FpASPYNnTrQACZmJscuna/UOPdcmWyuvmPH4z+dnfziY5eRW2QDUKyzV8IlEszKgt2wdRNaM7f2hoJWEiQ+qp77j2O23fOdz+36rYYRKtAAddj+foMZnE7AyC3RZkn/w51fTm3rrqNoa09o0rl3hCaXs442tRt/AwAM0sQsGl66Z22ypfpOaOUEaLeHykqG1iJeu+G2hv/cU02Gi7iDsWbbkuTxqYKzfjze7Q58B7PPV6i8XUCIWMOiV8/edveV63Ss2ZThQgusNQ7kOwJaXS2AVmCt2Cmw+bXwy9hgbYDE6zv+8/dJm2aUvxP7eK4hAzkkfCEbzPqAygeQMeUrsFIM01QymAshTSm9gQkAukNKhjM3FKZPl1Yq/uC2u3/6u6bXnjWNvGKC6bJZ2R3Pt/3vVKuLAawyfesiMmwPnEmSENFNqy7b9Mtv39FY+ZA0gmFBvoAzLgdK+XHcW0RgdpWUFgPoltELvm6lx6lnde50mWyq+d3mm667P/LWa6ZR1I1hGIqVjQMqm23uLwXWmllrDdGObGYYkT39Dhsuc3JYJ5MH7j0xg20b8HhsI7dA1P7jbrn1zt9dBSEW4NxzOyyvdADrxu8Fh39nyVzVDKQxcWISWseiVbO3RKtmvwsgYOQV53kHDi+QPXrmy5yioPR5PYCGSseT3NwUsTavb0isWFZrN1Q3wCEaSmHCBBtCWM0LF7ayzraluB1iUCbI0fQddtTpRkFvpRItIBLtv2ytIbwhldywSCS+eO/fTgtTD5zSgyqdcUm9FFs573bPgAmG1lpRFqI3x8WVtn2Dywxf6ZDpLVtWLGitHZZB62KCCNEFW+66oL7w7F9fEZx0HoTpZrZTiq0UsVYCuzqVhGSdjMGq3/wFAIXpT+8S77IbFFhLCLGkbuGsSeltK/+ad8qPLg5NroDwBVmnk4qtpACzoLZTMtD++tJKRp2JW2n1DwuDyetlMt1EHp8Q/rzxAIB5N2kng6tcgIRye0PfzTvlh4MgzTTrmCSxNzMwawUjVKAiC19wNb5feSNIJEBTO+L80Zl07IbG+c/eHDq64l7/iKlpu6Vh7xoyBJCUUPEW7R8zjcNTyn9b8+bDz4A/joHIgBDr6xbMmmo37Hio8LxfTfOPOhYQUut0UsNOCdaaHObJjt7R7u/JmTDMEIJJSIZhMhkukBBCuAyDW/zEWn9dJSvsjGx80bhk3rHxZe9elXPyd3+Ze/LFvbyHjwGkwWylFFtJwUoREVHbybSj529VlJgzMTVgkoLJ5dFkuoQQEHZDjWye/wKa356zsOWDN/4AYOtupvLKSoXycpmorPz15lu/syOxdsntBdOv9hr5xdDasjmVJLYsJ7qsdUCynm65beVi4rZhISEYUjKZLiZpkoAWRsgkaB3O/nBtyJBwUlb+MwK1chd1Zu3pVHvsRMMcsPbQ+faADM0GZswQsZkzf7b+pss/ji394I7Ci64tdfcfDFaWrRNxYtsWbTE7Ha0nu8pJJt6EhGC43VoafhIBtzSLS30SOFxpvbH9RtqQYUKnrPEBmefFVz5kVFbqzDu4Yv1139pS/L2fzMi/4LtS5uaD7bTNyaTzDgBqczN1RjbbFteMbBoGk2kyGSYJsDCCbmJbh/d8LDIMZqDA7NZtolkQIqslBdiWzcnkzjVr15pfHQUpIzMW7IwF+bxauj2GtX2rseP3v23c/tCdP9TAUygrM1BZ2aE5/GAqPcBOhUTDUVLSGDJEIhRKFLp7Rmq3bqyOrPhI4P03W6Pnd8nGgQbCjAEDdeHwQbo2lVJoaVH48MNWyuyvmxtBQAjlkq6Tg6PO6C9cBtj2Yi9Cwv/X3pnG1lVccfw/M/fti+04ceIkzuqQBMfQQFjE5gBJUVUCbeHRRWpTWqmqhNSiCrVQoj4CUqV+qiqIiigtUtsPJCZRVUoLpQ1JMKA2CSVxbJI4i/cljtfnt917Z04/zH1+z4n3hBDa+5PuJ+udN3fOmfPGZ845k0sOUxI8YIjU8X1IJ/v/Bi4AdVmOtnLkOpWeTjd9cJQpdZMIRDDheuQAKSm85csQqLp363B7Yxy79wxhbJKvLlVk3E72nPp+5sVH/1p8+PWnIjc9cGtozZ2GUbLQqQYCQEqRUor7PLbV20HW+WbdF7iw6dXFSB1x4v3DrfVbky9+99Xig396MnLLl+8KX7fJECXl+nydpCRpk87qd25JHs1iZrpKhwswYYAJzhjjgogYpUdgdZxA+sS/kGo88FGm+civoJ2p/iDfI6GovPjurb8Ira+BTKa9es4uRPtUMjOi/62XPrQsa6dTWTTFWVOtgiKeZezl/r/s+EFo3V2rRSB8sbMdtXolmDAw96EnKpLHDmxLcv4kYjFCbS0H522DDQc2p39W972ie775eOSW+9cG1tzCjZIyMMMDIkWgXBSu4C623HflnAvnOY+nz8qVYmRlIUcGYfV3wew8BbPr7LDZ0thCVqoOAMbvWPuJo22DiLKM/brnjRdfHdq389Gi27Z8O7RhU3Ww+nbDM9exP1KKpFSQNoikTg4ucNpa35zAnTwGYYAzJhjnjCyT2f3dPN10BKmjdT3Jo++9OfTxB38E8I8JR+ZsfLK1tc93/OHne0f+/c+no3c98JXIrff5fBWVENESAAwEJYmUjjRcGG3gOZ1wXbXFwHMlMiRtplIjsPvaYXa3INtynMye9s7kkffeBpAh22bQ/UzGgaLc7xNQUoyWbxeuQCLAMAT3BUA0rU1tlPs9gtv+i5utMkeeMAT3B2cg74LxjRm+Mz5/AEQ0k003Yft2Qiwm1Guv7eze/dK+RN3bTxR/4ZHvFG16cI5/VRVEOKrvTVO2thWtF5ZfKwwQnHS+i6FzTRgXYMyxk16eaWpA8lBd//AHe1+XQL3TnHASP04h7g8KGIZg5Bn/h51IcL8PUDSe85kJhO3bgXicZ7Zvf67ll0+/Obz/708Xbd7yxeidmwzvkmUQ4SicDYQk5dgmVMFawaivYDpH0ql15BwEkG0zlUzA6u6E2daMzJmT0uxo6cwc+3A3oP+es03SPYxSibq9O1p//KOHwzfdtjBQfQM888vBvB69bpXSupBS66Jw3fKcb+dgwgDXeT+CTJOZLc188K3X7cE/79w59PF/ngFjp0APC+yffMOTe8UrTeFhIQduZLg2zYAqwEwwVAJojRDQAAQChMOHC8u5P62ozngIADIQLnswuPqOnzBvUEHZE2e6ExQTHp5pP/phovXIYyCaxHFd2phCJYu/Fqi89Yfw+CRITpKMRgTDy+ze1rTZevAbqXS6e4IOowzxOMOzzyrHQdREK6q3eJdU3+Gdv3KVZ86iOZ45i8BDxRCRUpjtjeh4/ltbM1bq90CNMe41F+PKfy6X97KxaPkNX/dX3nyPb0lVpb+iCkbJQvBwCZjHl0sOdcKsEiRtqHQCMjkAe6AL2c4mZZ9vbct2nqjPNNe/m+pr3Qvg0AXfyQGmPJ5gVWTt7b8RJQtAMssmWhJcCCVTw0jWv/NUJjNyABdfGzERAmDSF4zcF6m++xnuDyulo2PjTAJAgBSGlydPHnx/pOPjJxCPc+fKFg7Gc/PjFcDm8MoNW/wrqm/zlq+s9CxYHvCULgIPF4H7gmAi52AJpCTINkFWFpRNQyaHYA/1wu7vtuzBc91Wb1uL3dvemO0+22gOdtVLoAlAO66atRYTeoMqAT3vd0WXrdvir1xf46tYvca3dHXQM38pRFEpRCgCGJ6xNqKUngMzC5kaghzqg9XTgmz76RGz/WRT9vSx90fOfPS2BOoA9OmEeABKTaHjMeNaFSpacL//2ps3+5avud67YMlCb/kyGMVzwQNhMK9Pb7gIemMmbSjLBGUzUOkR2MP9sPu6YQ2cG7R7u9qsnrbTVmdLQ6bj5FEL6gSA05igrb6GGMCoqHL9C/4VVTeStBVNkL7AhJBkmSLTePilRM+ZVzCuLefkfe4F/8p1Wh5dIM+JUzMhJNmmyDQc+k2i5+zvppD3vH/Vug1kTzI+ruWlGw+9PNJ19rfjy5sUAS5yelkYCBQ/FLp545f8a6+/MVhZVeStWAajZB54KOwkLnPnl6XAToYHYQ+ch9l+Ftm2s8Nm25mmzOnjB1PHD+2zdD1q95RjAGSk4prHg9eu/yoxLonUuBWcjEEyzkXqeP07iTP1P83P7CUQiwns3i2dzfa68LyKLYHrNtzrX7G62luxtMy7aCmMklLwYAjM5wPTexp9ZGXbINOEyqShUiOwB/pg9XaTfb53wOrpaLM625uy7c3HzPZT9SZwAsAZTH0tR7EAbg9VVn8+ULX+zsCqNWt9q9b4vQsXQ5TMgQiFdfsGIfIbQ1IgaYPSaT2GznakjjeamRMNDamD77+R6Dj1KoAGHUCQ07aRT/8s0uWzhADnMucgHOYCWCyAxR6Pv1RE5pUQI0OYQ68kEok+jI0cTS2fSIGLXH6ED8BqP/es88xfcY2IlC7hoeIS5gsGGeeCpC3JNjMyPTxEqeHzcrCn2RrsajZ1J+hW6GsI8ncxzWBhXGZmMgdT4PzQFkZzgAoDWGFwT4UoWTifB0PFzPD59RmmVCSVRXY2TWZ6xE70DVI21W/qfKsuAL0AkvmR6ugDQDrv6+q5N4qhpkbg3Tp7NP9IUyGANd5w6UrPvMUrWShaJoKRYu71+5nQlQVkmSZl0iMynRhUiYEu81xbi7RSZ2y9uevU0p331sexwPSjyRxxAp4zVEEFYhTAUgGs8IZLF4jo3HnC5wuT4B6AEaSUZFtZMrNJmUwM2yO9Awrok1ofPdA6KfgGp5Jo2zY+izsL/19hiMU49uyRkKN6KRfAOm9x+VpvWXmliBTNE+FIFIZXN6+SliXTqYRKDg/aQwMddmdrc0ZlTkFvOHscqTr6rMvEr4YTh8ngOhInCpO6SwAs9QLLRXReuae4dC483hA4N8AYMSltlbPNxNCQPdKXs81OaJ/RNyqdQUcqGQNNZpuxmMCePXoDlvdZSwWw1ls8f6WnbMFyEY6WiXA4yr2BAIQwGEDKNk2VSiXU0FCf3X/uTKq7+aQEGqD1oTdI22zuXKPkrosrCEOcOGiaTzx+JTqQTX88+THNZAOsb6kmYuAC45WKXyICsZgYLR2dESxfxh4nPsGtvmM/MBP9zT75f2Y6waR2wjBmjsTM598p1dXHh8QR2yWcuSq8pfxqRL97TY0x63cHnKM+5/P5976Ud+bOmuBjS55nMp4CnezK6SM2M33E4zw+fVueWmaBvPgkz7TXhiNvqucS11ohbNRXiVnYCuf6x3X2djKt941/sr8NedvMRbZm8gY52xROm5Bds/IVY9ftbHSR65eU18UsOya6uFwaLP8U3HS+f38uyfwyyK7hWu5GoKyKsCuWl/tILcO5Bpa7eM/JPyl8/tcZf/4nYj8wpm/SZ3+etNMdbUOwEXisihCLjX2nQjvJz8En9Z96Xidj2yOMj+7oe6EuPss6uVrRuaM5nZQ9NtaXAEBtLcOOK2YnnwY52+SomcpZYDzbvFzzML11W1vLsMPJC837djeq4+Li4uLi4uLi4uLi4uLi4uLi4uLi4uLi4uLi4uLi4uLi4uLi4uLi4uLi4uLi4uLi4uLi4nLV8V+7S/Iv9Jmt7wAAAABJRU5ErkJggg==" alt="Alexa Cameras" style="height:30px;display:block"></div>
    <nav class="links">
      <a href="__REPO__#readme" target="_blank" rel="noopener">&#128214; Docs</a>
      <a href="__REPO__/blob/main/docs/END-TO-END-SETUP.md" target="_blank" rel="noopener">&#128736;&#65039; Setup guide</a>
    </nav>
  </header>

  <div class="tabs">
    <button class="tab active" data-tab="overview" onclick="showTab('overview')">Overview</button>
    <button class="tab" data-tab="config" onclick="showTab('config')">Configuration</button>
    <button class="tab" data-tab="validate" onclick="showTab('validate')">Validate streams</button>
    <button class="tab" data-tab="public" onclick="showTab('public')">Public URL check</button>
    <button class="tab" data-tab="logs" onclick="showTab('logs')">Logs</button>
  </div>

  <section id="tab-overview">
    <div class="grid2">
      <div class="panel"><h2>Status</h2><div class="kv" id="status"><div class="k">Loading&hellip;</div><div></div></div></div>
      <div class="panel"><h2>Quick links</h2><div class="quick">
        <button onclick="showTab('config')">&#9881;&#65039; Edit configuration</button>
        <button onclick="showTab('logs')">&#128196; View logs</button>
        <a href="__REPO__#readme" target="_blank" rel="noopener">&#128214; Documentation (README)</a>
        <a href="__REPO__/blob/main/docs/END-TO-END-SETUP.md" target="_blank" rel="noopener">&#128736;&#65039; End-to-end setup guide</a>
      </div></div>
    </div>
    <div class="panel"><h2>Cameras</h2><div id="camsummary">Loading&hellip;</div>
      <div style="margin-top:12px"><button class="primary" onclick="showTab('validate'); validateAll()">Validate all streams &rarr;</button></div>
    </div>
    <div style="text-align:right; margin-top:8px"><span class="dbg-dot" title="Advanced diagnostics" onclick="showTab('debug')">&#129514;</span></div>
  </section>

  <section id="tab-config" hidden>
    <div class="row" style="margin-bottom:10px">
      <button class="primary" onclick="saveConfig()">Save &amp; apply</button>
      <button onclick="toggleYaml()"><span id="yamlbtn">View as YAML</span></button>
      <button onclick="discardChanges()">Discard changes</button>
      <span id="cfgmsg"></span>
    </div>
    <p class="sub">Stored in the add-on's own <code>config.yaml</code> and applied immediately (the camera streams restart). This does <b>not</b> use the Home Assistant add-on options.</p>
    <div id="cfgform">
      <div class="panel"><h2>RTSP defaults (used by cameras without a full url)</h2>
        <div class="cfg-grid">
          <label>Username<input id="f-user" type="text" placeholder="admin"></label>
          <label>Password<span class="pwwrap"><input id="f-pass" type="password" placeholder="(camera password)"><button type="button" id="pwbtn" class="pwtoggle" onclick="togglePw()">Show</button></span></label>
          <label>Port<input id="f-port" type="number" placeholder="554"></label>
          <label>Default RTSP path<input id="f-path" type="text" placeholder="/cam/realmonitor?channel=1&amp;subtype=1"></label>
        </div>
      </div>
      <div class="panel"><h2>Home Assistant IP (required)</h2>
        <p class="sub" style="margin:0 0 4px">The Home Assistant server's internal (private) IPv4 address on your LAN &mdash; four numbers, <b>not a hostname</b>.</p>
        <div class="iprow">
          <input id="f-ip1" class="octet" inputmode="numeric" maxlength="3" placeholder="192" oninput="octetInput(this,'f-ip2')">
          <span class="ipdot">.</span>
          <input id="f-ip2" class="octet" inputmode="numeric" maxlength="3" placeholder="168" oninput="octetInput(this,'f-ip3')">
          <span class="ipdot">.</span>
          <input id="f-ip3" class="octet" inputmode="numeric" maxlength="3" placeholder="1" oninput="octetInput(this,'f-ip4')">
          <span class="ipdot">.</span>
          <input id="f-ip4" class="octet" inputmode="numeric" maxlength="3" placeholder="100" oninput="octetInput(this,'')">
        </div>
      </div>
      <div class="panel"><h2>Cameras</h2>
        <div style="overflow-x:auto"><table class="cams" id="camrows"></table></div>
        <div style="margin-top:10px"><button class="btn-add" onclick="addCamRow()">+ Add camera</button></div>
        <p class="sub" style="margin-top:10px">Each camera needs a <b>name</b> (lowercase, no spaces) and either a <b>host</b> or a full <b>url</b>. <b>mode</b>: <code>copy</code> if the source is already H.264 Baseline/Main, else <code>transcode</code>. Tick <b>On-demand</b> for a source that's expected to be idle/absent when inactive (e.g. Frigate <b>birdseye</b>) &mdash; it quiets that camera's logs, skips the stall watchdog, and validates as <i>Idle</i> rather than an error.</p>
      </div>
      <div class="panel"><h2>Audio injection (optional)</h2>
        <p class="sub" style="margin:0 0 6px">Experimental &mdash; announce <i>through</i> a camera instead of a separate Alexa announcement that tears the view down. Set a camera's <b>Audio</b> (in the table above) to <code>inject</code> (replace its audio) or <code>inject_mix</code> (keep its audio + overlay), then send audio to <code>POST http://&lt;this-host&gt;:8790/say</code>. See the docs.</p>
        <div class="cfg-grid">
          <label>Control API token<span class="pwwrap"><input id="f-inject-token" type="password" placeholder="a long random secret (protects :8790)"><button type="button" id="itbtn" class="pwtoggle" onclick="toggleInjectToken()">Show</button></span></label>
          <label>Default TTS engine<select id="f-tts-engine"><option value="">(none)</option></select></label>
        </div>
        <p class="sub" style="margin:12px 0 0;padding-top:10px;border-top:1px solid var(--line)"><b>Note:</b> The control port defaults to <code>8790</code>; change it (or its host mapping) under the add-on's <b>Network</b> settings if you need to.</p>
      </div>
    </div>
    <div id="cfgyaml" hidden><textarea id="yamlbox" spellcheck="false"></textarea></div>
  </section>

  <section id="tab-logs" hidden>
    <div class="row" style="margin-bottom:10px">
      <button onclick="loadLogs()">Refresh</button>
      <label style="font-size:.85rem"><input type="checkbox" id="logauto" checked> auto-refresh</label>
      <span class="sub" style="margin:0">Live add-on output (also visible in the HA add-on log).</span>
    </div>
    <pre id="logbox">Loading&hellip;</pre>
  </section>

  <section id="tab-validate" hidden>
    <div class="row" style="margin-bottom:10px">
      <p class="sub" style="margin:0; flex:1">Checks each camera end to end: the upstream <b>RTSP source</b> (codec vs. mode) and this add-on's <b>HLS output</b> (live and Alexa-decodable). The config columns show how the add-on read each camera &mdash; <b>tip:</b> click a purple <b>inject</b>/<b>inject_mix</b> pill to fire a quick test message into that camera's audio.</p>
      <button class="primary" onclick="validateAll()">Validate all</button>
    </div>
    <div id="list">Loading&hellip;</div>
  </section>

  <section id="tab-public" hidden>
    <p class="sub">Compares each camera's <b>internal</b> LAN stream (<code>:8888</code>) with your <b>external HTTPS URL</b> (what Amazon's servers fetch, via Cloudflare / tunnel). If <b>internal is OK but external fails</b>, the problem is your tunnel or Cloudflare &mdash; not the add-on. Nothing is stored here.</p>
    <div class="publicbar">
      <input id="base" type="text" class="pub" placeholder="https://cam.example.com">
      <button class="primary" onclick="checkPublic()">Verify all cameras</button>
    </div>
    <div class="panel" style="font-size:.87rem">
      <h2>How to read the result</h2>
      <div class="kv" style="grid-template-columns:auto 1fr; gap:9px 14px; align-items:start">
        <div><span class="badge ok">403</span></div><div><b>Ideal.</b> DNS, tunnel and TLS all work, and the Cloudflare WAF lockdown is blocking non-Amazon IPs (like this add-on's). Amazon's fetchers still get through.</div>
        <div><span class="badge warn">200</span></div><div>Reachable, but the stream is <b>not locked down</b> &mdash; anyone with the URL could view it. Consider the WAF lockdown rule (see the setup guide).</div>
        <div><span class="badge error">404</span></div><div>Host reached but wrong path &mdash; check the camera name, or that the tunnel points at this add-on's port <code>8888</code>.</div>
        <div><span class="badge error">TLS</span></div><div>Certificate problem &mdash; Alexa requires a valid HTTPS certificate.</div>
        <div><span class="badge error">unreachable</span></div><div>Couldn't reach the host at all &mdash; DNS or the tunnel is down.</div>
      </div>
    </div>
    <div id="publist">Loading&hellip;</div>
  </section>

  <section id="tab-debug" hidden>
    <div class="dbg-head">
      <div>
        <h2 style="margin:0">Advanced diagnostics</h2>
        <p class="sub" style="margin:4px 0 0; max-width:72ch">Deep troubleshooting for a camera that opens <b>slowly</b> or looks <b>choppy</b> on an Echo Show. Each probe samples the live stream for several seconds, so run it only when you need it. Most setups never do &mdash; this is mainly for on-demand sources like an idle Frigate <b>birdseye</b>.</p>
      </div>
      <div class="dbg-actions"><button onclick="showTab('overview')">&larr; Back</button><button class="primary" onclick="runDeepAll()">Run all</button></div>
    </div>
    <div id="dbglist">Loading&hellip;</div>
  </section>

<script>
var CAMS = null, CFG = {cameras:[]}, LANIP = '', yamlMode = false, cfgLoaded = false, logsTimer = null;
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }
// Camera name becomes the URL path segment (/<name>/stream.m3u8) plus HLS dir + FIFO name,
// so force it to safe characters live: lowercase letters, numbers, underscore. This stops a
// user pasting/typing a space or capital that would break the stream; the server validates too.
function sanitizeName(el){ var s=el.value.toLowerCase().replace(/[^a-z0-9_]/g,''); if(s!==el.value) el.value=s; }
// A camera uses EITHER host OR url, never both. Whichever the user fills in locks the other
// (greyed + disabled) until they clear it; url wins if both somehow carry a value.
function hostUrlExclusive(el){
  var tr = el.closest ? el.closest('tr') : null; if(!tr) return;
  var h = tr.querySelector('[data-f="host"]'), u = tr.querySelector('[data-f="url"]'), p = tr.querySelector('[data-f="path"]');
  if(!h || !u) return;
  var hv = h.value.trim()!=='', uv = u.value.trim()!=='';
  if(uv){ h.disabled = true; u.disabled = false; }
  else if(hv){ u.disabled = true; h.disabled = false; }
  else { h.disabled = false; u.disabled = false; }
  [h,u].forEach(function(x){
    x.style.opacity = x.disabled ? '0.4' : '';
    x.title = x.disabled ? 'A camera uses host OR url — clear the other field to edit this one.' : '';
  });
  // Path applies only to a host-based camera; a full URL already includes its path, so lock it out.
  if(p){
    p.disabled = uv;
    p.style.opacity = uv ? '0.4' : '';
    p.title = uv ? 'A full URL already includes its path — clear the URL to set a per-camera Path.' : '';
  }
}
function val(id){ return (document.getElementById(id).value||'').trim(); }
function msg(id,h){ var e=document.getElementById(id); if(e) e.innerHTML=h; }
function isIPv4(v){ return /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(v) && v.split('.').every(function(o){ return +o<=255; }); }
function octetInput(el,nextId){
  el.value = el.value.replace(/[^0-9]/g,'').slice(0,3);
  if(el.value!=='' && +el.value>255) el.value='255';
  if(el.value.length===3 && nextId){ var nx=document.getElementById(nextId); if(nx) nx.focus(); }
}
function lanFromFields(){ var o=[val('f-ip1'),val('f-ip2'),val('f-ip3'),val('f-ip4')]; return o.some(function(x){return x!=='';}) ? o.join('.') : ''; }
function showTab(t){
  ['overview','config','logs','validate','public','debug'].forEach(function(x){ document.getElementById('tab-'+x).hidden = (x!==t); });
  document.querySelectorAll('.tab').forEach(function(b){ b.classList.toggle('active', b.dataset.tab===t); });
  if(t==='debug'){ renderDebug(); }
  if(t==='config' && !cfgLoaded){ cfgLoaded=true; loadConfig(); }
  if(t==='logs'){ loadLogs(); if(logsTimer) clearInterval(logsTimer); logsTimer=setInterval(function(){ if(document.getElementById('logauto').checked) loadLogs(); },3000); }
  else if(logsTimer){ clearInterval(logsTimer); logsTimer=null; }
}

/* ---------- Overview ---------- */
async function loadCameras(){
  try { CAMS = await (await fetch('api/cameras')).json(); } catch(e){ CAMS = []; }
  try { var cf = await (await fetch('api/config')).json(); LANIP = (cf.data && cf.data.lan_ip) || ''; } catch(e){}
  var st = document.getElementById('status');
  var srv = 'http://'+(LANIP||location.hostname)+':8888';
  st.innerHTML =
    '<div class="k">Version</div><div>__VERSION__</div>'+
    '<div class="k">Cameras</div><div>'+(CAMS?CAMS.length:'…')+'</div>'+
    '<div class="k">Served at</div><div><a href="'+srv+'" target="_blank" rel="noopener" style="color:#2563eb;text-decoration:none">'+srv+'</a> <span style="opacity:.55">&mdash; browse what is being served (new tab)</span></div>';
  var cs = document.getElementById('camsummary');
  if(!CAMS || !CAMS.length){ cs.innerHTML = '<p>No cameras yet &mdash; open <b>Configuration</b> to add some.</p>'; }
  else { cs.innerHTML = '<table class="cams"><thead><tr><th>Name</th><th>Mode</th><th>Source</th></tr></thead><tbody>'+
    CAMS.map(function(c){ return '<tr><td><b>'+esc(c.name)+'</b></td><td><span class="mode">'+esc(c.mode)+'</span></td><td class="src">'+esc(c.source)+'</td></tr>'; }).join('')+'</tbody></table>'; }
  renderValidate(); renderPublic();
}

/* ---------- Configuration ---------- */
function togglePw(){
  var i=document.getElementById('f-pass'), b=document.getElementById('pwbtn');
  if(i.type==='password'){ i.type='text'; b.textContent='Hide'; } else { i.type='password'; b.textContent='Show'; }
}
function toggleInjectToken(){
  var i=document.getElementById('f-inject-token'), b=document.getElementById('itbtn');
  if(i.type==='password'){ i.type='text'; b.textContent='Hide'; } else { i.type='password'; b.textContent='Show'; }
}
function discardChanges(){
  if(confirm('Discard unsaved changes and reload the saved configuration?')) loadConfig();
}
async function loadConfig(){
  var r; try { r = await (await fetch('api/config')).json(); } catch(e){ r = {data:{cameras:[]}}; }
  CFG = (r && r.data) || {cameras:[]}; if(!CFG.cameras) CFG.cameras=[];
  yamlMode = false;
  document.getElementById('cfgform').hidden = false;
  document.getElementById('cfgyaml').hidden = true;
  document.getElementById('yamlbtn').textContent = 'View as YAML';
  renderForm();
  msg('cfgmsg', r && r.error ? '<span class="badge warn">FILE WARNING</span> '+esc(r.error) : '');
}
function renderForm(){
  document.getElementById('f-user').value = CFG.rtsp_user || '';
  document.getElementById('f-pass').value = CFG.rtsp_password || '';
  document.getElementById('f-port').value = CFG.rtsp_port || 554;
  document.getElementById('f-path').value = CFG.default_path || '';
  var _it=document.getElementById('f-inject-token'); if(_it) _it.value = CFG.inject_token || '';
  populateTtsEngines();
  var _oc=(CFG.lan_ip||'').split('.');
  document.getElementById('f-ip1').value=_oc[0]||''; document.getElementById('f-ip2').value=_oc[1]||'';
  document.getElementById('f-ip3').value=_oc[2]||''; document.getElementById('f-ip4').value=_oc[3]||'';
  var head ='<tr><th>Name</th><th>Host</th><th>URL (override)</th><th>Path</th><th>Mode</th><th>Audio</th><th>On-demand</th><th></th></tr>';
  document.getElementById('camrows').innerHTML = head + (CFG.cameras||[]).map(camRow).join('');
  // Reflect host/url mutual exclusion on the freshly-rendered rows (loaded config).
  document.querySelectorAll('#camrows tr [data-f="host"]').forEach(hostUrlExclusive);
}
function populateTtsEngines(){
  var sel = document.getElementById('f-tts-engine'); if(!sel) return;
  var cur = CFG.tts_engine || '';
  function render(engines){
    var seen = {};
    var opts = '<option value=""'+(cur===''?' selected':'')+'>(none)</option>';
    engines.forEach(function(e){ if(!e.id || seen[e.id]) return; seen[e.id]=1;
      var label = e.id + (e.name && e.name!==e.id ? '  ('+e.name+')' : '');
      opts += '<option value="'+esc(e.id)+'"'+(e.id===cur?' selected':'')+'>'+esc(label)+'</option>'; });
    if(cur && !seen[cur]) opts += '<option value="'+esc(cur)+'" selected>'+esc(cur)+'  (not detected)</option>';
    sel.innerHTML = opts;
  }
  render([]);   // baseline (none + current) while the fetch runs
  fetch('api/tts_engines').then(function(r){return r.json();}).then(function(r){ render((r&&r.engines)||[]); }).catch(function(){});
}
function camRow(c,i){
  c = c || {};
  var av = c.audio_source || '';
  var opts = ['','inject','inject_mix']; if(av && opts.indexOf(av)<0) opts.push(av);
  var asel = '<select data-f="audio_source">'+opts.map(function(o){
    return '<option value="'+esc(o)+'"'+(av===o?' selected':'')+'>'+(o||'(none)')+'</option>'; }).join('')+'</select>';
  return '<tr>'+
    '<td><input value="'+esc(c.name)+'" data-f="name" oninput="sanitizeName(this)" placeholder="frontporch" title="URL segment: lowercase letters, numbers, and underscore only — no spaces or capitals"></td>'+
    '<td><input value="'+esc(c.host)+'" data-f="host" oninput="hostUrlExclusive(this)" placeholder="192.168.1.64"></td>'+
    '<td><input value="'+esc(c.url)+'" data-f="url" oninput="hostUrlExclusive(this)" placeholder="rtsp://…"></td>'+
    '<td><input value="'+esc(c.path)+'" data-f="path"></td>'+
    '<td><select data-f="mode"><option'+(c.mode==='copy'?' selected':'')+'>copy</option><option'+(c.mode!=='copy'?' selected':'')+'>transcode</option></select></td>'+
    '<td>'+asel+'</td>'+
    '<td style="text-align:center"><input type="checkbox" data-f="on_demand"'+(c.on_demand?' checked':'')+' title="On-demand source (e.g. Frigate birdseye): expected to be idle / 404 when inactive — quiets its logs, skips the stall watchdog, and validates as Idle instead of an error"></td>'+
    '<td><button class="btn-del" onclick="delCamRow('+i+')" title="remove camera">&#10005;</button></td></tr>';
}
function gatherForm(){
  // Start from the loaded config so unknown top-level keys (e.g. ha_base) survive a save.
  var d = {}; for(var k in CFG){ if(CFG.hasOwnProperty(k) && k!=='cameras') d[k]=CFG[k]; }
  if(val('f-user')) d.rtsp_user = val('f-user'); else delete d.rtsp_user;
  if(val('f-pass')) d.rtsp_password = val('f-pass'); else delete d.rtsp_password;
  d.rtsp_port = parseInt(val('f-port')||'554',10);
  if(val('f-path')) d.default_path = val('f-path'); else delete d.default_path;
  if(val('f-inject-token')) d.inject_token = val('f-inject-token'); else delete d.inject_token;
  if(val('f-tts-engine')) d.tts_engine = val('f-tts-engine'); else delete d.tts_engine;
  var _lan=lanFromFields(); if(_lan) d.lan_ip=_lan; else delete d.lan_ip;
  var cams = [], idx = 0;
  document.querySelectorAll('#camrows tr').forEach(function(tr){
    var inputs = tr.querySelectorAll('[data-f]'); if(!inputs.length) return;
    // Start from the existing camera object so fields the form doesn't render
    // (e.g. audio_source) are preserved instead of dropped on save.
    var base = (CFG.cameras && CFG.cameras[idx]) || {}; idx++;
    var c = {}; for(var k in base){ if(base.hasOwnProperty(k)) c[k]=base[k]; }
    inputs.forEach(function(el){
      var f=el.getAttribute('data-f');
      if(el.type==='checkbox'){ if(el.checked) c[f]=true; else delete c[f]; return; }
      var v=(el.value||'').trim();
      if(v){ c[f]=v; } else { delete c[f]; }
    });
    if(c.url && c.path) delete c.path;  // a full URL includes its path — never save a stray path with it
    if(c.name){ if(!c.mode) c.mode='transcode'; cams.push(c); }
  });
  d.cameras = cams;
  return d;
}
function addCamRow(){ CFG = gatherForm(); CFG.cameras.push({mode:'copy'}); renderForm(); }
function delCamRow(i){ CFG = gatherForm(); CFG.cameras.splice(i,1); renderForm(); }
async function toggleYaml(){
  if(!yamlMode){
    var r = await (await fetch('api/to-yaml',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({data:gatherForm()})})).json();
    document.getElementById('yamlbox').value = r.yaml||'';
    document.getElementById('cfgform').hidden = true;
    document.getElementById('cfgyaml').hidden = false;
    document.getElementById('yamlbtn').textContent = 'View as form';
    yamlMode = true;
  } else {
    var r2 = await (await fetch('api/from-yaml',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({yaml:document.getElementById('yamlbox').value})})).json();
    if(r2.error){ msg('cfgmsg','<span class="badge error">YAML ERROR</span> '+esc(r2.error)); return; }
    CFG = r2.data || {cameras:[]}; if(!CFG.cameras) CFG.cameras=[];
    renderForm();
    document.getElementById('cfgform').hidden = false;
    document.getElementById('cfgyaml').hidden = true;
    document.getElementById('yamlbtn').textContent = 'View as YAML';
    yamlMode = false; msg('cfgmsg','');
  }
}
async function saveConfig(){
  if(!yamlMode){
    var ip=lanFromFields();
    if(!ip){ msg('cfgmsg','<span class="badge error">ERROR</span> Home Assistant IP is required (fill all four fields).'); return; }
    if(!isIPv4(ip)){ msg('cfgmsg','<span class="badge error">ERROR</span> Home Assistant IP must be four numbers 0-255 (e.g. 192.168.1.100).'); return; }
  }
  var body = yamlMode ? {yaml:document.getElementById('yamlbox').value} : {data:gatherForm()};
  msg('cfgmsg','<span class="pending">saving&hellip;</span>');
  var r; try { r = await (await fetch('api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json(); }
  catch(e){ msg('cfgmsg','<span class="badge error">ERROR</span> '+esc(e)); return; }
  if(r.ok){ msg('cfgmsg','<span class="badge ok">SAVED</span> applied &mdash; streams restarting'); loadCameras(); }
  else { msg('cfgmsg','<span class="badge error">ERROR</span> '+esc(r.error||'save failed')); }
}

/* ---------- Logs ---------- */
async function loadLogs(){
  try { var r = await (await fetch('api/logs')).json(); var b=document.getElementById('logbox');
    var atBottom = b.scrollTop+b.clientHeight >= b.scrollHeight-40;
    b.textContent = r.text || '(no output yet)';
    if(atBottom) b.scrollTop = b.scrollHeight;
  } catch(e){}
}

/* ---------- Validate ---------- */
function cfgc(v,cls,title){ return '<span class="c'+(cls?' '+cls:'')+'"'+(title?' title="'+esc(title)+'"':'')+'>'+esc(v)+'</span>'; }
function renderValidate(){
  var list = document.getElementById('list');
  if(!CAMS || !CAMS.length){ list.innerHTML='<p>No cameras configured.</p>'; return; }
  // Header row: labels the right-justified config columns so a user can scan down each column
  // and instantly spot a camera the add-on read differently than expected.
  var head = '<div class="vhead"><span style="flex:1"></span><div class="cfg">'+
    '<span class="c">On-demand</span><span class="c">Mode</span><span class="c">Source</span>'+
    '<span class="c">Path</span><span class="c">Audio</span></div></div>';
  var cards = CAMS.map(function(c){
    var btn = c.on_demand
      ? '<button class="vbtn" onclick="validateCam(\''+esc(c.name)+'\', true)" title="Runs the live check for this on-demand camera, which briefly wakes the source (e.g. Frigate birdseye)">Check stream</button>'
      : '<button class="vbtn" onclick="validateCam(\''+esc(c.name)+'\')">Validate</button>';
    var cfg = '<div class="cfg">'+
      (c.on_demand ? cfgc('yes','','Connects only while watched; skipped by Validate all so it isn\'t woken') : cfgc('–','off','Always-on camera'))+
      cfgc(c.mode||'?', '', c.mode==='copy'?'copy: remux only (near-zero CPU) — source is already H.264 Baseline/Main':'transcode: re-encode to H.264 Baseline (uses CPU)')+
      cfgc(c.source_label||'—', c.source_label==='Restream'?'rst':'', c.source_why||'')+
      cfgc(c.path_label||'—', '', c.path_why||'')+
      (c.audio ?
        ((c.audio==='inject'||c.audio==='inject_mix')
          ? '<span class="c aud" title="Click to inject a test message into this camera\'s stream — then view the camera on an Echo Show to hear it" onclick="sayTest(\''+esc(c.name)+'\',this)">'+esc(c.audio)+'</span>'
          : cfgc(c.audio, '', 'audio: '+esc(c.audio)))
        : cfgc('–','off','No audio injection on this camera'))+
    '</div>';
    return '<div class="card" data-cam="'+esc(c.name)+'"'+(c.on_demand?' data-od="1"':'')+'>'+
      '<div class="row">'+btn+'<span class="name">'+esc(c.name)+'</span>'+
        '<span style="flex:1"></span>'+cfg+'</div>'+
      '<div class="src2">'+esc(c.source)+'</div><div class="results">'+
        row('Source',c.name+'-source')+row('Output',c.name+'-output')+
      '</div></div>';
  }).join('');
  list.innerHTML = head + cards;
}
function badge(s){ return '<span class="badge '+s+'">'+s.toUpperCase()+'</span>'; }
function badgeText(s,t){ return '<span class="badge '+s+'">'+esc(t)+'</span>'; }
function row(k,kind){ return '<div class="res" id="res-'+kind+'"><div class="k">'+k+'</div><div class="v" id="v-'+kind+'"><span class="pending">not checked</span></div></div>'; }
function setRes(id,r){ var v=document.getElementById('v-'+id); if(!v) return;
  v.innerHTML = badge(r.status)+(r.detail?' <span class="detail">'+esc(r.detail)+'</span>':'')+' <span>'+esc(r.msg||'')+'</span>'; }
function setPending(id){ var v=document.getElementById('v-'+id); if(v) v.innerHTML='<span class="pending">checking&hellip;</span>'; }
async function validateCam(name, force){
  showTab('validate');
  var kinds=['source','output'];
  var qs = force ? '&force=1' : '';
  for(var i=0;i<kinds.length;i++){ setPending(name+'-'+kinds[i]);
    try { setRes(name+'-'+kinds[i], await (await fetch('api/validate/'+kinds[i]+'?cam='+encodeURIComponent(name)+qs)).json()); }
    catch(e){ setRes(name+'-'+kinds[i], {status:'error', msg:String(e)}); } }
}
async function validateAll(){ var cards=document.querySelectorAll('.card[data-cam]'); for(var i=0;i<cards.length;i++){ await validateCam(cards[i].getAttribute('data-cam')); } }
async function sayTest(name, el){
  var orig=el.textContent, ot=el.title;
  el.textContent='…'; el.style.pointerEvents='none';
  try{
    var r=await (await fetch('api/say?cam='+encodeURIComponent(name), {method:'POST'})).json();
    if(r.ok){ el.textContent='sent ✓'; el.title='Test audio injected into '+name+' — view it on an Echo Show to hear it'; }
    else { el.textContent='failed'; el.title=r.error||'failed'; }
  }catch(e){ el.textContent='failed'; el.title=String(e); }
  setTimeout(function(){ el.textContent=orig; el.title=ot; el.style.pointerEvents=''; }, 2800);
}
/* ---------- Advanced diagnostics (hidden page) ---------- */
var DBG_METRICS=[
  ['rate','Real-time output rate','Content produced per wall-second on this add-on\'s HLS output. About <b>1&times;</b> is healthy; well under 1&times; means the stream is falling behind real-time (starved), so Alexa opens it slowly or it stutters.'],
  ['keyframe','Source keyframe interval','How often the <b>source</b> emits a keyframe. A player can\'t draw a picture until it receives one, so a large gap means a slow first frame. On-demand sources (e.g. an idle Frigate birdseye) are the usual offenders.'],
  ['firstframe','Time to first frame','Wall-clock time to open the served <b>output</b> and decode the first frame &mdash; i.e. <b>how long Alexa waits before the picture appears</b>. Tracks the served keyframe/segment cadence, so lowering a camera\'s keyframe interval shows up here.']
];
function renderDebug(){
  var el=document.getElementById('dbglist');
  if(!CAMS || !CAMS.length){ el.innerHTML='<p>No cameras configured.</p>'; return; }
  el.innerHTML = CAMS.map(function(c){ var n=esc(c.name); return ''+
    '<div class="dbg-cam" data-dcam="'+n+'">'+
      '<div class="dbg-cam-head"><span class="name">'+n+'</span><span class="mode">mode: '+esc(c.mode)+'</span>'+
        '<span style="flex:1"></span><button onclick="runDeep(\''+n+'\')">Run deep check</button></div>'+
      '<div class="dbg-metrics">'+ DBG_METRICS.map(function(m){ return metricCard(n,m[0],m[1],m[2]); }).join('') +'</div>'+
    '</div>'; }).join('');
}
function metricCard(name,kind,title,desc){
  var id=name+'-'+kind;
  return '<div class="dbg-metric" id="dm-'+id+'">'+
    '<div class="dbg-metric-head"><span class="dbg-metric-title">'+title+'</span><span class="dbg-badge" id="dmb-'+id+'"></span></div>'+
    '<div class="dbg-value" id="dmv-'+id+'">&mdash;</div>'+
    '<div class="dbg-desc">'+desc+'</div>'+
    '<div class="dbg-verdict" id="dmm-'+id+'"></div></div>';
}
function setDeepPending(id){
  var c=document.getElementById('dm-'+id); if(c) c.className='dbg-metric pending';
  var b=document.getElementById('dmb-'+id); if(b){ b.className='dbg-badge'; b.textContent=''; }
  var v=document.getElementById('dmv-'+id); if(v) v.innerHTML='<span class="dots">checking&hellip;</span>';
  var m=document.getElementById('dmm-'+id); if(m) m.textContent='';
}
function setDeep(id,r){
  var st=r.status||'error';
  var c=document.getElementById('dm-'+id); if(c) c.className='dbg-metric '+st;
  var b=document.getElementById('dmb-'+id); if(b){ b.className='dbg-badge '+st; b.textContent=st.toUpperCase(); }
  var v=document.getElementById('dmv-'+id); if(v) v.textContent=(r.detail||'—');
  var m=document.getElementById('dmm-'+id); if(m) m.textContent=(r.msg||'');
}
async function runDeep(name){
  var kinds=['rate','keyframe','firstframe'];
  kinds.forEach(function(k){ setDeepPending(name+'-'+k); });
  await Promise.all(kinds.map(function(k){
    return fetch('api/validate/'+k+'?cam='+encodeURIComponent(name)).then(function(r){return r.json();})
      .then(function(j){ setDeep(name+'-'+k, j); })
      .catch(function(e){ setDeep(name+'-'+k, {status:'error', detail:'', msg:String(e)}); }); }));
}
async function runDeepAll(){ if(!CAMS) return; for(var i=0;i<CAMS.length;i++){ await runDeep(CAMS[i].name); } }
/* ---------- Public URL check ---------- */
function renderPublic(){
  var el=document.getElementById('publist');
  if(!CAMS || !CAMS.length){ el.innerHTML='<p>No cameras configured.</p>'; return; }
  var ih='http://'+(LANIP||location.hostname)+':8888';
  el.innerHTML = CAMS.map(function(c){ var n=esc(c.name); return ''+
    '<div class="card"><div class="name" style="margin-bottom:10px">'+n+'</div>'+
    '<div class="pline"><div class="prow"><span class="plabel int">Internal</span>'+
      '<span id="ires-'+n+'"><span class="pending">not checked</span></span></div>'+
      '<a class="detail purl" target="_blank" rel="noopener" href="'+ih+'/'+n+'/stream.m3u8">'+ih+'/'+n+'/stream.m3u8</a>'+
      '<a class="detail purl" target="_blank" rel="noopener" href="'+ih+'/'+n+'/snapshot.jpg">'+ih+'/'+n+'/snapshot.jpg</a></div>'+
    '<div class="pline" style="margin-bottom:0"><div class="prow"><span class="plabel ext">External</span>'+
      '<span id="eres-'+n+'"><span class="pending">not checked</span></span></div>'+
      '<a class="detail purl" target="_blank" rel="noopener" id="eurl-'+n+'">https://&lt;your-domain&gt;/'+n+'/stream.m3u8</a>'+
      '<a class="detail purl" target="_blank" rel="noopener" id="esnap-'+n+'">https://&lt;your-domain&gt;/'+n+'/snapshot.jpg</a></div>'+
    '</div>'; }).join('');
}
async function checkPublic(){
  var base=(document.getElementById('base').value||'').trim().replace(/\/+$/,'');
  if(!base){ alert('Enter your external https:// URL first.'); return; }
  var EV={'403':'Reachable &amp; locked to Amazon &mdash; expected &amp; good',
          '200':'Reachable, but NOT locked down &mdash; anyone could view it',
          '404':'Wrong path &mdash; check the camera name / tunnel',
          'TLS':'Certificate problem',
          'unreachable':'Host unreachable &mdash; DNS or tunnel down'};
  for(var i=0;i<CAMS.length;i++){ var name=CAMS[i].name;
    var iv=document.getElementById('ires-'+name); if(iv) iv.innerHTML='<span class="pending">checking&hellip;</span>';
    try { var ir=await (await fetch('api/validate/internal?cam='+encodeURIComponent(name))).json();
      if(iv) iv.innerHTML=badgeText(ir.status, ir.detail||ir.status.toUpperCase())+' '+esc((ir.msg||'').replace(/\s*\(.*/,'')); }
    catch(e){ if(iv) iv.innerHTML='<span class="badge error">ERROR</span>'; }
    var eu=document.getElementById('eurl-'+name); if(eu){ eu.href=base+'/'+name+'/stream.m3u8'; eu.textContent=base+'/'+name+'/stream.m3u8'; }
    var es=document.getElementById('esnap-'+name); if(es){ es.href=base+'/'+name+'/snapshot.jpg'; es.textContent=base+'/'+name+'/snapshot.jpg'; }
    var ev=document.getElementById('eres-'+name); if(ev) ev.innerHTML='<span class="pending">checking&hellip;</span>';
    try { var er=await (await fetch('api/validate/public?base='+encodeURIComponent(base)+'&cam='+encodeURIComponent(name))).json();
      var verdict=EV[er.detail]||esc(er.msg||'');
      if(ev) ev.innerHTML=badgeText(er.status, er.detail||er.status.toUpperCase())+' '+verdict; }
    catch(e){ if(ev) ev.innerHTML='<span class="badge error">ERROR</span>'; } }
}
loadCameras();
</script>
</body></html>
""".replace("__REPO__", REPO).replace("__VERSION__", VERSION)


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", INGRESS_PORT), Handler)
    print(f"Web UI listening on :{INGRESS_PORT} (ingress)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
