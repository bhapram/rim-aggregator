#!/usr/bin/env python3
"""
RIM feed aggregator (Option 2).

Runs on open-internet infrastructure (e.g. a GitHub Actions runner), fetches every
RSS feed in feeds.csv, keeps recent + risk-relevant items, dedups, tags each with
country + tier, and writes output/feed_items.json — the single file the RIM run
reads with one WebFetch.

No API keys required. RSS feeds are public URLs.
"""

import csv
import json
import os
import re
import socket
import hashlib
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser

# ---------------------------------------------------------------- config
FEEDS_CSV     = "feeds.csv"
OUTPUT        = "output/feed_items.json"
WINDOW_HOURS  = 30      # rolling window kept in the file; RIM narrows to 4h / 24h
MAX_WORKERS   = 20      # parallel feed fetches
PER_FEED_CAP  = 40      # max items kept per feed
TOTAL_CAP     = 600     # max items in the output file (keeps it WebFetch-friendly)
FETCH_TIMEOUT = 20      # seconds per feed
KEYWORD_FILTER = True   # True = keep only risk-relevant items; False = keep all recent

# Risk keywords (multilingual starter set — expand freely).
KEYWORDS = [
    # English
    "protest", "strike", "attack", "bomb", "explosion", "shooting", "clash", "riot",
    "unrest", "coup", "election", "terror", "militant", "hostage", "kidnap", "cyber",
    "ransomware", "hack", "breach", "outbreak", "epidemic", "earthquake", "flood",
    "storm", "wildfire", "evacuat", "sanction", "corruption", "fraud", "assassinat",
    "curfew", "border", "military", "airstrike", "missile", "drone", "killed",
    "wounded", "arrest", "crackdown", "blackout", "outage", "strike", "protest",
    # Arabic
    "احتجاج", "إضراب", "هجوم", "انفجار", "اشتباك", "إرهاب", "اختطاف", "زلزال",
    "فيضان", "عقوبات", "قتيل", "اعتقال",
    # French
    "grève", "attentat", "manifestation", "émeute", "explosion", "otage", "séisme",
    "inondation", "cyberattaque", "sanctions", "couvre-feu",
    # Turkish
    "protesto", "saldırı", "patlama", "grev", "deprem", "siber", "gözaltı",
]
KW_RE = re.compile("|".join(re.escape(k) for k in KEYWORDS), re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


def parse_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def fetch(row):
    url = (row.get("url") or "").strip()
    meta = {"source": row.get("source_name"), "country": row.get("country"),
            "tier": row.get("tier"), "url": url}
    try:
        d = feedparser.parse(url)
        if getattr(d, "bozo", 0) and not d.entries:
            exc = getattr(d, "bozo_exception", "")
            return {"ok": False, "reason": f"parse error: {str(exc)[:120]}", "meta": meta, "items": []}
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
                "source": meta["source"], "country": meta["country"], "tier": meta["tier"],
                "title": title, "link": link,
                "published": pub.isoformat() if pub else None,
                "summary": summary,
            })
        return {"ok": True, "meta": meta, "items": items[:PER_FEED_CAP]}
    except Exception as ex:
        return {"ok": False, "reason": str(ex)[:160], "meta": meta, "items": []}


def main():
    socket.setdefaulttimeout(FETCH_TIMEOUT)
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

    all_items = []
    for r in ok:
        all_items.extend(r["items"])

    # dedup by title+link, newest first
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
        "item_count": len(dedup),
        "failures": [{"source": r["meta"]["source"], "url": r["meta"]["url"],
                      "reason": r["reason"]} for r in failed],
        "items": dedup,
    }
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"wrote {OUTPUT}: {len(dedup)} items | feeds ok {len(ok)} / failed {len(failed)}")


if __name__ == "__main__":
    main()
