#!/usr/bin/env python3
"""Update data/prices.json and data/thumbnails.json."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
RECHECK_MISSING_AFTER = timedelta(days=7)


def load(name: str, default=None):
    path = DATA / name
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save(name: str, obj) -> None:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    (DATA / name).write_text(text + "\n", encoding="utf-8")


def warn(message: str) -> None:
    print(f"::warning::{message}" if IN_ACTIONS else f"warning: {message}")


def clean(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, 4)


def shares_outstanding(symbol: str) -> float | None:
    ticker = yf.Ticker(symbol)
    try:
        value = ticker.fast_info.get("shares")
        if value:
            return float(value)
    except Exception:
        pass
    try:
        value = ticker.info.get("sharesOutstanding")
        if value:
            return float(value)
    except Exception:
        pass
    return None


def update_prices(config: dict) -> None:
    tickers = [t["symbol"] for t in config["tickers"]]
    benchmarks = [b["symbol"] for b in config["benchmarks"]]
    symbols = tickers + benchmarks
    print(f"Downloading daily prices for {len(symbols)} symbols from {config['start']}")

    frame = yf.download(
        symbols,
        start=config["start"],
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if frame is None or frame.empty:
        sys.exit("No price data came back from Yahoo Finance; prices.json left unchanged.")

    closes = frame["Close"] if isinstance(frame.columns, pd.MultiIndex) else frame[["Close"]]
    if not isinstance(closes, pd.DataFrame):
        closes = closes.to_frame(name=symbols[0])
    closes = closes.dropna(how="all").sort_index()
    closes = closes.ffill(limit=3)

    missing = [s for s in symbols if s not in closes.columns or closes[s].isna().all()]
    for symbol in list(missing):
        try:
            one = yf.download(symbol, start=config["start"], auto_adjust=True,
                              progress=False, threads=False)
        except Exception:  # a retry is best-effort
            continue
        if one is not None and not one.empty and "Close" in one:
            series = one["Close"]
            if hasattr(series, "columns"):
                series = series.iloc[:, 0]
            closes[symbol] = series.reindex(closes.index)
            if not closes[symbol].isna().all():
                missing.remove(symbol)
                print(f"Recovered {symbol} on a second attempt")
    for s in missing:
        warn(f"No prices for {s}; it will be left out of the site.")

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "dates": [d.strftime("%Y-%m-%d") for d in closes.index],
        "close": {},
        "shares": {},
    }
    for s in symbols:
        if s in missing:
            continue
        out["close"][s] = [clean(v) for v in closes[s].tolist()]

    for s in tickers:
        if s in missing:
            continue
        n = shares_outstanding(s)
        if n:
            out["shares"][s] = n
        else:
            warn(f"No share count for {s}; it is left out of market-value views.")

    save("prices.json", out)
    print(f"Wrote {len(out['dates'])} trading days, {out['dates'][0]} to {out['dates'][-1]}")


META_KEYS = (
    ("property", "og:image"),
    ("property", "og:image:url"),
    ("name", "og:image"),
    ("name", "twitter:image"),
    ("property", "twitter:image"),
    ("name", "twitter:image:src"),
)


def find_image(url: str) -> tuple[int | None, str | None]:
    response = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
    if response.status_code >= 400:
        return response.status_code, None
    soup = BeautifulSoup(response.text[:800_000], "html.parser")
    for attr, key in META_KEYS:
        tag = soup.find("meta", attrs={attr: key})
        if tag and tag.get("content"):
            return response.status_code, urljoin(response.url, tag["content"].strip())
    return response.status_code, None


def needs_check(entry: dict | None, recheck: bool) -> bool:
    if recheck or not entry:
        return True
    if entry.get("image"):
        return False
    try:
        checked = datetime.fromisoformat(entry["checked"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return True
    return datetime.now(timezone.utc) - checked > RECHECK_MISSING_AFTER


def update_thumbnails(events: list[dict], recheck: bool) -> None:
    cache = load("thumbnails.json", {}) or {}
    urls = {e["url"] for e in events
            if e.get("url") and not e.get("thumbnail") and not e.get("paywall")}
    behind = sum(1 for e in events if e.get("paywall") and not e.get("thumbnail"))
    if behind:
        print(f"{behind} subscriber-only links have no preview image in the feed; "
              "those events show a lettered placeholder.")
    todo = [u for u in sorted(urls) if needs_check(cache.get(u), recheck)]
    print(f"Checking {len(todo)} of {len(urls)} event links for preview images")

    for url in todo:
        entry = {"image": None, "status": None,
                 "checked": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")}
        try:
            entry["status"], entry["image"] = find_image(url)
        except requests.RequestException as err:
            entry["error"] = type(err).__name__
        cache[url] = entry

        if entry.get("error"):
            warn(f"Could not reach {url} ({entry['error']}). Check that the link still works.")
        elif entry["status"] and entry["status"] >= 400:
            kind = "Page not found" if entry["status"] in (404, 410) else f"HTTP {entry['status']}"
            warn(f"{kind} for {url}. The site may block automated requests, or the link may be broken.")
        elif not entry["image"]:
            print(f"  no preview image on {url}; the site will show a placeholder")

    cache = {u: v for u, v in cache.items() if u in urls}
    save("thumbnails.json", cache)


def update_intake(config) -> None:
    """Record, per source, how much was pulled and how much survived the filter.

    The counts come from news_seen.json, which is the whole history rather than
    the last run, and they answer the question the source control invites: when
    a source says "every article the paper published", how much is that, and how
    much of it reached the timeline? Without it the page states the reach of a
    source without ever stating its cost.
    """
    seen = load("news_seen.json", {}) or {}
    events = load("events.json", []) or []
    coverage = load("coverage.json", {}) or {}
    if not seen or not coverage:
        return

    hosts = [(s["id"], tuple(s.get("hosts") or [])) for s in config.get("sources", [])]

    def source_of(url):
        host = urlparse(url or "").netloc.lower()
        for sid, hs in hosts:
            if any(host == h or host.endswith("." + h) for h in hs):
                return sid
        return None

    pulled, dropped = {}, {}
    for entry in seen.values():
        if not isinstance(entry, dict):
            continue
        sid = source_of(entry.get("url"))
        if not sid:
            continue
        pulled[sid] = pulled.get(sid, 0) + 1
        if entry.get("dropped"):
            dropped[sid] = dropped.get(sid, 0) + 1

    # bucket an event the way the page does: by configured source, with
    # anything hand-written folded into the curated bucket rather than becoming
    # a source of its own
    lookup, curated = {}, None
    for src in config.get("sources", []):
        if src.get("manual") or src.get("kind") == "curated":
            curated = src["id"]
            continue
        lookup[src["id"]] = src["id"]
        for name in src.get("match") or []:
            lookup[name] = src["id"]

    retained = {}
    for e in events:
        sid = curated if e.get("manual") else lookup.get(e.get("source"), curated)
        if sid:
            retained[sid] = retained.get(sid, 0) + 1

    known = {src["id"] for src in config.get("sources", [])}
    out = coverage.setdefault("sources", {})
    for sid in (pulled.keys() | retained.keys()) & known:
        row = out.setdefault(sid, {})
        # hand-picked entries were never "pulled", so report only what exists
        if sid in pulled:
            row["pulled"] = pulled[sid]
            row["dropped"] = dropped.get(sid, 0)
        row["retained"] = retained.get(sid, 0)
    save("coverage.json", coverage)
    print("intake tallies written for " + ", ".join(sorted(out)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prices", action="store_true", help="update prices only")
    parser.add_argument("--thumbnails", action="store_true", help="update thumbnails only")
    parser.add_argument("--recheck", action="store_true", help="re-fetch every thumbnail")
    args = parser.parse_args()
    both = not args.prices and not args.thumbnails

    config = load("config.json")
    events = load("events.json", [])
    if config is None:
        sys.exit("data/config.json is missing.")

    if both or args.prices:
        update_prices(config)
    if both or args.thumbnails or args.recheck:
        update_thumbnails(events, args.recheck)
    update_intake(config)


if __name__ == "__main__":
    main()
