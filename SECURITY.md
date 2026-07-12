# Security Policy

## Supported versions

This add-on ships as a rolling latest release — security fixes land in the newest
version. Please update to the latest release before reporting an issue.

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue with details.

- **Preferred:** use GitHub's private vulnerability reporting — go to the
  [**Security** tab](https://github.com/Hu1kSmash/ha-alexa-cameras/security) →
  **Report a vulnerability**.

Please include:

- the add-on version (Info page),
- your Home Assistant install type (OS / Supervised),
- clear reproduction steps, and
- the impact.

You'll get an acknowledgement, and we'll work on a fix. Please allow a reasonable
period for a patch before any public disclosure.

## Deployment notes (important)

This add-on **only produces the camera stream**, over plain **HTTP** on port `8888`
on your LAN. Securing the path to Alexa is the operator's responsibility:

- **Do not expose port `8888` directly to the internet.**
- Put **HTTPS** (with a valid certificate) in front of it, and **lock external access
  to Amazon's fetchers** with a WAF rule. The
  [End-to-End Setup guide](docs/END-TO-END-SETUP.md) walks through a Cloudflare Tunnel
  + WAF configuration that does exactly this.
- The built-in **Public URL check** tab helps confirm your external endpoint is
  reachable *and* locked down (a green `403` for non-Amazon clients is the goal).
