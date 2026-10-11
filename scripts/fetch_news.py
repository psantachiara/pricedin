#!/usr/bin/env python3
"""Add announcements to data/events.json from the news feeds in data/news_rules.json."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

import requests

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except Exception:
    EASTERN = None

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

MEDIA_NS = "{http://search.yahoo.com/mrss/}content"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; priced-in/1.0; +https://github.com)",
    "Accept": "application/rss+xml, application/xml, text/xml",
}


def load(name, default=None):
    path = DATA / name
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save(name, obj, indent=None):
    text = json.dumps(obj, ensure_ascii=False, indent=indent,
                      separators=None if indent else (",", ":"))
    (DATA / name).write_text(text + "\n", encoding="utf-8")


def warn(message):
    print(f"::warning::{message}" if IN_ACTIONS else f"warning: {message}")


def clean_text(value):
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical(url):
    """Drop the tracking query WSJ appends, so the same story matches itself."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def slug(text, published, used):
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48].strip("-")
    base = f"{published:%Y%m%d}-{base}" if base else f"{published:%Y%m%d}-item"
    candidate, n = base, 2
    while candidate in used:
        candidate, n = f"{base}-{n}", n + 1
    used.add(candidate)
    return candidate


def compile_terms(terms):
    """A term ending in * matches any word that starts with it: acquir* covers acquires.

    A space in a term matches a space OR a hyphen, because headlines hyphenate
    freely and a vocabulary cannot carry both spellings of every phrase. Without
    this, "Out-of-Control AI Fears" did not match "out of control" and the item
    was recorded as neutral.
    """
    out = []
    gap = r"[\s-]+"
    for term in terms:
        stem = term.endswith("*")
        body = re.escape(term[:-1] if stem else term)
        # a space or a hyphen in the term matches either in the text, so one
        # spelling in the vocabulary covers "out of control" and "Out-of-Control"
        body = body.replace(r"\ ", gap).replace(r"\-", gap)
        out.append(re.compile(r"(?<!\w)" + body + ("" if stem else r"(?!\w)"), re.I))
    return out


def after_close(published):
    """True when the story landed after the US close on a trading weekday."""
    if EASTERN is None:
        return published.hour >= 20
    local = published.astimezone(EASTERN)
    if local.weekday() >= 5:
        return False
    return (local.hour, local.minute) >= (16, 0)


def parse_feed(feed, timeout=30):
    response = requests.get(feed["url"], headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    items = []
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
            # optional per-feed allowlist, by TOPIC id (no feed sets one today)
            "allow": feed.get("topics"),
        })
    return items


def merge(batch):
    """One entry per story. A feed that carries every category wins over a
    restricted one, so a story both feeds ran is judged by the broader rule."""
    best = {}
    for item in batch:
        current = best.get(item["guid"])
        if current is None or (current.get("allow") and not item.get("allow")):
            best[item["guid"]] = item
    return list(best.values())


# Which coder produced an item's frames. Keyword matching is a weak instrument
# for axis 2 because framing is semantic, not lexical: the first pass fired
# `breach*` on a copyright suit and `drone*` on Amazon deliveries. So every row
# records who coded it, and the keyword pass never presents itself as the
# finished coding. Later passes overwrite frames and raise this to "llm-v1" or
# "hand"; the validation sample is only meaningful if hand-coded rows are
# distinguishable from machine-coded ones.
CODER = "keyword-v1"


def compile_axes(rules):
    """Compile the three axes once. All four entry points build this the same way
    so a rules change cannot drift between the live fetchers and the backfill."""
    return {
        "ai": compile_terms(rules.get("aiTerms", [])),
        "topics": [(t["id"], compile_terms(t["match"]))
                   for t in rules.get("topics", [])],
        "frames": [(f["id"], compile_terms(f["match"]))
                   for f in rules.get("frames", [])],
    }


def _hits(pats, text):
    """How many distinct patterns of a list match. Distinct terms, not occurrences:
    one word repeated is weaker evidence than two different words."""
    return sum(1 for p in pats if p.search(text))


