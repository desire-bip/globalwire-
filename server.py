#!/usr/bin/env python3
"""
GlobalWire — professional auto-updating news aggregator.

Backend: Python 3 standard library ONLY (no pip installs, nothing to break).
Sources: public RSS/Atom feeds published by the news sites themselves
(sources.json) — 9 categories, 36 sources. Refreshes every REFRESH_SECONDS
(default 3600 = 1 hour), keeps last-good cache on failure.

Endpoints:
  GET  /                    -> frontend (static/index.html)
  GET  /api/news?category=x -> {updated_at, next_refresh_in, items:[...]}
  GET  /api/status          -> per-source health + counts
  POST /api/refresh         -> trigger an immediate refresh cycle
"""

from __future__ import annotations

import email.utils
import html
import json
import re
import ssl
import threading
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.json"
STATIC = ROOT / "static"
PORT = 8000
REFRESH_SECONDS = 3600  # 1 hour, as required
FEED_TIMEOUT = 10
UA = ("Mozilla/5.0 (compatible; GlobalWire/1.0; +news-aggregator) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

MEDIA = "http://search.yahoo.com/mrss/"
CONTENT = "http://purl.org/rss/1.0/modules/content/"
ATOM = "http://www.w3.org/2005/Atom"

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE  # aggregator tolerance; feeds remain read-only

IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str, limit: int = 280) -> str:
    if not text:
        return ""
    text = html.unescape(TAG_RE.sub(" ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return (text[: limit - 1] + "…") if len(text) > limit else text


def parse_date(raw):
    """RSS pubDate (RFC822) or Atom ISO -> epoch seconds (UTC)."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _text(el):
    return (el.text or "").strip() if el is not None else ""


def _image_from(item, desc_html: str):
    # media:thumbnail / media:content
    for tag in (f"{{{MEDIA}}}thumbnail", f"{{{MEDIA}}}content"):
        for m in item.findall(tag):
            url = m.get("url")
            if url and (m.get("medium") in (None, "image") or (m.get("type", "").startswith("image"))):
                if url.startswith("http"):
                    return url
    # enclosure
    for e in item.findall("enclosure"):
        url = e.get("url")
        if e.get("type", "").startswith("image") and url and url.startswith("http"):
            return url
    # first <img> inside content/description
    m = IMG_RE.search(desc_html or "")
    if m and m.group(1).startswith("http"):
        return m.group(1)
    return None


def parse_feed(xml_bytes: bytes, source: str, category: str, limit=25):
    root = ET.fromstring(xml_bytes)
    items = []

    # RSS 2.0
    for item in root.iter("item"):
        desc = _text(item.find(f"{{{CONTENT}}}encoded")) or _text(item.find("description"))
        items.append({
            "title": strip_html(_text(item.find("title")), 200),
            "url": _text(item.find("link")),
            "summary": strip_html(desc),
            "published_ts": parse_date(_text(item.find("pubDate")) or _text(item.find(f"{{{ATOM}}}updated"))),
            "source": source,
            "category": category,
            "image": _image_from(item, desc),
        })
    # Atom
    for entry in root.iter(f"{{{ATOM}}}entry"):
        link = ""
        for l in entry.findall(f"{{{ATOM}}}link"):
            if l.get("rel") in (None, "alternate") and l.get("href"):
                link = l.get("href")
                break
        desc = _text(entry.find(f"{{{ATOM}}}summary")) or _text(entry.find(f"{{{ATOM}}}content"))
        items.append({
            "title": strip_html(_text(entry.find(f"{{{ATOM}}}title")), 200),
            "url": link,
            "summary": strip_html(desc),
            "published_ts": parse_date(_text(entry.find(f"{{{ATOM}}}published")) or _text(entry.find(f"{{{ATOM}}}updated"))),
            "source": source,
            "category": category,
            "image": _image_from(entry, desc),
        })

    now = time.time()
    out = []
    for it in items:
        if not it["title"] or not it["url"].startswith("http"):
            continue
        if it["published_ts"] is None:
            it["published_ts"] = now
        it["published_ts"] = min(it["published_ts"], now + 300)
        it["published_at"] = datetime.fromtimestamp(it["published_ts"], tz=timezone.utc).isoformat(timespec="seconds")
        out.append(it)
    return out[:limit]


# --------------------------------------------------------------------------- #
class NewsCache:
    def __init__(self):
        self.sources = json.loads(SOURCES_FILE.read_text())
        self.items_by_cat: dict[str, list[dict]] = {c: [] for c in self.sources}
        self.status: dict[str, dict] = {}
        self.updated_at: float = 0
        self.next_refresh: float = time.time() + REFRESH_SECONDS
        self.lock = threading.Lock()
        self.refreshing = threading.Event()

    def fetch_one(self, source: dict, category: str):
        name, url = source["name"], source["url"]
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml,*/*"})
        try:
            with urllib.request.urlopen(req, timeout=FEED_TIMEOUT, context=_ssl_ctx) as r:
                items = parse_feed(r.read(), name, category)
            return name, items, None
        except Exception as e:  # noqa: BLE001
            return name, [], f"{type(e).__name__}: {str(e)[:120]}"

    def refresh(self):
        if self.refreshing.is_set():
            return
        self.refreshing.set()
        try:
            new_items: dict[str, list[dict]] = {}
            new_status: dict[str, dict] = {}
            jobs = [(s, c) for c, feeds in self.sources.items() for s in feeds]
            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = [pool.submit(self.fetch_one, s, c) for s, c in jobs]
                for (s, c), fut in zip(jobs, futs):
                    name, items, err = fut.result()
                    new_status[name] = {"ok": not err, "count": len(items), "category": c, "error": err}
                    new_items.setdefault(c, []).extend(items)
            with self.lock:
                for cat, items in new_items.items():
                    if not items:  # whole category failed: keep last-good
                        continue
                    seen, merged = set(), []
                    for it in sorted(items, key=lambda x: -x["published_ts"]):
                        key = it["url"].split("#")[0]
                        if key in seen:
                            continue
                        seen.add(key)
                        merged.append(it)
                    self.items_by_cat[cat] = merged[:60]
                self.status = new_status
                self.updated_at = time.time()
                self.next_refresh = time.time() + REFRESH_SECONDS
        finally:
            self.refreshing.clear()
        ok = sum(1 for s in self.status.values() if s["ok"])
        total = sum(s["count"] for s in self.status.values())
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] refresh: {ok}/{len(self.status)} sources ok, {total} items", flush=True)

    def snapshot(self, category: str):
        with self.lock:
            if category == "all":
                items = [it for lst in self.items_by_cat.values() for it in lst]
                items.sort(key=lambda x: -x["published_ts"])
                items = items[:120]
            else:
                items = self.items_by_cat.get(category, [])
            return {
                "updated_at": datetime.fromtimestamp(self.updated_at, tz=timezone.utc).isoformat(timespec="seconds") if self.updated_at else None,
                "updated_ts": self.updated_at,
                "next_refresh_in": max(0, int(self.next_refresh - time.time())),
                "category": category,
                "categories": list(self.sources.keys()),
                "count": len(items),
                "items": items,
            }


CACHE = NewsCache()


def refresher_loop():
    CACHE.refresh()
    while True:
        time.sleep(REFRESH_SECONDS)
        try:
            CACHE.refresh()
        except Exception as e:  # noqa: BLE001
            print("refresh cycle error:", e, flush=True)


# --------------------------------------------------------------------------- #
class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if urlparse(self.path).path == "/api/refresh":
            threading.Thread(target=CACHE.refresh, daemon=True).start()
            return self._json({"ok": True, "message": "refresh started"})
        self._json({"error": "not found"}, 404)

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/api/news":
            q = parse_qs(p.query)
            cat = (q.get("category", ["all"])[0] or "all").lower()
            if cat != "all" and cat not in CACHE.sources:
                cat = "all"
            return self._json(CACHE.snapshot(cat))
        if p.path == "/api/status":
            ok = sum(1 for s in CACHE.status.values() if s["ok"])
            return self._json({
                "updated_at": CACHE.snapshot("all")["updated_at"],
                "next_refresh_in": CACHE.snapshot("all")["next_refresh_in"],
                "sources_ok": ok, "sources_total": len(CACHE.status),
                "refresh_interval_seconds": REFRESH_SECONDS,
                "sources": CACHE.status,
            })
        if p.path == "/":
            self.path = "/index.html"
        return super().do_GET()


def main():
    threading.Thread(target=refresher_loop, daemon=True).start()
    import os
    os.chdir(STATIC)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"GlobalWire live on 0.0.0.0:{PORT} — {sum(len(v) for v in CACHE.sources.values())} sources, "
          f"{len(CACHE.sources)} categories, refresh every {REFRESH_SECONDS//60}m", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
