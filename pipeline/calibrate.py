#!/usr/bin/env python3
"""Merge rater verdicts and mechanical signals into one difficulty score per question.

This is where the two halves of calibration meet. ``rate_questions.py`` says how
obscure a fact is; ``calibrate_signals.py`` says whether the item can be beaten
without knowing it. Neither is sufficient alone — a genuinely obscure fact with
three invented distractors is an easy question, and a famous fact with four real
competing options is not.

Output is ``calibration/difficulty.json``, keyed by ``norm_key(question)`` — the
same key ``validate.py`` already uses for de-duplication and the golden check — so
the overlay applies to the built bank without touching either question source.

Scoring, in order:

1. **Base score.** The median of the raters' knowledge scores (median, not mean, so
   one outlier rater can't move a question a whole tier). With no ratings yet, hold
   the question's authored tier and record ``basis: "authored"``, so a partially
   calibrated bank stays honest about which half of it has actually been graded.
2. **Guessability penalty.** Subtract for raters who marked the item guessable and
   for each mechanical tell that fired. A fact nobody knows still isn't hard if the
   option set hands it over.
3. **Prominence ceiling.** A headline subject can't come out hard on the coarse
   path alone — but raters, who see the actual fact rather than a page-length proxy,
   can override it. Every clamp is recorded in ``reasons``.
4. **Tier.** Cut points below, deliberately module constants: re-tuning the balance
   of the bank is a re-run of this file, not a re-grade.

Usage:
    py pipeline/calibrate.py
    py pipeline/calibrate.py --review        # disagreements and clamps
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent
CALIB_DIR = PIPELINE / "calibration"
DEFAULT_SIGNALS = CALIB_DIR / "signals.json"
DEFAULT_RATINGS = CALIB_DIR / "ratings.json"
DEFAULT_OUT = CALIB_DIR / "difficulty.json"

# Tier cut points. Tunable: change these, re-run, rebuild — no re-grading.
EASY_MAX = 33
MEDIUM_MAX = 66

# How far a unanimous "you can guess this" drags a score down. Sized so a fully
# guessable item drops roughly one tier rather than bottoming out — a guessable
# question about an obscure fact is still harder than a guessable one about Luffy.
RATER_GUESS_PENALTY = 30

# Per-signal penalties, and the ceiling on their sum. Capped because the flags
# overlap heavily (a numeric ladder is usually also a lone-canon-option), and
# stacking them unclamped would push every flagged item to zero.
SIGNAL_PENALTY = {
    "stem_leak": 30,
    "cross_domain": 25,
    "numeric_ladder": 25,
    "lone_canon_option": 20,
    "meta_noise_option": 20,
    "weak_distractors": 15,
    "answer_len_outlier": 10,
}
MAX_SIGNAL_PENALTY = 40

# Prior used when a question has no ratings yet: its own authored tier. Deliberately
# *not* the prominence proxy — scoring the unrated bank from article length reproduces
# the exact bug this work exists to fix (Wanda has a long page, so the proxy calls her
# affiliation easy). Anchoring on the authored label instead means an unrated question
# keeps its tier unless a mechanical tell demotes it: strictly better than today, and
# it never invents a new miscalibration. Ratings replace this entirely.
AUTHORED_PRIOR = {"easy": 20, "medium": 50, "hard": 80}
UNKNOWN_PRIOR = 50

# A rater median at or above this overrides the prominence ceiling: the raters read
# the actual fact, the proxy only saw how long the subject's wiki page is.
RATER_OVERRIDE = 70

# Rater disagreement worth a human look.
SPREAD_REVIEW = 30


def tier_for(score: int) -> str:
    if score <= EASY_MAX:
        return "easy"
    if score <= MEDIUM_MAX:
        return "medium"
    return "hard"


def score_one(signal: dict, ratings: dict | None) -> dict:
    """Combine one question's signals and rater verdicts into a scored entry."""
    reasons: list[str] = []
    rater_scores = []
    guess_votes = 0

    if ratings:
        for name, rating in ratings.items():
            if not isinstance(rating, dict) or "knowledge_score" not in rating:
                continue                      # cache metadata (question text, key)
            rater_scores.append(int(rating["knowledge_score"]))
            guess_votes += bool(rating.get("guessable"))

    if rater_scores:
        base = int(round(statistics.median(rater_scores)))
        basis = "raters"
        spread = max(rater_scores) - min(rater_scores)
    else:
        base = AUTHORED_PRIOR.get(signal.get("authored_difficulty"), UNKNOWN_PRIOR)
        basis = "authored"
        spread = 0
        reasons.append("no ratings; holding the authored tier pending a rating pass")

    penalty = 0
    if rater_scores and guess_votes:
        share = guess_votes / len(rater_scores)
        penalty += round(RATER_GUESS_PENALTY * share)
        reasons.append(f"{guess_votes}/{len(rater_scores)} raters called it guessable")

    flags = signal.get("guessability_flags", [])
    signal_penalty = min(MAX_SIGNAL_PENALTY, sum(SIGNAL_PENALTY.get(f, 0) for f in flags))
    if signal_penalty:
        penalty += signal_penalty
        reasons.append(f"signals: {', '.join(flags)} (-{signal_penalty})")

    score = max(0, min(100, base - penalty))

    # Prominence ceiling — a headline subject shouldn't land in the hard tier off the
    # coarse path alone. Gated on the hand-curated must-know list rather than on
    # prominence, because prominence tier 2 is also reached by article length, and a
    # minor character with a long wiki page (Gloriosa) is exactly the case where that
    # proxy is wrong. Raters, who read the actual fact, can override the cap.
    if signal.get("must_know") and score > MEDIUM_MAX:
        if not (rater_scores and statistics.median(rater_scores) >= RATER_OVERRIDE):
            reasons.append(f"capped at {MEDIUM_MAX}: headline subject "
                           f"({signal.get('subject')!r}) without strong rater support")
            score = MEDIUM_MAX

    return {
        "score": score,
        "tier": tier_for(score),
        "basis": basis,
        "spread": spread,
        "flags": flags,
        "reasons": reasons,
        "authored_difficulty": signal.get("authored_difficulty"),
        "category": signal.get("category"),
        "question": signal.get("question"),
    }


