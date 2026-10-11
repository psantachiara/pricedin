#!/usr/bin/env python3
"""Re-code events already in data/events.json against the current rules.

Coding happens when an item is fetched, so changing data/news_rules.json does
not reach anything already stored. This applies the current rules to what is
there, across all three axes: relevance, topics and frames.

  python scripts/reclassify.py --dry-run --report   what would change
  python scripts/reclassify.py                      apply it

The company, the tickers and everything else stay as they are, and entries you
wrote yourself ("manual": true) are left alone unless you pass --include-manual.

Items fetched before the summary was stored carry only a headline, so a rule
that depends on wording in the summary cannot reach them, and a frame they
really carry may be missed or recorded a level too low. The report says how
many are in that position; the codebook treats a thin input as a bias towards
under-coding rather than as a neutral loss.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from fetch_news import classify, compile_axes, compile_terms, load, normalise_source, save


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--report", action="store_true", help="list every change")
    parser.add_argument("--include-manual", action="store_true",
                        help="also re-judge entries you wrote yourself")
    parser.add_argument("--axis", choices=["all", "relevance", "topics", "frames"],
                        default="all", help="limit the re-coding to one axis")
    parser.add_argument("--recode-hand", action="store_true",
                        help="also overwrite rows coded by hand (destroys ground truth)")
    args = parser.parse_args()

    rules = load("news_rules.json")
    events = load("events.json", []) or []
    if not rules or not events:
        sys.exit("data/news_rules.json or data/events.json is missing.")

    entity_res = [(e, compile_terms(e["match"])) for e in rules.get("entities", [])]
    axes = compile_axes(rules)

    considered = touched = headline_only = unreachable = renamed = protected = 0
    rel_moves, frame_counts, topic_counts = Counter(), Counter(), Counter()
    details = []

    for event in events:
        # one spelling per outlet, whatever path wrote the row
        fixed = normalise_source(event.get("source"), rules)
        if fixed and fixed != event.get("source"):
            event["source"] = fixed
            renamed += 1

        # Hand-coded rows are the validation ground truth. A machine pass that
        # overwrote them would destroy the only thing agreement is measured
        # against, so they are skipped unless asked for explicitly.
        if event.get("coder") == "hand" and not args.recode_hand:
            protected += 1
            continue
        if event.get("manual") and not args.include_manual:
            continue
        considered += 1
        if not event.get("summary"):
            headline_only += 1

        item = {"title": event.get("title", ""), "summary": event.get("summary", "")}
        tagged = classify(item, rules, entity_res, axes)
        if not tagged:
            # already in the corpus but no longer passes the gate: left in place
            # and counted, never silently dropped
            unreachable += 1
            continue

        before = (event.get("relevance"), tuple(event.get("topics") or []),
                  tuple(sorted((event.get("frames") or {}).items())))

        if args.axis in ("all", "relevance"):
            event["relevance"] = tagged["relevance"]
        if args.axis in ("all", "topics"):
            event["topics"] = tagged["topics"]
            event["category"] = tagged["category"]
        if args.axis in ("all", "frames"):
            event["frames"] = tagged["frames"]
            event["coder"] = tagged["coder"]

        after = (event.get("relevance"), tuple(event.get("topics") or []),
                 tuple(sorted((event.get("frames") or {}).items())))

        for topic in event.get("topics") or []:
            topic_counts[topic] += 1
        for frame in (event.get("frames") or {}):
            frame_counts[frame] += 1

        if before != after:
            touched += 1
            if before[0] != after[0]:
                rel_moves[f"{before[0]} -> {after[0]}"] += 1
            details.append((event.get("date", ""), event.get("title", ""), before, after))

    verb = "would change" if args.dry_run else "changed"
    print(f"Considered {considered} events; {touched} {verb}.")
    if renamed:
        print(f"{renamed} source labels normalised to one spelling per outlet.")
    if protected:
        print(f"{protected} hand-coded rows left untouched.")
    if unreachable:
        print(f"{unreachable} no longer pass the gate and were left untouched.")
    if headline_only:
        print(f"{headline_only} have no stored summary, so only the headline could be read "
              f"(biases towards under-coding).")

    if topic_counts:
        print("\nTopics (multi-label, so these sum above the event count):")
        for topic, n in topic_counts.most_common():
            print(f"  {n:5d}  {topic}")
    if frame_counts:
        print("\nFrames (multi-label):")
        for frame, n in frame_counts.most_common():
            print(f"  {n:5d}  {frame}  ({100 * n / max(considered, 1):.1f}% of coded items)")
    if rel_moves:
        print("\nRelevance moves:")
        for move, n in rel_moves.most_common():
            print(f"  {n:5d}  {move}")

    if args.report and details:
        print("\nEvery change:")
        for date, title, before, after in sorted(details):
            print(f"  {date}  r{before[0]}->r{after[0]}  "
                  f"{'/'.join(after[1])[:24]:24s} "
                  f"{','.join(f'{k}{v}' for k, v in after[2])[:34]:34s} {title[:52]}")

    if args.dry_run:
        print("\nDry run: no files were written.")
        return
    if touched or renamed:
        save("events.json", events, indent=2)
        print("\nevents.json written.")


if __name__ == "__main__":
    main()
