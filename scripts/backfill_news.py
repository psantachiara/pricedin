#!/usr/bin/env python3
"""Backfill data/events.json from archived copies of the news feeds.

The live feeds only carry two or three weeks. The Internet Archive has been
crawling them for years, and every snapshot is a full copy of the feed, so
replaying the snapshots recovers the items that have since scrolled off.

  python scripts/backfill_news.py --probe              what the archive holds
  python scripts/backfill_news.py --dry-run --report   what would be added
  python scripts/backfill_news.py --from 2025-01-01    add it

Snapshots overlap heavily, so items are de-duplicated by feed ID. Everything
added carries "backfill": true, which makes a bad run easy to undo:

  python scripts/backfill_news.py --undo
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from xml.etree import ElementTree

import requests

from fetch_news import (
    DATA, after_close, canonical, clean_text, compile_terms, classify,
    load, save, slug, warn, MEDIA_NS,
)

CDX = "http://web.archive.org/cdx/search/cdx"
SNAPSHOT = "https://web.archive.org/web/{timestamp}id_/{url}"
UA = {"User-Agent": "priced-in-backfill/1.0 (github pages site; one-off historical import)"}

# Feed addresses change over time; the archive still holds the old ones.
LEGACY_URLS = {
    "https://feeds.content.dowjones.io/public/rss/RSSWSJD": [
        "https://feeds.a.dj.com/rss/RSSWSJD.xml",
        "http://feeds.a.dj.com/rss/RSSWSJD.xml",
    ],
}


def get(url, params=None, timeout=60, tries=4, pause=2.0):
    """archive.org rate-limits; back off and retry rather than giving up."""
    for attempt in range(tries):
        try:
            response = requests.get(url, params=params, headers=UA, timeout=timeout)
        except requests.RequestException as err:
            if attempt == tries - 1:
                raise
            time.sleep(pause * (attempt + 2))
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == tries - 1:
                response.raise_for_status()
            time.sleep(pause * (attempt + 2) * 2)
            continue
        response.raise_for_status()
        return response
    raise RuntimeError(f"gave up on {url}")


def captures(url, start, end, per_day):
    """List archived snapshots of one feed address, at most per_day each day."""
    params = {
        "url": url,
        "output": "json",
        "filter": "statuscode:200",
        "collapse": f"timestamp:{10 if per_day > 1 else 8}",
        "fl": "timestamp,original",
        "from": start.strftime("%Y%m%d"),
        "to": end.strftime("%Y%m%d"),
    }
    rows = get(CDX, params).json()
    if not rows or len(rows) < 2:
        return []
    rows = rows[1:]  # first row is the header

    by_day = {}
    for timestamp, original in rows:
        by_day.setdefault(timestamp[:8], []).append((timestamp, original))
    out = []
    for day in sorted(by_day):
        day_rows = by_day[day]
        if len(day_rows) <= per_day:
            out.extend(day_rows)
        else:  # spread the picks across the day rather than taking the first few
            step = len(day_rows) / per_day
            out.extend(day_rows[int(i * step)] for i in range(per_day))
    return out


def feed_urls(feed):
    return [feed["url"]] + LEGACY_URLS.get(feed["url"], [])


def parse_snapshot(xml, feed):
    """Same shape as the live fetcher's parse_feed, from archived bytes."""
    items = []
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return items
    for node in root.iter("item"):
        link = (node.findtext("link") or "").strip()
        title = clean_text(node.findtext("title"))
        if not link or not title:
            continue
        raw_date = node.findtext("pubDate") or ""
        try:
            from email.utils import parsedate_to_datetime
            published = parsedate_to_datetime(raw_date)
        except (TypeError, ValueError):
            continue
        if published is None:
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        media = node.find(MEDIA_NS)
        items.append({
            "guid": (node.findtext("guid") or link).strip(),
            "title": title,
            "summary": clean_text(node.findtext("description")),
            "url": link,
            "published": published.astimezone(timezone.utc),
            "image": (media.get("url") if media is not None else None),
            "source": feed.get("source") or feed.get("name"),
            "paywall": bool(feed.get("paywall")),
        })
    return items


