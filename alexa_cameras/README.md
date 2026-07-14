<p align="center">
  <img src="https://raw.githubusercontent.com/Hu1kSmash/ha-alexa-cameras/main/docs/images/social-preview.png" alt="Alexa Cameras (HLS)" width="820">
</p>

# Alexa Cameras (HLS)

**Serve any RTSP camera to an Amazon Echo Show / Alexa as a stream it will actually
play** — H.264 Baseline, MPEG-TS HLS, read **directly from the camera** (no go2rtc in
the media path, no Nabu Casa, no MonocleCam).

A plain go2rtc/Home Assistant HLS feed shows up **black** on an Echo Show, because its
segments drop the in-band SPS/PPS headers Amazon's picky camera relay needs. This add-on
runs a dedicated `ffmpeg` pipeline per camera that produces clean, decodable segments
instead.

## What it does

- **Per camera → `http://<ha-ip>:8888/<name>/stream.m3u8`** (live) and
  **`/<name>/snapshot.jpg`** (still, used as the Alexa thumbnail).
- **`copy` mode** — source already H.264: remux only, near-zero CPU. **`transcode` mode**
  — H.265 / H.264-High source: re-encode to H.264 Baseline 720p.
- **Self-healing** — each camera is its own ffmpeg process with exponential backoff, so
  one bad camera can't take down the others and a wrong password won't hammer it.
- **Announce _through_ a camera (experimental)** — mix a spoken TTS announcement into a
  camera's audio track so an Alexa alert plays *over* the live view instead of tearing it
  down. See [DOCS.md](DOCS.md).
- **Built-in Web UI** — edit config (a per-camera editor with validation), validate each
  stream's codec, read its **live-view latency** and camera I-frame interval, and check the
  public URL, all from the add-on's own dashboard (not the Home Assistant *Options* tab).

## Quick start

1. Add this repo in **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, install
   **Alexa Cameras (HLS)**, and **Start** it.
2. **Open Web UI → Configuration**: set your **Home Assistant IP** (a private IPv4) and
   add your cameras.
3. Confirm each feed in the **Validate streams** tab.

**This add-on only produces the stream.** Getting it to Alexa also needs HTTPS in front
of port 8888 and a self-hosted Alexa Smart Home skill — that full build is the
[End-to-End Setup guide](../docs/END-TO-END-SETUP.md).

## Documentation

- **[DOCS.md](DOCS.md)** — full configuration reference, `copy` vs `transcode`, audio
  injection (announce through a camera), the Web UI tabs, and troubleshooting.
- **[Repository README](../README.md)** — *why* Alexa needs exactly this stream format,
  why go2rtc's HLS goes black, and how this add-on fixes it.
- **[End-to-End Setup](../docs/END-TO-END-SETUP.md)** — the complete self-hosted path:
  Cloudflare Tunnel + WAF, the Alexa skill, and the AWS Lambda camera override.

## License

Apache License 2.0 — see [LICENSE](../LICENSE) and [NOTICE](../NOTICE).
