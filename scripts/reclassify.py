#!/usr/bin/env python3
"""Re-judge the category of events already in data/events.json.

A category is decided when an item is fetched, so changing the rules in
data/news_rules.json does not reach anything already stored. This applies the
current rules to what is there.

  python scripts/reclassify.py --dry-run --report   what would change
  python scripts/reclassify.py                      apply it

Only the category changes. The company, the tickers and everything else stay
as they are, and entries you wrote yourself ("manual": true) are left alone
unless you pass --include-manual.

Items fetched before the summary was stored carry only a headline, so a rule
that depends on wording in the summary cannot reach them. The report says how
many are in that position.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from fetch_news import classify, compile_terms, load, save


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--report", action="store_true", help="list every change")
    parser.add_argument("--include-manual", action="store_true",
                        help="also re-judge entries you wrote yourself")
    parser.add_argument("--only", default=None,
                        help="only move items INTO this category, leaving the rest untouched")
    args = parser.parse_args()

    rules = load("news_rules.json")
    events = load("events.json", []) or []
    if not rules or not events:
        sys.exit("data/news_rules.json or data/events.json is missing.")

    entity_res = [(e, compile_terms(e["match"])) for e in rules.get("entities", [])]
    category_res = [(c["id"], compile_terms(c["match"])) for c in rules.get("categories", [])]

    considered = changed = headline_only = 0
    moves = Counter()
    details = []

    for event in events:
        if event.get("manual") and not args.include_manual:
            continue
        considered += 1
        if not event.get("summary"):
            headline_only += 1
        item = {"title": event.get("title", ""), "summary": event.get("summary", "")}
        tagged = classify(item, rules, entity_res, category_res)
        if not tagged:
            continue
        category = tagged[2]
        before = event.get("category")
        if category == before:
            continue
        if args.only and category != args.only:
            continue
        moves[f"{before} -> {category}"] += 1
        details.append((before, category, event.get("date", ""), event.get("title", "")))
        event["category"] = category
        changed += 1

    print(f"Considered {considered} events; {changed} would change category."
          if args.dry_run else
          f"Considered {considered} events; {changed} changed category.")
    if headline_only:
        print(f"{headline_only} of them have no stored summary, so only the headline could be "
              f"read. Items fetched from now on keep their summary.")
    if moves:
        print("\nMoves:")
        for move, n in moves.most_common():
            print(f"  {n:5d}  {move}")
    if args.report and details:
        print("\nEvery change:")
        for before, after, date, title in sorted(details):
            print(f"  {date}  {before:12s} -> {after:12s} {title[:64]}")

    if args.dry_run:
        print("\nDry run: no files were written.")
        return
    if changed:
        save("events.json", events, indent=2)


if __name__ == "__main__":
    main()
