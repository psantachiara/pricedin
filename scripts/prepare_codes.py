#!/usr/bin/env python3
"""Set up the coder provenance files before a model pass runs.

Two jobs, both idempotent, so this is safe to run at the start of every run:

  1. Snapshot the keyword codes to data/keyword_codes.jsonl, if that file does
     not already exist. The model pass overwrites events.json, so without a
     snapshot taken first there is nothing left to compare the new codes
     against. Taken from events.json as it stands, never regenerated.

  2. Stamp the hand-coded rows into events.json from data/reference_codes.json,
     so that code_frames.py can see and protect them. Hand codes are the only
     human reference in the study and the only thing agreement is measured
     against; they live in reference_codes.json because they were produced in
     a separate tool, and this is what puts them into the corpus.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "data" / "events.json"
KEYWORD = ROOT / "data" / "keyword_codes.jsonl"
REFERENCE = ROOT / "data" / "reference_codes.json"


def main() -> int:
    events = json.loads(EVENTS.read_text())
    by_id = {e["id"]: e for e in events if e.get("id")}
    dirty = False

    if KEYWORD.exists():
        print(f"{KEYWORD.name} already exists; left alone "
              f"({sum(1 for _ in KEYWORD.open())} rows).")
    else:
        rows = [{"id": e["id"], "relevance": e.get("relevance"),
                 "topics": e.get("topics") or [], "frames": e.get("frames") or {}}
                for e in events if e.get("coder") == "keyword-v1"]
        if rows:
            KEYWORD.write_text("\n".join(
                json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
            print(f"Snapshotted {len(rows)} keyword-coded rows to {KEYWORD.name}.")
        else:
            print("No keyword-coded rows to snapshot. If a model pass has "
                  "already run, the baseline is gone and the comparison "
                  "against it cannot be rebuilt.", file=sys.stderr)

    if not REFERENCE.exists():
        print(f"{REFERENCE.name} not found; no hand codes to stamp.")
        return 0

    reference = json.loads(REFERENCE.read_text())
    hand = (reference.get("hand") or {}).get("codes") or {}
    stamped = already = absent = 0
    for iid, codes in hand.items():
        event = by_id.get(iid)
        if event is None:
            absent += 1
            continue
        if event.get("coder") == "hand":
            already += 1
            continue
        topics = codes.get("topics") or []
        event["relevance"] = codes.get("relevance")
        event["topics"] = topics
        event["category"] = topics[0] if topics else "other"
        event["frames"] = codes.get("frames") or {}
        event["coder"] = "hand"
        stamped += 1
        dirty = True

    print(f"Hand codes: {stamped} stamped, {already} already in place, "
          f"{absent} not found in the corpus.")
    if dirty:
        EVENTS.write_text(json.dumps(events, ensure_ascii=False) + "\n")
        print(f"Wrote {EVENTS.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
