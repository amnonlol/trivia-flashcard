#!/usr/bin/env python3
"""Validate generated questions and write the final app question bank.

Phase 3, step 3 (PLAN 5d). Reads a JSON array of questions (default
``wiki-data/questions.generated.json`` from ``generate_questions.py``), enforces
the exact schema the quiz engine expects, drops anything malformed or duplicated,
and writes the survivors to ``app/public/data/questions.json``.

Checks per question (a failure drops that one question, not the whole run):

* required keys present; ``type == "multiple"``; ``difficulty`` in easy/medium/hard;
* ``category`` in the app's known category list (``categories.js``);
* exactly 4 ``options``; ``correct_answer`` appears in ``options`` **exactly once**;
* ``incorrect_answers`` are the other 3 options; no duplicate / near-duplicate
  options (case/punctuation-insensitive, substring collisions);
* non-empty question text; ``source`` is an ``onepiece.fandom.com`` URL.

Global passes:

* drop exact-duplicate questions (same normalised question text keeps the first);
* report per-category / per-difficulty counts.

Exit code is non-zero only on a *fatal* problem (missing input, zero valid
questions) so it can gate CI; individual dropped questions are warnings.

Usage:
    py pipeline/validate.py
    py pipeline/validate.py --in wiki-data/questions.generated.json
    py pipeline/validate.py --check-only        # validate, don't write output
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IN = REPO_ROOT / "wiki-data" / "questions.generated.json"
DEFAULT_OUT = REPO_ROOT / "app" / "public" / "data" / "questions.json"

VALID_DIFFICULTIES = {"easy", "medium", "hard"}
# Must stay in sync with app/src/constants/categories.js.
VALID_CATEGORIES = {
    "Characters", "Devil Fruits", "Crews & Organizations",
    "Arcs & Story", "Bounties", "Geography",
}
SOURCE_RE = re.compile(r"^https://onepiece\.fandom\.com/wiki/.+")

# Real-world / publishing / meta tokens that are never valid in-world answers.
# Kept in sync with generate_questions.py's META_NOISE so a regression there is
# caught here instead of shipping.
META_NOISE = {
    "bandai", "toei", "toeianimation", "4kids", "funimation", "shueisha",
    "viz", "vizmedia", "namco", "bandainamco", "crunchyroll", "netflix",
    "space", "n/a", "na", "unknown", "none", "various", "other",
}


def norm_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def near_dupe(a: str, b: str) -> bool:
    ka, kb = norm_key(a), norm_key(b)
    if not ka or not kb:
        return True
    return ka == kb or ka in kb or kb in ka


def validate_one(q: dict) -> str | None:
    """Return an error string if ``q`` is invalid, else None."""
    required = {"category", "type", "difficulty", "question",
                "correct_answer", "incorrect_answers", "options", "source"}
    missing = required - q.keys()
    if missing:
        return f"missing keys: {sorted(missing)}"
    if q["type"] != "multiple":
        return f"type != multiple: {q['type']!r}"
    if q["difficulty"] not in VALID_DIFFICULTIES:
        return f"bad difficulty: {q['difficulty']!r}"
    if q["category"] not in VALID_CATEGORIES:
        return f"unknown category: {q['category']!r}"
    if not str(q["question"]).strip():
        return "empty question text"
    if not SOURCE_RE.match(str(q["source"])):
        return f"bad source url: {q['source']!r}"

    options = q["options"]
    if not isinstance(options, list) or len(options) != 4:
        return f"options must be exactly 4 (got {len(options) if isinstance(options, list) else options})"
    if any(not str(o).strip() for o in options):
        return "empty option present"

    correct = q["correct_answer"]
    exact_hits = sum(1 for o in options if o == correct)
    if exact_hits != 1:
        return f"correct_answer appears {exact_hits}x in options"

    incorrect = q["incorrect_answers"]
    if not isinstance(incorrect, list) or len(incorrect) != 3:
        return f"incorrect_answers must be 3 (got {len(incorrect) if isinstance(incorrect, list) else incorrect})"
    if norm_key(correct) in {norm_key(i) for i in incorrect}:
        return "correct_answer leaked into incorrect_answers"

    # No two options may be near-duplicates of each other.
    for i in range(len(options)):
        for j in range(i + 1, len(options)):
            if near_dupe(options[i], options[j]):
                return f"near-duplicate options: {options[i]!r} ~ {options[j]!r}"

    # Optional teaching aids. Present-but-malformed is a real defect (the app
    # renders them), so validate when present; absence is fine.
    if "explainer" in q and not str(q.get("explainer", "")).strip():
        return "empty explainer"
    if "image" in q and q["image"] is not None and not str(q["image"]).startswith("http"):
        return f"bad image url: {q['image']!r}"

    # No option may be a meta/real-world value or an un-split comma list. A
    # comma-*space* (e.g. "Red Arrows Pirates, The Four Wise Men") signals parse
    # noise; bounties like "1,234 Berries" use commas *without* a space, and Zoan
    # Devil Fruit names use a canonical ", Model: X" qualifier — both are legit and
    # exempted, so this only fires on un-split multi-entity lists.
    for opt in options:
        if norm_key(opt) in META_NOISE:
            return f"meta/real-world option: {opt!r}"
        if ", " in str(opt) and ", Model:" not in str(opt):
            return f"comma-joined list option: {opt!r}"
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", type=Path, default=DEFAULT_IN, help="input questions JSON array")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output question bank for the app")
    ap.add_argument("--check-only", action="store_true", help="validate but do not write output")
    args = ap.parse_args(argv)

    if not args.inp.exists():
        raise SystemExit(f"input not found: {args.inp}\n  run: py pipeline/generate_questions.py")

    questions = json.loads(args.inp.read_text(encoding="utf-8"))
    if not isinstance(questions, list):
        raise SystemExit("input must be a JSON array of questions")

    valid: list[dict] = []
    seen: set[str] = set()
    dropped = Counter()
    examples: dict[str, str] = {}

    for q in questions:
        err = validate_one(q)
        if err:
            reason = err.split(":")[0]
            dropped[reason] += 1
            examples.setdefault(reason, err)
            continue
        key = norm_key(q["question"])
        if key in seen:
            dropped["duplicate question"] += 1
            continue
        seen.add(key)
        valid.append(q)

    total = len(questions)
    print(f"validated {total} questions: {len(valid)} valid, {total - len(valid)} dropped")
    for reason, n in dropped.most_common():
        ex = f"  (e.g. {examples[reason]})" if reason in examples else ""
        print(f"  dropped {n:5d}  {reason}{ex}")

    if not valid:
        raise SystemExit("no valid questions — refusing to write an empty bank")

    print("  by category:")
    for cat, n in Counter(q["category"] for q in valid).most_common():
        print(f"    {n:5d}  {cat}")
    print("  by difficulty:", dict(Counter(q["difficulty"] for q in valid)))

    if args.check_only:
        print("check-only: not writing output")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(valid, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {len(valid)} questions -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
