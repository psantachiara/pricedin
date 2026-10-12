#!/usr/bin/env python3
"""Compare the coders against each other and against the human reference.

Reads whatever is on disk and reports, so it is safe to run at any point:

  data/llm_codes.jsonl      the current model pass
  data/keyword_codes.jsonl  the keyword pass it replaces (snapshot)
  data/reference_codes.json the 16 hand codes, and the opus-5 sample pass

The number that matters is agreement with the hand codes, and there are only
16 of those, so every figure here carries its n and none of them should be
reported without it. Agreement between two machine coders measures
consistency, not accuracy.
"""

from __future__ import annotations

import json
import sys
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DANGER = {"existential", "unreliable", "security", "societal",
          "individual", "displacement", "environment", "appropriation"}


def read_jsonl(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("id"):
            out[rec["id"]] = rec
    return out


def danger(rec: dict) -> bool:
    return bool(set(rec.get("frames") or {}) & DANGER)


def kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    n = len(pairs)
    if not n:
        return None
    a = sum(1 for x, y in pairs if x and y)
    b = sum(1 for x, y in pairs if not x and y)
    c = sum(1 for x, y in pairs if x and not y)
    d = n - a - b - c
    po = (a + d) / n
    pe = ((a + c) * (a + b) + (b + d) * (c + d)) / n ** 2
    if pe == 1:
        return None
    return (po - pe) / (1 - pe)


def compare(name_a: str, a: dict, name_b: str, b: dict) -> None:
    shared = sorted(set(a) & set(b))
    if not shared:
        print(f"\n{name_a} vs {name_b}: no items in common.")
        return

    rel = [(a[i].get("relevance"), b[i].get("relevance")) for i in shared]
    rel_ok = sum(1 for x, y in rel if x == y)
    dang = [(danger(a[i]), danger(b[i])) for i in shared]
    dang_ok = sum(1 for x, y in dang if x == y)
    k = kappa(dang)

    print(f"\n{name_a} vs {name_b}   (n = {len(shared)})")
    print(f"  relevance exact        {rel_ok}/{len(shared)} = "
          f"{100 * rel_ok / len(shared):.1f}%")
    print(f"  danger present         {dang_ok}/{len(shared)} = "
          f"{100 * dang_ok / len(shared):.1f}%"
          + (f"   kappa {k:.3f}" if k is not None else ""))

    only_a = sum(1 for x, y in dang if x and not y)
    only_b = sum(1 for x, y in dang if y and not x)
    print(f"  danger in {name_a} only: {only_a}   in {name_b} only: {only_b}")

    # topic overlap, as mean Jaccard over in-scope items
    js, n_js = 0.0, 0
    for i in shared:
        ta, tb = set(a[i].get("topics") or []), set(b[i].get("topics") or [])
        if not ta and not tb:
            continue
        js += len(ta & tb) / len(ta | tb)
        n_js += 1
    if n_js:
        print(f"  topic overlap (Jaccard) {js / n_js:.2f} over {n_js} items")

    # the specific failure: danger frames on items the other calls not-about-AI
    zero_b = [i for i in shared if b[i].get("relevance") == 0]
    if zero_b:
        leak = sum(1 for i in zero_b if danger(a[i]))
        print(f"  of {len(zero_b)} items {name_b} calls relevance 0, "
              f"{name_a} puts a danger frame on {leak}")


def profile(name: str, codes: dict) -> None:
    if not codes:
        return
    rel: dict[int, int] = {}
    frames: dict[str, int] = {}
    multi = in_scope = 0
    for rec in codes.values():
        r = rec.get("relevance")
        rel[r] = rel.get(r, 0) + 1
        fs = set(rec.get("frames") or {})
        for f in fs:
            frames[f] = frames.get(f, 0) + 1
        if r and r > 0:
            in_scope += 1
            if len(fs - {"neutral"}) > 1:
                multi += 1
    print(f"\n{name}   n = {len(codes)}")
    print("  relevance", dict(sorted((k, v) for k, v in rel.items()
                                     if k is not None)))
    share = f"{100 * multi / in_scope:.1f}%" if in_scope else "n/a"
    print(f"  in scope {in_scope}, carrying more than one substantive frame: "
          f"{multi} ({share})")
    for f, n in sorted(frames.items(), key=lambda kv: -kv[1]):
        print(f"    {n:5d}  {f}")


def main() -> int:
    ref_path = ROOT / "data" / "reference_codes.json"
    sets: dict[str, dict] = {}

    llm = read_jsonl(ROOT / "data" / "llm_codes.jsonl")
    if llm:
        model = next(iter(llm.values())).get("coder", "llm")
        sets[model] = llm
    kw = read_jsonl(ROOT / "data" / "keyword_codes.jsonl")
    if kw:
        sets["keyword-v1"] = kw
    if ref_path.exists():
        for name, block in json.loads(ref_path.read_text()).items():
            sets[name] = block.get("codes", {})

    if not sets:
        print("No coder output found on disk.")
        return 1

    print("=" * 68)
    print("CODER PROFILES")
    print("=" * 68)
    for name, codes in sets.items():
        profile(name, codes)

    print()
    print("=" * 68)
    print("PAIRWISE AGREEMENT")
    print("=" * 68)
    print("\nAgreement with `hand` is the only accuracy measure here, and it")
    print("rests on 16 items. Everything else is consistency between machines.")

    # hand first, then the rest
    names = list(sets)
    ordered = ([("hand", other) for other in names if other != "hand"]
               + [p for p in combinations(names, 2) if "hand" not in p])
    for a, b in ordered:
        if a in sets and b in sets:
            compare(a, sets[a], b, sets[b])

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
