#!/usr/bin/env python3
"""Turn parsed wiki facts into multiple-choice questions (deterministic).

Phase 3, step 2. Reads ``wiki-data/facts.jsonl`` (produced by ``parse_wiki.py``)
and emits template-generated MCQs. **No hallucination**: every question and its
correct answer come straight from an infobox field; distractors are sampled from
*real* values of the same field on other entities, so wrong answers are always
plausible and never invented.

Each template is ``(question text, correct field, distractor pool)``. See the
``TEMPLATES`` section below. For every question we:

* normalise the raw field (lists/translations/qualifiers -> one clean value),
* draw 3 distractors from the same-field pool, rejecting near-duplicates and any
  value that collides with the correct answer,
* tag ``category`` / ``difficulty`` (article-length proxy) / ``source`` (wiki URL),
* pre-shuffle ``options`` deterministically (seeded per question) so runs are
  reproducible — the app reshuffles at load anyway.

Generation stays liberal about volume; per-answer and per-entity caps here keep
one value ("Marines") or one character from dominating. Schema enforcement, global
dedupe and near-duplicate rejection are ``validate.py``'s job.

Output (under ``wiki-data/``, gitignored):
    questions.generated.json   a JSON array of question objects.

Usage:
    py pipeline/generate_questions.py
    py pipeline/generate_questions.py --out other.json
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WIKI_DATA_DIR = REPO_ROOT / "wiki-data"
DEFAULT_FACTS = WIKI_DATA_DIR / "facts.jsonl"
DEFAULT_OUT = WIKI_DATA_DIR / "questions.generated.json"

# Deterministic global seed; every question also mixes in its own subject so the
# option order is stable per question regardless of generation order.
SEED = 20260722

# article_len prominence proxy -> difficulty tier (see parse_wiki.py).
EASY_LEN = 20000
MEDIUM_LEN = 6000

# Keep any single value ("Marines") from dominating a template, and any single
# entity from spawning too many questions.
MAX_PER_ANSWER = 6
MAX_PER_ENTITY = 6

# Small-cardinality fields (a Devil Fruit *class* is one of only a handful of
# values) legitimately answer many questions, so they get a looser per-answer cap
# than name-shaped fields where MAX_PER_ANSWER prevents one crew swamping the bank.
MAX_PER_ANSWER_CLASS = 30


# --------------------------------------------------------------------------- #
# Value normalisation
# --------------------------------------------------------------------------- #

_PAREN = re.compile(r"\s*\([^()]*\)")          # balanced "(VIZ)", "(former)" ...
_DANGLE = re.compile(r"\s*\(.*$")               # unbalanced "(Ryugu Kingdom" -> ""
_BOUNTY = re.compile(r"^[\d,]+$")               # a bare comma-grouped number


def as_list(v):
    """Normalise to a list, dropping missing values so ``None`` never becomes the
    literal string ``"None"`` downstream."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def strip_quals(s: str) -> str:
    """Drop parenthetical qualifiers/translation notes and surrounding cruft.

    Handles both balanced ``(...)`` groups and a dangling ``(`` left behind when
    an earlier ``;`` split landed inside the parentheses.
    """
    s = _PAREN.sub("", str(s))
    s = _DANGLE.sub("", s)
    return s.strip(" ;,.").strip()


def primary(v) -> str | None:
    """First listed value, first ``;``-separated clause, no qualifiers.

    Used for affiliation / occupation / origin / region where the source packs a
    primary value plus history: ``"Marines; G-2"`` -> ``"Marines"``. Parentheses
    are stripped *before* the ``;`` split so ``"Grand Line (Ryugu Kingdom; ...)"``
    reduces cleanly to ``"Grand Line"``.
    """
    for item in as_list(v):
        head = strip_quals(item).split(";")[0].strip()
        if head and head.lower() != "none":
            return head
    return None


def clean_name(v) -> str | None:
    """First listed name with translation notes stripped (Devil Fruit names)."""
    for item in as_list(v):
        head = strip_quals(str(item).split(";")[0])
        if head and "{{{" not in head and head.lower() != "none":
            return head
    return None


def combined_df_name(english, romaji) -> str | None:
    """Render a Devil Fruit as ``"English / Romaji"`` (e.g. ``"Gum-Gum Fruit /
    Gomu Gomu no Mi"``).

    Both halves are ``clean_name``-normalised (first listed value, translation
    notes stripped). Falls back to whichever half exists, and drops the ``" / "``
    when the two coincide so a fruit known only by its romaji doesn't read
    ``"Gomu Gomu no Mi / Gomu Gomu no Mi"``.
    """
    eng = clean_name(english)
    rom = clean_name(romaji)
    if eng and rom and norm_key(eng) != norm_key(rom):
        return f"{eng} / {rom}"
    return eng or rom


