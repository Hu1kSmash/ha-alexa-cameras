# Changelog

## 1.9.4

- **Config form polish:** the **Default TTS engine** dropdown now uses the same styling as
  the rest of the form (it was rendering with the raw browser default). The control-port
  note under **Audio injection** is now visually separated and prefixed **Note:** so it no
  longer reads as if it's describing the Control API token field above it.

## 1.9.3

- **Config form tidy-up:** the **Audio injection** panel now sits *below* the **Cameras**
  table (you pick a camera's Audio mode first, then configure injection), and the
  **Control API token** field is masked with a **Show/Hide** toggle like the RTSP password.
- **Docs:** documented the graceful "audio is `none`" case — calling `POST /say` for a
  camera whose **Audio** isn't `inject`/`inject_mix` returns a clear
  `400 no inject camera '<name>'` (with a `cams` list of what *is* enabled) and touches no
  stream, rather than failing silently.

## 1.9.2

- **The "Default TTS engine" field is now a dropdown** of your installed Home Assistant TTS
  engines (fetched live from HA via a new `/api/tts_engines`), instead of a free-text field you
  had to guess. Falls back to your current value / manual entry if HA can't be reached.
- **Docs:** clarified that `{text}` mode uses Home Assistant's built-in **Assist** TTS engines
  (managed under **Settings → Voice assistants**), how to find an engine's entity ID, and that
  audio can be enabled on any number of cameras; noted the small per-camera CPU cost of
  `inject_mix` (it re-encodes the audio; the video is still copied).

## 1.9.1

- **The config form now exposes the audio-injection settings** (no more hand-editing YAML):
  each camera row has an **Audio** column (`inject` / `inject_mix` / none), and a new
  **Audio injection** panel adds the `inject_token` and default `tts_engine` fields. The form
  also now **preserves any unknown top-level config keys** on save (e.g. `ha_base`).
- **The Overview → Status panel shows the add-on version.**
- **Docs:** the injection reference automation now says exactly where the `rest_command`
  goes (`configuration.yaml`) and uses placeholder IPs; the control port is noted as
  configurable via the add-on's **Network** settings; and the **Troubleshooting** section is
  expanded (injection / watchdog / version rows) and moved to the end (before Notes).

## 1.9.0

- **Audio injection — announce _through_ a camera (experimental).** A spoken Alexa
  announcement tears the Echo Show's live camera view down; this plays the announcement in
  the camera's *own* audio track instead, so the view never drops. Two per-camera modes:
  - `audio_source: inject` — **replace** the audio with the announcement feed (silence
    between announcements). For silent sources like Frigate **birdseye**.
  - `audio_source: inject_mix` — **keep** the camera's own audio and **mix** announcements
    on top (for cameras with real audio; requires the source to have an audio track).

  Both work with `copy` or `transcode`, on any camera. New control API
  `POST :8790/say` accepts `{"text": …}` (add-on renders TTS via HA using your `tts_engine`),
  `{"url": …}` (play any audio URL — fully TTS-agnostic), or `{"test": true}`. Protect it
  with a top-level `inject_token`. See DOCS for the reference automation. Adds
  `homeassistant_api` (for the `text` mode) and exposes port **8790** (LAN-only).
- **Stall watchdog.** An ffmpeg worker can keep running yet stop producing (a frozen mux).
  The add-on now detects a camera whose playlist stops advancing (~60s) and restarts **only
  that camera's** worker — never the whole add-on — up to 3 times, then gives up and warns
  once so a chronically-broken source can't cause an endless restart loop.
- **Logs now use the host's local timezone** instead of UTC (s6-overlay had stripped `TZ`
  from the service environment).

## 1.8.0

- **Experimental: `audio_source: inject` + audio injector engine.** Groundwork for playing
  an announcement *through* a camera's audio track (audio the Echo already plays) instead
  of a separate Alexa announcement that tears the live camera view down. A new `injector.py`
  runs alongside the HLS server: for each camera set to `audio_source: inject` it creates a
  FIFO, feeds it real-time silence, and splices in audio on command via `POST /say`
  (`{"url": "..."}` fetch+decode, or `{"test": true}` for a tone). `run.sh` points that
  camera's ffmpeg audio input at the FIFO (`-max_interleave_delta 0`, sample-based PTS —
  **not** wallclock, which mis-aligns the two inputs and stalls the muxer).

  **Proven end-to-end** by capturing an injected tone in a live camera's output at the exact
  expected level, and hearing it on an Echo Show. Note: audio can only flow while the camera
  produces video, so on a source that goes idle (e.g. Frigate birdseye at rest) an injected
  clip only plays once activity resumes — fine for detection announcements, which fire during
  activity. The injector is inert unless a camera opts in with `audio_source: inject`; TTS
  generation and the Home Assistant automation wiring are not included yet.

## 1.7.1