def intensity(pats, title, summary):
    """1 to 3, from where the frame's language sits and how much of it there is.

    A proxy for the codebook's mentioned / substantial / dominant, and the part of
    the machine pass most in need of validation: the threshold between the event
    hypothesis and the discourse hypothesis rides on it.
    """
    in_title = _hits(pats, title)
    if in_title >= 2:
        return 3
    if in_title == 1:
        return 2
    return 1 if _hits(pats, summary) else 0


def classify(item, rules, entity_res, axes):
    """Code one item on all three axes.

    Returns a dict, or None when the item passes neither branch of the gate.
    The gate admits an item when a tracked company appears in the title OR an AI
    term appears anywhere in title or summary. Relevance is then recorded rather
    than used to discard, so the analysis sets its own threshold.
    """
    title, summary = item["title"], item.get("summary") or ""
    haystack = f"{title} {summary}"

    in_title = [e for e, pats in entity_res if any(p.search(title) for p in pats)]
    in_body = [e for e, pats in entity_res if any(p.search(haystack) for p in pats)]
    company = in_title or in_body

    ai_in_title = _hits(axes["ai"], title) > 0
    ai_in_summary = _hits(axes["ai"], summary) > 0

    if not in_title and not (ai_in_title or ai_in_summary):
        return None

    # ---- axis 0: relevance ------------------------------------------------
    if ai_in_title:
        relevance = 2
    elif ai_in_summary:
        relevance = 1
    else:
        relevance = 0

    tickers, seen = [], set()
    for entity in company:
        for symbol in entity["tickers"]:
            if symbol not in seen:
                seen.add(symbol)
                tickers.append(symbol)

    # ---- axes 1 and 2: every label the item carries, not the first match ---
    topics = [tid for tid, pats in axes["topics"] if _hits(pats, haystack)]
    frames = {}
    for fid, pats in axes["frames"]:
        level = intensity(pats, title, summary)
        if level:
            frames[fid] = level
    if not frames:
        frames = {rules.get("neutralFrame", "neutral"): 2}

    return {
        "org": company[0]["org"] if company else None,
        "tickers": tickers,
        "relevance": relevance,
        "topics": topics,
        "frames": frames,
        "coder": CODER,
        # legacy single label, kept so the current front end keeps rendering.
        # Taken from the topic axis, so danger framing no longer displaces
        # subject matter the way the old first-match-wins scheme did.
        "category": topics[0] if topics else rules.get("defaultTopic", "other"),
    }


