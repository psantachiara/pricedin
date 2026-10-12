#!/usr/bin/env python3
"""Code the corpus on the three axes with a language model.

Two phases, deliberately separate so a long run survives interruption:

  code   read events.json, send uncoded items to the model, append each
         returned record to data/llm_codes.jsonl
  apply  merge that file into events.json

Phase one is resumable: ids already in the jsonl are skipped, so a run that
times out or is cancelled can simply be dispatched again. Phase two is
idempotent. Hand-coded rows are never touched by either phase.

The rubric below is the measuring instrument. It is derived from the framing
codebook, and any change to it changes what the numbers mean, so it carries a
version string that is recorded on every row it produces.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "data" / "events.json"
RULES = ROOT / "data" / "news_rules.json"
CONFIG = ROOT / "data" / "config.json"
CODES = ROOT / "data" / "llm_codes.jsonl"

API = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

RUBRIC_VERSION = "rubric-v2"          # v2 = the 11-frame scheme, 12 Oct 2026

# Price per million tokens: (base input, cache write 5m, cache read, output).
# From platform.claude.com/docs/en/about-claude/pricing, 12 Oct 2026.
PRICES = {
    "claude-haiku-5-5":  (0.10, 0.125, 0.01, 0.50),
    "claude-sonnet-5-5": (2.00, 2.50,  0.10, 10.00),
    "claude-opus-5":     (5.00, 6.25,  0.50, 25.00),
    "claude-opus-5-5":   (4.00, 5.00,  0.20, 20.00),
}
DEFAULT_MODEL = "claude-sonnet-5-5"


# --------------------------------------------------------------------------
# the rubric
# --------------------------------------------------------------------------

def build_rubric(config: dict) -> str:
    topics = config.get("topics", [])
    frames = config.get("frames", [])

    topic_lines = "\n".join(
        f"- `{t['id']}` — {t.get('name') or t.get('short')}" for t in topics
    ) + "\n- `other` — none of the above fits"

    by_family: dict[str, list] = {}
    for f in frames:
        by_family.setdefault(f.get("family", "neutral"), []).append(f)

    frame_lines = []
    for fam in ("danger", "contrast", "neutral"):
        for f in by_family.get(fam, []):
            frame_lines.append(f"- `{f['id']}` ({fam}) — {f.get('name')}")
    frame_block = "\n".join(frame_lines)

    return f"""\
You are coding news headlines for a content analysis of how the press
characterises artificial intelligence. You apply a codebook. You do not
summarise, editorialise, or judge whether a claim in an item is true.

You see only a headline and a one-line summary. That is deliberate: it is the
same evidence every other coder in this study sees. Code what is on the page.

## Axis 0 — relevance (exactly one value)

- `2` — AI is the subject. The test is counterfactual: remove AI from the story
  and nothing is left. "Wall St. wants another half-trillion for the AI boom"
  is 2, because there is no half-trillion without AI.
- `1` — AI is incidental. The story stands on its own and AI appears as a
  clause, a cause, or a passing mention. "Microsoft beats earnings, citing AI
  demand" is 1: strip AI and there is still an earnings story.
- `0` — not about AI. The item mentions no AI at all, or only as background
  colour with no bearing on the story.

Judge by what the story is about, not by whether the letters "AI" appear. A
story about Nvidia's results, a data-centre buildout, or chip export controls
is usually about AI even when the word is absent. A story that drops "AI" into
a headline about something else is not.

**Coding stops at 0.** An item coded `0` gets an empty topic list and an empty
frame object. Never assign a topic or a frame to a relevance-0 item.

## Axis 1 — topics (zero or more; what the item is about)

{topic_lines}

Assign every topic that genuinely applies, usually one to three. The first one
you list is treated as the item's lead topic, so put the most central first.
For a relevance-1 item, code the topic of the AI mention, not of the whole
story. An in-scope item should normally carry at least one topic; use `other`
when nothing fits.

## Axis 2 — frames (zero or more; how the item characterises AI)

A frame is an evaluative claim *about AI*. Each frame you assign carries an
intensity: 1 mentioned in passing, 2 substantial, 3 dominant — the item is
largely this claim.

{frame_block}

