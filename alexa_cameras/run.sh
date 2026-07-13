#!/usr/bin/env bash
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

# Use the host's local timezone for all log timestamps. The image ships tzdata, but
# s6-overlay strips TZ from the service environment (only `docker exec` sessions get it),
# so the main process would default to UTC. s6 does persist the env as files, so read the
# zone from there and apply it for this process and every child (HTTP server, UI, injector,
# date output).
TZ_NAME="$(cat /run/s6/container_environment/TZ 2>/dev/null || true)"
if [ -n "$TZ_NAME" ] && [ -e "/usr/share/zoneinfo/$TZ_NAME" ]; then
  export TZ="$TZ_NAME"
  ln -sf "/usr/share/zoneinfo/$TZ_NAME" /etc/localtime 2>/dev/null || true
  echo "[init] timezone: $TZ_NAME"
fi

# One-time migration: seed config.yaml from the HA add-on options on first run,
# so existing installs keep their cameras when upgrading to self-managed config.
if [ ! -f "$CONFIG" ]; then
  echo "[init] no $CONFIG yet — seeding it from the add-on options (one-time)"
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

WPIDS=()   # PIDs of the current camera workers (hls_loop + snap_loop)

read_config() {
  RUSER="$(python3 -c 'import yaml;print(yaml.safe_load(open("/data/config.yaml")).get("rtsp_user",""))' 2>/dev/null)"
  RPASS="$(python3 -c 'import yaml;print(yaml.safe_load(open("/data/config.yaml")).get("rtsp_password",""))' 2>/dev/null)"
  RPORT="$(python3 -c 'import yaml;print(yaml.safe_load(open("/data/config.yaml")).get("rtsp_port",554))' 2>/dev/null)"
  # One line per camera: name|host|path|mode|url
  mapfile -t CAMLINES < <(python3 - <<'PY'
import yaml
try:
    o = yaml.safe_load(open("/data/config.yaml")) or {}
except Exception as e:
    print("[config] ERROR parsing config.yaml: %s" % e)
    raise SystemExit
d = o.get("default_path", "")
for c in (o.get("cameras") or []):
    print("|".join([
        str(c.get("name", "")).strip(),
        str(c.get("host", "")).strip(),
        (c.get("path") or d),
        str(c.get("mode", "transcode")).strip(),
        str(c.get("url", "")).strip(),
        str(c.get("audio_source", "")).strip(),
        "1" if str(c.get("on_demand", "")).strip().lower() in ("true", "1", "yes", "on") else "",
    ]))
PY
)
}