def normalise_source(name, rules):
    """One spelling per outlet. The live path and the backfill had drifted apart,
    which would have split an outlet in two under within-source normalisation."""
    if not name:
        return name
    return rules.get("sourceAliases", {}).get(name.strip().lower(), name.strip())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be added without writing files")
    parser.add_argument("--report", action="store_true",
                        help="also list the items that were filtered out")
    args = parser.parse_args()

    rules = load("news_rules.json")
    if not rules:
        sys.exit("data/news_rules.json is missing.")
    events = load("events.json", []) or []
    seen = load("news_seen.json", {}) or {}

    entity_res = [(e, compile_terms(e["match"])) for e in rules.get("entities", [])]
    axes = compile_axes(rules)
    excluded_paths = tuple(rules.get("excludePaths", []))
    excluded_prefixes = tuple(p.lower() for p in rules.get("excludeSummaryPrefixes", []))
    excluded_titles = compile_terms(rules.get("excludeTitlePatterns", []))
    max_age = timedelta(days=rules.get("maxAgeDays", 21))
    now = datetime.now(timezone.utc)

    known_ids = {e.get("id") for e in events}
    known_urls = {canonical(e["url"]) for e in events if e.get("url")}
    valid_tickers = None
    config = load("config.json")
    if config:
        valid_tickers = {t["symbol"] for t in config.get("tickers", [])}

    added, dropped, batch = [], [], []
    live_feeds = [f for f in rules.get("feeds", []) if not f.get("backfillOnly")]
    for feed in live_feeds:
        try:
            batch.extend(parse_feed(feed))
        except (requests.RequestException, ElementTree.ParseError) as err:
            warn(f"Could not read {feed.get('name', feed['url'])}: {type(err).__name__}. "
                 "Existing announcements are unchanged.")

    if not batch:
        print("No feed items were read; events.json left unchanged.")
        return

    batch = merge(batch)
    batch.sort(key=lambda i: i["published"])
    for item in batch:
        key = item["guid"]
        url = canonical(item["url"])
        reason = None

        if key in seen or url in known_urls:
            continue
        if any(part in url for part in excluded_paths):
            reason = "section excluded"
        elif excluded_prefixes and item["summary"].lower().startswith(excluded_prefixes):
            reason = "newsletter roundup"
        elif any(p.search(item["title"]) for p in excluded_titles):
            reason = "column or review"
        elif now - item["published"] > max_age:
            reason = "older than the window"
        else:
            tagged = classify(item, rules, entity_res, axes)
            if not tagged:
                reason = "no tracked company and no AI term"
            elif item.get("allow") and tagged["category"] not in item["allow"]:
                reason = f"{tagged['category']} not carried by {item.get('feed') or 'this feed'}"

        if reason:
            dropped.append((reason, item))
            seen[key] = {"url": url, "dropped": reason}
            continue

        tickers = tagged["tickers"]
        if valid_tickers:
            tickers = [t for t in tickers if t in valid_tickers]

        event = {
            "id": slug(item["title"], item["published"], known_ids),
            "date": item["published"].strftime("%Y-%m-%d"),
            "title": item["title"],
            "source": normalise_source(item["source"], rules),
            "url": item["url"],
            "org": tagged["org"],
            "category": tagged["category"],
            "relevance": tagged["relevance"],
            "topics": tagged["topics"],
            "frames": tagged["frames"],
            "coder": tagged["coder"],
            "tickers": tickers,
        }
        if item.get("summary"):
            event["summary"] = item["summary"][:240]
        if after_close(item["published"]):
            event["afterClose"] = True
        if item["image"]:
            event["thumbnail"] = item["image"]
        if item["paywall"]:
            event["paywall"] = True

        events.append(event)
        known_urls.add(url)
        seen[key] = {"url": url, "added": event["id"]}
        added.append(event)

    events.sort(key=lambda e: (e.get("date", ""), e.get("title", "")))

    print(f"Read {len(batch)} feed items: {len(added)} added, {len(dropped)} filtered out.")
    for event in added:
        frames = ",".join(f"{k}{v}" for k, v in event["frames"].items())
        print(f"  + {event['date']}  r{event['relevance']} {event['category']:8s} "
              f"{frames[:30]:30s} {','.join(event['tickers']) or '-':14s} {event['title'][:58]}")
    if args.report:
        for reason, item in dropped:
            print(f"  - {item['published']:%Y-%m-%d}  {reason:32s} {item['title'][:72]}")

    if args.dry_run:
        print("Dry run: no files were written.")
        return

    save("events.json", events, indent=2)
    save("news_seen.json", seen)

    # From the day the live job starts, every feed is read on every publishing
    # day, so the strip can treat that stretch as fully covered by all of them.
    today = now.strftime("%Y-%m-%d")
    coverage = load("coverage.json", {}) or {}
    names = list(coverage.get("feeds") or [])
    for feed in live_feeds:
        if feed["name"] not in names:
            names.append(feed["name"])
    changed = names != list(coverage.get("feeds") or [])
    if coverage.get("liveFrom", "9999") > today:
        coverage["liveFrom"] = today
        changed = True
    coverage["feeds"] = names
    coverage["liveFeeds"] = sorted(names.index(f["name"]) for f in live_feeds)
    if changed or not (DATA / "coverage.json").exists():
        save("coverage.json", coverage)


if __name__ == "__main__":
    main()
