# Changelog

## 1.16.7

- Docs: added a screenshot of the Output check's latency breakdown (Detected / Buffer / Latency /
  Tune) to the Live-view latency section, replacing the hand-typed example.

## 1.16.6

- Docs: reword the Live-view latency intro to describe what the Output check reports rather than the
  UI mechanics ("adds four small rows").

## 1.16.5

- Docs: expand **HLS** to **HLS (HTTP Live Streaming)** on first use in the add-on docs, README, and
  the end-to-end guide.

## 1.16.4

- Docs: fixed the example camera `porch` to `mode: copy` in the Option A config and the Logs example
  (it was `transcode` there but `copy` in the screenshots, the Validate write-up, and the birdseye
  recipe) so every example is internally consistent.

## 1.16.3

- Docs: added a **Jittery or stuttering video** section explaining that an Echo Show pulls a
  real-time stream over Wi-Fi (little buffer), so intermittent stutter/freeze is almost always the
  Echo's Wi-Fi, not the add-on — with fixes (improve Echo Wi-Fi, *raise* HLS buffer segments, use the
  low-res sub stream) and the wired-browser tell-tale. Cross-referenced from Troubleshooting and the
  end-to-end setup guide's latency appendix.

## 1.16.2

- Docs: refreshed the Configuration screenshot (Streaming panel now at the bottom, per-camera config)
  and renamed it `config-tab.png` → `configuration.png`.

## 1.16.1

- Removed the **Validate all streams** button from the Overview tab (the Validate tab has its own
  **Validate all** button; the Overview now just shows the per-camera config at a glance).

## 1.16.0

- **Overview now shows the full per-camera config pills** — On-demand / Mode / Source / Path / Audio,
  the same read-out as the Validate tab — so you can see how the add-on interpreted every camera at a
  glance, without kicking off the live stream checks. (Validate keeps the clickable inject/inject_mix
  test pill; on Overview those pills are read-only.)

## 1.15.11

- Overview → Status now also shows the configured **TTS engine** (used for audio-injection text) and
  the **HLS buffer** depth (`hls_list_size`, default 4), so both are visible at a glance without
  opening Configuration.

## 1.15.10

- Docs: rounded out the **Live-view latency** section — corrected the row count (now four:
  Detected/Buffer/Latency/Tune), added a per-brand guide to **finding the camera's I-frame interval**
  (Amcrest/Dahua, Hikvision, Reolink, ONVIF; frames-vs-seconds; restream cameras), an "if the readout
  didn't change after editing the camera" note, and a table of exactly **what the Tune row says in
  each state**.

## 1.15.9

- Latency **Tune** row: hardened the advice so it's correct in every state — it only suggests
  shortening the camera I-frame interval when segments are actually >1s, only suggests lowering the
  buffer when it's above the 2-segment floor, and never falsely claims "segments are already ~1s"
  when they aren't (e.g. a copy source whose fps couldn't be read).

## 1.15.8

- Latency **Detected** row now bolds the **segment length** (e.g. **2s**) rather than the I-frame
  interval, so the eye follows it straight into the **Latency** row's `seg × length` math.

## 1.15.7

- Latency block: added a **Buffer** row between Detected and Latency showing the current segment count
  (`hls_list_size`) and where to change it (Configuration → Streaming), so the **Latency** row's
  `seg × length` math reads straight off the two rows above it.

## 1.15.6

- Latency **Detected** row now spells out the arithmetic — *"Source keyframe every 2s (camera I-frame
  interval 30 frames ÷ 15 fps = 2s)"* — so it's obvious where the segment length comes from.

## 1.15.5

- Latency **Tune** row: no longer suggests lowering **HLS buffer segments** when it's already at the
  floor of **2**, and fixed a stray gap that flexbox inserted before bolded phrases in the debug rows.

## 1.15.4

