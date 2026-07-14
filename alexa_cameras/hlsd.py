#!/usr/bin/env python3
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
"""HLS/snapshot file server on :8888 — with lazy on-demand triggering.

Serves /tmp/hls statically (the per-camera stream.m3u8, .ts segments, and snapshot.jpg),
exactly like the plain `python -m http.server` it replaces — same access-log lines, which
are how you tell whether Amazon's relay (172.x) is reaching the add-on.

The one addition: for a camera marked `on_demand: true` in /data/config.yaml, every request
for its files TOUCHES /tmp/ondemand/<cam>.req. run.sh's lazy worker runs ffmpeg for that
camera only while that file is fresh, so an idle on-demand source (e.g. Frigate birdseye) is
never connected to at all — no polling, no churn, nothing to wedge the upstream. When a client
actually asks for it (Alexa, the auto-show automation, a browser), the touch wakes the worker.
"""
import os
import sys
import time

import yaml
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HLS = "/tmp/hls"
ONDEMAND = "/tmp/ondemand"
CONFIG = "/data/config.yaml"

# Which cameras are on_demand — cached from config, refreshed at most every few seconds so a
# config reload is picked up without re-reading YAML on every single segment request.
_od = {"cams": set(), "t": 0.0}


def on_demand_cams():
    now = time.time()
    if now - _od["t"] > 5:
        try:
            cfg = yaml.safe_load(open(CONFIG)) or {}
            _od["cams"] = {
                str(c.get("name", "")).strip()
                for c in (cfg.get("cameras") or [])
                if str(c.get("on_demand", "")).strip().lower() in ("true", "1", "yes", "on")
            }
        except Exception:
            pass
        _od["t"] = now
    return _od["cams"]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=HLS, **k)

    def log_message(self, fmt, *args):
        # Match the rest of the add-on's log format: "[HH:MM:SS] <client-ip> <message>"
        # (default is "<ip> - - [DD/Mon/YYYY HH:MM:SS] <message>", which reads inconsistently).
        sys.stderr.write("[%s] %s %s\n" % (time.strftime("%H:%M:%S"), self.address_string(), fmt % args))

    def _signal_demand(self):
        # path is like /birdseye/stream.m3u8 or /birdseye/seg_00001.ts or /birdseye/snapshot.jpg
        seg = self.path.lstrip("/").split("/", 1)
        if len(seg) == 2 and seg[0] and seg[0] in on_demand_cams():
            try:
                os.makedirs(ONDEMAND, exist_ok=True)
                # touch: create/update mtime — the freshness of this file IS the demand signal
                with open(os.path.join(ONDEMAND, seg[0] + ".req"), "w"):
                    pass
            except Exception:
                pass

    def do_GET(self):
        self._signal_demand()
        super().do_GET()

    def do_HEAD(self):
        self._signal_demand()
        super().do_HEAD()


if __name__ == "__main__":
    os.makedirs(HLS, exist_ok=True)
    os.makedirs(ONDEMAND, exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", 8888), Handler).serve_forever()
