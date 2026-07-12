![Alexa Cameras (HLS)](https://raw.githubusercontent.com/Hu1kSmash/ha-alexa-cameras/main/docs/images/social-preview.png)

# Alexa Cameras (HLS)

This add-on reads your RTSP cameras and re-serves each one as an
**Alexa/Echo-compatible HLS stream** (H.264 Baseline, MPEG-TS segments). That's the
one stream format Amazon's camera relay can actually decode — a plain go2rtc/Home
Assistant HLS feed shows up **black** on an Echo Show because its segments drop the
in-band SPS/PPS headers. This add-on produces clean, decodable segments instead.

It can also **mix spoken announcements into a camera's audio track** — so an "*a car is
approaching*" message plays *through* the live camera view on the Echo instead of the
usual Alexa announcement that tears the view down. See the **Audio injection** section below.

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

## The add-on Web UI

Everything is done from the add-on's own dashboard — click **Open Web UI**, or use the **Alexa
Cameras** item in the Home Assistant sidebar. Its tabs:

- **Overview** — status, a clickable **Served at** `http://<HA-IP>:8888` link (browse the raw
  served files), and a summary of your cameras.
- **[Configuration](#configuration-overview)** — the form / YAML editor (covered next).
- **[Validate streams](#validate-streams)** — per-camera **Source** + **Output** codec checks.
- **[Public URL check](#public-url-check)** — compares the **Internal** LAN stream (`:8888`) with
  your **External** HTTPS URL; a **403** on the external side is the good result.
- **[Logs](#logs)** — live add-on output, and the quickest black-Echo triage.

![Overview tab](https://raw.githubusercontent.com/Hu1kSmash/ha-alexa-cameras/main/docs/images/overview.png)

---

## Configuration Overview

**You configure this add-on in its own Web UI, not in the Home Assistant *Configuration*
(Options) tab.** Open the add-on and click **Open Web UI**, or use the **Alexa Cameras**
item in the Home Assistant sidebar, then go to the **Configuration** tab.

- Settings are stored in the add-on's own `/data/config.yaml` and applied **instantly**
  on save (the camera streams restart in place — no add-on restart needed).
- The add-on has **no settings in the Home Assistant *Configuration* (Options) tab** —
  that tab is intentionally empty; everything lives in the Web UI.

The Configuration tab has a **form** with a **View as YAML** toggle — use whichever you
prefer; they edit the same file.

![The Configuration tab — set the Home Assistant IP (a private IPv4, not a hostname) and add your cameras. Each camera row has an Audio column (none / inject / inject_mix), and the Audio injection panel below the table holds the control-API token and default TTS engine](https://raw.githubusercontent.com/Hu1kSmash/ha-alexa-cameras/main/docs/images/config-tab.png)

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
5. *(Optional)* To announce **through** a camera instead of a separate Alexa announcement
   that tears the view down, set that camera's **Audio** to `inject` or `inject_mix` and
   fill in the **Audio injection** panel (a control-API token and default TTS engine). See
   the **Audio injection** section below.
6. **Save & apply.** Then use the **Validate streams** tab to confirm each camera, and
   the **Logs** tab if something's wrong.

---

## Configuration reference

### RTSP defaults

**RTSP** is the standard way IP cameras stream live video. To pull a camera's feed, the add-on
builds a link that looks like `rtsp://username:password@camera-ip:554/path`. These settings fill
in the parts that are usually the **same for every camera** — the login, the port, and the
stream path — so you enter them once here instead of on each camera. (A camera you set up with a
full `url` ignores these and uses whatever is in that URL.)

| Field | Required | Description |
|---|---|---|
| **Username** (`rtsp_user`) | No | The username you log in to your cameras with — the same account you'd type into the camera's app, its web page, or a viewer like VLC to see the video. IP cameras almost always require a login before they'll hand over their video stream. This one username is applied to every camera you add by IP/hostname; a camera set up with a full `url` uses whatever is in that URL instead. Many cameras ship with `admin` as the default — set this to the account you actually use. |
| **Password** (`rtsp_password`) | No | The password that goes with the username above — the login for your camera's video stream. It gets inserted into the RTSP link, so any characters that have a special meaning in a URL must be **percent-encoded**: `@`→`%40`, `:`→`%3A`, `/`→`%2F`, `?`→`%3F`, `#`→`%23`, `%`→`%25` (a `$` is fine as-is). If the **Logs** show a repeating `401 Unauthorized`, the camera rejected the login — almost always a wrong password or a special character that wasn't encoded. |
| **Port** (`rtsp_port`) | No | The network port your cameras use for RTSP. This is almost always **`554`** (the industry standard), so if you've never deliberately changed it, leave it at 554. Only change it if your camera or NVR documentation lists a different RTSP port. |
| **Default RTSP path** (`default_path`) | No | The last part of the RTSP link — everything after the camera's IP and port — that tells the camera **which** video feed to send. Most cameras offer two: a high-resolution **main** stream and a lower-resolution **sub** stream, and the exact path text differs by brand. This value is used for every camera that doesn't set its own **Path**, so if all your cameras are the same brand you set it once here. The shipped default `/cam/realmonitor?channel=1&subtype=1` is the Amcrest/Dahua **sub-stream** — low-res, which is perfect for the small Echo Show screen and usually already H.264 (so `copy` works). Don't know yours? See the **Finding your camera's RTSP path** section below — you can discover it with VLC or the camera's web page. |

### Home Assistant IP

| Field | Required | Description |
|---|---|---|
| **Home Assistant IP** (`lan_ip`) | **Yes** | The local-network address of the machine running Home Assistant — the device this add-on is installed on (e.g. `192.168.1.100`). You can find it in Home Assistant under **Settings → System → Network**, or in your router's list of connected devices. Enter it as four numbers in the boxes. Must be an **IP, not a hostname**, and it's **required** for a real reason: it's the exact address your **Cloudflare tunnel must point at**. The add-on publishes port **8888 on the HA host**, so the tunnel's route (or `additional_hosts` service) has to target `http://<this-IP>:8888` (see the [setup guide](https://github.com/Hu1kSmash/ha-alexa-cameras/blob/main/docs/END-TO-END-SETUP.md)).<br><br>Entering it also gives you two ways to check your work. On the **Overview** tab, the **Served at** link shows `http://<lan_ip>:8888` — the exact URL to copy into your tunnel's config. On the **Public URL check** tab, that same internal address is compared against your external HTTPS URL, so if the internal stream works but the external one doesn't, the problem is in the tunnel/WAF, not the add-on. Point the tunnel anywhere else — the `homeassistant` hostname (HA Core on `:8123`) or Frigate's go2rtc (`:1984`) — and the camera won't serve: a black screen. **`lan_ip` and the tunnel target must be the same host.** |

### Cameras

Each camera is one row in the **Cameras** table: a `name`, plus **either** a `host` **or** a
full `url`.

| Field | Required | Description |
|---|---|---|
| **Name** (`name`) | **Yes** | A short nickname for this camera, used **only** inside the stream's web address — the camera is served at `/<name>/stream.m3u8`. Keep it to lowercase letters / numbers / underscore with **no spaces**. It is deliberately **not** the name you speak to Alexa. Three separate identifiers are in play when you say *"Alexa, show Front Porch"*:<br>• **What Alexa speaks / you say** (e.g. *Front Porch*) — the camera **entity's friendly name in Home Assistant** (exposed via `alexa: smart_home`); spaces/capitals fine.<br>• **The routing key** (e.g. `frontporch`) — the HA **entity_id** suffix, which becomes the Alexa *endpointId*.<br>• **This add-on's `name`** (e.g. `frontporch`) — the URL segment.<br><br>The last two are bridged by the **`CAMERA_MAP`** in your Alexa Lambda (endpointId suffix → add-on name; see the [setup guide](https://github.com/Hu1kSmash/ha-alexa-cameras/blob/main/docs/END-TO-END-SETUP.md)). They're conventionally identical, which is why they're easy to conflate — but a mismatch (add-on serves `/front_porch/` while the endpointId is `frontporch`, with no map entry) is a classic **black screen**: Alexa resolves the spoken name fine, then fetches a URL that 404s. Pick a nice **friendly name in Home Assistant** for what Alexa says, and keep this `name` matched to your Lambda map. |
| **Host** (`host`) | One of host/url | The camera's own address on your local network — its **IP address** (e.g. `192.168.1.64`) or hostname. This is the *camera itself*, not Home Assistant. The add-on combines it with the Username, Password, Port, and Path above to build the full RTSP link it pulls video from. Find a camera's IP in your router's device list or the camera's app. Give a camera **either** a Host **or** a full URL — not both. |
| **URL** (`url`) | One of host/url | The **complete** RTSP link to a camera's stream, all in one field — e.g. `rtsp://admin:pass@192.168.1.64:554/stream`. Use this instead of **Host** when a camera doesn't fit the shared Username/Password/Port/Path pattern above, or for a non-standard source like a Frigate **birdseye** feed (`rtsp://ccab4aaf-frigate:8554/birdseye`). Whatever you enter is used **exactly as typed** and overrides all the RTSP defaults. If you fill this in, leave **Host** blank. |
| **Path** (`path`) | No | Overrides the **Default RTSP path** for this **one** camera only. Use it when most of your cameras share a path but one is different — for example, a camera that only exposes its high-res **main** stream (`/cam/realmonitor?channel=1&subtype=0`). Leave it blank to use the Default RTSP path above. Ignored when the camera uses a full **URL**. |
| **Mode** (`mode`) | **Yes** | How the add-on prepares this camera's video for Alexa — the single most important per-camera choice, since it decides whether the add-on uses ~0% CPU or a real chunk of a core:<br>• **`copy`** — the source is *already* H.264 (Baseline/Main), so ffmpeg only **remuxes** it into MPEG-TS. Near-zero CPU. **Use this whenever you can.**<br>• **`transcode`** — the source is **H.265/HEVC**, H.264 **High** profile, or otherwise not Alexa-decodable, so ffmpeg **re-encodes** it (scales to 1280×720, H.264 Baseline). ~0.3–0.5 of a core per camera — use only where `copy` won't work.<br>**Rule of thumb:** try **`copy`** first; if the Echo shows a **black screen** but the snapshot works, the source needs **`transcode`**.<br>**Tip (Amcrest/Dahua & most NVRs):** set the camera's **sub / second stream** to **H.264B** (Baseline), ~720p, low bitrate, then use `copy`. Reserve `transcode` for sources you can't reconfigure — like Frigate birdseye (H.264 **High**). |
| **Audio** (`audio_source`) | No | Optional. Lets an Alexa announcement play **through this camera's sound** instead of interrupting the live view. Leave it blank for normal behaviour (the camera's own audio, if it has any).<br>• **`inject`** — replace the camera's audio with the announcement (good for silent cameras like Frigate birdseye).<br>• **`inject_mix`** — keep the camera's own audio and mix the announcement on top (needs a camera that actually has audio).<br>Only relevant if you use the **Audio injection** feature (below). |
| **On-demand** (`on_demand`) | No | Tick this for a source that's **expected to be absent / `404` when nothing's using it** — most notably a Frigate **birdseye** (`mode: objects`) feed, which only exists while Frigate is tracking activity. When set, the add-on treats that camera's idle state as *normal* instead of an error: it **quiets the repeated `404` / connection errors** in the Logs (announcing the wait just once), **excludes the camera from the stall watchdog** (no restart-loop or "giving up" warning), retries on a **calm fixed interval**, and shows it as **Idle** (not red/amber) in Validate streams. Leave it off for normal always-on cameras, where those errors *are* real problems. See the **Frigate birdseye** notes under *Validate streams*. |

### Audio injection

Optional — for announcing *through* a camera (pair with a camera's **Audio** set to
`inject`/`inject_mix` above). Full walkthrough in the **Audio injection** section below.

| Field | Required | Description |
|---|---|---|
| **Control API token** (`inject_token`) | No (recommended) | A password **you make up** to protect the audio-announcement API (the `POST :8790/say` endpoint that plays sound through a camera). Anything on your network that can reach that endpoint could otherwise play audio through your cameras, so set a long random string here and send the **same** value on every call (HTTP header `X-Inject-Token`, JSON field `token`, or `?token=` in the URL) — a wrong or missing value gets a **403**. This is unrelated to your camera or Home Assistant passwords; you invent it. Leave it blank only for a quick local test. |
| **Default TTS engine** (`tts_engine`) | No | Which **text-to-speech voice** Home Assistant uses when you send a `{"text": "…"}` announcement and let the add-on speak it. It's the entity ID of a TTS engine you've set up in Home Assistant under **Settings → Voice assistants** — for example `tts.google_en_com`. The Configuration form gives you a **dropdown** of the engines you already have installed, so you don't have to type or guess it. Only needed for the *text* mode of audio injection (not when you play a ready-made audio URL). |
| `ha_base` *(advanced, YAML only)* | No | Advanced, rarely changed — there's no form field for it. The web address the add-on uses to fetch audio that Home Assistant generates for `{"text": …}` announcements. Defaults to `http://homeassistant:8123` (Home Assistant's internal hostname), which works for almost every install. Only set this if your Home Assistant isn't reachable at that address. |

---

## Example configuration

A complete config with **every** field, showing how they fit together — RTSP defaults, the
optional audio-injection keys, and three cameras: a plain `copy` camera, a `transcode` camera
with a per-camera `path` that mixes announcements into its own audio, and a `url`-override
birdseye whose (silent) audio is replaced with announcements. The **View as YAML** toggle in the
Configuration tab shows yours in exactly this form.

```yaml
lan_ip: 192.168.1.100                                # Home Assistant server's LAN IP (required)

# RTSP login shared by cameras that use `host`
rtsp_user: admin
rtsp_password: "your-password"                       # percent-encode reserved chars
rtsp_port: 554
default_path: "/cam/realmonitor?channel=1&subtype=1" # Amcrest/Dahua SUB stream

# Audio injection (optional) — announce THROUGH a camera; see "Audio injection" below
inject_token: "a-long-random-secret"                 # protects the :8790 control API
tts_engine: "tts.google_en_com"                      # default HA voice for {"text": ...}
# ha_base: "http://homeassistant:8123"               # advanced; the default is fine

cameras:
  - name: frontporch                                 # -> /frontporch/stream.m3u8
    host: 192.168.1.201                              # this camera's IP
    mode: copy                                        # already H.264 -> remux only, ~0% CPU
  - name: garagedoors
    host: 192.168.1.206
    path: "/cam/realmonitor?channel=1&subtype=0"     # this one only has a main stream
    mode: transcode
    audio_source: inject_mix                          # keep its audio + overlay announcements
  - name: birdseye                                    # Frigate follow-cam (H.264 High)
    url: "rtsp://ccab4aaf-frigate:8554/birdseye"      # hostname = the standard Frigate add-on
    mode: transcode
    audio_source: inject                              # birdseye is silent -> replace with TTS
    on_demand: true                                   # idle/404 when Frigate isn't tracking -> quiet it
```

---

## Finding your camera's RTSP path

Every camera brand serves its video at a slightly different RTSP **path** — the part of the
link after the IP address and port (the **Default RTSP path** / per-camera **Path** setting).
Which one is right is **specific to your camera manufacturer** (outside this add-on's control),
but it's straightforward to find:

- Look up **your camera model's "RTSP URL"** in its manual, or in a community database like
  **[iSpyConnect's camera list](https://www.ispyconnect.com/cameras)** (searchable by brand/model).
- Common starting points — always verify against **your** model/firmware:

  | Brand | Typical **sub-stream** path | Typical **main-stream** path |
  |---|---|---|
  | Amcrest / Dahua | `/cam/realmonitor?channel=1&subtype=1` | `/cam/realmonitor?channel=1&subtype=0` |
  | Hikvision | `/Streaming/Channels/102` | `/Streaming/Channels/101` |
  | Reolink | `/h264Preview_01_sub` | `/h264Preview_01_main` |
  | Other / ONVIF | check the manufacturer or iSpyConnect | — |

- **Prefer the sub-stream** (lower resolution) for Alexa — it's plenty for a small Echo Show
  screen and, if it's H.264, needs no transcoding.
- **Test it before wiring up Alexa.** The full stream URL is
  `rtsp://<user>:<password>@<camera-ip>:554<path>`. Paste it into **VLC** (*Media → Open Network
  Stream*), or run `ffprobe "rtsp://user:pass@192.168.1.201:554/your/path"`. If it plays / prints
  codec info, the path is correct — and `codec_name` tells you whether to use `copy` (`h264`) or
  `transcode` (`hevc`). Rather not fiddle with VLC/ffprobe? The add-on's own
  **[Validate streams](#validate-streams)** tool runs this exact check for you — that's next.

---

## Validate streams

The **Validate streams** tab runs the manual RTSP check above *for you*, and adds a second check
on the add-on's own output. Per camera it reports:

- **Source** — ffprobes the camera's RTSP feed and checks its codec/profile against the camera's
  `mode` — e.g. it flags an H.265 / H.264-**High** source left on `copy`.
- **Output** — confirms the add-on's `:8888` HLS is live and decodable H.264 Baseline (what Alexa
  actually opens).

Green means good; anything wrong is called out in plain English. Here it flags a `transcode`
camera whose source is *already* H.264 Baseline, so it could switch to `copy` and save CPU:

![Validate streams — source already H.264 Baseline, could use copy](https://raw.githubusercontent.com/Hu1kSmash/ha-alexa-cameras/main/docs/images/validate-stream-h246.png)

![Validate streams — healthy cameras](https://raw.githubusercontent.com/Hu1kSmash/ha-alexa-cameras/main/docs/images/validate-streams.png)

> ⚠️ **Heads-up about Frigate birdseye — expect idle errors, and they're harmless.**
>
> Birdseye (`mode: objects`) is a **follow-cam with nothing to follow when idle.** When Frigate
> isn't tracking any activity, its birdseye restream goes **fully cold — the RTSP endpoint returns
> `404 Not Found`.** So the add-on's worker logs a burst of `method DESCRIBE failed: 404` /
> `Error opening input file rtsp://…/birdseye`, retries every few seconds, and
> `/<birdseye>/stream.m3u8` 404s too — which also shows as a red **Source** / amber **Output** here
> in Validate streams.
>
> **Important — this is the part people get wrong:** while birdseye is cold, **trying to view it
> does *not* start it.** *"Alexa, show birdseye"* (or opening the stream in a browser / Validate)
> just fails until **Frigate itself brings birdseye up — which only happens when it detects
> activity and starts tracking.** The stream isn't being produced yet; there is nothing the add-on
> can do to conjure it.
>
> **This is normal and expected — it's how Frigate's birdseye works, not a fault in this add-on or
> your configuration.** And it doesn't break your setup: birdseye displays correctly whenever
> there *is* activity — which is exactly when you'd want it up. So the **reliable** way to use it is
> the **auto-show-on-detection** automation — push birdseye to the Echo *when Frigate detects
> motion* (see the **birdseye** recipe below) — rather than an on-demand *"show birdseye."* Frigate's
> `idle_heartbeat_fps` can keep it warmer and quicker to open *when it's already up*, but it won't
> keep a follow-cam alive when there's nothing to follow.
>
> **Quiet the noise:** tick the **On-demand** box for the birdseye camera (config key
> `on_demand: true`). The add-on then treats its idle `404`s as normal — it collapses the repeated
> errors into a single "waiting" line, skips the stall watchdog for it, and shows it as **Idle**
> here instead of red/amber.
>
> Bottom line: for **birdseye specifically**, idle `404` / Source-timeout / watchdog-restart noise
> is **safe to ignore** (and, with **On-demand** ticked, mostly silenced). On a *normal, always-on*
> camera, red/amber here is a real problem worth chasing.

![Validate streams — an idle Frigate birdseye (on-demand): Source times out (red), Output warns (amber)](https://raw.githubusercontent.com/Hu1kSmash/ha-alexa-cameras/main/docs/images/validate-stream-birdseye.png)

---

## Public URL check

Once your camera is reachable over the internet (via the tunnel + Alexa skill — see the
[setup guide](https://github.com/Hu1kSmash/ha-alexa-cameras/blob/main/docs/END-TO-END-SETUP.md)),
this tab confirms the whole chain per camera. It compares:

- **Internal** — the LAN stream on `:8888` (built from your **Home Assistant IP**), what the
  add-on serves directly on your network.
- **External** — your public **HTTPS** URL, what Amazon's relay actually fetches.

Both show clickable stream + snapshot links. On the **External** side a **403** is the *ideal*
result: the URL is reachable **and** your Cloudflare WAF rule is locking it to Amazon's fetchers.
A **200** warns that the stream is reachable but **not** locked down — anyone with the URL could
watch it (see the setup guide's WAF step).

![Public URL check — a green 403 per camera is the ideal result](https://raw.githubusercontent.com/Hu1kSmash/ha-alexa-cameras/main/docs/images/public-url-check.png)

---

## Logs

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

There's also a health check: **`GET http://<addon-host>:8790/health`** returns
`{"ok": true, "cams": ["birdseye", …]}` — a quick way to confirm the injector is running and
see exactly which cameras are inject-enabled (no token required).

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

### Recommended: add three `rest_command`s (no curl needed)

The control API only answers **POST**, so you can't fire it from a browser address bar. The
easy way to POST — from automations *and* by hand — is a Home Assistant **`rest_command`**:
Home Assistant does the POST for you, and each becomes a one-click action under **Developer
Tools → Actions**. Add these three to your **`configuration.yaml`** (create the
`rest_command:` block if you don't already have one), then reload from **Developer Tools →
YAML → "REST commands"** (no HA restart needed once the block already exists):

```yaml
rest_command:
  # Speak text — the add-on renders TTS with your default engine and injects it.
  cam_say:
    url: "http://192.168.1.100:8790/say"    # your HA / add-on host's LAN IP + the control port
    method: POST
    content_type: "application/json"
    payload: '{"cam": "{{ cam }}", "text": "{{ message }}", "token": "a-long-random-secret"}'
  # Test beep — proves the pipe end-to-end without any TTS.
  cam_beep:
    url: "http://192.168.1.100:8790/say"
    method: POST
    content_type: "application/json"
    payload: '{"cam": "{{ cam }}", "test": true, "token": "a-long-random-secret"}'
  # Play any audio URL the add-on's container can reach.
  cam_url:
    url: "http://192.168.1.100:8790/say"
    method: POST
    content_type: "application/json"
    payload: '{"cam": "{{ cam }}", "url": "{{ url }}", "token": "a-long-random-secret"}'
```

Use the same `inject_token` value in all three payloads as in the add-on config (change it in
both places if you rotate it, or keep it in `secrets.yaml` and reference `{{ … }}`).

**Try them by hand** from **Developer Tools → Actions** while a camera is showing on an Echo:

```yaml
# test beep
action: rest_command.cam_beep
data:
  cam: garagedoors
```
```yaml
# speak text
action: rest_command.cam_say
data:
  cam: garagedoors
  message: "A vehicle is approaching the house"
```
```yaml
# play an audio URL
action: rest_command.cam_url
data:
  cam: garagedoors
  url: "http://…/clip.mp3"
```

![Running rest_command.cam_say from Developer Tools → Actions (YAML mode); the Response panel shows the injector's reply — `ok: true`, the target `cam`, the clip length in `ms`, and `status: 200`](https://raw.githubusercontent.com/Hu1kSmash/ha-alexa-cameras/main/docs/images/developer-tools-test-cam-say.png)

A successful call returns `ok: true` with the injected clip's length in `ms`; if the camera's
**Audio** isn't `inject`/`inject_mix` you'll get `400 no inject camera '<name>'` instead (nothing
is played, no stream is touched).

**In an automation**, wire your detection trigger to `cam_say`:

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
  set Frigate `birdseye.idle_heartbeat_fps: 10` (see the **birdseye** recipe below for why).
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
| **Frigate birdseye** logs `404` / errors when idle (`DESCRIBE failed: 404`, Source timeout, Output "not advancing", watchdog restarts) | **Normal.** With `mode: objects`, birdseye's restream goes cold (RTSP `404`) when Frigate isn't tracking anything — this is how birdseye works, nothing the add-on can change | **Safe to ignore for birdseye.** It comes up **only** when Frigate detects activity — *viewing a cold birdseye (even "Alexa, show birdseye") won't start it.* Use the auto-show-on-detection automation (see the birdseye recipe). |
| Log timestamps are in **UTC** | Older build | Update to **≥ 1.9.0** (logs use the host's local timezone). |
| **Audio injection:** nothing heard | Camera isn't being **viewed**, or `audio_source` not set | Audio only plays while the camera is shown on an Echo; set the camera's **Audio** to `inject`/`inject_mix` (`inject_mix` needs the source to *have* audio). |
| **Audio injection:** `POST /say` → **403** | Missing / wrong token | Send `inject_token` (header `X-Inject-Token`, JSON `token`, or `?token=`). |
| **Audio injection:** `POST /say` → **400 `no inject camera '<name>'`** | That camera's **Audio** is `none` (or the name is wrong) — nothing is injected, no stream is touched | Set the camera's **Audio** to `inject`/`inject_mix` and **Save & apply** (no add-on restart needed — the injector re-reads its cameras on save). The reply's `cams` list shows which cameras *are* inject-enabled; target one of those (or omit `cam` to use the first). |
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

---

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

`idle_heartbeat_fps` matters for two reasons: it keeps birdseye emitting **while it's up** (so the
add-on's puller doesn't lose a live stream mid-idle), **and** it keeps that stream running at
real-time so Alexa opens it promptly — set it too low and the idle timeline crawls (slower than
real-time), adding several seconds to *"show birdseye"* while a normal camera opens instantly.

> **It does not make birdseye available 24/7, though.** With `mode: objects` birdseye is a
> follow-cam: when Frigate is tracking *nothing*, the restream still goes cold and returns `404`,
> and **no viewer request will revive it — only Frigate detecting activity will** (see the birdseye
> idle-errors note under *Validate streams* above). That's exactly why the **automation** below —
> which shows birdseye *when Frigate detects motion* — is the reliable trigger, not an on-demand
> *"show birdseye."*

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

---

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
