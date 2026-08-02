#!/usr/bin/env python3
"""Blind LLM rating pass over the question bank — the semantic half of calibration.

``calibrate_signals.py`` measures what can be measured mechanically. This module
covers what can't: how obscure a fact actually is, and whether an item is
answerable by reasoning over the options alone. Three raters read each question
against ``DIFFICULTY.md`` and return a knowledge score plus a guessability verdict;
``calibrate.py`` merges their scores with the signals.

**Blind by construction.** The payload carries the question and its four options and
nothing else — no authored difficulty, no explainer, no source URL, no category.
Option order is shuffled (deterministically, per question) so a rater can't infer
the answer from position, and the raters are never told which option is correct.
That last point is the whole design: a rater who knows the answer cannot judge
whether the item is guessable without it.

**Three framings, not three copies.** Redundant raters agree with each other and
learn nothing; these disagree usefully — a series completionist, a casual watcher,
and a test-design skeptic who tries to answer from the option set alone. Their
spread is itself a signal, surfaced by ``calibrate.py`` as the review queue.

Results are cached by content hash in ``calibration/ratings.json``, so runs are
resumable and only genuinely new or repaired questions cost anything.

Usage:
    py pipeline/rate_questions.py --limit 50 --grep Buggy   # pilot first
    py pipeline/rate_questions.py                           # full bank
    py pipeline/rate_questions.py --estimate                # cost, no API calls
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE = Path(__file__).resolve().parent
DEFAULT_BANK = REPO_ROOT / "app" / "public" / "data" / "questions.json"
RUBRIC = PIPELINE / "DIFFICULTY.md"
CALIB_DIR = PIPELINE / "calibration"
DEFAULT_CACHE = CALIB_DIR / "ratings.json"

DEFAULT_MODEL = "claude-sonnet-5"
BATCH_SIZE = 20
LETTERS = "ABCD"

# Per-million-token list prices for the cost estimate. Rough by design — it exists
# to stop a full-bank run being a surprise, not to bill anyone.
PRICES = {
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# The panel. Each rater answers the same two questions from a different seat; the
# disagreement between seats is the point, so keep the framings genuinely distinct
# rather than three rewordings of "how hard is this".
RATERS = {
    "completionist": """You are scoring One Piece trivia for a fan who has read or watched the
entire series, once, attentively — not a superfan who looks things up, and not
someone rereading for detail.

For each question, estimate what percentage of such people would pick the correct
option, and convert that to a knowledge score: 100% correct -> 0, 80% -> 25,
50% -> 55, 20% -> 85, almost nobody -> 100.""",

    "casual": """You are scoring One Piece trivia for a lapsed anime watcher — they have seen
the major arcs, remember the Straw Hats and the headline villains, and have hazy
recall of everything else. They have not read the manga.

For each question, estimate what percentage of such people would pick the correct
option, and convert that to a knowledge score on the same scale: 100% correct -> 0,
80% -> 25, 50% -> 55, 20% -> 85, almost nobody -> 100.""",

    "skeptic": """You are a multiple-choice test designer looking for items a player can beat
without knowing the answer.

For each question, actually try to answer it using only the wording of the question,
the shape of the four options, and general reasoning — not your knowledge of One
Piece. Then score how obscure the underlying fact is for someone who cannot do that
(0 = universally known, 100 = almost nobody knows it).

