#!/usr/bin/env python3
"""Local app for the KSA laptop chooser.

Serves `viewer/` on localhost and — with no cron or timer — checks on every
start whether the data snapshot is older than `--ttl` hours (default 24). When
stale it refreshes in the background (scrapers honouring the same TTL, then
normalize) while the current snapshot stays browsable; the page shows an
"updating" banner and reloads itself when the new data lands.

Usage:
    python3 app.py                 # check + serve + open browser
    python3 app.py --no-update     # just serve whatever is there
    python3 app.py --force         # refresh even if fresh
    python3 app.py --ttl 48        # consider data fresh for two days
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
VIEWER = ROOT / "viewer"
DATA = ROOT / "data"
UNIFIED = DATA / "unified_laptops.json"
META = DATA / "unified_laptops.meta.json"
SHORTLIST = DATA / "shortlist.json"
UPDATE_SCRIPT = ROOT / "update.sh"

_state = {"updating": False, "error": None, "started": None, "finished": None}
_lock = threading.Lock()
_ttl_hours = 24

# Anti-enumeration: a human/UI session loads a handful of files per minute; a
# scraper walking the payload hits this immediately. Deliberately generous for
# real use (page, laptops.js, logos, shortlist) — the point is friction, not
# blocking a shared household.
RATE_LIMIT = 300          # requests
RATE_WINDOW = 60.0        # seconds
_hits: dict[str, list[float]] = {}
_hits_lock = threading.Lock()

# POST /api/shortlist abuse caps: the payload is a handful of SKUs-notes, never
# megabytes. Unbounded Content-Length + unbounded field lengths = trivial local
# memory/disk DoS, so reject anything beyond these.
MAX_SHORTLIST_BODY = 100 * 1024   # bytes
MAX_SHORTLIST_ITEMS = 500
MAX_SKU_LEN = 120
MAX_STORE_LEN = 64
MAX_NOTE_LEN = 500


def _rate_limited(ip: str) -> bool:
    now = time.time()
    with _hits_lock:
        recent = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
        recent.append(now)
        _hits[ip] = recent
        if len(_hits) > 4096:                      # bound the table
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > RATE_WINDOW]:
                _hits.pop(k, None)
        return len(recent) > RATE_LIMIT


def snapshot_age() -> float:
    try:
        return max(0.0, time.time() - UNIFIED.stat().st_mtime)
    except FileNotFoundError:
        return float("inf")


def read_meta() -> dict:
    try:
        return json.loads(META.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def read_shortlist() -> dict:
    try:
        data = json.loads(SHORTLIST.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"items": []}


def run_update() -> None:
    with _lock:
        _state.update(updating=True, error=None, started=time.time(), finished=None)
    try:
        env = {**os.environ, "TTL_HOURS": str(_ttl_hours)}
        subprocess.run(["bash", str(UPDATE_SCRIPT), "--force"], cwd=ROOT, check=True, env=env)
        print("[app] refresh complete", flush=True)
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _state["error"] = str(exc)
        print(f"[app] refresh failed: {exc}", flush=True)
    finally:
        with _lock:
            _state.update(updating=False, finished=time.time())


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(VIEWER), **kwargs)

    def list_directory(self, path):  # noqa: A003
        # No directory listings: the served tree is small and known; a listing
        # only helps someone enumerate what to copy.
        self.send_error(404, "Not found")
        return None

    def _json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _blocked(self) -> bool:
        if _rate_limited(self._client_ip()):
            self.send_error(429, "Too many requests")
            return True
        return False

    @staticmethod
    def _loopback_host(host: str | None) -> bool:
        return host in ("127.0.0.1", "localhost", "::1", "[::1]")

    def _same_origin(self) -> bool:
        """Reject cross-site POSTs (CSRF) while allowing same-origin + curl.

        Same-origin fetch and curl send no usable cross-site signal, so only
        reject when the browser *does* declare a cross-site origin: an
        `Origin`/`Referer` whose host is not loopback. Requiring
        `Content-Type: application/json` additionally forces a CORS preflight
        for cross-site JSON, which this server never answers, so plain
        `no-cors` form/text POSTs cannot smuggle JSON in either.
        """
        for hdr in ("Origin", "Referer"):
            val = self.headers.get(hdr)
            if not val:
                continue
            try:
                host = urlparse(val).hostname
            except Exception:  # noqa: BLE001
                return False
            if not self._loopback_host(host):
                return False
        return True

    ROBOTS = (
        "User-agent: *\n"
        "Disallow: /\n"
        "User-agent: GPTBot\nDisallow: /\n"
        "User-agent: ClaudeBot\nDisallow: /\n"
        "User-agent: CCBot\nDisallow: /\n"
        "User-agent: Google-Extended\nDisallow: /\n"
    )

    def _text(self, body: str, status: int = 200, ctype: str = "text/plain") -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/robots.txt":
            self._text(self.ROBOTS)
            return
        if self._blocked():
            return
        if path == "/api/status":
            age = snapshot_age()
            meta = read_meta()
            self._json({
                "updating": _state["updating"],
                "error": _state["error"],
                "age_seconds": None if age == float("inf") else int(age),
                "generated_at": meta.get("generated_at"),
                "count": meta.get("count"),
                "ttl_hours": _ttl_hours,
            })
            return
        if path == "/api/shortlist":
            self._json(read_shortlist())
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/shortlist":
            self.send_error(404)
            return
        if self._blocked():
            return
        if not self._same_origin():
            self._json({"error": "cross-site request rejected"}, status=403)
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._json({"error": "Content-Type must be application/json"}, status=415)
            return
        try:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("invalid Content-Length")
            if length < 0 or length > MAX_SHORTLIST_BODY:
                self._json({"error": f"body too large (max {MAX_SHORTLIST_BODY} bytes)"}, status=413)
                return
            raw = self.rfile.read(length) if length else b"{}"
            if len(raw) > MAX_SHORTLIST_BODY:
                self._json({"error": f"body too large (max {MAX_SHORTLIST_BODY} bytes)"}, status=413)
                return
            body = json.loads(raw or b"{}")
            items = body.get("items", [])
            if not isinstance(items, list):
                raise ValueError("items must be a list")
            if len(items) > MAX_SHORTLIST_ITEMS:
                raise ValueError(f"too many items (max {MAX_SHORTLIST_ITEMS})")
            clean = []
            for item in items:
                if isinstance(item, dict) and item.get("sku"):
                    clean.append({
                        "sku": str(item["sku"])[:MAX_SKU_LEN],
                        "store": str(item.get("store") or "")[:MAX_STORE_LEN],
                        "note": str(item.get("note") or "")[:MAX_NOTE_LEN],
                    })
            payload = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": clean}
            SHORTLIST.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            print(f"[app] shortlist saved ({len(clean)} item{'s' if len(clean) != 1 else ''})", flush=True)
            self._json(payload)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, status=400)

    def end_headers(self):
        # never cache the snapshot in the browser, so a reload after a refresh
        # always picks up the new laptops.js
        self.send_header("Cache-Control", "no-store")
        # baseline hardening that never breaks the inline-script viewer:
        # no MIME sniffing, no framing (localhost app), minimal referrer.
        # (No CSP: the page relies on inline scripts/styles + Google Fonts.)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, fmt, *args):  # noqa: A003
        pass  # keep the console for update progress only


def main() -> int:
    global _ttl_hours
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ttl", type=float, default=24, metavar="HOURS",
                        help="refresh the snapshot when older than this (default: 24)")
    parser.add_argument("--force", action="store_true", help="refresh even if the snapshot is fresh")
    parser.add_argument("--no-update", action="store_true", help="never refresh, just serve")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    _ttl_hours = args.ttl

    if not VIEWER.exists():
        print("[app] viewer/index.html not found — run normalize.py first", file=sys.stderr)
        return 1

    try:
        httpd = None
        for port in range(args.port, args.port + 20):
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
                break
            except OSError:
                continue
        if httpd is None:
            print(f"[app] no free port in {args.port}–{args.port + 19}", file=sys.stderr)
            return 1
    except KeyboardInterrupt:
        return 0

    url = f"http://127.0.0.1:{httpd.server_address[1]}/"

    age = snapshot_age()
    stale = age > args.ttl * 3600
    if args.no_update:
        print(f"[app] serving {url} (auto-update disabled)", flush=True)
    elif stale or args.force:
        reason = "forced" if args.force and not stale else f"snapshot is {age / 3600:.1f}h old (ttl {args.ttl:g}h)"
        print(f"[app] serving {url} — refreshing in the background ({reason})", flush=True)
        threading.Thread(target=run_update, daemon=True).start()
    else:
        print(f"[app] serving {url} — snapshot is fresh ({age / 3600:.1f}h old)", flush=True)

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[app] stopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
