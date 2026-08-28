#!/usr/bin/env python3
"""
RIM feed aggregator (Option 2).

Runs on open-internet infrastructure (e.g. a GitHub Actions runner), fetches every
RSS feed in feeds.csv, keeps recent + risk-relevant items, dedups, tags each with
country + tier, and writes output/feed_items.json — the single file the RIM run
reads with one WebFetch.

Fetch hardening:
  * a real browser User-Agent (many sites / Cloudflare reject the default),
  * automatic retries on transient errors (429 / 5xx),
  * feed auto-discovery — if a feed URL fails, look for the site's declared feed
    (<link rel="alternate" type="application/rss+xml">) and try common feed paths,
    and use the recovered feed. Recovered URLs are logged so feeds.csv can be updated.

No API keys required. RSS feeds are public URLs.
"""

import csv
import json
import os
import re
import hashlib
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin

import feedparser
import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry

# ---------------------------------------------------------------- config
FEEDS_CSV      = "feeds.csv"
OUTPUT         = "output/feed_items.json"
WINDOW_HOURS   = 30      # rolling window kept in the file; RIM narrows to 4h / 24h
MAX_WORKERS    = 16      # parallel feed fetches
PER_FEED_CAP   = 40      # max items kept per feed
TOTAL_CAP      = 600     # max items in the output file (keeps it WebFetch-friendly)
FETCH_TIMEOUT  = 20      # seconds per HTTP request
DISCOVERY_CAP  = 6       # max candidate feed URLs to try when auto-discovering
KEYWORD_FILTER = True    # True = keep only risk-relevant items; False = keep all recent

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en,ar;q=0.8,fr;q=0.7,tr;q=0.6",
}

# Common feed locations to probe during auto-discovery.
FEED_PATHS = [
    "/feed/", "/rss.xml", "/rss", "/feed", "/index.xml",
    "/arc/outboundfeeds/rss/?outputType=xml", "/?feed=rss2", "/feeds/posts/default",
]

# Risk keywords (multilingual starter set — expand freely).
KEYWORDS = [
    # English
    "protest", "strike", "attack", "bomb", "explosion", "shooting", "clash", "riot",
    "unrest", "coup", "election", "terror", "militant", "hostage", "kidnap", "cyber",
    "ransomware", "hack", "breach", "outbreak", "epidemic", "earthquake", "flood",
    "storm", "wildfire", "evacuat", "sanction", "corruption", "fraud", "assassinat",
    "curfew", "border", "military", "airstrike", "missile", "drone", "killed",
    "wounded", "arrest", "crackdown", "blackout", "outage",
    # Arabic
    "احتجاج", "إضراب", "هجوم", "انفجار", "اشتباك", "إرهاب", "اختطاف", "زلزال",
    "فيضان", "عقوبات", "قتيل", "اعتقال",
    # French
    "grève", "attentat", "manifestation", "émeute", "explosion", "otage", "séisme",
    "inondation", "cyberattaque", "sanctions", "couvre-feu",
    # Turkish
    "protesto", "saldırı", "patlama", "grev", "deprem", "siber", "gözaltı",
]
KW_RE  = re.compile("|".join(re.escape(k) for k in KEYWORDS), re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
LINK_TAG_RE = re.compile(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.I)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)

