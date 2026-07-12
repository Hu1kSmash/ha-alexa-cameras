![Alexa Cameras (HLS)](https://raw.githubusercontent.com/Hu1kSmash/ha-alexa-cameras/main/docs/images/social-preview.png)

# Alexa Cameras (HLS)

This add-on reads your RTSP cameras and re-serves each one as an
**Alexa/Echo-compatible HLS stream** (H.264 Baseline, MPEG-TS segments). That's the
one stream format Amazon's camera relay can actually decode — a plain go2rtc/Home
Assistant HLS feed shows up **black** on an Echo Show because its segments drop the
in-band SPS/PPS headers. This add-on produces clean, decodable segments instead.

Each camera you configure is served on the add-on's port **8888** at:

- `http://<HA-IP>:8888/<name>/stream.m3u8` — the live HLS stream
- `http://<HA-IP>:8888/<name>/snapshot.jpg` — a still JPEG (used as the Alexa thumbnail)

> **This add-on only produces the stream.** Getting that stream to Alexa also needs
> HTTPS in front of port 8888 and a self-hosted Alexa Smart Home skill that hands the
> URL to Amazon. For *why* the stream must be H.264 Baseline MPEG-TS HLS, read the
> [project README](https://github.com/Hu1kSmash/ha-alexa-cameras#readme). For the
> **complete end-to-end setup** — Cloudflare Tunnel, the Cloudflare WAF rule that locks
> the streams to Amazon's fetchers, and the Alexa skill + AWS Lambda camera override —
> follow the step-by-step
> [End-to-End Setup guide](https://github.com/Hu1kSmash/ha-alexa-cameras/blob/main/docs/END-TO-END-SETUP.md).

---

## Where the configuration lives

**You configure this add-on in its own Web UI, not in the Home Assistant *Configuration*
(Options) tab.** Open the add-on and click **Open Web UI**, or use the **Alexa Cameras**
item in the Home Assistant sidebar, then go to the **Configuration** tab.

- Settings are stored in the add-on's own `/data/config.yaml` and applied **instantly**
  on save (the camera streams restart in place — no add-on restart needed).
- The add-on has **no settings in the Home Assistant *Configuration* (Options) tab** —
  that tab is intentionally empty; everything lives in the Web UI. (Very old versions
  seeded `config.yaml` from it once on first run; it's no longer shown.)

The Configuration tab has a **form** with a **View as YAML** toggle — use whichever you
prefer; they edit the same file.

![The Configuration tab — set the Home Assistant IP (a private IPv4, not a hostname) and add your cameras](https://raw.githubusercontent.com/Hu1kSmash/ha-alexa-cameras/main/docs/images/config-tab.png)

---

## Quick start

1. Start the add-on, then **Open Web UI** → **Configuration**.
2. Set the **Home Assistant IP** (required) — your HA server's private LAN IPv4 (the four
   boxes, e.g. `192.168.1.100`). This is used for the "Served at" and "Internal" links.
3. Fill in **RTSP defaults** (username / password / port) — the login shared by your
   cameras.
4. Add a **camera**: a `name` (lowercase, no spaces), a **host** (its IP) *or* a full
   **url**, and a **mode** (`copy` if the source is already H.264 Baseline/Main, else
   `transcode`).
5. **Save & apply.** Then use the **Validate streams** tab to confirm each camera, and
   the **Logs** tab if something's wrong.

---

## Configuration reference

The form (and the underlying `config.yaml`) has these settings:

| Setting | Required | Description |
|---|---|---|
| **Home Assistant IP** (`lan_ip`) | **yes** | Your HA server's internal **private IPv4** address (e.g. `192.168.1.100`). **Must be an IP, not a hostname.** Used to build the *Served at* (Overview) and *Internal* (Public URL check) links so they point at the real LAN address. |
| **Username** (`rtsp_user`) | no | RTSP username, applied to every camera that uses `host` (not `url`). Default `admin`. |
| **Password** (`rtsp_password`) | no | RTSP password, applied to every camera that uses `host`. See the note on special characters below. |
| **Port** (`rtsp_port`) | no | RTSP port. Standard is `554`. |
| **Default RTSP path** (`default_path`) | no | Path used for any camera that doesn't set its own. The default `/cam/realmonitor?channel=1&subtype=1` is the Amcrest/Dahua **sub-stream** (low-res second stream — ideal for Echo Show). |

Each **camera** row:

| Field | Required | Description |
|---|---|---|
| `name` | **yes** | URL segment — becomes `/<name>/stream.m3u8`. Lowercase letters/numbers/underscore only. This is *not* the name Alexa speaks (that comes from Home Assistant). |
| `host` | one of host/url | Camera IP or hostname. Combined with the RTSP defaults + `path`/`default_path` into the RTSP URL. |
| `url` | one of host/url | A **full** RTSP URL that overrides host/path/credentials/port. Use for non-standard sources — e.g. a Frigate **birdseye** feed at `rtsp://ccab4aaf-frigate:8554/birdseye`. |
| `path` | no | Per-camera RTSP path, overriding `default_path`. |
| `mode` | **yes** | `copy` or `transcode` (see below). |

> **Password characters.** `rtsp_password` is inserted into the RTSP URL, so any
> URL-reserved characters must be **percent-encoded**: `@` → `%40`, `:` → `%3A`,
> `/` → `%2F`, `?` → `%3F`, `#` → `%23`, `%` → `%25`. (A `$` is fine as-is.) A
> `401`/auth loop in the Logs usually means a mis-encoded password.

---

## `copy` vs `transcode`

The most important per-camera choice — it decides whether the add-on uses ~0% CPU or a
real chunk of a core.

- **`copy`** — the source is *already* H.264 (Baseline or Main). ffmpeg only **remuxes**
  it into MPEG-TS. Near-zero CPU. **Use this whenever you can.**
- **`transcode`** — the source is **H.265/HEVC**, H.264 **High** profile, or otherwise
  not Alexa-decodable. ffmpeg **re-encodes** it: scales to 1280×720, H.264 Baseline.
  ~0.3–0.5 of a core per camera, so only where `copy` won't work.

**Tip (Amcrest/Dahua and most NVRs):** in the camera's web UI set its **sub / second
stream** to **H.264B** (Baseline), ~720p, low bitrate, then use `mode: copy`. Reserve
`transcode` for sources you can't reconfigure — like Frigate birdseye (H.264 **High**).

---

## The YAML (what "View as YAML" shows)

```yaml
lan_ip: 192.168.1.100                                # Home Assistant server's LAN IP (required)

# RTSP login shared by cameras that use `host`
rtsp_user: admin
rtsp_password: "your-password"                       # percent-encode reserved chars
rtsp_port: 554
default_path: "/cam/realmonitor?channel=1&subtype=1" # Amcrest/Dahua SUB stream

cameras:
  - name: frontporch                                 # -> /frontporch/stream.m3u8
    host: 192.168.1.201                              # this camera's IP
    mode: copy                                        # already H.264 -> remux only, ~0% CPU
  - name: garagedoors
    host: 192.168.1.206
    path: "/cam/realmonitor?channel=1&subtype=0"     # this one only has a main stream
    mode: transcode
  - name: birdseye                                    # Frigate follow-cam (H.264 High)
    url: "rtsp://ccab4aaf-frigate:8554/birdseye"      # hostname = the standard Frigate
    mode: transcode                                   # add-on; a different variant differs
```

---

## The Web UI tabs

- **Overview** — status, a clickable **Served at** `http://<HA-IP>:8888` link (browse
  the raw served files), and a summary of your cameras.
- **Configuration** — the form / YAML editor described above.
- **Logs** — live add-on output (also shown in the HA add-on log). See below.
- **Validate streams** — per camera: **Source** (ffprobes the RTSP feed and checks its
  codec/profile against `mode`) and **Output** (confirms this add-on's `:8888` HLS is
  live and decodable H.264 Baseline).
- **Public URL check** — per camera, compares the **Internal** LAN stream (`:8888`) with
  your **External** HTTPS URL (what Amazon fetches). Both show clickable stream +
  snapshot links. A **403** on external is *good* (reachable + WAF-locked to Amazon); a
  **200** means it's *not* locked down.

---

## Logs — and telling whether Alexa is reaching the add-on

Every request to `:8888` is logged with the **client IP**. This is the quickest way to
tell where a "black Echo Show" problem is:

1. Open the **Logs** tab.
2. Say **"Alexa, show camera &lt;name&gt;"** on an Echo Show.
3. Watch for `GET /<name>/stream.m3u8` and `.ts` requests:
   - **From a `172.x` address** → that's Amazon's relay coming in via your Cloudflare
     tunnel — **the stream IS reaching the add-on.** If the Echo is still black, the
     problem is codec/decoding (see the black-screen row below), not connectivity.
   - **Only your LAN IP** (your HA host's address), and **no `172.x` hits** when you ask
     Alexa → the stream **isn't** getting to the add-on. The problem is upstream:
     Cloudflare / the WAF rule / the tunnel / the Alexa skill Lambda.

(Requests from your own browser — e.g. the *Served at* link — show your machine's LAN IP,
which is how you tell them apart from Amazon's `172.x` relay traffic.)

The Logs tab — internal validation traffic from `127.0.0.1`:

![The Logs tab — internal validation traffic](https://raw.githubusercontent.com/Hu1kSmash/ha-alexa-cameras/main/docs/images/logs-validation-streams.png)

…and Amazon's relay reaching the add-on while an Echo shows a camera — every request from
a `172.x` address (`172.30.32.1`, via the tunnel):

![The Logs tab — Amazon's relay reaching the add-on](https://raw.githubusercontent.com/Hu1kSmash/ha-alexa-cameras/main/docs/images/logs-alexa.png)

The **Public URL check** tab — a green **`403`** per camera is the ideal result:

![Public URL check](https://raw.githubusercontent.com/Hu1kSmash/ha-alexa-cameras/main/docs/images/public-url-check.png)

---

## Audio injection — announce *through* a camera (experimental)

A spoken Alexa announcement is a foreground interrupt: it tears the live camera view off
the Echo Show. This lets you instead play an announcement **through the camera's own audio
track**, which the Echo already plays — so the camera view never drops.

Enable it per camera with `audio_source` — pick it from the **Audio** column of the config
form, or set it in YAML:

| `audio_source` | Behaviour | Use for |
|---|---|---|
| *(none)* | Normal — the camera's own audio (if any) | default |
| `inject` | **Replace** the audio with the announcement feed (silence between announcements) | silent sources (Frigate **birdseye**), or any camera whose own audio you don't want |
| `inject_mix` | **Keep** the camera's own audio and **mix** announcements on top | cameras with useful audio (a doorbell, a mic'd cam). Requires the source to *have* audio |

Both work with `copy` **or** `transcode`, on any camera — and you can enable audio on as
many cameras as you like (each gets its own injection channel; `/say`'s `cam` field picks
which one). **On CPU:** `inject`/`inject_mix` re-encode only the *audio* (the video is still
copied in `copy` mode), so the overhead is small. But `inject_mix` on *every* camera does add
a little per-camera work (decode the source audio + mix + re-encode), so it's not quite the
near-zero of plain `copy` — negligible for a handful of cameras, worth knowing at scale.

```yaml
cameras:
  - name: birdseye
    url: rtsp://ccab4aaf-frigate:8554/birdseye
    mode: transcode
    audio_source: inject          # birdseye is silent -> replace
  - name: frontdoorbell
    host: 192.168.1.207
    mode: copy
    audio_source: inject_mix      # keep the doorbell's audio, announce over it

# Optional, top-level:
inject_token: "a-long-random-secret"   # require this on the control API (recommended)
tts_engine: "tts.google_en_com"        # default engine for the {"text": ...} convenience
# ha_base: "http://homeassistant:8123" # where the add-on fetches HA audio from (default is fine)
```

### The control API — `POST http://<addon-host>:8790/say`

Send it audio to play through a camera. Include the token (header `X-Inject-Token`, JSON
`token`, or `?token=`) if you set `inject_token`.

```jsonc
{"cam": "birdseye", "text": "A vehicle is approaching"}   // add-on renders TTS (tts_engine) itself
{"cam": "birdseye", "url": "http://…/clip.mp3"}           // play any audio URL you provide
{"cam": "birdseye", "test": true}                          // built-in test beep
```

### Auth (`inject_token`) and choosing a TTS

**`inject_token` guards the control API.** The injector can make any `inject`-mode camera play
*arbitrary* audio, so `/say` shouldn't be open on your LAN. Set `inject_token` to a long random
string in the add-on config; then every request must carry the **same** value — header
`X-Inject-Token: <token>`, JSON `"token": "<token>"`, or `?token=<token>` — or it gets **403
Forbidden**. Leave it empty and `/say` accepts anything (fine for a quick test, but set one). It's
a *static* secret: the same value goes in the add-on config **and** in whatever calls `/say` (so if
you change it, change it in both places — e.g. the `rest_command` payload below). The port is
LAN-only regardless; the token is the second layer so nothing on your network fires a camera by
accident.

**Using a different TTS** — two independent paths, and this is the point of the design:

- **`{text}` mode** renders speech with a **Home Assistant TTS engine** — the *same* engines
  Home Assistant's built-in **Assist** uses. You manage/add them under **Settings → Voice
  assistants** (`/config/voice-assistants/assistants`); install a TTS add-on/integration (Google
  Translate, local **Piper**, Home Assistant Cloud, ElevenLabs, a local-LLM TTS, …) and it appears
  there and as a `tts.*` entity. The `tts_engine` value is that **entity ID** — e.g.
  `tts.google_en_com`. Find the exact ID under **Developer Tools → States** (filter `tts.`), or
  just pick it from the dropdown in the add-on's config **form** (it lists your installed engines,
  so there's nothing to guess). Set the default with `tts_engine`, or override per request with an
  `engine` field: `{"cam":"birdseye","text":"…","engine":"tts.piper"}`. Switching voices is a
  one-line change — the add-on just asks HA to render with that engine.
- **`{url}` mode** is **completely TTS-agnostic** — you make the audio *however you like* (any
  engine, a local LLM writing an MP3, a pre-recorded clip, a chime) and hand `/say` a URL to fetch.
  The add-on plays whatever's there; it has no idea what produced it. Use this for anything that
  isn't an HA TTS entity. (The URL must be reachable from the add-on's container.)

### Reference automation

First add a **`rest_command`** that posts to the control API. In your Home Assistant
**`configuration.yaml`** (add the `rest_command:` block if you don't already have one), then
reload it from **Developer Tools → YAML → "REST commands"** (or restart HA):

```yaml
rest_command:
  cam_say:
    url: "http://192.168.1.100:8790/say"    # your HA / add-on host's LAN IP + the control port
    method: POST
    content_type: "application/json"
    payload: '{"cam": "{{ cam }}", "text": "{{ message }}", "token": "a-long-random-secret"}'
```

Now any automation can announce through a camera:

```yaml
automation:
  - alias: "Car approaching → speak through birdseye"
    triggers: [ … your detection trigger … ]
    actions:
      - action: rest_command.cam_say
        data:
          cam: birdseye
          message: "A vehicle is approaching the house"
```

> **Tip:** to try it by hand, call `rest_command.cam_say` from **Developer Tools → Actions**
> with `cam` + `message` while a camera is showing on an Echo.

**Notes / limits.** Audio only flows while the camera produces video, so on a source that
goes idle (e.g. birdseye at rest) an injected clip plays once activity resumes — fine for
detection announcements, which fire during activity. There's ~2-5 s of HLS latency on the
injected line. The injected audio plays at the Echo's camera-view volume (no independent
duck). The control port defaults to **8790** and is **LAN-only** — change it (or its host
mapping) under the add-on's **Network** settings if it conflicts with something, never
expose it to the internet, and protect it with `inject_token`.

## How restarts & logging work

- Each camera runs as its **own** ffmpeg process — one bad camera can't take down the
  others — and is restarted automatically with **exponential backoff** (3s → 60s, reset
  after a healthy run) so a camera failing on bad credentials isn't hammered (some lock
  out an IP after repeated failed logins).
- A **stall watchdog** covers the other failure mode: an ffmpeg that keeps *running* but
  stops producing (a frozen mux). If a camera's playlist stops advancing for ~60s it
  restarts **only that camera's** worker (never the add-on, never the other cameras), up
  to 3 times; if it still won't recover it gives up and logs a one-time warning for you to
  look into, rather than restarting forever.
- There's no input read-timeout on the pull: ffmpeg waits patiently for the first
  keyframe, which matters for on-demand sources (e.g. Frigate birdseye). Keeping such a
  stream warm — *and* running at real-time so it opens promptly on Alexa (too low and the
  idle stream time-dilates, adding seconds to *"show birdseye"*) — is the *source's* job:
  set Frigate `birdseye.idle_heartbeat_fps: 10` (see the README's birdseye bonus for why).
- ffmpeg errors are surfaced into the Logs, each line prefixed with the camera name
  (`[frontporch] ...`), so you can tell *which* camera is failing and *why*.

---

## Troubleshooting

| Symptom (in the Logs / Validate tabs) | Likely cause | Fix |
|---|---|---|
| `[cam] 401 Unauthorized` looping | Wrong password / unencoded reserved chars | Fix the password; the backoff prevents locking the camera out. |
| `[cam] Connection refused` / `timed out` | Wrong host, port, or camera offline | Verify IP/port; test the RTSP URL in VLC. |
| `[cam] 404 Not Found` | Wrong `path` for this camera/brand | Fix `path` / `default_path`; confirm in VLC or the Validate tab. |
| Echo shows **black**, snapshot OK | Source is H.265 / H.264 **High** in `copy` mode | Switch that camera to `mode: transcode` (or set its sub stream to H.264B). |
| Config won't save | **Home Assistant IP** missing or a hostname | Enter the HA server's **private IPv4** (e.g. `192.168.1.100`), not a hostname. |
| Alexa black, **no `172.x`** in Logs when asked | Stream not reaching the add-on | Look upstream: Cloudflare / WAF / tunnel / Lambda (see the setup guide). |
| Camera on Alexa **frozen** after an add-on restart/update | Alexa holds the last frame when the HLS stream restarts | Re-show it (*"Alexa, show &lt;camera&gt;"*). Any add-on restart interrupts a live view. |
| `[watchdog] <cam> … frozen → restarting` in Logs | That camera's stream stalled (frozen mux) and was auto-recovered | Usually self-heals. If it logs *"giving up after 3 restarts"*, investigate that camera/source. |
| Log timestamps are in **UTC** | Older build | Update to **≥ 1.9.0** (logs use the host's local timezone). |
| **Audio injection:** nothing heard | Camera isn't being **viewed**, or `audio_source` not set | Audio only plays while the camera is shown on an Echo; set the camera's **Audio** to `inject`/`inject_mix` (`inject_mix` needs the source to *have* audio). |
| **Audio injection:** `POST /say` → **403** | Missing / wrong token | Send `inject_token` (header `X-Inject-Token`, JSON `token`, or `?token=`). |
| **Audio injection:** `POST /say` → **400 `no inject camera '<name>'`** | That camera's **Audio** is `none` (or the name is wrong) — nothing is injected, no stream is touched | Set the camera's **Audio** to `inject`/`inject_mix` and restart the add-on. The reply's `cams` list shows which cameras *are* inject-enabled; target one of those (or omit `cam` to use the first). |
| **Audio injection:** `{text}` → **401 Unauthorized** | Add-on can't reach HA's TTS | Update to **≥ 1.9.0** (grants `homeassistant_api`) and set a valid `tts_engine`. |
| **Audio injection:** `/say` → **500 "No such file"** | The audio URL isn't reachable from the add-on's container | Prefer `{text}` (the add-on fetches internally). With `{url}`, point at something the container can reach — not an HA *external* LAN-IP URL. |

---

## Notes

- **Latency floor.** Output is tuned for low latency (1-second segments), but Amazon's
  relay does **not** support LL-HLS, so **~3 seconds** glass-to-glass is the floor.
- **Use sub streams.** An Echo Show is small; a low-res sub stream looks fine, cuts
  latency, and lets you use `copy`.
- **Port 8888 is plain HTTP.** Alexa requires HTTPS with a valid cert, so put HTTPS in
  front of 8888 (Cloudflare Tunnel, nginx, Caddy…). See the setup guide.
- **Camera names in Alexa** come from Home Assistant, not the `name` field here.