- **Fix: the Configuration form no longer drops unknown camera fields.** The structured
  editor rebuilt each camera from only the rendered inputs (name/host/url/path/mode), so a
  field it doesn't show — like the new `audio_source` — was silently deleted on the next
  save/add/delete. It now starts from the existing camera object and overlays the form
  fields, preserving anything extra. (Edit `audio_source` itself via the **YAML** toggle;
  the form preserves it but doesn't render a control for it.)

## 1.7.0

- **Experimental: per-camera `audio_source`.** Adds a synthetic audio track to a camera
  whose source carries none. Alexa only plays a camera's audio if the stream has an audio
  track *from the start*, so a silent source (notably **Frigate birdseye**, a video-only
  mosaic) can never carry sound. Two values:
  - `audio_source: tone` — a quiet 440 Hz test tone, to confirm an Echo Show actually plays
    the stream's audio.
  - `audio_source: silent` — a silent AAC track (same negotiated audio, no sound).

  This is **groundwork** for playing announcements *through* the birdseye stream (audio the
  Echo already plays) instead of a separate Alexa announcement that interrupts the camera
  view. When `audio_source` is unset, the ffmpeg command is byte-for-byte unchanged and
  every other camera is unaffected. Add it under the birdseye camera in the Web UI config:
  `audio_source: tone`.

## 1.6.4

- Deep check: **"Time to first frame" now measures the add-on's OUTPUT** (what Alexa
  actually opens over HTTP) instead of a raw source RTSP connect. The old source probe was
  dominated by RTSP negotiation overhead and didn't reflect changes like lowering a camera's
  keyframe interval; the output measure does, and it's a truer "how long Alexa waits."
  The **Source keyframe interval** row still covers the upstream diagnostic, so no signal is
  lost — just less redundancy.

## 1.6.3

- Deep check: a **404 on the output** in the real-time rate probe now reads as a clear
  verdict ("output isn't being served — the transcode has stalled/restarted") instead of a
  generic HTTP error, so a deep-idle birdseye report reads as one coherent diagnosis across
  all three rows.

## 1.6.2

Deep check polish:

- A **probe timeout is now a verdict, not a vague error.** If the keyframe or first-frame
  probe times out, that *itself* means the source is severely starved or stalled (a Frigate
  birdseye at deep idle is the classic case) — the result now says exactly that and points at
  the fix, instead of just "timed out."
- Clarified the **Time to first frame** wording: it's measured against the *source*, and
  transcoded streams re-keyframe on their own output, so Alexa's real open can be a touch
  quicker than the source number.

## 1.6.1

Refines the 1.6.0 Deep check:

- **Moved Deep check off the Validate Streams page** into its own hidden **Advanced
  diagnostics** page, reached via a small unlabeled icon on the Overview page — it's
  power-user troubleshooting, not something most setups need. Redesigned the output into
  clear per-metric cards (title · big value · status badge · plain-English "what it
  measures" · verdict) instead of the cramped inline rows.
- **Probe fixes:** the time-to-first-frame check dropped an ffmpeg input option that this
  build rejected on RTSP sources (`Option not found`); widened the real-time factor and
  keyframe-spacing thresholds (and the sampling window) so a normal camera reads OK
  instead of a false WARN.

## 1.6.0

**Validate streams — new "Deep check" (per camera).** Three opt-in diagnostics that
pinpoint *slow-to-open / choppy* streams without digging into ffmpeg/go2rtc by hand:

- **Real-time** — content produced per wall-second on the add-on's HLS output. Well under
  1× means the stream is starved / time-dilated (e.g. an idle Frigate birdseye running
  below real-time) and Alexa will open slowly or stutter.
- **Keyframes** — max keyframe spacing on the source; a large gap = a slow first frame.
  The hint points at raising `idle_heartbeat_fps` for a birdseye.
- **First frame** — wall time for a fresh connect to decode one frame (the literal "how
  long Alexa waits before the picture appears").

Each samples a live stream for a few seconds, so they're opt-in via a per-camera **Deep
check** button — separate from the fast "Validate all". New endpoints
`GET /api/validate/{rate|keyframe|firstframe}?cam=`. No config or `run.sh` change.

## 1.5.1

Docs only — no add-on code change:

- Corrected the recommended Frigate **birdseye** `idle_heartbeat_fps` from `5` to **`10`**.
  Besides keeping the restream alive, this setting controls idle *responsiveness*: Frigate's
  birdseye producer feeds at 10 fps internally, so a lower value time-dilates the idle
  stream (it runs slower than real-time), which makes *"Alexa, show birdseye"* take ~8-10 s
  to open while a normal camera is instant. `10` keeps idle birdseye at real-time so it
  opens promptly. Updated `README.md` and `DOCS.md`.

## 1.5.0

First stable tagged release. Serves RTSP cameras to **Amazon Echo Show / Alexa** as
H.264 Baseline MPEG-TS HLS, read **directly from each camera** (no go2rtc in the media
path, no Nabu Casa, no MonocleCam).

Highlights:

- Per-camera **`copy`** (remux, near-zero CPU) or **`transcode`** (H.265 / H.264-High
  → H.264 Baseline 720p).
- **Self-healing** — each camera is its own ffmpeg worker with exponential backoff, so
  one bad camera can't take down the others.
- **Built-in Web UI** — edit config, validate each stream's codec, check the public
  URL, and read logs, all from the add-on's own dashboard.
- **Self-managed** `/data/config.yaml` (not the Home Assistant Options tab); changes
  apply instantly.
- Full **end-to-end docs** — Cloudflare Tunnel + WAF, the Alexa Smart Home skill, and
  the AWS Lambda camera override.
