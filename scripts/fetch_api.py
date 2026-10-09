#!/usr/bin/env python3
"""Read announcements from publishers' own archives rather than from snapshots.

The archived-RSS route in backfill_news.py can only recover what the Internet
Archive happened to crawl, and its crawl schedule has nothing to do with the
news: whole months of 2025 are missing from feeds that were captured daily a
few months later. Anything built on it inherits that shape.

These two publishers expose their own indexes, so a query returns what they
published rather than what somebody else copied. Coverage is even because the
index is complete, and the same call answers for January 2025 and for
yesterday -- there is no backfill boundary to defend.

  NYT       one request returns every article in a month, back to 1851
  Guardian  a date range, paged, back to 1999

Both need a free key, read from the environment:

  export NYT_API_KEY=...        developer.nytimes.com  (enable the Archive API)
  export GUARDIAN_API_KEY=...   open-platform.theguardian.com/access

  python scripts/fetch_api.py --from 2025-01-01 --dry-run --report
  python scripts/fetch_api.py --from 2025-01-01
  python scripts/fetch_api.py --source nyt --from 2026-01-01

Items are filtered and tagged by exactly the rules in data/news_rules.json that
the RSS path uses, so a story qualifies on the same terms whichever source it
came through. Nothing written here carries "backfill", so the archive undo
cannot touch it.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from fetch_news import (
    after_close, canonical, clean_text, compile_terms, classify,
    load, save, slug, warn,
)

NYT_ARCHIVE = "https://api.nytimes.com/svc/archive/v1/{year}/{month}.json"
GUARDIAN_SEARCH = "https://content.guardianapis.com/search"
UA = {"User-Agent": "priced-in/1.0 (github pages site; research use)"}

# The free tiers are 500 requests a day each; the Times also publishes a
# 5-per-minute ceiling. One NYT call covers a whole month, so a two-year run is
# about two dozen requests and the minute limit is what binds.
NYT_PAUSE = 13.0
GUARDIAN_PAUSE = 1.0

# Sections worth reading. Everything still has to pass the entity and category
# rules afterwards; this only keeps us from paging through sport and culture.
NYT_SECTIONS = {
    "Technology", "Business", "Business Day", "Science", "U.S.", "World",
    "Briefing", "The Upshot",
}
GUARDIAN_SECTIONS = "technology|business|world|us-news|science"


def month_starts(start, end):
    out, cursor = [], start.replace(day=1)
    while cursor <= end:
        out.append((cursor.year, cursor.month))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def get(url, params, pause, tries=5):
    """One request, retried on throttling and server trouble."""
    for attempt in range(tries):
        time.sleep(pause if attempt == 0 else pause * 2 ** attempt)
        try:
            response = requests.get(url, params=params, headers=UA, timeout=60)
        except requests.RequestException:
            if attempt == tries - 1:
                raise
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == tries - 1:
                response.raise_for_status()
            continue
        if response.status_code in (401, 403):
            raise SystemExit(
                f"{url} refused the key ({response.status_code}). Check the key is "
                f"right and, for the Times, that the Archive API is enabled for it."
            )
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"gave up on {url}")


def nyt_image(doc):
    """A usable image URL, across the shapes the Archive API has used."""
    media = doc.get("multimedia")
    if isinstance(media, dict):                      # newer: {"default": {...}}
        for key in ("default", "thumbnail"):
            url = (media.get(key) or {}).get("url")
            if url:
                return url
    if isinstance(media, list):                      # older: [{"url": "images/..."}]
        for entry in media:
            url = (entry or {}).get("url")
            if not url:
                continue
            return url if url.startswith("http") else f"https://static01.nyt.com/{url}"
    return ""


def from_nyt(start, end, key):
    """Every article the Times published in each month of the range."""
    items = []
    months = month_starts(start, end)
    print(f"NYT: {len(months)} month(s) to read, one request each.", flush=True)
    for n, (year, month) in enumerate(months, 1):
        payload = get(NYT_ARCHIVE.format(year=year, month=month),
                      {"api-key": key}, NYT_PAUSE)
        docs = (payload.get("response") or {}).get("docs") or []
        kept = 0
        for doc in docs:
            section = doc.get("section_name") or ""
            if NYT_SECTIONS and section not in NYT_SECTIONS:
                continue
            raw = doc.get("pub_date") or ""
            try:
                published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            published = published.astimezone(timezone.utc)
            if not (start <= published <= end):
                continue
            title = clean_text((doc.get("headline") or {}).get("main"))
            url = doc.get("web_url") or ""
            if not title or not url:
                continue
            items.append({
                "title": title,
                "summary": clean_text(doc.get("abstract") or doc.get("snippet") or ""),
                "url": url,
                "published": published,
                "image": nyt_image(doc),
                "paywall": True,
                "source": "NYT",
                "guid": doc.get("_id") or url,
            })
            kept += 1
        print(f"  [{n}/{len(months)}] {year}-{month:02d}  {len(docs):5d} published, "
              f"{kept:4d} in scope", flush=True)
    return items


def from_guardian(start, end, key, terms):
    """Guardian articles in the range that mention any tracked company.

    The search is only a way to avoid paging the whole paper: anything it
    returns still has to carry a tracked company in its headline to be kept, so
    the bar is the same one every other source is held to.
    """
    items, page, pages = [], 1, 1
    query = " OR ".join(sorted({t.strip('*') for t in terms if len(t.strip('*')) > 2}))
    print("Guardian: paging the date range.", flush=True)
    while page <= pages:
        payload = get(GUARDIAN_SEARCH, {
            "api-key": key,
            "from-date": start.strftime("%Y-%m-%d"),
            "to-date": end.strftime("%Y-%m-%d"),
            "section": GUARDIAN_SECTIONS,
            "q": query,
            "page": page,
            "page-size": 50,
            "order-by": "oldest",
            "show-fields": "trailText,thumbnail",
        }, GUARDIAN_PAUSE)
        block = payload.get("response") or {}
        if block.get("status") != "ok":
            warn(f"Guardian returned status {block.get('status')!r}; stopping.")
            break
        pages = block.get("pages") or 1
        results = block.get("results") or []
        for doc in results:
            raw = doc.get("webPublicationDate") or ""
            try:
                published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            published = published.astimezone(timezone.utc)
            if not (start <= published <= end):
                continue
            fields = doc.get("fields") or {}
            title = clean_text(doc.get("webTitle"))
            url = doc.get("webUrl") or ""
            if not title or not url:
                continue
            items.append({
                "title": title,
                "summary": clean_text(fields.get("trailText") or ""),
                "url": url,
                "published": published,
                "image": fields.get("thumbnail") or "",
                "paywall": False,
                "source": "Guardian",
                "guid": doc.get("id") or url,
            })
        print(f"  page {page}/{pages}  {len(results)} results, {len(items)} kept so far",
              flush=True)
        page += 1
    return items


def record_sources(names, start, end):
    """Note that these sources are complete over the range, not sampled.

    The strip marks stretches where the record is thin. That question only
    applies to a source rebuilt from archived snapshots; for one read from the
    publisher's own index the answer is that everything published is here, so
    the range is recorded and no weekly shares are kept.
    """
    coverage = load("coverage.json", {}) or {}
    sources = coverage.setdefault("sources", {})
    for name in names:
        entry = sources.setdefault(name, {"kind": "index"})
        entry["kind"] = "index"
        first = entry.get("from")
        begins = start.strftime("%Y-%m-%d")
        entry["from"] = min(first, begins) if first else begins
        ends = end.strftime("%Y-%m-%d")
        entry["to"] = max(entry.get("to", ""), ends)
    save("coverage.json", coverage)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="start", default="2025-01-01",
                        help="earliest date (default 2025-01-01)")
    parser.add_argument("--to", dest="end", default=None, help="latest date (default today)")
    parser.add_argument("--source", choices=["nyt", "guardian", "both"], default="both")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--report", action="store_true", help="also list what was filtered out")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = (datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
           if args.end else datetime.now(timezone.utc))
    if start > end:
        sys.exit("--from is after --to.")

    rules = load("news_rules.json")
    if not rules:
        sys.exit("data/news_rules.json is missing.")
    events = load("events.json", []) or []
    seen = load("news_seen.json", {}) or {}
    config = load("config.json", {}) or {}
    valid = {t["symbol"] for t in config.get("tickers", [])}

    entity_res = [(e, compile_terms(e["match"])) for e in rules.get("entities", [])]
    category_res = [(c["id"], compile_terms(c["match"])) for c in rules.get("categories", [])]
    terms = [t for e in rules.get("entities", []) for t in e["match"]]

    batch, used = [], []
    if args.source in ("nyt", "both"):
        key = os.environ.get("NYT_API_KEY")
        if not key:
            sys.exit("NYT_API_KEY is not set.")
        batch += from_nyt(start, end, key)
        used.append("NYT")
    if args.source in ("guardian", "both"):
        key = os.environ.get("GUARDIAN_API_KEY")
        if not key:
            sys.exit("GUARDIAN_API_KEY is not set.")
        batch += from_guardian(start, end, key, terms)
        used.append("Guardian")

    known_urls = {canonical(e["url"]) for e in events if e.get("url")}
    known_ids = {e["id"] for e in events}
    excluded_paths = tuple(rules.get("excludePaths", []))
    excluded_prefixes = tuple(p.lower() for p in rules.get("excludeSummaryPrefixes", []))
    excluded_titles = compile_terms(rules.get("excludeTitlePatterns", []))

    added, dropped = [], []
    for item in sorted(batch, key=lambda i: i["published"]):
        key, url = item["guid"], canonical(item["url"])
        if key in seen or url in known_urls:
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
            seen[key] = {"url": url, "dropped": reason}
            continue

        org, tickers, category = tagged
        if valid:
            tickers = [t for t in tickers if t in valid]

        event = {
            "id": slug(item["title"], item["published"], known_ids),
            "date": item["published"].strftime("%Y-%m-%d"),
            "title": item["title"],
            "source": item["source"],
            "url": item["url"],
            "org": org,
            "category": category,
            "tickers": tickers,
        }
        if item["summary"]:
            event["summary"] = item["summary"][:240]
        if after_close(item["published"]):
            event["afterClose"] = True
        if item["image"]:
            event["thumbnail"] = item["image"]
        if item["paywall"]:
            event["paywall"] = True

        events.append(event)
        known_urls.add(url)
        known_ids.add(event["id"])
        seen[key] = {"url": url, "added": event["id"]}
        added.append(event)

    events.sort(key=lambda e: (e.get("date", ""), e.get("title", "")))

    print(f"\nRead {len(batch)} articles from {', '.join(used)}: "
          f"{len(added)} added, {len(dropped)} filtered out.")
    if added:
        months = {}
        for event in added:
            months[event["date"][:7]] = months.get(event["date"][:7], 0) + 1
        peak = max(months.values())
        print("\nAdded per month:")
        for month in sorted(months):
            n = months[month]
            print(f"  {month}  {n:4d}  {'#' * round(n / peak * 50)}")
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
    record_sources(used, start, end)
    print(f"events.json now holds {len(events)} announcements.")


if __name__ == "__main__":
    main()
