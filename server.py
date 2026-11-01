#!/usr/bin/env python3
"""Pi dashboard server (Python standard library only).

Two servers run in this one process:
  - Dashboard (dashboard_port, localhost only): clocks, weather, photos, latest drawing.
    Only the Pi itself can open it, so your photos never leave the Pi.
  - Draw (draw_port): the drawing page + upload endpoint. This is the only port
    exposed to the internet (via Tailscale Funnel). Uploads require the secret key.
"""
import base64
import glob
import hmac
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(ROOT, "config.json")) as f:
    CFG = json.load(f)

WEB = os.path.join(ROOT, "web")
PHOTOS = os.path.join(ROOT, "photos")
DRAWINGS = os.path.join(ROOT, "drawings")
for d in (PHOTOS, DRAWINGS):
    os.makedirs(d, exist_ok=True)

IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "application/javascript",
    ".css": "text/css", ".json": "application/json", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
}
MAX_UPLOAD = 8 * 1024 * 1024


def safe_join(base, name):
    p = os.path.realpath(os.path.join(base, name))
    return p if p.startswith(os.path.realpath(base) + os.sep) else None


def latest_meta():
    p = os.path.join(DRAWINGS, "latest.json")
    if os.path.isfile(p):
        with open(p) as f:
            return json.load(f)
    return {"ts": 0, "note": ""}


HISTORY_SECONDS = float(CFG.get("drawing_history_hours", 24)) * 3600


def timeline():
    """Drawings from the last HISTORY_SECONDS, newest first: [{ts, note}]."""
    cutoff, out = time.time() - HISTORY_SECONDS, []
    for f in os.listdir(DRAWINGS):
        m = re.match(r"^(\d+)\.png$", f)
        if not m or int(m.group(1)) < cutoff:
            continue
        note = ""
        try:
            with open(os.path.join(DRAWINGS, m.group(1) + ".json")) as jf:
                note = json.load(jf).get("note", "")
        except (OSError, ValueError):
            pass
        out.append({"ts": int(m.group(1)), "note": note})
    return sorted(out, key=lambda d: d["ts"], reverse=True)


def prune_forever():
    """Rolling window: each drawing is deleted HISTORY_SECONDS after it was sent.
    latest.png (the one in the square) is never deleted."""
    while True:
        cutoff = time.time() - HISTORY_SECONDS
        for f in os.listdir(DRAWINGS):
            m = re.match(r"^(\d+)\.(png|json)$", f)
            if m and int(m.group(1)) < cutoff:
                try:
                    os.remove(os.path.join(DRAWINGS, f))
                except OSError:
                    pass
        time.sleep(600)


class Base(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def file(self, path):
        if not path or not os.path.isfile(path):
            return self.send(404, {"error": "not found"})
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            self.send(200, f.read(), TYPES.get(ext, "application/octet-stream"))


class Dashboard(Base):
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            return self.file(os.path.join(WEB, "dashboard.html"))
        if path == "/api/config":
            keys = ("weather", "clocks", "photo_seconds", "sleep", "drawing_history_hours")
            return self.send(200, {k: CFG[k] for k in keys if k in CFG})
        if path == "/api/photos":
            files = sorted(
                f for f in os.listdir(PHOTOS)
                if not f.startswith(".") and os.path.splitext(f)[1].lower() in IMG_EXT
            )
            return self.send(200, files)
        if path.startswith("/photos/"):
            return self.file(safe_join(PHOTOS, path[len("/photos/"):]))
        if path == "/api/drawing":
            return self.send(200, latest_meta())
        if path == "/drawing.png":
            return self.file(os.path.join(DRAWINGS, "latest.png"))
        if path == "/api/drawings":
            return self.send(200, timeline())
        m = re.match(r"^/drawings/(\d+)\.png$", path)
        if m:
            return self.file(os.path.join(DRAWINGS, m.group(1) + ".png"))
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/api/screen":
            return self.send(404, {"error": "not found"})
        try:
            on = bool(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))["on"])
        except Exception:
            return self.send(400, {"error": "bad request"})
        self.send(200, {"ok": True, "backlight": set_backlight(on)})