`neutral` means the item makes no evaluative claim about AI. It stands alone:
never combine `neutral` with any other frame. Intensity for `neutral` is
always 2.

**An empty frame object and `neutral` both mean no stance.** Use `neutral`.
This is a common and correct answer: a product launch, a funding round, a
regulatory filing or an appointment often characterises AI not at all. Do not
reach for `capability` because something shipped, or `bubble` because a large
number appears.

## Rules that decide the hard cases

**Attribution.** A frame counts whenever the claim is present in the item,
whoever voices it — the outlet, a regulator, a researcher, a chief executive,
a protester. A front page reporting that a minister called AI a risk to
children carries `individual`. What is being measured is the framing in
circulation, not the outlet's own editorial position.

**Raised in order to rebut still counts.** "AWS chief pushes back on AI job
apocalypse warnings" carries `displacement`. The frame is in the item.

**Code the claim, not its truth.** You never assess whether the danger is
real.

**Hypothetical and future claims count.** "AI could end human civilisation"
is `existential`. Hedging lowers intensity; it does not remove the frame.

**AI as victim is not `security`.** `security` is AI used as an instrument
against systems or people — intrusion, exfiltration, surveillance, fraud,
weaponisation. A cyberattack *on* an AI company is not this frame; that item
is usually `neutral`.

**Military and weapons use goes to `security`.** Lethal autonomous systems,
battlefield deployment, intelligence analysis. Add `existential` beside it
only where the item makes a humanity-scale or loss-of-control claim.

**Surveillance of workers goes to `security`,** with topic `labour`.
`displacement` is reserved for work removed or degraded — including
deskilling. An item about employers *requiring* AI use, or tying promotion to
it, carries no frame: that is a claim about workplace power, not about AI.

**`environment` is harm from the physical buildout** — water, power, land,
emissions, the burden on nearby residents. A story about AI demand raising
chip or memory prices is NOT `environment`: that is a market effect, coded
`capability` or `bubble` by the item's stance, or `neutral`.

**`appropriation` is work taken** to build or run AI — copyright claims,
scraping without licence, artists' and authors' objections.

**`existential` against `unreliable`.** `existential` is catastrophic or
civilisational risk and loss of control. `unreliable` is a system that errs,
hallucinates or behaves erratically. A hallucinating chatbot is `unreliable`;
a model its makers cannot predict or constrain is `existential`.

**`societal` against `individual`.** `societal` is harm to collective
structures — misinformation, elections, shared epistemics. `individual` is
harm to identifiable persons — mental health, abuse, children, sexual
content, discrimination.

**`capability` is a claim about power or reach,** not the fact of a release.
**`bubble`** is overvaluation, unsustainable investment, or a coming
correction.

## What goes wrong most often

The pass this replaces matched keywords, and failed in ways you must not
repeat:

- It fired danger frames on general-news vocabulary. "Threat", "risk",
  "warning", "collapse", "crackdown", "attack" are ordinary newspaper words.
  They are a frame only when the claim attaches to AI.
- It put danger frames on items that were not about AI at all. If relevance
  is 0, there is no frame, however alarming the wording.
- It almost never assigned two frames, so items making two distinct claims
  were flattened to one. Multi-label freely where the item earns it.

## Output

Return a JSON array and nothing else — no prose, no code fence. One object
per item you were given, in the same order, each exactly:

{{"i": <the item's number>, "relevance": 0|1|2, "topics": ["id", ...], "frames": {{"id": 1|2|3}}}}

Use only the ids listed above. Every item you were given must appear exactly
once.
"""


# --------------------------------------------------------------------------
# calling the model
# --------------------------------------------------------------------------

class Usage:
    """Token counters, shared across worker threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fresh_in = self.cache_write = self.cache_read = self.out = 0
        self.calls = self.retries = 0

    def add(self, u: dict) -> None:
        with self.lock:
            self.fresh_in += u.get("input_tokens", 0)
            self.cache_write += u.get("cache_creation_input_tokens", 0)
            self.cache_read += u.get("cache_read_input_tokens", 0)
            self.out += u.get("output_tokens", 0)
            self.calls += 1

    def cost(self, model: str) -> float | None:
        p = PRICES.get(model)
        if not p:
            return None
        base, write, read, out = p
        return (self.fresh_in * base + self.cache_write * write
                + self.cache_read * read + self.out * out) / 1_000_000


def render_items(batch: list[tuple[int, dict]]) -> str:
    lines = []
    for n, ev in batch:
        summary = (ev.get("summary") or "").strip().replace("\n", " ")
        lines.append(f"[{n}] ({ev.get('source', '?')}) {ev.get('title', '')}")
        lines.append(f"     {summary}" if summary else "     (no summary)")
    return "\n".join(lines)


def extract_json(text: str):
    """The model is told to return bare JSON; tolerate a fence anyway."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        raise ValueError("no JSON array in reply")
    return json.loads(text[start:end + 1])


def call_model(session, key, model, rubric, batch, usage, max_attempts=6):
    body = {
        "model": model,
        "max_tokens": 64 * len(batch) + 512,
        "temperature": 0,
        "system": [{"type": "text", "text": rubric,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": render_items(batch)}],
    }
    headers = {"x-api-key": key, "anthropic-version": API_VERSION,
               "content-type": "application/json"}
    # A key scoped to one workspace needs no workspace header. A key that can
    # reach several (an unscoped, organization-level key) requires one, and
    # returns 400 on every request without it — so pass the id through when
    # it is configured, and let a scoped key work with nothing set.
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if workspace:
        headers["anthropic-workspace-id"] = workspace

    for attempt in range(max_attempts):
        try:
            r = session.post(API, headers=headers, json=body, timeout=180)
            if r.status_code in (429, 500, 502, 503, 529):
                raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code == 401:
                raise SystemExit(
                    "API key rejected (401). Check the ANTHROPIC_API_KEY "
                    "secret — it should start with 'sk-ant-'.")
            if r.status_code == 400 and "workspace" in r.text.lower():
                raise SystemExit(
                    "The API wants a workspace id (400). This key reaches "
                    "more than one workspace, which needs an "
                    "anthropic-workspace-id header on every request.\n"
                    "Either set an ANTHROPIC_WORKSPACE_ID secret to the "
                    "workspace id, or create a key scoped to a single "
                    "workspace, which needs no header.\n"
                    f"API said: {r.text[:300]}")
            r.raise_for_status()
            payload = r.json()
            usage.add(payload.get("usage") or {})
            text = "".join(b.get("text", "") for b in payload.get("content", []))
            return extract_json(text)
        except SystemExit:
            raise
        except Exception as exc:                      # noqa: BLE001
            if attempt == max_attempts - 1:
                raise
            with usage.lock:
                usage.retries += 1
            wait = min(60, 2 ** attempt) + random.random()
            print(f"  retry {attempt + 1}/{max_attempts - 1} in {wait:.0f}s "
                  f"({type(exc).__name__}: {str(exc)[:90]})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def validate(rec: dict, topics: set[str], frames: set[str]) -> tuple[dict, str | None]:
    rel = rec.get("relevance")
    if rel not in (0, 1, 2):
        return {}, f"relevance {rel!r}"

    tops = [t for t in (rec.get("topics") or []) if isinstance(t, str)]

    # An empty list where an object was asked for still means "no frames", so
    # it is accepted and normalised below. Anything else of the wrong type is
    # a model that did not follow the schema, and the item is left uncoded
    # rather than guessed at.
    frs_in = rec.get("frames")
    if frs_in in (None, [], ()):
        frs_in = {}
    if not isinstance(frs_in, dict):
        return {}, f"frames is {type(frs_in).__name__}, not an object"

    bad = [t for t in tops if t not in topics]
    if bad:
        return {}, f"unknown topic {bad[0]!r}"

    frs: dict[str, int] = {}
    for fid, lvl in frs_in.items():
        if fid not in frames:
            return {}, f"unknown frame {fid!r}"
        if not isinstance(lvl, int) or not 1 <= lvl <= 3:
            return {}, f"intensity {fid}={lvl!r}"
        frs[fid] = lvl

    if rel == 0:
        tops, frs = [], {}
    else:
        if "neutral" in frs and len(frs) > 1:
            frs = {k: v for k, v in frs.items() if k != "neutral"}
        if not frs:
            frs = {"neutral": 2}
        if not tops:
            tops = ["other"]

    # dedupe topics, keep order (the first is the lead topic)
    tops = list(dict.fromkeys(tops))
    return {"relevance": rel, "topics": tops, "frames": frs}, None


# --------------------------------------------------------------------------
# phases
# --------------------------------------------------------------------------

def load_done() -> dict[str, dict]:
    done: dict[str, dict] = {}
    if CODES.exists():
        with CODES.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("id"):
                    done[rec["id"]] = rec
    return done


def phase_code(args) -> int:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key and not args.dry_run:
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 2

    config = json.loads(CONFIG.read_text())
    events = json.loads(EVENTS.read_text())
    rubric = build_rubric(config)

    topic_ids = {t["id"] for t in config.get("topics", [])} | {"other"}
    frame_ids = {f["id"] for f in config.get("frames", [])}

    done = load_done()
    sample_ids = None
    if args.scope == "sample":
        if not args.sample_file:
            print("--scope sample needs --sample-file", file=sys.stderr)
            return 2
        sample = json.loads(Path(args.sample_file).read_text())
        rows = sample["items"] if isinstance(sample, dict) else sample
        sample_ids = {r["id"] for r in rows}

    todo, hand_n = [], 0
    for ev in events:
        # Hand-coded rows ARE coded here, into llm_codes.jsonl, because the
        # agreement figure needs the model's opinion on the same items a human
        # judged. They are protected where it matters: the apply phase refuses
        # to write over them, so the corpus keeps the human codes while the
        # comparison still has both.
        if ev.get("coder") == "hand":
            hand_n += 1
        if sample_ids is not None and ev.get("id") not in sample_ids:
            continue
        if ev.get("id") in done and not args.redo:
            continue
        todo.append(ev)

    if args.limit:
        todo = todo[:args.limit]

    print(f"{len(events)} events; {len(todo)} to code "
          f"({hand_n} of them hand-coded — coded here for the comparison, "
          f"never written back over the human codes); "
          f"{len(done)} already in {CODES.name}.")
    print(f"model {args.model} · rubric {RUBRIC_VERSION} · "
          f"batch {args.batch} · {args.workers} workers")
    if args.dry_run:
        print("\nDry run: nothing sent. Rubric is "
              f"{len(rubric)} characters, roughly {len(rubric) // 4} tokens "
              "(cached after the first call).")
        if todo:
            print("\nFirst batch as the model would see it:\n")
            print(render_items([(i, e) for i, e in enumerate(todo[:args.batch])]))
        return 0
    if not todo:
        print("Nothing to do.")
        return 0

    batches = [todo[i:i + args.batch] for i in range(0, len(todo), args.batch)]
    usage = Usage()
    write_lock = threading.Lock()
    counted = Counter()
    rejected: list[tuple[str, str]] = []
    started = time.time()

    CODES.parent.mkdir(parents=True, exist_ok=True)
    out = CODES.open("a")
    session = requests.Session()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def work(bi: int, chunk: list[dict]):
        numbered = list(enumerate(chunk))
        replies = call_model(session, key, args.model, rubric, numbered, usage)
        by_i = {}
        for rec in replies:
            if isinstance(rec, dict) and isinstance(rec.get("i"), int):
                by_i[rec["i"]] = rec
        lines, local = [], Counter()
        for n, ev in numbered:
            rec = by_i.get(n)
            if rec is None:
                rejected.append((ev.get("id", "?"), "missing from reply"))
                continue
            clean, err = validate(rec, topic_ids, frame_ids)
            if err:
                rejected.append((ev.get("id", "?"), err))
                continue
            local[clean["relevance"]] += 1
            for f in clean["frames"]:
                local[f"frame:{f}"] += 1
            lines.append(json.dumps({
                "id": ev["id"], **clean,
                "coder": f"llm-{args.model}",
                "rubric": RUBRIC_VERSION,
                "at": stamp,
            }, ensure_ascii=False))
        with write_lock:
            out.write("\n".join(lines) + "\n")
            out.flush()
            counted.update(local)
        return bi, len(lines)

    coded = 0
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(work, i, c): i for i, c in enumerate(batches)}
            for n, fut in enumerate(as_completed(futures), start=1):
                try:
                    _, got = fut.result()
                except Exception as exc:                 # noqa: BLE001
                    print(f"  batch failed permanently: {exc}", file=sys.stderr)
                    continue
                coded += got
                if n % 10 == 0 or n == len(batches):
                    rate = coded / max(1e-9, time.time() - started)
                    left = (len(todo) - coded) / rate if rate else 0
                    print(f"  {n}/{len(batches)} batches · {coded} coded · "
                          f"{rate * 60:.0f}/min · ~{left / 60:.0f} min left")
    except KeyboardInterrupt:
        print("\nInterrupted. Progress is on disk; dispatch again to resume.")
    finally:
        out.close()

    secs = time.time() - started
    print(f"\nCoded {coded} items in {secs / 60:.1f} min "
          f"({usage.calls} calls, {usage.retries} retries).")
    print(f"tokens: {usage.fresh_in:,} input · {usage.cache_write:,} cache write "
          f"· {usage.cache_read:,} cache read · {usage.out:,} output")
    cost = usage.cost(args.model)
    if cost is not None:
        print(f"cost at list price: ${cost:.2f}")

    rel = {k: v for k, v in counted.items() if isinstance(k, int)}
    print("\nrelevance:", dict(sorted(rel.items())))
    frames = sorted(((k[6:], v) for k, v in counted.items()
                     if isinstance(k, str) and k.startswith("frame:")),
                    key=lambda kv: -kv[1])
    print("frames:")
    for fid, n in frames:
        print(f"  {n:5d}  {fid}")

    if rejected:
        print(f"\n{len(rejected)} items rejected and left uncoded:")
        for iid, why in rejected[:20]:
            print(f"  {iid}  — {why}")
        if len(rejected) > 20:
            print(f"  … and {len(rejected) - 20} more")
        print("Dispatch again to retry them; they are not in the output file.")
    return 0


def phase_apply(args) -> int:
    events = json.loads(EVENTS.read_text())
    done = load_done()
    if not done:
        print(f"{CODES.name} is empty or missing; nothing to apply.")
        return 1

    applied = protected = unchanged = missing = 0
    for ev in events:
        rec = done.get(ev.get("id"))
        if rec is None:
            missing += 1
            continue
        if ev.get("coder") == "hand":
            protected += 1
            continue
        before = (ev.get("relevance"), tuple(ev.get("topics") or []),
                  tuple(sorted((ev.get("frames") or {}).items())))
        ev["relevance"] = rec["relevance"]
        ev["topics"] = rec["topics"]
        ev["category"] = rec["topics"][0] if rec["topics"] else "other"
        ev["frames"] = rec["frames"]
        ev["coder"] = rec["coder"]
        ev["rubric"] = rec.get("rubric", RUBRIC_VERSION)
        after = (ev["relevance"], tuple(ev["topics"]),
                 tuple(sorted(ev["frames"].items())))
        if before == after:
            unchanged += 1
        else:
            applied += 1

    print(f"{applied} rows changed, {unchanged} already matched, "
          f"{protected} hand-coded left alone, {missing} not in {CODES.name}.")
    if args.dry_run:
        print("Dry run: events.json not written.")
        return 0
    EVENTS.write_text(json.dumps(events, ensure_ascii=False) + "\n")
    print(f"Wrote {EVENTS.relative_to(ROOT)}.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=("code", "apply"))
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"model id (default {DEFAULT_MODEL})")
    ap.add_argument("--scope", choices=("all", "sample"), default="all")
    ap.add_argument("--sample-file", help="JSON list or {items:[…]} of sampled rows")
    ap.add_argument("--batch", type=int, default=25, help="items per request")
    ap.add_argument("--workers", type=int, default=4, help="requests in flight")
    ap.add_argument("--limit", type=int, help="stop after this many items")
    ap.add_argument("--redo", action="store_true",
                    help="re-code items already in llm_codes.jsonl")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.phase == "code":
        return phase_code(args)
    return phase_apply(args)


if __name__ == "__main__":
    sys.exit(main())
