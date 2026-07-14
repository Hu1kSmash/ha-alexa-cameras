#!/usr/bin/env bash
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
# Alexa Cameras (HLS) — serve RTSP cameras to Amazon Echo Show / Alexa as
# H.264 Baseline MPEG-TS HLS over HTTP, read directly from each camera.
#
# Config is self-managed in /data/config.yaml (edited from the add-on's own Web UI),
# NOT the Home Assistant add-on options — so it needs no Supervisor API access.
# On save, the UI touches /tmp/reload and this script restarts the camera workers
# without a container restart.
set -u

HLS=/tmp/hls
CONFIG=/data/config.yaml
OPTS=/data/options.json
RELOAD=/tmp/reload
LOG=/tmp/addon.log

mkdir -p "$HLS"
: > "$LOG"
# Mirror all output to a log file (for the panel's Logs tab) AND stdout (HA log).
exec > >(tee -a "$LOG") 2>&1

# Timestamped, single-line log line for add-on activity (startup, reloads, watchdog, etc.).
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# Free space on the filesystem holding the HLS segments + log. Prints "PCT AVAIL SIZE" (e.g.
# "42 1.2G 2.0G"). If it fills, ffmpeg writes fail with "No space left on device", so we watch it.
disk_stat() { df -P -h "$HLS" 2>/dev/null | awk 'NR==2 { gsub("%","",$5); print $5, $4, $2 }'; }
DISK_WARN_PCT=90   # warn in the log when the filesystem is this % full or more

VERSION="$(python3 -c 'import yaml;print(yaml.safe_load(open("/manifest.yaml")).get("version",""))' 2>/dev/null)"

# Big startup banner — a clear visual break in the log so restarts are easy to spot, and it looks neat.
banner() {
  printf '\n████████████████████████████████████████████████████████████████\n'
  cat <<'ART'
   █████╗ ██╗     ███████╗██╗  ██╗ █████╗
  ██╔══██╗██║     ██╔════╝╚██╗██╔╝██╔══██╗
  ███████║██║     █████╗   ╚███╔╝ ███████║
  ██╔══██║██║     ██╔══╝   ██╔██╗ ██╔══██║
  ██║  ██║███████╗███████╗██╔╝ ██╗██║  ██║
  ╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝
ART
  printf '        C A M E R A S  (HLS)  —  RTSP → Amazon Echo Show\n'
  printf '        v%s   started %s\n' "${VERSION:-?}" "$(date '+%Y-%m-%d %H:%M:%S %Z')"
  printf '        \302\251 2026 Tom Hirt  \302\267  github.com/Hu1kSmash/ha-alexa-cameras\n'
  printf '████████████████████████████████████████████████████████████████\n\n'
}

