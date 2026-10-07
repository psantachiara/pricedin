#!/usr/bin/env python3
"""Backfill data/events.json from archived copies of the news feeds.

The live feeds only carry two or three weeks. The Internet Archive has been
crawling them for years, and every snapshot is a full copy of the feed, so
replaying the snapshots recovers the items that have since scrolled off.

  python scripts/backfill_news.py --probe              what the archive holds, by month
  python scripts/backfill_news.py --dry-run --report   what would be added
  python scripts/backfill_news.py --from 2025-01-01    add it

Snapshots overlap heavily, so items are de-duplicated by feed ID. Everything
added carries "backfill": true, which makes a bad run easy to undo:

  python scripts/backfill_news.py --undo

Each run also writes data/coverage.json, recording which weeks the archive
actually covers, so the site can show where the record is thin.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import requests

from fetch_news import (
    after_close, canonical, clean_text, compile_terms, classify,
    load, save, slug, warn, MEDIA_NS,
)

LOOKBACK_DAYS = 14   # how far back a snapshot of these feeds still reaches

CDX = "http://web.archive.org/cdx/search/cdx"
SNAPSHOT = "https://web.archive.org/web/{timestamp}id_/{url}"
UA = {"User-Agent": "priced-in-backfill/1.0 (github pages site; one-off historical import)"}

# Feed addresses change over time; the archive still holds the old ones.
def legacy_urls(url):
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return [f"https://feeds.a.dj.com/rss/{name}.xml", f"http://feeds.a.dj.com/rss/{name}.xml"]


_throttle = threading.Semaphore(1)
_counter_lock = threading.Lock()
_pool_lock = threading.Lock()


def get(url, params=None, timeout=60, tries=4, pause=1.0):
    """archive.org rate-limits; back off and retry rather than giving up."""
    for attempt in range(tries):
        try:
            response = requests.get(url, params=params, headers=UA, timeout=timeout)
        except requests.RequestException:
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


def captures(url, start, end, per_day, window_days=120):
    """List archived snapshots of one feed address, at most per_day each day.

    The listing is requested in windows. A busy feed has a very large index,
    and asking for two years at once times out; a window that fails costs
    only its own slice instead of the whole feed.
    """
    rows, cursor, failures = [], start, []
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=window_days))
        params = {
            "url": url,
            "output": "json",
            "filter": "statuscode:200",
            "collapse": "timestamp:10",
            "fl": "timestamp,original",
            "from": cursor.strftime("%Y%m%d"),
            "to": stop.strftime("%Y%m%d"),
        }
        try:
            page = get(CDX, params, timeout=180, tries=5).json()
        except (requests.RequestException, ValueError, RuntimeError) as err:
            failures.append(f"{cursor:%Y-%m-%d}..{stop:%Y-%m-%d} ({type(err).__name__})")
            cursor = stop + timedelta(days=1)
            continue
        if page and len(page) > 1:
            rows.extend(page[1:])
        cursor = stop + timedelta(days=1)

    if failures:
        warn(f"Could not list {len(failures)} window(s) of {url}: {', '.join(failures)}")

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
    return [feed["url"]] + legacy_urls(feed["url"])


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
        try:
            published = parsedate_to_datetime(node.findtext("pubDate") or "")
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
            "feed": feed.get("name"),
            "allow": feed.get("categories"),
        })
    return items


def months_between(start, end):
    out, cursor = [], start.replace(day=1)
    while cursor <= end:
        out.append(f"{cursor:%Y-%m}")
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def probe(rules, start, end, per_day):
    """Report, month by month, how many days each feed has snapshots for."""
    months = months_between(start, end)
    grid, totals = {}, {}
    for feed in rules.get("feeds", []):
        days = set()
        for url in feed_urls(feed):
            try:
                rows = captures(url, start, end, per_day=99)
            except (requests.RequestException, ValueError) as err:
                warn(f"Could not check {url}: {type(err).__name__}")
                continue
            days.update(t[:8] for t, _ in rows)
        grid[feed["name"]] = days
        totals[feed["name"]] = len(days)

    names = list(grid)
    width = max((len(n) for n in names), default=10)
    print(f"\nDays with an archived snapshot, by month "
          f"(out of about 30; a feed only needs one snapshot every {14} days to stay complete)\n")
    print("  month   " + "".join(f"{n[:width]:>{max(12, len(n) + 2)}}" for n in names) + "   any")
    for month in months:
        cells, any_day = [], set()
        for name in names:
            hit = {d for d in grid[name] if d[:4] + "-" + d[4:6] == month}
            any_day |= hit
            cells.append(f"{len(hit):>{max(12, len(name) + 2)}}")
        flag = "" if any_day else "   <- nothing"
        print(f"  {month} " + "".join(cells) + f"{len(any_day):>6}{flag}")
    print("\n  total   " + "".join(f"{totals[n]:>{max(12, len(n) + 2)}}" for n in names))
    covered = set().union(*grid.values()) if grid else set()
    print(f"\n{len(covered)} distinct days covered by at least one feed.")
    est = sum(min(len(d), 10 ** 9) for d in grid.values()) * per_day
    print(f"A run at --per-day {per_day} would fetch roughly {est} snapshots.")


def write_coverage(read_by_feed, start, end, lookback=LOOKBACK_DAYS):
    """Record, per week, how much of it the source reaches and which feeds did.

    A week covered only by one desk is not the same record as a week covered
    by all four, so the strip can say which.
    """
    def reach(days):
        out = set()
        for day in days:
            for back in range(lookback + 1):
                out.add(day - timedelta(days=back))
        return out

    per_feed = {name: reach(days) for name, days in read_by_feed.items()}
    everything = set().union(*per_feed.values()) if per_feed else set()

    existing = load("coverage.json", {}) or {}
    names = list(existing.get("feeds") or [])
    for name in sorted(per_feed):
        if name not in names:
            names.append(name)

    weeks, cursor = {}, start.date() - timedelta(days=start.weekday())
    last = end.date()
    while cursor <= last:
        days = [cursor + timedelta(days=i) for i in range(7)]
        in_range = [d for d in days if start.date() <= d <= last]
        if in_range:
            share = sum(1 for d in in_range if d in everything) / len(in_range)
            contributors = [
                names.index(name) for name in sorted(per_feed)
                if sum(1 for d in in_range if d in per_feed[name]) / len(in_range) >= 0.5
            ]
            weeks[cursor.isoformat()] = {"share": round(share, 3), "feeds": sorted(contributors)}
        cursor += timedelta(days=7)

    # A later run may cover only some feeds, so coverage is merged rather than
    # replaced: a week keeps the best share and the union of contributors.
    merged = {}
    for week, value in (existing.get("weeks") or {}).items():
        merged[week] = ({"share": value, "feeds": []} if isinstance(value, (int, float))
                        else {"share": value.get("share", 0), "feeds": list(value.get("feeds") or [])})
    for week, value in weeks.items():
        old_week = merged.get(week, {"share": 0, "feeds": []})
        merged[week] = {
            "share": max(value["share"], old_week["share"]),
            "feeds": sorted(set(old_week["feeds"]) | set(value["feeds"])),
        }

    existing.update({
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "lookbackDays": lookback,
        "feeds": names,
        "weeks": merged,
    })
    save("coverage.json", existing)

    thin = sum(1 for v in merged.values() if v["share"] < 0.5)
    solo = sum(1 for v in merged.values() if len(v["feeds"]) == 1)
    print(f"Wrote coverage for {len(merged)} weeks; {thin} less than half covered, "
          f"{solo} resting on a single feed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="start", default="2025-01-01",
                        help="earliest date to import (default 2025-01-01)")
    parser.add_argument("--to", dest="end", default=None, help="latest date (default today)")
    parser.add_argument("--per-day", type=int, default=2,
                        help="snapshots to replay per day (default 2)")
    parser.add_argument("--pause", type=float, default=1.0,
                        help="seconds between archive requests, per worker (default 1.0)")
    parser.add_argument("--workers", type=int, default=4,
                        help="parallel archive requests (default 4; raise with care)")
    parser.add_argument("--feeds", default=None,
                        help="comma-separated feed names to use (default all)")
    parser.add_argument("--probe", action="store_true", help="report what the archive holds and stop")
    parser.add_argument("--dry-run", action="store_true", help="report without writing files")
    parser.add_argument("--report", action="store_true", help="also list what was filtered out")
    parser.add_argument("--max-snapshots", type=int, default=0, help="stop after this many (0 = no limit)")
    parser.add_argument("--undo", action="store_true", help="remove every event added by a backfill")
    args = parser.parse_args()

    rules = load("news_rules.json")
    if not rules:
        sys.exit("data/news_rules.json is missing.")
    events = load("events.json", []) or []

    if args.undo:
        kept = [e for e in events if not e.get("backfill")]
        removed = len(events) - len(kept)
        seen = {k: v for k, v in (load("news_seen.json", {}) or {}).items() if not v.get("backfill")}
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

    feeds = rules.get("feeds", [])
    if args.feeds:
        wanted = {n.strip().lower() for n in args.feeds.split(",")}
        feeds = [f for f in feeds if f["name"].lower() in wanted]
        if not feeds:
            sys.exit(f"No feed matched {args.feeds}")

    if args.probe:
        probe({"feeds": feeds}, start, end, args.per_day)
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

    # ---- list every snapshot to fetch -----------------------------------
    jobs = []
    for feed in feeds:
        print(f"Listing archived snapshots for {feed['name']}...", flush=True)
        for url in feed_urls(feed):
            try:
                found = captures(url, start, end + timedelta(days=LOOKBACK_DAYS), args.per_day)
            except (requests.RequestException, ValueError) as err:
                warn(f"Could not list snapshots of {url}: {type(err).__name__}")
                continue
            if found:
                print(f"  {len(found)} snapshots of {url}", flush=True)
                jobs.extend((feed, t, o) for t, o in found)
        if not any(j[0] is feed for j in jobs):
            warn(f"{feed['name']} contributed no snapshots to this run.")
    jobs.sort(key=lambda j: j[1])
    if args.max_snapshots:
        jobs = jobs[:args.max_snapshots]
    if not jobs:
        print("No archived snapshots found; events.json left unchanged.")
        return
    print(f"\nFetching {len(jobs)} snapshots with {args.workers} workers...", flush=True)

    # ---- fetch them in parallel -----------------------------------------
    pool, failed, done = {}, 0, 0
    read_by_feed = {}

    def fetch(job):
        feed, timestamp, original = job
        with _throttle:
            time.sleep(args.pause / max(1, args.workers))
        response = get(SNAPSHOT.format(timestamp=timestamp, url=original),
                       timeout=60, pause=args.pause)
        return feed["name"], timestamp, parse_snapshot(response.content, feed)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(fetch, job): job for job in jobs}
        for future in as_completed(futures):
            with _counter_lock:
                done += 1
                n = done
            try:
                feed_name, timestamp, items = future.result()
            except (requests.RequestException, RuntimeError):
                failed += 1
                continue
            read_by_feed.setdefault(feed_name, set()).add(
                datetime.strptime(timestamp[:8], "%Y%m%d").date())
            # Workers merge into one pool, so the read and the write must be
            # one step: otherwise a restricted feed can overwrite the broader
            # one that another thread just stored.
            with _pool_lock:
                for item in items:
                    current = pool.get(item["guid"])
                    if current is None or (current.get("allow") and not item.get("allow")):
                        pool[item["guid"]] = item
            if n % 50 == 0 or n == len(jobs):
                print(f"  {n}/{len(jobs)} snapshots, {len(pool)} distinct items so far", flush=True)

    if failed:
        warn(f"{failed} snapshots could not be read and were skipped.")
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
            elif item.get("allow") and tagged[2] not in item["allow"]:
                reason = f"{tagged[2]} not carried by {item.get('feed') or 'this feed'}"

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

    print(f"\nRead {len(jobs) - failed} snapshots holding {len(pool)} distinct items: "
          f"{len(added)} added, {len(dropped)} filtered out.")
    if added:
        months = {}
        for event in added:
            months[event["date"][:7]] = months.get(event["date"][:7], 0) + 1
        peak = max(months.values())
        print("\nAdded per month:")
        for month in months_between(start, end):
            n = months.get(month, 0)
            bar = "#" * round(n / peak * 50) if peak else ""
            print(f"  {month}  {n:4d}  {bar}")
    if args.report and dropped:
        counts = {}
        for reason, _ in dropped:
            counts[reason] = counts.get(reason, 0) + 1
        print("\nFiltered out:")
        for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {n:5d}  {reason}")

    if args.dry_run:
        print("\nDry run: no files were written.")
        return

    save("events.json", events, indent=2)
    save("news_seen.json", seen)
    write_coverage(read_by_feed, start, end)
    print(f"events.json now holds {len(events)} announcements.")


if __name__ == "__main__":
    main()
