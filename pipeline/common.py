"""Shared helpers: paths, polite HTTP client with on-disk cache."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
CONFIG = ROOT / "config"

UW_OPENALEX_ID = "I201448701"  # University of Washington
OPENALEX = "https://api.openalex.org"
USER_AGENT = "safs-connector/0.1 (+https://github.com/; research publication index)"

_last_call: dict[str, float] = {}


def _throttle(host: str, min_interval: float) -> None:
    wait = _last_call.get(host, 0) + min_interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.monotonic()


def cached_get(url: str, params: dict | None = None, *, min_interval: float = 0.15,
               refresh: bool = False, as_json: bool = True):
    """GET with a disk cache under data/raw/http/. Returns parsed JSON or text."""
    key_src = url + "?" + json.dumps(params or {}, sort_keys=True)
    key = hashlib.sha1(key_src.encode()).hexdigest()
    path = RAW / "http" / f"{key}.{'json' if as_json else 'html'}"
    if path.exists() and not refresh:
        text = path.read_text()
        return json.loads(text) if as_json else text

    params = dict(params or {})
    if "openalex.org" in url:
        # Polite pool / API key from env (never committed).
        if os.environ.get("OPENALEX_API_KEY"):
            params["api_key"] = os.environ["OPENALEX_API_KEY"]
        elif os.environ.get("OPENALEX_MAILTO"):
            params["mailto"] = os.environ["OPENALEX_MAILTO"]

    host = httpx.URL(url).host
    for attempt in range(5):
        _throttle(host, min_interval)
        r = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT},
                      timeout=60, follow_redirects=True)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        break
    else:
        r.raise_for_status()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(r.text)
    return r.json() if as_json else r.text