# One-shot diagnostics dump at startup: everything a support request would need, secrets masked.
# Safe to paste into an issue — no rtsp password, no inject token.
print_diag() {
  log "──── configuration summary (safe to share — passwords/tokens masked) ────"
  log "version=${VERSION:-?}  tz=${TZ_NAME:-UTC}  ffmpeg=$(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"
  read -r _dp _da _ds <<< "$(disk_stat)"; [ -n "$_dp" ] && printf '[diag] disk %s: %s%% used, %s free of %s\n' "$HLS" "$_dp" "$_da" "$_ds"
  python3 - <<'PY'
import re, yaml
try:
    o = yaml.safe_load(open("/data/config.yaml")) or {}
except Exception as e:
    print("[diag] config parse ERROR: %s" % e); raise SystemExit
mask = lambda u: re.sub(r'(rtsp://[^:@/]+):[^@/]*@', r'\1:***@', str(u))
flag = lambda v: "SET" if str(v or "").strip() else "unset"
print("[diag] lan_ip=%s   ports: hls=8888 inject=8790 ui=8099" % (o.get("lan_ip") or "NOT SET"))
print("[diag] rtsp defaults: user=%r port=%s path=%r password=%s" % (
    o.get("rtsp_user", ""), o.get("rtsp_port", 554), o.get("default_path", ""), flag(o.get("rtsp_password"))))
print("[diag] inject_token=%s   tts_engine=%r   ha_base=%s" % (
    flag(o.get("inject_token")), o.get("tts_engine", ""), o.get("ha_base") or "(default)"))
print("[diag] streaming defaults: buffer=%s scale=%s mode=%s fps=%s bitrate=%s" % (
    o.get("hls_list_size", 4), o.get("transcode_scale", "1280x720"), o.get("scale_mode", "fit"),
    o.get("transcode_fps", 15), o.get("transcode_bitrate") or "uncapped"))
cams = o.get("cameras") or []
print("[diag] cameras: %d" % len(cams))
for c in cams:
    src = ("url " + mask(c["url"])) if c.get("url") else ("host=" + str(c.get("host", "")))
    ov = ["%s=%s" % (lab, c[k]) for k, lab in
          (("scale", "scale"), ("scale_mode", "mode"), ("fps", "fps"),
           ("bitrate", "br"), ("hls_list_size", "buf"), ("tts_engine", "tts"))
          if str(c.get(k, "")).strip()]
    od = "YES" if str(c.get("on_demand", "")).strip().lower() in ("true", "1", "yes", "on") else "no"
    print("[diag]   %-16s %-10s %-52s audio=%-10s on_demand=%s%s" % (
        c.get("name", ""), c.get("mode", "transcode"), src, (c.get("audio_source") or "none"), od,
        ("  [" + " ".join(ov) + "]") if ov else ""))
print("[diag] " + "─" * 60)
PY
}

# Use the host's local timezone for all log timestamps. The image ships tzdata, but
# s6-overlay strips TZ from the service environment (only `docker exec` sessions get it),
# so the main process would default to UTC. s6 does persist the env as files, so read the
# zone from there and apply it for this process and every child (HTTP server, UI, injector,
# date output).
TZ_NAME="$(cat /run/s6/container_environment/TZ 2>/dev/null || true)"
if [ -n "$TZ_NAME" ] && [ -e "/usr/share/zoneinfo/$TZ_NAME" ]; then
  export TZ="$TZ_NAME"
  ln -sf "/usr/share/zoneinfo/$TZ_NAME" /etc/localtime 2>/dev/null || true
  log "[init] timezone: $TZ_NAME"
fi

# One-time migration: seed config.yaml from the HA add-on options on first run,
# so existing installs keep their cameras when upgrading to self-managed config.
if [ ! -f "$CONFIG" ]; then
  log "[init] no $CONFIG yet — seeding it from the add-on options (one-time)"
  python3 - "$OPTS" "$CONFIG" <<'PY'
import json, sys, yaml
try:
    o = json.load(open(sys.argv[1]))
except Exception:
    o = {}
cfg = {}
for k in ("rtsp_user", "rtsp_password", "rtsp_port", "default_path"):
    if o.get(k) not in (None, ""):
        cfg[k] = o[k]
cfg["cameras"] = o.get("cameras", [])
yaml.safe_dump(cfg, open(sys.argv[2], "w"), sort_keys=False, default_flow_style=False)
PY
fi

banner
print_diag

WPIDS=()   # PIDs of the current camera workers (hls_loop + snap_loop)

