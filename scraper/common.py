"""Shared utilities for the store scrapers (fetcher, caching, parsing helpers)."""
from __future__ import annotations

import hashlib
import html as htmlmod
import json
import logging
import random
import re
import csv
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"
DATA_DIR = ROOT / "data"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

JUNK_VALUES = {"", "n/a", "na", "n.a.", "-", "--", "none", "null", "unknown", "not specified"}

log = logging.getLogger("scrapers")


class NotFoundError(Exception):
    """Resource returns 404/410 (typically a delisted item)."""


def _looks_like_botwall(text: str) -> bool:
    """True for captcha / bot-manager walls served with HTTP 200."""
    low = (text or "").lower()
    return ("bm-verify" in low or "captch" in low or "robot check" in low
            or "verify you are a human" in low or "are you a robot" in low)


class Fetcher:
    """Thread-safe HTTP fetcher with on-disk cache, throttle and retries."""

    def __init__(
        self,
        cache_dir: Path,
        min_interval: float = 0.25,
        retries: int = 4,
        refresh: bool = False,
        headers: dict | None = None,
        max_age: float | None = None,
    ):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.retries = retries
        self.refresh = refresh
        self.max_age = max_age  # seconds; None = cached responses never expire
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._session = requests.Session()
        self._session.headers.update(HEADERS)
        if headers:
            self._session.headers.update(headers)
        self.stats = {"network": 0, "cache": 0, "errors": 0}

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".html")

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait + random.uniform(0, self.min_interval / 4))
            self._last_request = time.monotonic()

    def get(self, url: str, allow_cache: bool = True) -> str:
        path = self._cache_path(url)
        if allow_cache and not self.refresh and path.exists() and path.stat().st_size > 10_000:
            if self.max_age is None or (time.time() - path.stat().st_mtime) < self.max_age:
                self.stats["cache"] += 1
                return path.read_text(encoding="utf-8", errors="ignore")

        last_err: Exception | None = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                resp = self._session.get(url, timeout=45)
                self.stats["network"] += 1
                if resp.status_code == 200:
                    text = resp.text
                    if _looks_like_botwall(text):
                        # Akamai/captcha walls answer 200: usable as a signal
                        # ("index ends here / throttled") but must never be
                        # cached, or the poisoned file masks the real page
                        log.warning("bot-wall page, not caching: %s", url)
                    else:
                        path.write_text(text, encoding="utf-8")
                    return text
                if resp.status_code in (404, 410):
                    raise NotFoundError(f"HTTP {resp.status_code}: {url}")
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
            except NotFoundError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                sleep = (2**attempt) + random.uniform(0, 1.5)
                log.warning("retry %s/%s in %.1fs: %s (%s)", attempt + 1, self.retries, sleep, url, exc)
                time.sleep(sleep)
        self.stats["errors"] += 1
        raise RuntimeError(f"failed to fetch {url}: {last_err}")


def _clean(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    text = htmlmod.unescape(value)
    text = re.sub(r"\s+", " ", text).strip()
    if text.lower() in JUNK_VALUES:
        return None
    return text


def _first_number(*values):
    for value in values:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value.strip():
            try:
                return float(value.strip())
            except ValueError:
                continue
    return None


def extract_ld_json(html: str) -> dict[str, dict]:
    """Return {script id: parsed json} for every JSON-LD block."""
    out: dict[str, dict] = {}
    for match in re.finditer(r'<script([^>]*)type="application/ld\+json"([^>]*)>(.*?)</script>', html, re.S):
        attrs, body = match.group(1) + match.group(2), match.group(3)
        sid_match = re.search(r'id="([^"]*)"', attrs)
        sid = sid_match.group(1) if sid_match else f"anon-{len(out)}"
        try:
            out[sid] = json.loads(body)
        except json.JSONDecodeError:
            continue
    return out


def write_jsonl(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_wide_csv(records: list[dict], path: Path, base_cols: list[str]) -> None:
    """One row per record; one column per spec label (union over records)."""
    seen: set[str] = set(base_cols)
    spec_labels: list[str] = []
    for rec in records:
        for label in rec.get("specs", {}):
            if label not in seen:
                seen.add(label)
                spec_labels.append(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=base_cols + spec_labels, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            row = dict(rec)
            row.update(rec.get("specs", {}))
            writer.writerow(row)