_saved_brightness = {}


def set_backlight(on):
    """Turn the screen's backlight off/on (official Pi touchscreens and most DSI screens).
    Touch keeps working while it's off, so a tap can wake it. Returns False if this
    screen has no controllable backlight (then the dashboard just shows black)."""
    done = False
    for d in glob.glob("/sys/class/backlight/*"):
        try:
            bpath = os.path.join(d, "brightness")
            if on:
                with open(os.path.join(d, "max_brightness")) as f:
                    mx = f.read().strip()
                with open(bpath, "w") as f:
                    f.write(_saved_brightness.get(d, mx))
            else:
                with open(bpath) as f:
                    cur = f.read().strip()
                if cur != "0":
                    _saved_brightness[d] = cur
                with open(bpath, "w") as f:
                    f.write("0")
            done = True
        except OSError:
            pass
        try:
            with open(os.path.join(d, "bl_power"), "w") as f:
                f.write("0" if on else "1")
            done = True
        except OSError:
            pass
    return done


class Draw(Base):
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/draw"):
            return self.file(os.path.join(WEB, "draw.html"))
        self.send(404, {"error": "not found"})

    def read_upload(self, mime, magic):
        """Validate key + data URL. Returns (data, bytes) or None after sending an error."""
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_UPLOAD:
            self.send(413, {"error": "too large"})
            return None
        try:
            data = json.loads(self.rfile.read(n))
        except Exception:
            self.send(400, {"error": "bad request"})
            return None
        if not hmac.compare_digest(str(data.get("key", "")), CFG["key"]):
            self.send(403, {"error": "wrong key"})
            return None
        m = re.match(r"^data:" + mime + r";base64,(.+)$", str(data.get("image", "")))
        try:
            raw = base64.b64decode(m.group(1), validate=True) if m else b""
        except Exception:
            raw = b""
        if not raw.startswith(magic):
            self.send(400, {"error": "bad image"})
            return None
        return data, raw

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/photo":
            got = self.read_upload("image/jpeg", b"\xff\xd8")
            if got:
                name = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.jpg"
                tmp = os.path.join(PHOTOS, "." + name)
                with open(tmp, "wb") as f:
                    f.write(got[1])
                os.replace(tmp, os.path.join(PHOTOS, name))
                self.send(200, {"ok": True, "name": name})
            return
        if path != "/api/drawing":
            return self.send(404, {"error": "not found"})
        got = self.read_upload("image/png", b"\x89PNG")
        if not got:
            return
        data, png = got

        ts = int(time.time())
        while os.path.exists(os.path.join(DRAWINGS, f"{ts}.png")):  # two in the same second
            ts += 1
        note = str(data.get("note", ""))[:140]
        with open(os.path.join(DRAWINGS, f"{ts}.png"), "wb") as f:  # timeline copy
            f.write(png)
        with open(os.path.join(DRAWINGS, f"{ts}.json"), "w") as f:
            json.dump({"ts": ts, "note": note}, f)
        tmp = os.path.join(DRAWINGS, "latest.png.tmp")
        with open(tmp, "wb") as f:
            f.write(png)
        os.replace(tmp, os.path.join(DRAWINGS, "latest.png"))
        meta = {"ts": ts, "note": note}
        tmp = os.path.join(DRAWINGS, "latest.json.tmp")
        with open(tmp, "w") as f:
            json.dump(meta, f)
        os.replace(tmp, os.path.join(DRAWINGS, "latest.json"))
        self.send(200, {"ok": True, "ts": ts})


def serve(handler, host, port):
    srv = ThreadingHTTPServer((host, port), handler)
    print(f"{handler.__name__} on http://{host}:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    if CFG.get("key") in ("", "CHANGE-ME"):
        raise SystemExit("Set a secret 'key' in config.json first (setup.sh does this).")
    threading.Thread(target=prune_forever, daemon=True).start()
    threading.Thread(
        target=serve, args=(Dashboard, "127.0.0.1", CFG["dashboard_port"]), daemon=True
    ).start()
    serve(Draw, "0.0.0.0", CFG["draw_port"])