def probe(rules, start, end):
    print(f"Archived snapshots between {start:%Y-%m-%d} and {end:%Y-%m-%d}\n")
    total = 0
    for feed in rules.get("feeds", []):
        for url in feed_urls(feed):
            try:
                rows = captures(url, start, end, per_day=99)
            except (requests.RequestException, ValueError) as err:
                print(f"  {url}\n    could not be checked ({type(err).__name__})")
                continue
            if not rows:
                print(f"  {url}\n    no snapshots")
                continue
            days = sorted({t[:8] for t, _ in rows})
            total += len(rows)
            span_days = (end - start).days or 1
            print(f"  {url}")
            print(f"    {len(rows)} snapshots across {len(days)} days "
                  f"({days[0]} to {days[-1]}, about {len(days) / span_days * 7:.1f} days covered per week)")
    print(f"\n{total} snapshots in all.")
    if total:
        print("Each one holds roughly 40 items, heavily overlapping. Run with --dry-run --report\n"
              "to see how many distinct announcements survive the filter.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="start", default="2025-01-01",
                        help="earliest date to import (default 2025-01-01)")
    parser.add_argument("--to", dest="end", default=None, help="latest date (default today)")
    parser.add_argument("--per-day", type=int, default=2,
                        help="snapshots to replay per day (default 2; more is slower, "
                             "and catches items that appeared and scrolled off between crawls)")
    parser.add_argument("--pause", type=float, default=1.5,
                        help="seconds between archive requests (default 1.5)")
    parser.add_argument("--probe", action="store_true",
                        help="report what the archive holds and stop")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be added without writing files")
    parser.add_argument("--report", action="store_true", help="also list what was filtered out")
    parser.add_argument("--max-snapshots", type=int, default=0,
                        help="stop after this many snapshots (0 means no limit)")
    parser.add_argument("--undo", action="store_true",
                        help="remove every event previously added by a backfill")
    args = parser.parse_args()

    rules = load("news_rules.json")
    if not rules:
        sys.exit("data/news_rules.json is missing.")
    events = load("events.json", []) or []

    if args.undo:
        kept = [e for e in events if not e.get("backfill")]
        removed = len(events) - len(kept)
        seen = load("news_seen.json", {}) or {}
        seen = {k: v for k, v in seen.items() if not v.get("backfill")}
        if args.dry_run:
            print(f"Would remove {removed} backfilled events.")
            return
        save("events.json", kept, indent=2)
        save("news_seen.json", seen)
        print(f"Removed {removed} backfilled events; {len(kept)} remain.")
        return

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = (datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
           if args.end else datetime.now(timezone.utc))

    if args.probe:
        probe(rules, start, end)
        return

    seen = load("news_seen.json", {}) or {}
    entity_res = [(e, compile_terms(e["match"])) for e in rules.get("entities", [])]
    category_res = [(c["id"], compile_terms(c["match"])) for c in rules.get("categories", [])]
    excluded_paths = tuple(rules.get("excludePaths", []))
    excluded_prefixes = tuple(p.lower() for p in rules.get("excludeSummaryPrefixes", []))
    excluded_titles = compile_terms(rules.get("excludeTitlePatterns", []))

    known_ids = {e.get("id") for e in events}
    known_urls = {canonical(e["url"]) for e in events if e.get("url")}
    config = load("config.json")
    valid_tickers = {t["symbol"] for t in config.get("tickers", [])} if config else None

    # ---- collect every archived item, de-duplicated by feed ID ----------
    pool, snapshots_read, snapshots_failed = {}, 0, 0
    for feed in rules.get("feeds", []):
        rows = []
        for url in feed_urls(feed):
            try:
                found = captures(url, start, end, args.per_day)
            except (requests.RequestException, ValueError) as err:
                warn(f"Could not list snapshots of {url}: {type(err).__name__}")
                continue
            if found:
                print(f"{len(found)} snapshots of {url}")
            rows.extend((t, o) for t, o in found)

        rows.sort()
        if args.max_snapshots:
            rows = rows[:args.max_snapshots]
        for n, (timestamp, original) in enumerate(rows, 1):
            try:
                response = get(SNAPSHOT.format(timestamp=timestamp, url=original),
                               timeout=60, pause=args.pause)
                found = parse_snapshot(response.content, feed)
            except (requests.RequestException, RuntimeError):
                snapshots_failed += 1
                continue
            snapshots_read += 1
            for item in found:
                pool.setdefault(item["guid"], item)
            if n % 25 == 0 or n == len(rows):
                print(f"  read {n}/{len(rows)} snapshots, {len(pool)} distinct items so far")
            time.sleep(args.pause)

    if snapshots_failed:
        warn(f"{snapshots_failed} snapshots could not be read and were skipped.")
    if not pool:
        print("No archived items were recovered; events.json left unchanged.")
        return

    # ---- same filter and tagging as the daily fetcher --------------------
    added, dropped = [], []
    for item in sorted(pool.values(), key=lambda i: i["published"]):
        key, url = item["guid"], canonical(item["url"])
        if key in seen or url in known_urls:
            continue
        if not (start <= item["published"] <= end):
            continue

        reason = None
        if any(part in url for part in excluded_paths):
            reason = "section excluded"
        elif excluded_prefixes and item["summary"].lower().startswith(excluded_prefixes):
            reason = "newsletter roundup"
        elif any(p.search(item["title"]) for p in excluded_titles):
            reason = "column or review"
        else:
            tagged = classify(item, rules, entity_res, category_res)
            if not tagged:
                reason = "no tracked company in the headline"

        if reason:
            dropped.append((reason, item))
            seen[key] = {"url": url, "dropped": reason, "backfill": True}
            continue

        org, tickers, category = tagged
        if valid_tickers:
            tickers = [t for t in tickers if t in valid_tickers]

        event = {
            "id": slug(item["title"], item["published"], known_ids),
            "date": item["published"].strftime("%Y-%m-%d"),
            "title": item["title"],
            "source": item["source"],
            "url": item["url"],
            "org": org,
            "category": category,
            "tickers": tickers,
            "backfill": True,
        }
        if after_close(item["published"]):
            event["afterClose"] = True
        if item["image"]:
            event["thumbnail"] = item["image"]
        if item["paywall"]:
            event["paywall"] = True

        events.append(event)
        known_urls.add(url)
        seen[key] = {"url": url, "added": event["id"], "backfill": True}
        added.append(event)

    events.sort(key=lambda e: (e.get("date", ""), e.get("title", "")))

    print(f"\nRead {snapshots_read} snapshots holding {len(pool)} distinct items: "
          f"{len(added)} added, {len(dropped)} filtered out.")
    if added:
        months = {}
        for event in added:
            months[event["date"][:7]] = months.get(event["date"][:7], 0) + 1
        print("\nAdded per month:")
        for month in sorted(months):
            print(f"  {month}  {months[month]:4d}  {'#' * min(60, months[month])}")
    if args.report:
        print("\nFiltered out:")
        counts = {}
        for reason, _ in dropped:
            counts[reason] = counts.get(reason, 0) + 1
        for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {n:5d}  {reason}")

    if args.dry_run:
        print("\nDry run: no files were written.")
        return

    save("events.json", events, indent=2)
    save("news_seen.json", seen)
    print(f"\nevents.json now holds {len(events)} announcements.")


if __name__ == "__main__":
    main()