# ---------------------------------------------------------------- http session
def build_session():
    s = requests.Session()
    retry = Retry(total=2, connect=2, read=2, backoff_factor=0.6,
                  status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update(HEADERS)
    return s

SESSION = build_session()


def http_get(url):
    try:
        r = SESSION.get(url, timeout=FETCH_TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and r.content:
            return r.content, None
        return None, f"HTTP {r.status_code}"
    except Exception as ex:
        return None, str(ex)[:120]


# ---------------------------------------------------------------- feed parsing
def parse_feed(url):
    content, err = http_get(url)
    if content is None:
        return None, err
    d = feedparser.parse(content)
    if d.entries:
        return d, None
    return None, "no entries / not a feed"


def discover_feed(feed_url):
    """If a feed URL fails, find the site's real feed. Returns (working_url, parsed) or (None, None)."""
    p = urlparse(feed_url)
    if not p.scheme or not p.netloc:
        return None, None
    base = f"{p.scheme}://{p.netloc}"
    candidates = []

    # 1) feeds the homepage declares in its <head>
    html, _ = http_get(base + "/")
    if html:
        try:
            text = html.decode("utf-8", "ignore")
        except Exception:
            text = ""
        for tag in LINK_TAG_RE.findall(text):
            m = HREF_RE.search(tag)
            if m:
                candidates.append(urljoin(base + "/", m.group(1)))

    # 2) common feed paths
    for pth in FEED_PATHS:
        candidates.append(base + pth)

    # dedup, drop the already-failed URL, cap
    seen, ordered = set(), []
    for c in candidates:
        if c in seen or c == feed_url:
            continue
        seen.add(c)
        ordered.append(c)

    for c in ordered[:DISCOVERY_CAP]:
        d, _ = parse_feed(c)
        if d:
            return c, d
    return None, None


def parse_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def collect_items(d, row):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    items = []
    for e in d.entries[:200]:
        pub = parse_time(e)
        if pub and pub < cutoff:
            continue
        title = (e.get("title") or "").strip()
        if not title:
            continue
        link = (e.get("link") or "").strip()
        summary = TAG_RE.sub("", (e.get("summary") or ""))[:400].strip()
        if KEYWORD_FILTER and not KW_RE.search(title + " " + summary):
            continue
        items.append({
            "source": row.get("source_name"), "country": row.get("country"),
            "tier": row.get("tier"), "title": title, "link": link,
            "published": pub.isoformat() if pub else None, "summary": summary,
        })
    return items[:PER_FEED_CAP]


def fetch(row):
    url = (row.get("url") or "").strip()
    meta = {"source": row.get("source_name"), "country": row.get("country"),
            "tier": row.get("tier"), "url": url}
    d, err = parse_feed(url)
    recovered = None
    if d is None:
        new_url, d2 = discover_feed(url)
        if d2 is not None:
            d, recovered = d2, new_url
    if d is None:
        return {"ok": False, "reason": err or "no feed found", "meta": meta,
                "items": [], "recovered": None}
    return {"ok": True, "meta": meta, "items": collect_items(d, row), "recovered": recovered}


# ---------------------------------------------------------------- main
def main():
    rows = []
    with open(FEEDS_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("type") or "").strip().lower() == "rss" and (r.get("url") or "").strip():
                rows.append(r)
    print(f"RSS feeds to fetch: {len(rows)}")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(fetch, r) for r in rows]
        for fu in as_completed(futs):
            results.append(fu.result())

    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    recovered = [r for r in ok if r.get("recovered")]

    all_items = []
    for r in ok:
        all_items.extend(r["items"])

    seen, dedup = set(), []
    for it in sorted(all_items, key=lambda x: (x["published"] or ""), reverse=True):
        h = hashlib.md5((it["title"].lower() + "|" + it["link"].lower()).encode("utf-8")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        dedup.append(it)
    dedup = dedup[:TOTAL_CAP]

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": WINDOW_HOURS,
        "keyword_filter": KEYWORD_FILTER,
        "feeds_total": len(rows),
        "feeds_ok": len(ok),
        "feeds_failed": len(failed),
        "feeds_recovered": len(recovered),
        "item_count": len(dedup),
        "recovered_feeds": [{"source": r["meta"]["source"], "country": r["meta"]["country"],
                             "old_url": r["meta"]["url"], "new_url": r["recovered"]} for r in recovered],
        "failures": [{"source": r["meta"]["source"], "url": r["meta"]["url"],
                      "reason": r["reason"]} for r in failed],
        "items": dedup,
    }
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"wrote {OUTPUT}: {len(dedup)} items | feeds ok {len(ok)} "
          f"(of which {len(recovered)} auto-recovered) / failed {len(failed)}")


if __name__ == "__main__":
    main()