read_config() {
  RUSER="$(python3 -c 'import yaml;print(yaml.safe_load(open("/data/config.yaml")).get("rtsp_user",""))' 2>/dev/null)"
  RPASS="$(python3 -c 'import yaml;print(yaml.safe_load(open("/data/config.yaml")).get("rtsp_password",""))' 2>/dev/null)"
  RPORT="$(python3 -c 'import yaml;print(yaml.safe_load(open("/data/config.yaml")).get("rtsp_port",554))' 2>/dev/null)"
  # One line per camera. Streaming controls (buffer / scale / scale_mode / fps / bitrate) are resolved
  # HERE: global default (top-level keys) unless the camera sets its own override, all validated +
  # clamped, so the bash side just uses the effective values. Fields (| separated):
  #   name|host|path|mode|url|audio_source|on_demand|scale|scale_mode|fps|bitrate|buffer
  mapfile -t CAMLINES < <(python3 - <<'PY'
import yaml, re
try:
    o = yaml.safe_load(open("/data/config.yaml")) or {}
except Exception as e:
    print("[config] ERROR parsing config.yaml: %s" % e)
    raise SystemExit
d = o.get("default_path", "")

def cscale(v, dflt):
    s = str(v if v is not None else "").strip().lower().replace(" ", "")
    m = re.match(r"^(\d{2,4})x(\d{2,4})$", s)
    if not m:
        return dflt
    w, h = int(m.group(1)), int(m.group(2))
    if not (160 <= w <= 1920 and 120 <= h <= 1080):
        return dflt
    return "%dx%d" % (w - w % 2, h - h % 2)          # force even (libx264)

def cint(v, lo, hi, dflt):
    try:
        return max(lo, min(hi, int(v)))
    except Exception:
        return dflt

def cbr(v, dflt):
    s = str(v if v is not None else "").strip()
    if s == "":
        return dflt
    try:
        n = int(v)
    except Exception:
        return dflt
    return 0 if n <= 0 else max(200, min(20000, n))

def cmode(v, dflt):
    s = str(v if v is not None else "").strip().lower()
    return s if s in ("fit", "stretch") else dflt

g_scale = cscale(o.get("transcode_scale"), "1280x720")
g_mode  = cmode(o.get("scale_mode"), "fit")
g_fps   = cint(o.get("transcode_fps"), 5, 30, 15)
g_br    = cbr(o.get("transcode_bitrate"), 0)
g_buf   = cint(o.get("hls_list_size"), 2, 10, 4)

def ov(c, k):   # True if the camera sets a non-empty override for key k
    return str(c.get(k, "")).strip() != ""

for c in (o.get("cameras") or []):
    sc  = cscale(c.get("scale"), g_scale) if ov(c, "scale") else g_scale
    smd = cmode(c.get("scale_mode"), g_mode) if ov(c, "scale_mode") else g_mode
    fp  = cint(c.get("fps"), 5, 30, g_fps) if ov(c, "fps") else g_fps
    br  = cbr(c.get("bitrate"), g_br) if ov(c, "bitrate") else g_br
    bf  = cint(c.get("hls_list_size"), 2, 10, g_buf) if ov(c, "hls_list_size") else g_buf
    print("|".join([
        str(c.get("name", "")).strip(),
        str(c.get("host", "")).strip(),
        (c.get("path") or d),
        str(c.get("mode", "transcode")).strip(),
        str(c.get("url", "")).strip(),
        str(c.get("audio_source", "")).strip(),
        "1" if str(c.get("on_demand", "")).strip().lower() in ("true", "1", "yes", "on") else "",
        sc, smd, str(fp), str(br), str(bf),
    ]))
PY
)
}

# True if file $1 exists and was touched within $2 seconds. The freshness of
# /tmp/ondemand/<cam>.req (touched by hlsd.py on each client request) IS the demand signal
# for a lazy on_demand camera.
req_fresh() {
  [ -f "$1" ] || return 1
  [ "$(( $(date +%s) - $(stat -c %Y "$1" 2>/dev/null || echo 0) ))" -lt "$2" ]
}

