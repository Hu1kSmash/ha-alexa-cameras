<p align="center">
  <img src="docs/images/social-preview.png" alt="Alexa Cameras (HLS)" width="820">
</p>

# Home Assistant Add-on: Alexa Cameras (HLS)

Serve any RTSP camera to **Amazon Echo Show / Alexa** as a stream Alexa will
actually play — H.264 Baseline, MPEG-TS HLS, read **directly from the camera**
(no go2rtc in the media path, no Nabu Casa). It can also **mix spoken announcements
into a camera's audio track**, so an alert plays *through* the live camera view on the
Echo instead of the usual Alexa announcement that tears the view down (see the add-on's
**Documentation** tab for setup).

> **This add-on is one piece of a larger, fully self-hosted solution.** For the
> complete build — the Alexa Smart Home skill, the AWS Lambda camera override,
> and the Cloudflare Tunnel — follow **[docs/END-TO-END-SETUP.md](docs/END-TO-END-SETUP.md)**.
> That guide is step-by-step and self-contained.

## Contents

- [The problem: black screen on the Echo Show](#the-problem-black-screen-on-the-echo-show)
- [What Alexa's camera relay actually requires](#what-alexas-camera-relay-actually-requires)
- [Why go2rtc's HLS produces a black screen](#why-go2rtcs-hls-produces-a-black-screen)
- [How this add-on fixes it](#how-this-add-on-fixes-it)
- [How the add-on works (internals)](#how-the-add-on-works-internals)
- [Where this fits — the end-to-end picture](#where-this-fits--the-end-to-end-picture)
- [Installation](#installation)
- [Configuration](#configuration)
- [Bonus tool: bulk-clean stale Alexa devices](#bonus-tool-bulk-clean-stale-alexa-devices)
- [Bonus: auto-pushing a camera (birdseye) to an Echo Show](#bonus-auto-pushing-a-camera-birdseye-to-an-echo-show)
- [License](#license)

---

## The problem: black screen on the Echo Show

You self-host a Home Assistant Alexa Smart Home skill (not Nabu Casa), you say
*"Alexa, show front porch,"* and the Echo Show says **"connecting to camera…"**
then shows a **black screen** — or **"camera isn't responding."** Frustratingly,
if you look at your logs, the Echo's relay *is* fetching your playlist and
downloading segments. The bytes flow, but nothing renders.

That happens because **Amazon's camera relay is extremely picky about the
stream**, and most "just point Alexa at go2rtc/HA" setups produce a stream that
is subtly undecodable. This add-on produces a stream that satisfies every one of
Amazon's requirements, so the Echo actually decodes and displays it.

## What Alexa's camera relay actually requires

Alexa does **not** connect the Echo Show directly to your camera. When you ask to
see a camera, Amazon's cloud fetches your stream through a relay (internally
"ACRS"). Two things about that relay matter:

- The fetcher is **old**. It identifies itself as
  `GStreamer souphttpsrc libsoup/2.48.1` and only understands **MPEG-TS HLS**.
- It comes from **AWS** — ASNs **16509** (AMAZON-02) and **14618** (AMAZON-AES).
  If a WAF/CDN challenges those requests, the relay silently fails.

Through the Alexa `CameraStreamController` (`InitializeCameraStreams`) interface,
the relay will only play a stream that is **all** of the following:

| Requirement | Detail |
|---|---|
| **Codec** | **H.264**, **Baseline or Main** profile. **No H.265/HEVC.** |
| **Container** | **MPEG-TS HLS** — a `.m3u8` playlist of `.ts` segments. **Not** fragmented-MP4 HLS, **not** LL-HLS (`#EXT-X-PART`), **not** MJPEG, **not** raw RTSP. |
| **Transport** | **HTTPS on 443** with a **valid** (publicly-trusted, non-self-signed) TLS certificate. |
| **Decodability** | Each segment must carry **in-band SPS/PPS** (the H.264 parameter sets) so a decoder can start on any segment. |
| **Reachability** | Amazon's AWS-ASN fetchers must **not** be bot-challenged/blocked. |

Miss **any single one** and you get a black screen or "not responding" — with no
useful error on the Echo.

## Why go2rtc's HLS produces a black screen

go2rtc is excellent, and its **RTSP** and **WebRTC/MSE** outputs are clean. But
its **MPEG-TS/HLS** output, in this pipeline, drops the **in-band SPS/PPS** across
an internal RTP hop. The resulting `.ts` segments reference parameter sets that
aren't in the stream, so decoders bail out:

```
non-existing PPS 0 referenced
```

The segments download perfectly and are the right codec/container — they are
simply **undecodable**, which the Echo renders as a black screen.

You can confirm this yourself: point `ffmpeg`/`ffprobe` at go2rtc's HLS output and
you'll see a flood of `non-existing PPS 0 referenced` errors; point it at go2rtc's
**RTSP** of the same camera and it decodes with **zero** errors. The bug is only
in the TS mux, and only across that hop.

## How this add-on fixes it

Instead of relying on go2rtc's HLS, this add-on runs a **dedicated single-process
`ffmpeg` pipeline per camera** that produces a proper MPEG-TS HLS stream:

- The `mpegts` muxer **re-emits SPS/PPS in-band** in every segment → decodable.
- **Clean, constant-rate timestamps** and short, forced keyframes → each segment
  is independently decodable, and startup is fast.
- For H.265 cameras, it **transcodes to H.264 Baseline** so Alexa can play it.

The output is exactly what Amazon's relay expects, so the Echo Show decodes and
displays it. Because ffmpeg reads the camera's RTSP **directly**, there is no
go2rtc muxing hop to drop parameter sets.

## How the add-on works (internals)

```
RTSP camera ──► ffmpeg (per camera) ──► /tmp/hls/<name>/*.ts + stream.m3u8
                                        └► snapshot.jpg
                          Python http.server :8888  ──►  /<name>/stream.m3u8
                                                          /<name>/snapshot.jpg
```

- **Reads each camera's RTSP directly** — no go2rtc in the media path.
- **Per-camera mode:**
  - **`copy`** — source is already H.264 (Baseline/Main): ffmpeg only **remuxes**
    into MPEG-TS. Near-zero CPU. Use this whenever you can.
  - **`transcode`** — source is H.265/HEVC (or otherwise incompatible): ffmpeg
    scales to 720p and **encodes H.264 Baseline**. ~0.3–0.5 core per camera.
- **The ffmpeg command** (per camera loop, simplified):
  ```
  ffmpeg -nostdin -loglevel error -fflags nobuffer -flags low_delay \
    -rtsp_transport tcp -i "rtsp://<user>:<pass>@<host>:554/<path>" \
    <copy: -c:v copy | transcode: -vf scale=1280:720,fps=15 -c:v libx264 \
       -profile:v baseline -level:v 3.1 -pix_fmt yuv420p -preset veryfast \
       -tune zerolatency -g 15 -keyint_min 15 -force_key_frames expr:gte(t,n_forced*1) -bf 0> \
    -c:a aac -ar 48000 -ac 2 -b:a 64k \
    -f hls -hls_time 1 -hls_list_size 4 \
    -hls_flags delete_segments+omit_endlist+independent_segments \
    -hls_segment_type mpegts -hls_allow_cache 0 \
    -hls_segment_filename /tmp/hls/<name>/seg_%05d.ts /tmp/hls/<name>/stream.m3u8
  ```
- **Serving:** a tiny Python `http.server` on **:8888** serves
  `/<name>/stream.m3u8`, the `.ts` segments, and `/<name>/snapshot.jpg`.
- **Robustness:** each camera runs in a restart loop with **exponential backoff**
  (3s → 60s) so a wrong password can't hammer a camera into an auth-lockout;
  ffmpeg's stderr is surfaced (per-camera prefixed) into the add-on **Log**.
- **Latency:** 1-second segments. Amazon's relay does **not** support LL-HLS, so
  ~3 seconds glass-to-glass is the practical floor.

See **[`alexa_cameras/DOCS.md`](alexa_cameras/DOCS.md)** for every configuration
option, `copy` vs `transcode`, and the `url` override (e.g. Frigate birdseye).

## Where this fits — the end-to-end picture

This add-on **only** produces the Alexa-compatible stream. The full path is:

```
 Camera (RTSP, H.264/H.265)
    │
    ▼
 THIS ADD-ON  ──►  H.264 Baseline MPEG-TS HLS on http://<ha-host>:8888/<name>/stream.m3u8
    │
    ▼
 Cloudflare Tunnel  ──►  https://<your-domain>/<name>/stream.m3u8   (valid TLS cert)
    │                     (+ WAF rule locking the camera host to AWS ASNs 16509/14618)
    ▼
 AWS Lambda (your self-hosted Alexa Smart Home skill)
    │   • proxies normal directives to HA's /api/alexa/smart_home
    │   • intercepts Alexa.CameraStreamController InitializeCameraStreams
    │     and returns https://<your-domain>/<name>/stream.m3u8 as the camera URI
    ▼
 Alexa cloud relay (ACRS)  ──►  Echo Show
```

The **other four pieces** (skill, Lambda + camera override, account linking,
Cloudflare Tunnel + WAF) are documented step-by-step in
**[docs/END-TO-END-SETUP.md](docs/END-TO-END-SETUP.md)**.

## Installation

[![Open your Home Assistant instance and add this add-on repository.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FHu1kSmash%2Fha-alexa-cameras)

**One click** with the button above adds this repository to your Home Assistant. Or add
it manually:

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
   and add:

   ```
   https://github.com/Hu1kSmash/ha-alexa-cameras
   ```

2. Install **Alexa Cameras (HLS)** from the store, then **Start** it.
3. Click **Open Web UI** and use the **Configuration** tab to set your **Home
   Assistant IP** and add your cameras — see
   [`alexa_cameras/DOCS.md`](alexa_cameras/DOCS.md). Configuration lives in the add-on's
   **own Web UI**, *not* the Home Assistant *Options* tab.
4. Each camera is served at:
   - `http://<host>:8888/<name>/stream.m3u8`
   - `http://<host>:8888/<name>/snapshot.jpg`
5. Continue with **[docs/END-TO-END-SETUP.md](docs/END-TO-END-SETUP.md)** to
   expose it over HTTPS and wire up the Alexa skill.

## Configuration

You configure the add-on in its **own Web UI** — click **Open Web UI**, then the
**Configuration** tab (a form with a *View as YAML* toggle). It's saved to the add-on's
`config.yaml` and applied instantly; it is **not** the Home Assistant *Options* tab.
Full reference: **[`alexa_cameras/DOCS.md`](alexa_cameras/DOCS.md)**. The YAML looks like:

```yaml
lan_ip: 192.168.1.100             # your Home Assistant server's private LAN IP (REQUIRED,
                                  # an IPv4 address — not a hostname)
rtsp_user: admin                  # the username you use to log into your cameras
rtsp_password: "your-password"    # that camera password — keep the quotes around it
rtsp_port: 554                    # the camera's RTSP port; 554 is the standard default

# The RTSP "path" your cameras use for video. The default below is for
# Amcrest / Dahua cameras. If yours are a different brand, change it —
# see "Finding your camera's RTSP path" below.
default_path: "/cam/realmonitor?channel=1&subtype=1"

cameras:
  # One block per camera. Copy/paste a block for each camera you own.
  - name: frontporch              # a short, lowercase label with NO spaces. This is the
                                  # URL segment the camera is served at (/frontporch/...).
                                  # (What you SAY to Alexa comes from Home Assistant —
                                  # see the note under this example.)
    host: 192.168.1.201            # the camera's IP address on your home network
    mode: copy                    # "copy" if the camera stream is already H.264
                                  # (most sub-streams are); "transcode" if it's H.265.

  - name: frontdriveway
    host: 192.168.1.200
    mode: transcode
```

The same thing in the form:

![The Configuration tab](docs/images/config-tab.png)

> ⚠️ **Home Assistant IP must be your HA server's private LAN IP (an IPv4 address like
> `192.168.1.100`) — *not* a hostname.** It's the most common setup mistake.

> **Camera *names* in Alexa come from Home Assistant — not this add-on** (that's
> standard HA→Alexa behavior). By default each camera appears in Alexa **exactly as
> it's named in HA**. Alexa matches loosely, so a one-word HA name like `Frontporch`
> is usually still found when you say *"front porch"* — but you can give each camera a
> friendlier spoken name in HA. See the
> [end-to-end guide](docs/END-TO-END-SETUP.md#5-home-assistant-configuration).

**`mode` — copy vs transcode:**

- **`copy`** — the camera stream is already H.264; the add-on just repackages it.
  Near-zero CPU. **Use this whenever you can.**
- **`transcode`** — the camera stream is H.265/HEVC (which Alexa can't play); the
  add-on converts it to H.264. Costs some CPU per camera.

Not sure which yours is? Try `copy` first — if the Echo Show stays black, switch that
camera to `transcode`. (Best of all: set the camera's sub-stream to **H.264** in its
own web app, then use `copy`.) The **Validate streams** tab tells you directly — here it
flags a `transcode` camera whose source is already H.264 Baseline, so it could switch to
`copy` and save CPU:

![Validate streams — source already Alexa-ready, could use copy](docs/images/validate-stream-h246.png)

### Finding your camera's RTSP path (`default_path`)

Every camera brand serves its video at a slightly different RTSP "path" — the part
after the IP address. Choosing the right one is **specific to your camera
manufacturer** (outside this add-on's control), but it's straightforward to find:

- Look up **your camera model's "RTSP URL"** in its manual, or in a community
  database like **[iSpyConnect's camera list](https://www.ispyconnect.com/cameras)**
  (searchable by brand/model).
- Common starting points — always verify against **your** model/firmware:

  | Brand | Typical **sub-stream** path | Typical **main-stream** path |
  |---|---|---|
  | Amcrest / Dahua | `/cam/realmonitor?channel=1&subtype=1` | `/cam/realmonitor?channel=1&subtype=0` |
  | Hikvision | `/Streaming/Channels/102` | `/Streaming/Channels/101` |
  | Reolink | `/h264Preview_01_sub` | `/h264Preview_01_main` |
  | Other / ONVIF | check the manufacturer or iSpyConnect | — |

- **Prefer the sub-stream** (lower resolution) for Alexa — it's plenty for a small
  Echo Show screen and, if it's H.264, needs no transcoding.
- **Test it before wiring up Alexa.** The full stream URL is
  `rtsp://<user>:<password>@<camera-ip>:554<path>`. Paste it into **VLC**
  (*Media → Open Network Stream*), or run
  `ffprobe "rtsp://user:pass@192.168.1.201:554/your/path"`. If it plays / prints codec
  info, the path is correct — and `codec_name` tells you whether to use `copy`
  (`h264`) or `transcode` (`hevc`).

A single camera can also override the shared `default_path` with its own `path:` —
see [`alexa_cameras/DOCS.md`](alexa_cameras/DOCS.md).

### The built-in Web UI

The add-on's Web UI (**Open Web UI** / the **Alexa Cameras** sidebar panel) is where you
do everything:

![Overview tab](docs/images/overview.png)

- **Configuration** — form + YAML editor (Home Assistant IP, RTSP defaults, cameras).
- **Validate streams** — per camera: **Source** (ffprobe the RTSP feed and check its
  codec/profile against your `mode` — flags an H.265/H.264-**High** source left on
  `copy`) and **Output** (this add-on's `:8888` HLS is live and decodable H.264 Baseline).
- **Public URL check** — per camera, **Internal** (`:8888` on your LAN) vs **External**
  (your public HTTPS URL). Both show clickable stream + snapshot links. A **403**
  external result is *good* (reachable + WAF-locked to Amazon); a **200** warns it isn't
  locked down.
- **Logs** — live output, and the quickest black-Echo triage: say *"Alexa, show camera
  X"* and watch it. `GET /<name>/stream.m3u8` requests from a **`172.x`** address mean
  Amazon's relay (via your tunnel) **is** reaching the add-on — so a black screen is a
  codec problem, not connectivity. **No** `172.x` hits when you ask Alexa → the stream
  isn't getting through (look at Cloudflare / WAF / tunnel / Lambda). Your own LAN IP in
  the log is just your browser.

**Validate streams** — source + output per camera (green = good); it also flags problems
in plain English, e.g. a source that won't connect:

![Validate streams — healthy cameras](docs/images/validate-streams.png)

An *on-demand* source like a **Frigate birdseye** feed validates differently: while it's
idle (nothing actively pulling it), its restream can be cold, so **Source** may show a
red timeout and **Output** an amber *"playlist isn't advancing"* warning. For an
on-demand feed **that's expected, not a misconfiguration** — it wakes up when something
requests it, and Frigate's `idle_heartbeat_fps` keeps it warm 24/7 (see the
[birdseye bonus](#bonus-auto-pushing-a-camera-birdseye-to-an-echo-show) below). On a
normal always-on camera, though, red/amber here signals a real problem worth chasing.

![Validate streams — an idle Frigate birdseye (on-demand): Source times out (red), Output warns (amber)](docs/images/validate-stream-birdseye.png)

**Public URL check** — Internal vs External per camera; a green **`403`** means
"reachable and locked to Amazon" — the ideal result:

![Public URL check tab](docs/images/public-url-check.png)

**Logs** — every `:8888` request with its client IP. Here it's the internal validation
traffic from `127.0.0.1`:

![Logs tab — internal validation traffic (127.0.0.1)](docs/images/logs-validation-streams.png)

…and here's Amazon's relay **actually reaching the add-on** while an Echo shows a camera —
every request is from a `172.x` address (`172.30.32.1`, via the tunnel). Seeing this when
you ask Alexa means connectivity is fine (a black screen would be a codec problem); *not*
seeing it means the stream isn't getting through:

![Logs tab — Amazon's relay reaching the add-on from 172.x](docs/images/logs-alexa.png)

## Bonus tool: bulk-clean stale Alexa devices

> **Not part of this add-on** — just a handy community tool you'll probably want once
> you've added a pile of cameras and need to wipe Alexa's device list and start clean.

Whenever you change which entities you expose to Alexa (or rename cameras), Alexa
tends to keep the **old devices lingering** — and duplicate/stale names collide
with voice commands and routines (e.g. *"Alexa, show front doorbell"* landing on
the wrong camera). Amazon has no built-in "delete all devices" button.

[**Shereef/Python-Delete-Alexa-Devices**](https://github.com/Shereef/Python-Delete-Alexa-Devices)
documents a browser-console method to bulk-delete **all** your Alexa smart-home
devices at once, after which you re-discover a clean set. Method write-up:
[issue #9](https://github.com/Shereef/Python-Delete-Alexa-Devices/issues/9).

> ⚠️ **Use at your own risk.** This deletes **every** smart-home device from your
> Alexa account — not just cameras. Re-discovery rebuilds whatever you currently
> expose, but any Alexa Groups/Routines referencing those devices may need to be
> rebuilt. It uses an unofficial Amazon endpoint that can change without notice.

**1. Find your region's JSON endpoint.** Sign in to Amazon, then open each of
these in a tab until one returns JSON/text containing your device list (the
others 404 or redirect):

```
https://alexa.amazon.com/api/behaviors/entities?skillId=amzn1.ask.1p.smarthome
https://pitangui.amazon.com/api/behaviors/entities?skillId=amzn1.ask.1p.smarthome
https://layla.amazon.com/api/behaviors/entities?skillId=amzn1.ask.1p.smarthome
https://alexa.amazon.de/api/behaviors/entities?skillId=amzn1.ask.1p.smarthome
https://alexa.amazon.co.jp/api/behaviors/entities?skillId=amzn1.ask.1p.smarthome
```

**2. On that same domain**, open DevTools → Console (F12). If pasting is blocked,
type `allow pasting` first, then run (lists every endpoint via GraphQL, then
DELETEs each):

```javascript
devices = await (await fetch('/nexus/v1/graphql', { method: 'POST', headers: {"Content-Type": "application/json","Accept": "application/json"}, body: JSON.stringify({query: `query { endpoints { items { friendlyName legacyAppliance { applianceId }}} } `})})).json();for (const device of devices.data.endpoints.items) console.log(await fetch(`/api/phoenix/appliance/${encodeURIComponent(device.legacyAppliance.applianceId)}`, { method: "DELETE", headers: { "Accept": "application/json", "Content-Type": "application/json"}}))
```

**3. Refresh the page**, then say **"Alexa, discover devices."**

If the fetch returns `401`, issue #9 documents a fallback that adds a CSRF token header.

## Bonus: auto-pushing a camera (birdseye) to an Echo Show

> **Out of scope for this add-on** — this needs the community **Alexa Media Player**
> integration and your own Home Assistant automation. But it's a great trick, and a
> frequent question, so here's the recipe.

**First, make birdseye playable at all.** Frigate's birdseye restream is H.264
**High** profile, and Alexa only plays H.264 **Baseline/Main** — so *"Alexa, show
birdseye"* fails by default. Fix it exactly like an H.265 camera: serve birdseye
*through this add-on* with a `url` override and `mode: transcode` (High → Baseline),
then map it in your Lambda `CAMERA_MAP` and HA `entity_config` like any other camera:

```yaml
cameras:
  - name: frontporch                               # a normal camera (already H.264)
    host: 192.168.1.201                              # the camera's IP on your home network
    mode: copy                                      # just remux, near-zero CPU
  - name: birdseye                                  # the Frigate follow-cam
    url: "rtsp://ccab4aaf-frigate:8554/birdseye"    # Frigate birdseye restream
    mode: transcode                                 # High -> Baseline for Alexa
```

> **Note:** the snippet above is the **add-on's** camera config, *not* Frigate.
>
> **On the hostname:** `ccab4aaf-frigate` is the internal Docker hostname of the
> *standard* Frigate add-on (the `ccab4aaf` slug comes from Frigate's add-on repo — it's
> the same for everyone on that add-on, and it's not sensitive). If you run a different
> Frigate variant (Beta, a proxy add-on, a custom repo), your slug — and thus the
> hostname — differs; confirm yours if birdseye won't connect.

On the **Frigate** side, enable and tune birdseye so the restream both *exists* and
*stays alive* — birdseye is on-demand, and by default it stops emitting frames when
idle, so the stream goes silent and consumers (go2rtc → this add-on → Alexa) eventually
drop it:

```yaml
# Frigate config.yml
birdseye:
  enabled: true
  restream: true  # exposes rtsp://<frigate>:8554/birdseye for the add-on to read
  mode: objects  # "follow-cam": show whichever camera currently has activity
  quality: 8  # 1 is max quality/bitrate (wasteful); 8 is plenty for an Echo Show
  # idle_heartbeat_fps is the KEY setting, and it does two jobs:
  #   1) keeps birdseye emitting when idle so the restream never goes silent
  #      (default 0 = goes cold and drops out; too low, e.g. 1, starves keyframes
  #      so consumers can't re-establish);
  #   2) sets idle RESPONSIVENESS. Frigate's birdseye producer declares a 10fps feed
  #      internally, so a lower value TIME-DILATES the idle stream (it runs slower than
  #      real-time) -- which is why "Alexa, show birdseye" can take ~8-10s to open while
  #      a normal camera is instant. Use 10 to match the producer so idle birdseye runs
  #      at real-time and opens promptly.
  idle_heartbeat_fps: 10
  layout:
    max_cameras: 1  # one camera, full-frame (a true single follow-cam)
```

`idle_heartbeat_fps` is the important one: it keeps birdseye's pipe warm 24/7 so the
add-on's puller never loses it, **and** it keeps the idle stream running at real-time so
Alexa opens it promptly — set it too low and the idle timeline crawls (slower than
real-time), adding several seconds to *"show birdseye"* while a normal camera opens
instantly. Without it, no amount of add-on-side reconnecting fully fixes the "birdseye
goes down after a while" problem — the fix belongs at the source.

That makes `camera.birdseye` a fully valid Alexa camera — it displays correctly on an
Echo Show. **But saying *"Alexa, show camera birdseye"* usually fails:** Alexa
transcribes *"birdseye"* as *"bird's eye"* (two words) and can't match a device named
`Birdseye`, so the command falls through and times out. Two ways to get a trigger that
actually works:

- **A voice Routine (nice trick).** Make an Alexa Routine whose *trigger phrase* is what
  Alexa actually hears — e.g. *"show birds eye"* — and whose *action* is a **Custom**
  command that runs the exact device phrase *"show camera birdseye"*. You say the
  natural phrase; the routine fires the exact command, bridging the transcription gap.
  (This works because the action is a custom **utterance**, not the routine's built-in
  "show camera" **device** action — that device action is the part that's unreliable
  for HLS cameras.) Alternatively, just rename the Alexa device to something it
  transcribes cleanly, like *"Overview"*.
- **An automation (hands-free / auto-show).** The most reliable trigger of all — Home
  Assistant sends the exact text command, skipping speech matching entirely. See below.

**Then, push it to a screen automatically (the reliable path).** *"Alexa, show …"* is
you asking. To make an Echo Show display a camera **on its own** — e.g. pop birdseye up
the moment Frigate detects motion, or as a manual trigger that actually works — use
the
**[Alexa Media Player](https://github.com/alandtse/alexa_media_player)** integration's
text-command feature. It sends a phrase to a specific Echo *as if you spoke it*:

```yaml
# inside a Home Assistant automation's actions:
- service: media_player.play_media
  target:
    entity_id: media_player.kitchen            # your Echo Show
  data:
    media_content_type: custom
    media_content_id: "show camera birdseye"   # exactly what you'd say out loud
```

Trigger that on a Frigate detection (e.g. `sensor.<cam>_all_active_count` above `0`,
which counts only *moving* objects) and you get hands-free "pop the camera up when
something moves." Because birdseye (`mode: objects`, `max_cameras: 1`) follows the
active camera **inside one stream**, the Echo never reconnects as activity moves
between cameras. Send `media_content_id: "stop"` the same way to dismiss it.

Caveats: this rides Alexa Media Player's **unofficial** text-command API (issues →
[that project](https://github.com/alandtse/alexa_media_player)); use the reliable
**`show camera <name>`** phrasing, since Alexa intercepts the bare word *"doorbell."*

## License

MIT — see [LICENSE](LICENSE).