# copy  = camera stream is already H.264 -> remux only (near-zero CPU)
# transcode = source is H.265/other -> scale to 720p H.264 Baseline for Alexa
hls_loop() {
  local cam="$1" ip="$2" path="$3" mode="$4" url="$5" audio_source="${6:-}" on_demand="${7:-}"
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
  local -a venc
  if [ "$mode" = "copy" ]; then
    venc=(-c:v copy)
  else
    venc=(-vf "scale=1280:720,fps=15" -c:v libx264 -profile:v baseline -level:v 3.1 \
          -pix_fmt yuv420p -preset veryfast -tune zerolatency \
          -g 15 -keyint_min 15 -force_key_frames "expr:gte(t,n_forced*1)" -bf 0)
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
        venc=(-c:v libx264 -profile:v baseline -level:v 3.1 -pix_fmt yuv420p -preset veryfast \
              -tune zerolatency -g 15 -keyint_min 15 -force_key_frames "expr:gte(t,n_forced*1)" -bf 0)
        amap=(-filter_complex "[0:v]scale=1280:720,fps=15[vout];[0:a][1:a]amix=inputs=2:normalize=0:duration=first[aout]" \
              -map "[vout]" -map "[aout]" -max_interleave_delta 0)
      fi ;;
  esac
  # Exponential backoff (3s -> 60s). A failing camera (e.g. bad credentials)
  # must NOT be retried every 3s: some cameras lock out an IP after repeated
  # failed logins. Backoff resets to 3s only after a healthy (>=30s) run.
  # No input read-timeout on purpose — ffmpeg waits patiently for the first
  # keyframe, which matters for on-demand sources (e.g. Frigate birdseye).
  # An `on_demand` source (Frigate birdseye etc.) is EXPECTED to be absent/404 when idle.
  # For those: filter the predictable "source down" noise out of the log, announce the wait
  # just once, and retry on a calm fixed interval instead of spamming an error every ~30s.
  local NOISE='method DESCRIBE failed|Error opening input|Server returned 404|404 Not Found|Connection refused|Connection timed out|Immediate exit requested'
  local delay=3 start ran waiting=0
  local -a filt=(cat)
  [ "$on_demand" = "1" ] && filt=(grep -vE "$NOISE")
  while true; do
    start=$(date +%s)
    ffmpeg -nostdin -loglevel error -fflags nobuffer -flags low_delay \
      -rtsp_transport tcp -i "$src" ${ain[@]+"${ain[@]}"} \
      ${amap[@]+"${amap[@]}"} "${venc[@]}" -c:a aac -ar 48000 -ac 2 -b:a 64k \
      -f hls -hls_time 1 -hls_list_size 4 \
      -hls_flags "delete_segments+omit_endlist+independent_segments" \
      -hls_segment_type mpegts -hls_allow_cache 0 \
      -hls_segment_filename "$HLS/$cam/seg_%05d.ts" "$HLS/$cam/stream.m3u8" \
      2>&1 | "${filt[@]}" | sed "s/^/[$cam] /"
    ran=$(( $(date +%s) - start ))
    if [ "$on_demand" = "1" ]; then
      # Announce the wait exactly ONCE, then stay silent while it's idle (a source that takes
      # ~30s to time out, like birdseye, would otherwise log a line every cycle). No attempt to
      # guess "was it serving?" — when it's actually up, ffmpeg runs and doesn't reach here.
      if [ "$waiting" = "0" ]; then
        echo "[$(date +%H:%M:%S)] $cam (on-demand) source idle / not producing — waiting quietly; errors suppressed until it returns"
        waiting=1
      fi
      sleep 15
      continue
    fi
    if [ "$ran" -ge 30 ]; then
      delay=3
    else
      delay=$(( delay * 2 )); [ "$delay" -gt 60 ] && delay=60
    fi
    echo "[$(date +%H:%M:%S)] $cam stream exited after ${ran}s; restarting in ${delay}s"
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
  local count=0 line name host path mode url audio_source on_demand
  for line in "${CAMLINES[@]}"; do
    [ -z "$line" ] && continue
    IFS='|' read -r name host path mode url audio_source on_demand <<< "$line"
    [ -z "$name" ] && continue
    if [ "$audio_source" = "inject" ] || [ "$audio_source" = "inject_mix" ]; then
      mkdir -p /tmp/inject && mkfifo -m 600 "/tmp/inject/$name.pcm" 2>/dev/null
    fi
    echo "Starting camera '$name' (${url:-$host}, mode=$mode${audio_source:+, audio=$audio_source}${on_demand:+, on-demand})"
    hls_loop "$name" "$host" "$path" "$mode" "$url" "$audio_source" "$on_demand" & WPIDS+=($!)
    snap_loop "$name" & WPIDS+=($!)
    count=$((count + 1))
  done
  echo "Serving $count camera(s): /<name>/stream.m3u8 and /<name>/snapshot.jpg on :8888"
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
  local now cam m d ondemand
  while true; do
    now=$(date +%s)
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
          echo "[$(date +%H:%M:%S)] [watchdog] $cam frozen ${STALL_SECS}s+ -> restarting its ffmpeg only (attempt ${fails[$cam]}/${STALL_MAX_RESTARTS})"
          pkill -f "$HLS/$cam/stream.m3u8" 2>/dev/null
          chg[$cam]=$now
        elif [ "${warned[$cam]:-0}" = "0" ]; then
          warned[$cam]=1
          echo "[$(date +%H:%M:%S)] [watchdog] $cam STILL frozen after ${STALL_MAX_RESTARTS} restarts -> giving up auto-recovery. Check this camera/source; it won't be restarted again until it recovers on its own."
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
( cd "$HLS" && python3 -m http.server 8888 ) &
python3 /ui.py &
# Birdseye audio injector (experimental): feeds inject-mode cameras' FIFOs with
# real-time silence + spliced announcements, and serves POST /say on :8790. Kept in
# its own restart loop and separate from the HLS file server above, so a hiccup here
# can't take camera serving down.
( while true; do python3 /injector.py; echo "[injector] exited; restarting in 2s"; sleep 2; done ) &
# Stall watchdog: auto-restart any camera whose playlist freezes (see watchdog_loop).
watchdog_loop &
echo "[init] stall watchdog started (${STALL_SECS}s threshold)"

# Reload watcher: the UI touches $RELOAD after saving config.yaml.
while true; do
  if [ -f "$RELOAD" ]; then
    rm -f "$RELOAD"
    echo "[reload] configuration changed — restarting camera workers"
    stop_workers
    start_workers
    pkill -f '/injector.py' 2>/dev/null   # respawns via its loop, re-reading inject cameras
  fi
  # Keep the log file from growing without bound (tee -a re-seeks to EOF).
  [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 2000000 ] && : > "$LOG"
  sleep 2
done