def clean_bounty(v) -> str | None:
    """Return a canonical ``"1,234 Berries"`` string, or None.

    Only accepts an official bare comma-number. Parenthesised amounts (estimates),
    ``Unknown`` and star ratings are rejected — they aren't crisp MCQ answers.
    """
    for item in as_list(v):
        s = str(item).strip()
        if _BOUNTY.match(s) and len(s.replace(",", "")) >= 4:
            return f"{s} Berries"
    return None


_NIHONGO = re.compile(r"\{\{Nihongo\|([^|}]*)")


def clean_epithet(v) -> str | None:
    """Pull the display (English) epithet out of a ``{{Nihongo|...}}`` template.

    Epithets are iconic ("Pirate Hunter", "God Enel") but the raw field is noisy:
    only the templated ``{{Nihongo|"Chaser"|...}}`` form carries a clean English
    string. Bare dub-note junk (``"(former)"``, ``"Beast Breaker (4Kids)"``) has no
    ``Nihongo`` wrapper, so it never matches and is dropped — we keep quality high
    at the cost of volume.
    """
    for item in as_list(v):
        m = _NIHONGO.search(str(item))
        if not m:
            continue
        e = strip_quals(m.group(1).replace('"', "").strip())
        if e and len(e) > 1 and "{{{" not in e:
            return e
    return None


def difficulty(article_len: int) -> str:
    if article_len >= EASY_LEN:
        return "easy"
    if article_len >= MEDIUM_LEN:
        return "medium"
    return "hard"