# copy  = camera stream is already H.264 -> remux only (near-zero CPU)
# transcode = source is H.265/other -> scale to 720p H.264 Baseline for Alexa
hls_loop() {
  local cam="$1" ip="$2" path="$3" mode="$4" url="$5" audio_source="${6:-}" on_demand="${7:-}"
  local scale="${8:-1280x720}" smode="${9:-fit}" fps="${10:-15}" bitrate="${11:-0}" cbuf="${12:-4}"
  local src
  mkdir -p "$HLS/$cam"
  if [ -n "$url" ]; then
    src="$url"
  elif [ -n "$RPASS" ]; then
    src="rtsp://${RUSER}:${RPASS}@${ip}:${RPORT}${path}"
  elif [ -n "$RUSER" ]; then
    src="rtsp://${RUSER}@${ip}:${RPORT}${path}"
  else
    src="rtsp://${ip}:${RPORT}${path}"
  fi
  # Transcode video pipeline from the already-resolved effective values (read_config did the global-
  # default-vs-per-camera-override + validation). scale is a validated even WxH; smode picks whether
  # the source is fit *within* that box (aspect preserved) or stretched to it exactly. fps also drives
  # the 1-second keyframe GOP; bitrate>0 caps peak bandwidth.
  local TW="${scale%x*}" TH="${scale#*x}" VF
  if [ "$smode" = "stretch" ]; then
    VF="scale=${TW}:${TH},fps=${fps}"
  else
    VF="scale=${TW}:${TH}:force_original_aspect_ratio=decrease:force_divisible_by=2,fps=${fps}"
  fi
  local -a X264=(-c:v libx264 -profile:v baseline -level:v 3.1 -pix_fmt yuv420p -preset veryfast \
                 -tune zerolatency -g "$fps" -keyint_min "$fps" \
                 -force_key_frames "expr:gte(t,n_forced*1)" -bf 0)
  local -a BRARGS=()
  [ "${bitrate:-0}" -gt 0 ] 2>/dev/null && BRARGS=(-maxrate "${bitrate}k" -bufsize "$((bitrate*2))k")
  local -a venc
  if [ "$mode" = "copy" ]; then
    venc=(-c:v copy)
  else
    venc=(-vf "$VF" "${X264[@]}" ${BRARGS[@]+"${BRARGS[@]}"})
  fi
  # Optional synthetic audio track for sources that carry no audio of their own
  # (e.g. Frigate birdseye, a silent mosaic). Alexa only plays a camera's audio
  # if the stream HAS an audio track from the start, so this makes one:
  #   audio_source: tone   -> a quiet 440Hz test tone (proves Alexa plays it)
  #   audio_source: silent -> a silent AAC track (groundwork for injecting TTS)
  # When unset, no extra input is added and the ffmpeg command below is unchanged
  # (the source's own audio, if any, is auto-mapped as before).
  local -a ain=() amap=()
  case "$audio_source" in
    tone)
      ain=(-f lavfi -i "sine=frequency=440:sample_rate=48000")
      amap=(-map 0:v -map 1:a -af "volume=0.25") ;;
    silent)
      ain=(-f lavfi -i "anullsrc=r=48000:cl=stereo")
      amap=(-map 0:v -map 1:a) ;;
    inject)
      # REPLACE the source audio with the injector's FIFO (real-time silence with
      # announcements spliced in — injector.py). NO wallclock timestamps — those put the
      # PCM ~1.7e9 s ahead of the RTSP video's near-zero PTS and stalled the muxer; raw
      # PCM instead gets clean sample-based PTS from 0. -max_interleave_delta 0 stops the
      # HLS muxer from holding segments while it interleaves the two independent inputs.
      # Camera-agnostic; works with copy or transcode. Best for silent sources (birdseye).
      ain=(-thread_queue_size 512 -f s16le -ar 48000 -ac 2 -i "/tmp/inject/$cam.pcm")
      amap=(-map 0:v -map 1:a -max_interleave_delta 0) ;;
    inject_mix)
      # KEEP the source's own audio and MIX announcements in on top (for cameras that have
      # useful audio). Requires the source to HAVE an audio track. Uses filter_complex, so
      # for transcode the scale filter moves into it (-vf and -filter_complex can't combine).
      # normalize=0 keeps both the camera audio and the announcement at full volume.
      ain=(-thread_queue_size 512 -f s16le -ar 48000 -ac 2 -i "/tmp/inject/$cam.pcm")
      if [ "$mode" = "copy" ]; then
        amap=(-filter_complex "[0:a][1:a]amix=inputs=2:normalize=0:duration=first[aout]" \
              -map 0:v -map "[aout]" -max_interleave_delta 0)
      else
        venc=("${X264[@]}" ${BRARGS[@]+"${BRARGS[@]}"})
        amap=(-filter_complex "[0:v]${VF}[vout];[0:a][1:a]amix=inputs=2:normalize=0:duration=first[aout]" \
              -map "[vout]" -map "[aout]" -max_interleave_delta 0)
      fi ;;
  esac
  # An `on_demand` source (Frigate birdseye) is expected to be absent when idle; filter its
  # predictable "source down" noise out of the log.
  local NOISE='method DESCRIBE failed|Error opening input|Server returned 404|404 Not Found|Connection refused|Connection timed out|Immediate exit requested'
  local -a filt=(cat)
  [ "$on_demand" = "1" ] && filt=(grep -vE "$NOISE")

  # The ffmpeg invocation, built once (reads the src/venc/ain/amap/filt set above). No input
  # read-timeout on purpose — ffmpeg waits patiently for the first keyframe, which matters for a
  # slow-to-start source. Backgrounding this pipeline makes $! the LAST stage (sed), so to stop
  # the actual ffmpeg the callers pkill on the unique segment path (which the snapshot ffmpeg,
  # reading only stream.m3u8, does NOT carry).
  _run_ffmpeg() {
    ffmpeg -nostdin -loglevel error -fflags nobuffer -flags low_delay \
      -rtsp_transport tcp -i "$src" ${ain[@]+"${ain[@]}"} \
      ${amap[@]+"${amap[@]}"} "${venc[@]}" -c:a aac -ar 48000 -ac 2 -b:a 64k \
      -f hls -hls_time 1 -hls_list_size "${cbuf:-4}" \
      -hls_flags "delete_segments+omit_endlist+independent_segments" \
      -hls_segment_type mpegts -hls_allow_cache 0 \
      -hls_segment_filename "$HLS/$cam/seg_%05d.ts" "$HLS/$cam/stream.m3u8" \
      2>&1 | "${filt[@]}" | sed "s/^/[$cam] /"
  }

  if [ "$on_demand" = "1" ]; then
    # LAZY on-demand. Do NOT connect to the source while nothing is watching — that idle polling
    # is what churns a fragile upstream (Frigate birdseye's go2rtc QSV encoder) until it wedges.
    # hlsd.py (the :8888 server) touches /tmp/ondemand/<cam>.req on every client request; we run
    # ffmpeg only while that file is fresh, then reap it once the stream goes unrequested. Idle =
    # zero source connections = zero churn. If a started ffmpeg produces NO output (the ~30s
    # birdseye cold-start losing the race with go2rtc's exec-timeout), we still back off
    # (5s -> 5 min) before any retry, so even under sustained demand a failing source can't be hammered.
    local REQ="/tmp/ondemand/$cam.req" IDLE=45 delay=5 mtime parked=0 ff
    mkdir -p /tmp/ondemand
    while true; do
      if ! req_fresh "$REQ" "$IDLE"; then
        [ "$parked" = "0" ] && { log "$cam (on-demand) idle — not connecting until requested"; parked=1; }
        sleep 2; continue
      fi
      parked=0
      log "$cam (on-demand) requested — starting stream"
      _run_ffmpeg &
      ff=$!
      while kill -0 "$ff" 2>/dev/null; do
        req_fresh "$REQ" "$IDLE" || { log "$cam (on-demand) unrequested ${IDLE}s — stopping"; pkill -f "$HLS/$cam/seg_" 2>/dev/null; break; }
        sleep 3
      done
      wait "$ff" 2>/dev/null
      # Did it actually serve? (fresh playlist just before it stopped.) Then a quick reset. If it
      # produced nothing, back off — even under continued demand — so a failing source isn't hammered.
      mtime=$(stat -c %Y "$HLS/$cam/stream.m3u8" 2>/dev/null || echo 0)
      rm -f "$HLS/$cam/stream.m3u8" "$HLS/$cam/"*.ts 2>/dev/null   # clear so the next view cold-starts clean
      if [ "$(( $(date +%s) - mtime ))" -lt 10 ]; then
        delay=5
      else
        delay=$(( delay * 2 )); [ "$delay" -gt 300 ] && delay=300
        log "$cam (on-demand) produced no output — backing off ${delay}s before any retry"
      fi
      sleep "$delay"
    done
  fi

  # Normal always-on camera: persistent worker + exponential backoff (3s -> 60s). A failing camera
  # (bad credentials) must NOT be retried every 3s — some lock out an IP after repeated failed
  # logins; backoff resets to 3s after a healthy (>=30s) run.
  local delay=3 start ran
  while true; do
    start=$(date +%s)
    _run_ffmpeg
    ran=$(( $(date +%s) - start ))
    if [ "$ran" -ge 30 ]; then delay=3; else delay=$(( delay * 2 )); [ "$delay" -gt 60 ] && delay=60; fi
    log "$cam stream exited after ${ran}s; restarting in ${delay}s"
    sleep "$delay"
  done
}