- **Latency readout moved into its own rows** beneath **Source** / **Output** on the Validate card, so
  it no longer crowds the Output result. Three small rows: **Detected** (source keyframe interval,
  camera I-frame interval, fps — what the add-on sees), **Latency** (⏱ the resulting lag with the
  `seg × length` math), and **Tune** (a concrete tweak, e.g. drop the camera's I-frame interval to N
  for ~1s segments, when there's headroom). Everything needed to self-tune, at a glance.

## 1.15.3

- **The latency readout now exposes the camera's detected I-frame interval** (in *frames* — the exact
  value a camera's web UI asks for), so you can trace the lag straight back to the setting and tune
  it. In `copy` mode a segment is one keyframe interval, so the readout shows e.g. *"source keyframe
  every 2.0s ≈ camera I-frame interval 30 @ 15 fps — set it to 15 for 1s segments"*, doing the
  frames = seconds × fps math for you. Transcode-mode streams are labelled as add-on-controlled
  instead (changing the camera keyframe interval wouldn't help there).
- **Docs: new "Live-view latency" section** in the add-on Documentation walking through the whole
  model — segment length (camera keyframe interval) × buffer segments (`hls_list_size`) = lag — with
  the frames math and a lever table for dialing latency down to ~2s.

## 1.15.2

- **Validate streams now shows live-view latency** on each camera's **Output** check —
  e.g. *"Alexa live view ≈ 8s behind real-time · 4 seg × 2.0s"*. It's measured from the playlist's
  own `#EXTINF` segment durations (no extra probing), so it reflects reality: in `copy` mode each
  segment is one source **keyframe interval**, and their sum is ~how far behind real-time Alexa
  starts. Makes it obvious whether to lower **HLS buffer segments** or shorten the camera's keyframe
  interval.

## 1.15.1

- Moved the **Streaming (advanced)** panel to the **bottom** of the Configuration form (after Audio
  injection), so the everyday settings come first and the rarely-touched buffer tuning is last. Docs
  reordered to match. No behavior change.

## 1.15.0

- **New `hls_list_size` setting (Configuration → Streaming) to tune live-view latency.** It controls
  how many segments Alexa buffers before playing — Alexa starts near the back of that buffer, so a
  smaller buffer means the live view sits **closer to real-time**. Default **4**; lower it to **3**
  or **2** to cut lag (a smaller buffer is less forgiving of a slow fetch, so watch for stutters).
  Range 2–10. Biggest win is still 1-second keyframes on the camera's sub stream (segments are only
  cut at source keyframes in `copy` mode).

## 1.14.2

- Validate streams: the Path pill shows **default** (not "in URL") for cameras using a full URL.

## 1.14.1

- **Fix: a per-camera Path is no longer accepted alongside a URL** (it was silently ignored, which
  was confusing). The form now **greys out the Path field when a URL is set**, and any stray `path`
  is dropped on save — including for YAML-mode edits.

## 1.14.0

- **Click an `inject` / `inject_mix` audio pill on the Validate page to fire a quick test** — the
  add-on injects a fixed spoken message straight into that camera's stream via the injector, so you
  can sanity-check audio injection in one click (view the camera on an Echo to hear it). Automations
  still use the `/say` control API directly; this is just a fast manual check.
- Fixed the Validate column **headers not lining up over the pills** (the header row was missing the
  card's horizontal padding).

## 1.13.2

- Validate streams polish: the config ovals are back to a **uniform width** (they'd shrunk to their
  text), and the action button is **smaller/lighter** — the on-demand one is now just **"Check stream"**.

## 1.13.1

- Validate streams polish: the action button now sits **before** the camera name, the config columns
  are **centered** under their headers, and the columns are reordered with **On-demand first**
  (On-demand · Mode · Source · Path · Audio).

## 1.13.0

- **Validate streams config badges are now aligned columns with a header.** The per-camera
  interpretation (**Mode · Source · Path · Audio · On-demand**) is right-justified into fixed columns
  under a header row, so scrolling the page lets you scan straight down a column and instantly spot a
  camera the add-on read differently than the rest. Added a **Path** column (**default** shared RTSP
  path / per-camera **override** / **in URL**). Hover any badge for the details.

## 1.12.0

- **Validate streams now shows how the add-on reads each camera's config**, so you can eyeball the
  whole page and confirm everything at a glance. Each card gains badges for **mode** (copy/transcode),
  **audio** (inject / inject_mix, when set), and **on-demand** — plus a **source type**:
  - **Direct** — a Host (IP) + the shared RTSP defaults (pulls straight from the camera).
  - **Restream** (green) — a URL pointing at a *local* restreamer, auto-detected when the host is your
    `lan_ip`, `localhost`, a Frigate/go2rtc/mediamtx hostname, or port **8554**. Useful confirmation
    that a camera is fanned out via go2rtc rather than pulled directly. Hover for the reason it matched.
  - **Direct URL** — a full RTSP URL that isn't a recognized local restream (used as-is).

## 1.11.1

- **Fix: stale HLS segments no longer pile up in `/tmp`.** On a config-reload worker restart, the
  new ffmpeg begins segment numbering from `0` again, and `delete_segments` only prunes segments in
  the *current* playlist — so the previous run's high-numbered `seg_*.ts` orphaned and lingered
  (a slow `/tmp` leak across restarts). Worker startup now wipes stale HLS output first, so every
  restart begins with a clean slate (this also clears leftover directories for cameras you've deleted).

## 1.11.0

- **Validate streams no longer wakes on-demand cameras.** "Validate all" (and the per-camera
  Validate) previously probed every camera's source/output — which, for an `on_demand` camera like
  Frigate **birdseye**, *requested* the stream and briefly woke it. Now on-demand cameras are
  **skipped** during a normal validation (shown as **Idle — on-demand, not queried**), so nothing
  pokes the source. Each on-demand card gets a **"Check on-demand stream"** button that runs the live
  check on purpose when you actually want to test it.
- **Clearer startup log for on-demand cameras.** Instead of `Starting camera 'birdseye' (rtsp://…)`
  — which read like a live connection — an on-demand camera now logs
  `Registered on-demand camera 'birdseye' (…) — connects only when watched`.

## 1.10.0

- **`on_demand` cameras are now truly lazy — zero source connections while nothing is watching.**
  Previously an on-demand camera still *polled* its source on a backoff (retrying every ~30s → 5
  min just to see if it was up). That polling was itself what churned a fragile upstream: each poke
  at Frigate **birdseye** restarted go2rtc's `h264_qsv` encoder, and repeated pokes wedged Frigate.
  Now the add-on connects **only while the stream is actually being requested**. The new `:8888`
  file server (`hlsd.py`) records each client request (Alexa, the auto-show automation, a browser);
  `run.sh` starts ffmpeg for an on-demand camera only while those requests are fresh and **reaps it
  ~45s after they stop**. Idle on-demand camera = no ffmpeg, no source connection, nothing to churn.
  If a requested source produces no output (e.g. birdseye losing the cold-start race), it still
  backs off (5s → 5 min) before any retry, so even under sustained demand it can't be hammered.
- Always-on cameras are unchanged (persistent worker, 3s → 60s restart backoff).

## 1.9.9

- **Fix: `on_demand` backoff now actually backs off.** 1.9.8 decided whether an attempt had *served*
  by its run duration (`>= 30s` = served → reset the retry). But a *failed* connect to a slow
  on-demand source — Frigate **birdseye**, whose `h264_qsv` restream encoder takes ~30 s to spin up
  and is then killed by go2rtc's exec-timeout — also lasts ~30 s, so failures were misread as
  successes: the backoff reset every 30 s and **kept hammering birdseye until it wedged Frigate**.
  The add-on now judges "served" by **actual output** (a playlist segment written in the last ~10 s),
  so a failed attempt correctly backs off (30 s → 5 min) while a real serve resets and holds the
  connection. Makes an on-demand source (birdseye) *safe* — it can't churn the upstream — even if it
  can't always be pulled reliably.

## 1.9.8

- **`on_demand` cameras are now genuinely hands-off** — a gentle exponential backoff (**30s → 5
  min**) instead of the old fixed 15s retry. The 15s retry was *shorter* than the ~29s cold-resume
  of an on-demand source like Frigate **birdseye**, so the add-on kept interrupting its own
  reconnects — hammering go2rtc's QSV encoder until it wedged Frigate. The add-on no longer tries
  to keep an on-demand source "warm": it announces the wait once, then backs off far and leaves the
  source alone, resetting only after it actually serves for a while. Much gentler on the upstream.

## 1.9.7

- **`on_demand` logging is now truly quiet.** An on-demand camera announces its idle wait
  **once**, then stays silent. (In 1.9.6 a source that takes ~30s to *time out* — like Frigate
  birdseye — still logged one benign "waiting to resume" line per retry cycle; now it doesn't.)

## 1.9.6

- **New: per-camera `on_demand` flag (an *On-demand* checkbox in the Cameras table).** For a
  source that's *expected* to be absent / `404` when nothing's using it — most notably a Frigate
  **birdseye** (`mode: objects`) feed, which only exists while Frigate is tracking activity — the
  add-on now treats that idle state as **normal** rather than an error. When ticked, that camera:
  - **quiets the Logs** — the repeated `method DESCRIBE failed: 404` / `Error opening input` /
    "stream exited; restarting" spam is filtered out and the wait is announced just **once**
    (genuine, non-404 errors still pass through);
  - is **excluded from the stall watchdog** (no restart loop, no "giving up" warning);
  - retries on a **calm fixed interval** instead of hammering;
  - validates as a neutral **Idle** (not red/amber) in **Validate streams**.
- **Config-form polish:** the camera **Name** field now sanitizes as you type (lowercase /
  numbers / underscore only — no spaces or capitals that would break the stream URL), and **Host**
  and **URL** are now mutually exclusive per row (filling one greys out the other).
- **Web UI tab order** now matches the docs: Overview · Configuration · Validate streams ·
  Public URL check · **Logs** (Logs moved to last).
- **Docs overhaul.** Every configuration field is now documented for a first-time user (what it is,
  where to find it, why it matters); the reference is reorganised to mirror the config screen; a
  worked **Example configuration** and a **Finding your camera's RTSP path** section live in the
  add-on docs (the README is now a lean overview that points here); and there's an honest,
  prominent write-up of **Frigate birdseye's** on-demand behaviour — it goes cold/`404` when idle,
  and *viewing it (even "Alexa, show birdseye") won't start it* — only Frigate detecting activity
  does. New **Public URL check** section and an anchor-linked Web UI tour.

## 1.9.5

- **Fix: the Cameras table no longer forces a horizontal scrollbar.** The per-column width
  rules still only covered 6 columns after the **Audio** column was added, so the widths
  landed on the wrong columns and, with a large fixed min-width, the table overflowed even
  on a wide screen. Rewrote the widths for all 7 columns (summing to 100%) and lowered the
  min-width, so the table now scales to fill the panel and only scrolls on very narrow
  (mobile) widths.

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