def print_report(entries: dict[str, dict], review: bool, limit: int) -> None:
    tiers = Counter(e["tier"] for e in entries.values())
    authored = Counter(e["authored_difficulty"] for e in entries.values())
    basis = Counter(e["basis"] for e in entries.values())

    print(f"\nbasis: {dict(basis)}")
    print("\ntier movement (authored -> calibrated):")
    print(f"  {'':10s} {'easy':>8s} {'medium':>8s} {'hard':>8s}")
    for old in ("easy", "medium", "hard"):
        row = Counter(e["tier"] for e in entries.values() if e["authored_difficulty"] == old)
        print(f"  {old:10s} " + " ".join(f"{row.get(t, 0):8d}" for t in ("easy", "medium", "hard")))
    print(f"\n  authored:   {dict(authored)}")
    print(f"  calibrated: {dict(tiers)}")

    moved = [e for e in entries.values() if e["tier"] != e["authored_difficulty"]]
    print(f"\n{len(moved)} of {len(entries)} questions change tier "
          f"({100 * len(moved) / max(1, len(entries)):.0f}%)")

    if not review:
        return

    demoted = sorted(
        (e for e in entries.values()
         if e["authored_difficulty"] == "hard" and e["tier"] != "hard"),
        key=lambda e: e["score"])
    print(f"\n--- demoted from hard ({len(demoted)}) ---")
    for entry in demoted[:limit]:
        print(f"\n  [{entry['score']}->{entry['tier']}] {entry['question']}")
        for reason in entry["reasons"]:
            print(f"    {reason}")

    disputed = [e for e in entries.values() if e["spread"] >= SPREAD_REVIEW]
    print(f"\n--- rater disagreement >= {SPREAD_REVIEW} ({len(disputed)}) ---")
    for entry in sorted(disputed, key=lambda e: -e["spread"])[:limit]:
        print(f"  [spread {entry['spread']}, score {entry['score']}] {entry['question']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--signals", type=Path, default=DEFAULT_SIGNALS, help="calibrate_signals.py output")
    ap.add_argument("--ratings", type=Path, default=DEFAULT_RATINGS, help="rate_questions.py cache")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="difficulty overlay to write")
    ap.add_argument("--review", action="store_true", help="print demotions and rater disagreements")
    ap.add_argument("--limit", type=int, default=20, help="examples per review section")
    args = ap.parse_args(argv)

    if not args.signals.exists():
        raise SystemExit(f"signals not found: {args.signals}\n  run: py pipeline/calibrate_signals.py")
    signals = json.loads(args.signals.read_text(encoding="utf-8"))

    # Ratings are cached by content hash — question *plus* option set — and the signals
    # carry the same hash, so a verdict only applies to the exact item that was rated.
    # Matching on the question text instead would let a stale rating survive an option
    # repair, which is the one move DIFFICULTY.md prefers over demotion: repair a
    # cross-domain distractor and the item should go back to being judged on knowledge,
    # not keep the guessable verdict earned by the options it no longer has.
    ratings_by_hash: dict[str, dict] = {}
    if args.ratings.exists():
        ratings_by_hash = json.loads(args.ratings.read_text(encoding="utf-8"))
        print(f"loaded ratings for {len(ratings_by_hash)} rated items from {args.ratings.name}")
    else:
        print(f"no ratings yet ({args.ratings.name} missing) — holding authored tiers and "
              f"applying signal demotions only; re-run after rate_questions.py to upgrade")

    entries = {key: score_one(signal, ratings_by_hash.get(signal.get("hash")))
               for key, signal in signals.items()}
    stale = len(ratings_by_hash) - sum(1 for e in entries.values() if e["basis"] == "raters")
    if stale > 0:
        print(f"  {stale} cached rating(s) no longer match any item — the question or its "
              f"options changed since; re-run rate_questions.py to re-rate those")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"scored {len(entries)} questions -> {args.out}")
    print_report(entries, args.review, args.limit)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