snap_loop() {
  local cam="$1"
  while true; do
    [ -f "$HLS/$cam/stream.m3u8" ] && \
      ffmpeg -nostdin -loglevel error -y -i "$HLS/$cam/stream.m3u8" \
        -frames:v 1 "$HLS/$cam/snapshot.jpg" >/dev/null 2>&1
    sleep 5
  done
}

start_workers() {
  read_config
  # Clean out any stale HLS output before (re)starting workers. ffmpeg restarts its segment
  # numbering from 0, and -hls_flags delete_segments only prunes segments still in the *current*
  # playlist — so old high-numbered seg_*.ts from a previous run (e.g. after a config reload) would
  # orphan and slowly eat /tmp until a future run happened to climb back over their numbers. Wiping
  # here guarantees a clean slate each start and also removes dirs for cameras you've since deleted.
  # (Workers are already stopped when this runs on reload; on first boot the dir is empty anyway.)
  rm -rf "$HLS"/* 2>/dev/null
  local count=0 line name host path mode url audio_source on_demand scale smode fps bitrate cbuf
  for line in "${CAMLINES[@]}"; do
    [ -z "$line" ] && continue
    IFS='|' read -r name host path mode url audio_source on_demand scale smode fps bitrate cbuf <<< "$line"
    [ -z "$name" ] && continue
    if [ "$audio_source" = "inject" ] || [ "$audio_source" = "inject_mix" ]; then
      mkdir -p /tmp/inject && mkfifo -m 600 "/tmp/inject/$name.pcm" 2>/dev/null
    fi
    if [ "$on_demand" = "1" ]; then
      log "Registered on-demand camera '$name' (${url:-$host}, mode=$mode${audio_source:+, audio=$audio_source}) — connects only when watched"
    else
      log "Starting camera '$name' (${url:-$host}, mode=$mode${audio_source:+, audio=$audio_source})"
    fi
    hls_loop "$name" "$host" "$path" "$mode" "$url" "$audio_source" "$on_demand" "$scale" "$smode" "$fps" "$bitrate" "$cbuf" & WPIDS+=($!); disown
    snap_loop "$name" & WPIDS+=($!); disown   # disown so stopping workers (reload) doesn't spew "Terminated" to the log
    count=$((count + 1))
  done
  log "Serving $count camera(s): /<name>/stream.m3u8 and /<name>/snapshot.jpg on :8888"
}

stop_workers() {
  [ ${#WPIDS[@]} -gt 0 ] && kill "${WPIDS[@]}" 2>/dev/null
  pkill -x ffmpeg 2>/dev/null   # reap ffmpeg children orphaned by killing the loops
  WPIDS=()
  sleep 1
}

# Stall watchdog. An ffmpeg worker can keep running yet stop writing segments (a frozen
# mux — distinct from a *dead* camera, whose ffmpeg exits and is already retried with
# exponential backoff by hls_loop). If a camera's playlist stops advancing for STALL_SECS,
# kill ONLY that camera's ffmpeg so its own hls_loop restarts it — never the add-on and
# never the other cameras. To avoid hammering a chronically-broken source forever, it only
# auto-restarts a camera STALL_MAX_RESTARTS times; after that it gives up and logs a loud
# one-time warning for the user to investigate, and stays quiet until the camera recovers.
# Idle birdseye still advances within STALL_SECS, so a healthy-but-slow stream isn't tripped.
STALL_SECS=60
STALL_MAX_RESTARTS=3
watchdog_loop() {
  local -A seen chg fails warned
  local now cam m d ondemand dpct davail dsize last_disk_info=0 last_disk_warn=0
  while true; do
    now=$(date +%s)
    # Disk watch: /tmp holds the HLS segments + log. Log usage hourly, and warn (rate-limited)
    # whenever it crosses DISK_WARN_PCT — so a filling disk is visible BEFORE ffmpeg starts
    # failing with "No space left on device".
    read -r dpct davail dsize <<< "$(disk_stat)"
    if [ -n "$dpct" ]; then
      if [ $(( now - last_disk_info )) -ge 3600 ]; then
        log "[disk] ${dpct}% used, ${davail} free of ${dsize} (holds HLS segments + log)"
        last_disk_info=$now
      fi
      if [ "$dpct" -ge "$DISK_WARN_PCT" ] && [ $(( now - last_disk_warn )) -ge 600 ]; then
        log "[disk] WARNING: filesystem ${dpct}% full — only ${davail} free of ${dsize}. When it fills, camera ffmpeg writes FAIL with 'No space left on device'. Free up disk."
        last_disk_warn=$now
      fi
    fi
    # on-demand cameras are EXPECTED to stop producing when idle — never watchdog-restart them.
    ondemand=" $(python3 -c 'import yaml
try: cams=(yaml.safe_load(open("/data/config.yaml")) or {}).get("cameras",[]) or []
except Exception: cams=[]
print(" ".join(str(c.get("name","")).strip() for c in cams if str(c.get("on_demand","")).strip().lower() in ("true","1","yes","on")))' 2>/dev/null) "
    for d in "$HLS"/*/; do
      [ -e "$d/stream.m3u8" ] || continue
      cam=$(basename "$d")
      case "$ondemand" in *" $cam "*) continue ;; esac
      m=$(stat -c %Y "$d/stream.m3u8" 2>/dev/null) || continue
      if [ "${seen[$cam]:-x}" != "$m" ]; then
        # advancing = healthy; clear the stall counters
        seen[$cam]=$m; chg[$cam]=$now; fails[$cam]=0; warned[$cam]=0
      elif [ $(( now - ${chg[$cam]:-$now} )) -ge $STALL_SECS ]; then
        if [ "${fails[$cam]:-0}" -lt "$STALL_MAX_RESTARTS" ]; then
          fails[$cam]=$(( ${fails[$cam]:-0} + 1 ))
          log "[watchdog] $cam frozen ${STALL_SECS}s+ -> restarting its ffmpeg only (attempt ${fails[$cam]}/${STALL_MAX_RESTARTS})"
          pkill -f "$HLS/$cam/stream.m3u8" 2>/dev/null
          chg[$cam]=$now
        elif [ "${warned[$cam]:-0}" = "0" ]; then
          warned[$cam]=1
          log "[watchdog] $cam STILL frozen after ${STALL_MAX_RESTARTS} restarts -> giving up auto-recovery. Check this camera/source; it won't be restarted again until it recovers on its own."
        fi
      fi
    done
    sleep 10
  done
}