def norm_key(s: str) -> str:
    """Loose key for collision checks between options."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# --------------------------------------------------------------------------- #
# Distractor sampling
# --------------------------------------------------------------------------- #

def near_dupe(a: str, b: str) -> bool:
    ka, kb = norm_key(a), norm_key(b)
    if not ka or not kb:
        return True
    return ka == kb or ka in kb or kb in ka


def sample_distractors(correct: str, pool: list[str], rng: random.Random, n: int = 3):
    """Pick ``n`` distinct distractors from ``pool``, skipping near-dupes.

    ``pool`` is the deduped list of every real value for this field. Returns None
    if fewer than ``n`` plausible distinct distractors survive filtering.
    """
    chosen: list[str] = []
    candidates = pool[:]
    rng.shuffle(candidates)
    for cand in candidates:
        if near_dupe(cand, correct):
            continue
        if any(near_dupe(cand, c) for c in chosen):
            continue
        chosen.append(cand)
        if len(chosen) == n:
            return chosen
    return None


def make_question(subject_seed, question, correct, pool, rng_master,
                  category, diff, source):
    rng = random.Random(f"{SEED}:{subject_seed}:{question}")
    distractors = sample_distractors(correct, pool, rng)
    if not distractors:
        return None
    options = distractors + [correct]
    rng.shuffle(options)
    return {
        "category": category,
        "type": "multiple",
        "difficulty": diff,
        "question": question,
        "correct_answer": correct,
        "incorrect_answers": distractors,
        "options": options,
        "source": source,
    }


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

def load_facts(path: Path):
    by_kind = defaultdict(list)
    with path.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            by_kind[rec["kind"]].append(rec)
    return by_kind


def build_pool(records, extract):
    """Deduped list of normalised values across ``records`` for a field."""
    seen = {}
    for rec in records:
        val = extract(rec)
        if val:
            seen[norm_key(val)] = val
    return list(seen.values())


def generate(by_kind):
    chars = by_kind.get("character", [])
    fruits = by_kind.get("devil_fruit", [])
    locs = by_kind.get("location", [])

    # Field pools (real values -> plausible distractors).
    pool_df_name = build_pool(
        chars,
        lambda c: combined_df_name(c["fields"].get("dfename"), c["fields"].get("dfname")),
    )
    pool_affiliation = build_pool(chars, lambda c: primary(c["fields"].get("affiliation")))
    pool_bounty = build_pool(chars, lambda c: clean_bounty(c["fields"].get("bounty")))
    pool_origin = build_pool(chars, lambda c: primary(c["fields"].get("origin")))
    pool_df_user = build_pool(fruits, lambda fr: clean_name(fr["fields"].get("user")))
    pool_df_type = build_pool(fruits, lambda fr: primary(fr["fields"].get("type")))
    pool_region = build_pool(locs, lambda l: primary(l["fields"].get("region")))
    pool_residence = build_pool(chars, lambda c: primary(c["fields"].get("residence")))
    pool_epithet = build_pool(chars, lambda c: clean_epithet(c["fields"].get("epithet")))
    pool_df_meaning = build_pool(fruits, lambda fr: clean_name(fr["fields"].get("meaning")))
    pool_loc_affil = build_pool(locs, lambda l: primary(l["fields"].get("affiliation")))

    out: list[dict] = []
    per_answer = Counter()
    per_entity = Counter()

    def emit(entity, q, max_answer=MAX_PER_ANSWER):
        if q is None:
            return
        if per_answer[(q["category"], norm_key(q["correct_answer"]))] >= max_answer:
            return
        if per_entity[entity] >= MAX_PER_ENTITY:
            return
        per_answer[(q["category"], norm_key(q["correct_answer"]))] += 1
        per_entity[entity] += 1
        out.append(q)

    rng = random.Random(SEED)

    # --- Character templates -------------------------------------------------
    for c in chars:
        name = c["title"]
        f = c["fields"]
        diff = difficulty(c.get("article_len", 0))
        src = c["source"]

        df = combined_df_name(f.get("dfename"), f.get("dfname"))
        if df:
            emit(name, make_question(
                name, f"Which Devil Fruit did {name} eat?", df, pool_df_name,
                rng, "Devil Fruits", diff, src))

        aff = primary(f.get("affiliation"))
        if aff:
            emit(name, make_question(
                name, f"Which crew or organization is {name} affiliated with?",
                aff, pool_affiliation, rng, "Crews & Organizations", diff, src))

        bounty = clean_bounty(f.get("bounty"))
        if bounty:
            emit(name, make_question(
                name, f"What is {name}'s known bounty?", bounty, pool_bounty,
                rng, "Bounties", diff, src))

        origin = primary(f.get("origin"))
        if origin:
            emit(name, make_question(
                name, f"Where does {name} originate from?", origin, pool_origin,
                rng, "Characters", diff, src))

        residence = primary(f.get("residence"))
        if residence:
            emit(name, make_question(
                name, f"Where does {name} reside?", residence, pool_residence,
                rng, "Geography", diff, src))

        epithet = clean_epithet(f.get("epithet"))
        if epithet:
            emit(name, make_question(
                name, f"By what epithet is {name} known?", epithet, pool_epithet,
                rng, "Characters", diff, src))

    # --- Devil Fruit templates ----------------------------------------------
    for fr in fruits:
        f = fr["fields"]
        fruit_name = combined_df_name(f.get("ename"), f.get("rname") or fr["title"])
        diff = difficulty(fr.get("article_len", 0))
        src = fr["source"]

        user = clean_name(f.get("user"))
        if user:
            emit(fr["title"], make_question(
                fr["title"], f"Who is the user of the {fruit_name}?", user,
                pool_df_user, rng, "Devil Fruits", diff, src))

        dtype = primary(f.get("type"))
        if dtype and dtype.lower() != "unknown":
            emit(fr["title"], make_question(
                fr["title"], f"What type of Devil Fruit is the {fruit_name}?",
                dtype, pool_df_type, rng, "Devil Fruits", diff, src),
                max_answer=MAX_PER_ANSWER_CLASS)

        meaning = clean_name(f.get("meaning"))
        if meaning:
            emit(fr["title"], make_question(
                fr["title"], f"What does the name of the {fruit_name} translate to?",
                meaning, pool_df_meaning, rng, "Devil Fruits", diff, src))

    # --- Location templates --------------------------------------------------
    for l in locs:
        f = l["fields"]
        diff = difficulty(l.get("article_len", 0))
        region = primary(f.get("region"))
        if region:
            emit(l["title"], make_question(
                l["title"], f"In which region of the world is {l['title']} located?",
                region, pool_region, rng, "Geography", diff, l["source"]))

        affil = primary(f.get("affiliation"))
        if affil:
            emit(l["title"], make_question(
                l["title"], f"Which faction is {l['title']} affiliated with?",
                affil, pool_loc_affil, rng, "Crews & Organizations", diff, l["source"]))

    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--facts", type=Path, default=DEFAULT_FACTS, help="input facts.jsonl")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output questions JSON array")
    args = ap.parse_args(argv)

    if not args.facts.exists():
        raise SystemExit(f"facts not found: {args.facts}\n  run: py pipeline/parse_wiki.py")

    by_kind = load_facts(args.facts)
    questions = generate(by_kind)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(questions, ensure_ascii=False, indent=1), encoding="utf-8")

    cats = Counter(q["category"] for q in questions)
    diffs = Counter(q["difficulty"] for q in questions)
    print(f"generated {len(questions)} questions -> {args.out}")
    print("  by category:")
    for cat, n in cats.most_common():
        print(f"    {n:5d}  {cat}")
    print("  by difficulty:", dict(diffs))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