Be aggressive about marking items guessable. An item is guessable when: the question
restates or translates the answer; only one option is a real thing and the rest look
invented; the distractors belong to a different category than the answer; the options
are numbers on a ladder; the distractors are near-synonyms of each other; only one
option is remotely plausible; or the correct option is conspicuously the longest and
most specific.""",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "ratings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "the question number, as labelled"},
                    "knowledge_score": {
                        "type": "integer",
                        "description": "0-100; how obscure the fact is, ignoring the options",
                    },
                    "guessable": {
                        "type": "boolean",
                        "description": "can this be answered without knowing the fact?",
                    },
                    "reason": {
                        "type": "string",
                        "description": "one short sentence justifying the score",
                    },
                },
                "required": ["n", "knowledge_score", "guessable", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ratings"],
    "additionalProperties": False,
}


def norm_key(s: str) -> str:
    """Same normalisation validate.py uses to key questions — keep them identical."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def content_hash(question: str, options: list[str]) -> str:
    """Identity of a *rateable item*: the question plus its option set.

    Sorted, so re-shuffling options doesn't invalidate a cached rating — but any
    repaired distractor does, which is exactly what should re-cost.
    """
    payload = json.dumps([question, sorted(str(o) for o in options)], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_items(bank: list[dict]) -> list[dict]:
    """One blind, deterministically-shuffled item per question."""
    items = []
    for q in bank:
        options = [str(q["correct_answer"])] + [str(i) for i in q["incorrect_answers"]]
        key = norm_key(q["question"])
        # Seeded on the question so the shuffle is stable across runs and machines.
        random.Random(key).shuffle(options)
        items.append({
            "key": key,
            "hash": content_hash(str(q["question"]), options),
            "question": str(q["question"]),
            "options": options,
            "authored_difficulty": q.get("difficulty"),
            "category": q.get("category"),
        })
    return items


def render_batch(items: list[dict]) -> str:
    lines = []
    for n, item in enumerate(items, 1):
        lines.append(f"Q{n}. {item['question']}")
        for letter, option in zip(LETTERS, item["options"]):
            lines.append(f"    {letter}. {option}")
        lines.append("")
    return "\n".join(lines)


def rate_batch(client, model: str, rubric: str, rater: str, items: list[dict]) -> list[dict]:
    """One request, one rater, up to BATCH_SIZE questions."""
    system = [
        # The rubric is identical on every request, so it caches; the questions
        # come after it in the user turn where they can vary freely.
        {"type": "text", "text": RATERS[rater]},
        {"type": "text", "text": f"Grade against this rubric:\n\n{rubric}",
         "cache_control": {"type": "ephemeral"}},
    ]
    prompt = (
        f"Rate all {len(items)} questions below. Return one entry per question, "
        f"using the question's number.\n\n"
        f"You are not told which option is correct — that is deliberate. Judge the "
        f"item as a player meeting it cold would.\n\n"
        + render_batch(items)
    )
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["ratings"], response.usage


def load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", type=Path, default=DEFAULT_BANK, help="question bank to rate")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="ratings cache (resumable)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"rater model (default {DEFAULT_MODEL})")
    ap.add_argument("--raters", default=",".join(RATERS), help="comma-separated rater subset")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="questions per request")
    ap.add_argument("--limit", type=int, help="rate at most N questions (pilot runs)")
    ap.add_argument("--grep", help="only questions whose text matches this (case-insensitive)")
    ap.add_argument("--difficulty", help="only questions with this authored difficulty")
    ap.add_argument("--estimate", action="store_true", help="report scope and cost, make no API calls")
    ap.add_argument("--force", action="store_true", help="re-rate even if cached")
    args = ap.parse_args(argv)

    if not args.bank.exists():
        raise SystemExit(f"bank not found: {args.bank}")
    raters = [r.strip() for r in args.raters.split(",") if r.strip()]
    unknown = set(raters) - set(RATERS)
    if unknown:
        raise SystemExit(f"unknown rater(s): {sorted(unknown)}; known: {sorted(RATERS)}")

    bank = json.loads(args.bank.read_text(encoding="utf-8"))
    items = build_items(bank)
    if args.grep:
        pattern = re.compile(args.grep, re.I)
        items = [i for i in items if pattern.search(i["question"])]
    if args.difficulty:
        items = [i for i in items if i["authored_difficulty"] == args.difficulty]
    if args.limit:
        items = items[:args.limit]

    cache = load_cache(args.cache)
    todo = {
        rater: [i for i in items if args.force or rater not in cache.get(i["hash"], {})]
        for rater in raters
    }
    pending = sum(len(v) for v in todo.values())
    requests = sum(-(-len(v) // args.batch_size) for v in todo.values())
    print(f"{len(items)} questions x {len(raters)} raters; {pending} ratings missing "
          f"({requests} requests, batch size {args.batch_size})")

    if args.estimate:
        # ~250 input tokens per question plus a cached ~1.5k rubric, ~60 output.
        in_tok = pending * 250 + requests * 1500
        out_tok = pending * 60
        price_in, price_out = PRICES.get(args.model, PRICES[DEFAULT_MODEL])
        cost = in_tok / 1e6 * price_in + out_tok / 1e6 * price_out
        print(f"estimate: ~{in_tok:,} in + ~{out_tok:,} out on {args.model} "
              f"-> roughly ${cost:.2f} (caching makes the real figure lower)")
        return 0
    if not pending:
        print("nothing to do — every question is already rated")
        return 0

    try:
        import anthropic
    except ImportError:
        raise SystemExit("pip install anthropic  (see pipeline/requirements.txt)")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("note: no ANTHROPIC_API_KEY set — relying on an `ant auth login` profile")
    client = anthropic.Anthropic()
    rubric = RUBRIC.read_text(encoding="utf-8")

    done = failed = 0
    cached_in = fresh_in = out = 0
    started = time.time()
    for rater in raters:
        queue = todo[rater]
        for start in range(0, len(queue), args.batch_size):
            batch = queue[start:start + args.batch_size]
            try:
                ratings, usage = rate_batch(client, args.model, rubric, rater, batch)
            except Exception as exc:                       # noqa: BLE001 - report and continue
                failed += len(batch)
                print(f"  [{rater}] batch at {start} failed: {type(exc).__name__}: {exc}")
                continue
            cached_in += getattr(usage, "cache_read_input_tokens", 0) or 0
            fresh_in += usage.input_tokens
            out += usage.output_tokens
            by_n = {r["n"]: r for r in ratings}
            for n, item in enumerate(batch, 1):
                rating = by_n.get(n)
                if rating is None:
                    failed += 1
                    continue
                entry = cache.setdefault(item["hash"], {})
                entry[rater] = {
                    "knowledge_score": max(0, min(100, int(rating["knowledge_score"]))),
                    "guessable": bool(rating["guessable"]),
                    "reason": str(rating["reason"]),
                }
                entry["question"] = item["question"]
                entry["key"] = item["key"]
                done += 1
            save_cache(args.cache, cache)     # checkpoint every batch, so a kill is cheap
            print(f"  [{rater}] {min(start + len(batch), len(queue))}/{len(queue)}", flush=True)

    elapsed = time.time() - started
    print(f"\nrated {done} (failed {failed}) in {elapsed:.0f}s")
    print(f"tokens: {fresh_in:,} in ({cached_in:,} from cache), {out:,} out")
    print(f"wrote {args.cache}")
    return 1 if failed and not done else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
