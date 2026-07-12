# Contributing

Thanks for your interest in improving **Alexa Cameras (HLS)**!

## Ways to help

- **Questions / setup help** → [Discussions](https://github.com/Hu1kSmash/ha-alexa-cameras/discussions)
  (please don't use the issue tracker for support).
- **Bugs** → open an [issue](https://github.com/Hu1kSmash/ha-alexa-cameras/issues/new/choose);
  the Bug Report template asks for the add-on version, HA install type, and Logs output.
- **Ideas / features** → the Feature Request template, or Discussions → *Ideas*.
- **Code** → pull requests are welcome — see below.

## Project layout

- `alexa_cameras/` — the add-on itself:
  - `run.sh` — per-camera ffmpeg workers (copy/transcode), backoff, reload watcher
  - `ui.py` — the ingress Web UI (config, validation, public-URL check, logs)
  - `Dockerfile`, `config.yaml`, `DOCS.md`
- `branding/` — `generate_branding.py` regenerates the icon, logo, Web-UI header, and
  social banner **from code**. Run it after any art change; don't hand-edit the PNGs.
- `docs/` — the End-to-End Setup guide and (masked) screenshots.

## Pull requests

- Branch off `main`; keep each PR focused and explain the what/why.
- Match the surrounding style — `run.sh` is Bash, `ui.py` is standard-library Python
  (plus PyYAML); no new runtime dependencies without a good reason.
- Sanity-check before pushing:
  ```bash
  bash -n alexa_cameras/run.sh
  python3 -m py_compile alexa_cameras/ui.py
  ```
- For user-facing changes, bump `version:` in `alexa_cameras/config.yaml` and add a
  `CHANGELOG.md` entry.

## Screenshots & privacy

Screenshots in the docs are **masked** — no real LAN IPs, public domains, passwords,
or tokens. If you add or update one, blur those first.

## Scope

This project is deliberately **fully self-hosted** — no Nabu Casa, no third-party
camera cloud (MonocleCam, etc.). Please keep contributions aligned with that.