trap 'kill 0' EXIT INT TERM

start_workers
# HLS/snapshot file server (what the tunnel/reverse proxy points at) + the
# ingress UI, both in the background so this script can watch for config reloads.
python3 /hlsd.py &
python3 /ui.py &
# Birdseye audio injector (experimental): feeds inject-mode cameras' FIFOs with
# real-time silence + spliced announcements, and serves POST /say on :8790. Kept in
# its own restart loop and separate from the HLS file server above, so a hiccup here
# can't take camera serving down.
( while true; do python3 /injector.py; log "[injector] exited; restarting in 2s"; sleep 2; done ) &
# Stall watchdog: auto-restart any camera whose playlist freezes (see watchdog_loop).
watchdog_loop &
log "[init] stall watchdog started (${STALL_SECS}s threshold)"

# Reload watcher: the UI touches $RELOAD after saving config.yaml.
while true; do
  if [ -f "$RELOAD" ]; then
    rm -f "$RELOAD"
    log "[reload] configuration changed — restarting camera workers"
    stop_workers
    start_workers
    pkill -f '/injector.py' 2>/dev/null   # respawns via its loop, re-reading inject cameras
  fi
  # Keep the log file from growing without bound (tee -a re-seeks to EOF).
  [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 2000000 ] && : > "$LOG"
  sleep 2
done
