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

# Subject-prominence tiers from the article-length proxy (see parse_wiki.py).
# Prominence is what makes a fact "easy" at a trivia event — everyone knows Luffy's
# Devil Fruit; almost no one knows a one-off marine's residence.
PROMINENT_LEN = 20000   # tier 2: headline characters / fruits / locations
KNOWN_LEN = 6000        # tier 1: recurring supporting cast

# The event core: names everyone is expected to know, pinned to the top prominence
# tier regardless of the length proxy (some huge pages are split across tabs, which
# deflates the raw article_len). Single-word keys match a whole word in the title;
# multi-word keys match as a substring — see is_must_know().
MUST_KNOW = {
    "luffy", "zoro", "nami", "usopp", "sanji", "chopper", "robin", "franky",
    "brook", "jinbe", "jimbei", "shanks", "buggy", "kaido", "kaidou", "linlin",
    "teach", "newgate", "roger", "rayleigh", "garp", "sengoku", "kuzan", "aokiji",
    "borsalino", "kizaru", "sakazuki", "akainu", "mihawk", "doflamingo",
    "crocodile", "ace", "sabo", "dragon", "hancock", "kuma", "moria", "oden",
    "yamato", "vivi", "koby", "smoker", "enel", "arlong", "katakuri", "marco",
    "vegapunk", "carrot", "momonosuke", "perona", "bartolomeo", "bellamy",
    "big mom", "boa hancock", "trafalgar", "eustass kid", "bon clay",
    "jewelry bonney", "gecko moria", "portgas d", "monkey d", "roronoa",
}
_MUST_MULTI = {k for k in MUST_KNOW if " " in k}
_MUST_SINGLE = {k for k in MUST_KNOW if " " not in k}

# Per-prominence cap on how many questions one entity may spawn. Famous subjects
# carry more of the bank (they have more headline facts worth knowing); one-off
# entities are limited so they can't flood it as they do today.
ENTITY_CAP = {2: 10, 1: 5, 0: 2}

# No single question template may exceed this many questions, so the bank stays
# varied instead of ~40% "which crew is X affiliated with?". Because entities are
# processed most-prominent-first, the slots fill with the famous subjects and the
# trimmed tail is the least-prominent ones.
MAX_PER_TEMPLATE = 300

# Keep any single value ("Marines") from dominating a template.
MAX_PER_ANSWER = 6

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

# Real-world / publishing / meta tokens that leak in from non-canon subjects
# (card-game mascots, license notes) and make nonsensical distractors like
# "Bandai" as a character's origin. Matched on norm_key so punctuation/casing
# don't matter. Kept deliberately small and unambiguous — anything here is never
# an in-world One Piece answer.
META_NOISE = {
    "bandai", "toei", "toeianimation", "4kids", "funimation", "shueisha",
    "viz", "vizmedia", "namco", "bandainamco", "crunchyroll", "netflix",
    "space", "n/a", "na", "unknown", "none", "various", "other",
}


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
    """First listed value, first clause, no qualifiers.

    Used for affiliation / occupation / origin / region where the source packs a
    primary value plus history: ``"Marines; G-2"`` -> ``"Marines"``. We split on
    both ``;`` and ``,`` because the source mixes the two separators
    (``"Marines, Marine 77th Branch"`` -> ``"Marines"``); keeping the comma tail
    would produce distractors like ``"Red Arrows Pirates, The Four Wise Men"`` that
    read as parse noise. Parentheses are stripped *before* the split so
    ``"Grand Line (Ryugu Kingdom; ...)"`` reduces cleanly to ``"Grand Line"``.
    """
    for item in as_list(v):
        head = re.split(r"[;,]", strip_quals(item))[0].strip()
        if head and head.lower() != "none":
            return head
    return None


def is_noise(value) -> bool:
    """True for real-world/meta values that are never a valid in-world answer."""
    return not value or norm_key(value) in META_NOISE


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


def is_must_know(title: str) -> bool:
    """True for the curated event-core names (see ``MUST_KNOW``)."""
    t = title.lower()
    if any(k in t for k in _MUST_MULTI):
        return True
    tokens = set(re.findall(r"[a-z]+", t))
    return bool(tokens & _MUST_SINGLE)


def prominence(title: str, article_len: int) -> int:
    """Subject fame tier: 2 (headline), 1 (recurring), 0 (obscure)."""
    if is_must_know(title) or article_len >= PROMINENT_LEN:
        return 2
    if article_len >= KNOWN_LEN:
        return 1
    return 0


def difficulty(prom: int, depth: int) -> str:
    """Difficulty from subject prominence and template depth.

    ``depth`` is 0 for a headline fact (Devil Fruit, crew, bounty, epithet) and 1
    for a deep cut (residence, region, translation). A deep cut is one tier harder
    than a headline fact about the same subject, so "Luffy's Devil Fruit" is easy
    while "Luffy's residence" is medium.
    """
    score = prom - depth
    if score >= 2:
        return "easy"
    if score >= 1:
        return "medium"
    return "hard"


# Manga-canon saga boundaries by *first* story chapter (upper bound inclusive).
# Ordered; the app's "up to saga" spoiler filter compares against ``order``.
SAGA_BOUNDS = [
    (100, 1, "East Blue"),
    (217, 2, "Alabasta"),
    (302, 3, "Sky Island"),
    (441, 4, "Water 7"),
    (489, 5, "Thriller Bark"),
    (597, 6, "Summit War"),
    (653, 7, "Fish-Man Island"),
    (801, 8, "Dressrosa"),
    (902, 9, "Whole Cake Island"),
    (1057, 10, "Wano Country"),
    (float("inf"), 11, "Final Saga"),
]

_CHAPTER_RE = re.compile(r"Chapter\s+(\d+)")


def saga_from_first(first) -> dict | None:
    """Map an entity's ``first`` field ("Chapter 234; Episode 151") to the saga it
    debuts in, so questions can be scoped by how far a reader has come. Returns
    ``{"name", "order"}`` or None when no chapter can be read."""
    for item in as_list(first):
        m = _CHAPTER_RE.search(str(item))
        if not m:
            continue
        ch = int(m.group(1))
        for max_ch, order, name in SAGA_BOUNDS:
            if ch <= max_ch:
                return {"name": name, "order": order}
    return None


# Title suffixes the wiki uses to disambiguate a *non-canon* variant of an entity
# (a stage-play, novel or explicitly filler version). These cite the canon
# counterpart's chapter in ``first``, so they'd otherwise slip past the chapter
# test below — exclude them by name. Plain "(Wano)"/"(Zombie)"-style qualifiers are
# canon disambiguators and are deliberately not listed.
_NON_CANON_TITLE = ("(non-canon)", "(novel)", "one piece in love")


def is_canon(rec: dict) -> bool:
    """True only for manga-canon entities.

    An entity is canon iff its ``first`` field cites a manga *Chapter* and its
    title isn't flagged as a non-canon variant. Anime-only fillers, movies, TV
    specials, stage shows, video games and novels debut with an
    ``Episode``/``Movie``/game-title ``first`` and never a chapter, so
    ``saga_from_first`` returns None for them — we drop those entirely rather than
    build questions (or distractors) from non-canon material.
    """
    title = rec["title"].lower()
    if any(marker in title for marker in _NON_CANON_TITLE):
        return False
    return saga_from_first(rec["fields"].get("first")) is not None


_KINGDOM_RE = re.compile(r"\bKingdom\b")


def is_kingdom(value) -> bool:
    """True when an affiliation value names a kingdom (e.g. ``Goa Kingdom``)."""
    return bool(value) and bool(_KINGDOM_RE.search(str(value)))


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
                  category, diff, source, explainer=None, image=None):
    # Never build a question around a meta/real-world value (e.g. a card-game
    # mascot whose "origin" parsed to "Bandai").
    if is_noise(correct):
        return None
    rng = random.Random(f"{SEED}:{subject_seed}:{question}")
    distractors = sample_distractors(correct, pool, rng)
    if not distractors:
        return None
    options = distractors + [correct]
    rng.shuffle(options)
    q = {
        "category": category,
        "type": "multiple",
        "difficulty": diff,
        "question": question,
        "correct_answer": correct,
        "incorrect_answers": distractors,
        "options": options,
        "source": source,
    }
    # Optional teaching aids shown after a wrong / "I don't know" answer. The
    # explainer is the subject's wiki lead ("who/what the question was about");
    # image is a portrait URL (populated by the optional image-enrichment step).
    if explainer:
        q["explainer"] = explainer
    if image:
        q["image"] = image
    return q


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
    """Deduped list of normalised values across ``records`` for a field.

    Meta/real-world noise (see ``META_NOISE``) is dropped so it can never surface
    as a distractor.
    """
    seen = {}
    for rec in records:
        val = extract(rec)
        if val and not is_noise(val):
            seen[norm_key(val)] = val
    return list(seen.values())


def generate(by_kind):
    # Drop non-canon entities up front so they seed neither questions nor
    # distractors (a filler-only crew must never appear as a wrong answer either).
    chars = [c for c in by_kind.get("character", []) if is_canon(c)]
    fruits = [fr for fr in by_kind.get("devil_fruit", []) if is_canon(fr)]
    locs = [l for l in by_kind.get("location", []) if is_canon(l)]

    # Kingdom distractor pool: every canon "* Kingdom" named as a place's
    # affiliation or a character's origin/affiliation, so "Which kingdom does X
    # belong to?" draws plausible same-category wrong answers.
    pool_kingdom = build_pool(
        locs + chars,
        lambda r: next(
            (primary(r["fields"].get(k))
             for k in ("affiliation", "origin")
             if is_kingdom(primary(r["fields"].get(k)))),
            None,
        ),
    )

    # Field pools (real values -> plausible distractors).
    pool_df_name = build_pool(
        chars,
        lambda c: combined_df_name(c["fields"].get("dfename"), c["fields"].get("dfname")),
    )
    pool_affiliation = build_pool(chars, lambda c: primary(c["fields"].get("affiliation")))
    pool_occupation = build_pool(chars, lambda c: primary(c["fields"].get("occupation")))
    pool_bounty = build_pool(chars, lambda c: clean_bounty(c["fields"].get("bounty")))
    pool_origin = build_pool(chars, lambda c: primary(c["fields"].get("origin")))
    pool_df_user = build_pool(fruits, lambda fr: clean_name(fr["fields"].get("user")))
    pool_df_type = build_pool(fruits, lambda fr: primary(fr["fields"].get("type")))
    pool_region = build_pool(locs, lambda l: primary(l["fields"].get("region")))
    pool_residence = build_pool(chars, lambda c: primary(c["fields"].get("residence")))
    pool_epithet = build_pool(chars, lambda c: clean_epithet(c["fields"].get("epithet")))
    pool_df_meaning = build_pool(fruits, lambda fr: clean_name(fr["fields"].get("meaning")))

    # Process the most-prominent subjects first so the per-template / per-answer
    # caps fill with the famous entities an event actually asks about, instead of
    # whichever obscure character happened to appear first in the dump.
    chars.sort(
        key=lambda c: (prominence(c["title"], c.get("article_len", 0)),
                       c.get("article_len", 0)),
        reverse=True,
    )
    fruits.sort(key=lambda fr: fr.get("article_len", 0), reverse=True)
    locs.sort(key=lambda l: l.get("article_len", 0), reverse=True)

    out: list[dict] = []
    per_answer = Counter()
    per_entity = Counter()
    per_template = Counter()

    def emit(entity, q, template, prom, saga=None, max_answer=MAX_PER_ANSWER):
        if q is None:
            return
        if per_answer[(q["category"], norm_key(q["correct_answer"]))] >= max_answer:
            return
        if per_entity[entity] >= ENTITY_CAP[prom]:
            return
        if per_template[template] >= MAX_PER_TEMPLATE:
            return
        per_answer[(q["category"], norm_key(q["correct_answer"]))] += 1
        per_entity[entity] += 1
        per_template[template] += 1
        # Spoiler scope: tag which saga the subject debuts in (order for the app's
        # "up to saga" filter, name for display). Omitted when unknown.
        if saga:
            q["saga"] = saga["name"]
            q["sagaOrder"] = saga["order"]
        out.append(q)

    rng = random.Random(SEED)

    # --- Character templates -------------------------------------------------
    # depth 0 = headline fact, depth 1 = deep cut (see difficulty()).
    for c in chars:
        name = c["title"]
        f = c["fields"]
        prom = prominence(name, c.get("article_len", 0))
        saga = saga_from_first(f.get("first"))
        src = c["source"]
        exp = c.get("summary")  # subject's wiki lead — the post-answer explainer

        df = combined_df_name(f.get("dfename"), f.get("dfname"))
        if df:
            emit(name, make_question(
                name, f"Which Devil Fruit did {name} eat?", df, pool_df_name,
                rng, "Devil Fruits", difficulty(prom, 0), src, exp),
                "char_df", prom, saga)

        aff = primary(f.get("affiliation"))
        if aff:
            emit(name, make_question(
                name, f"Which crew or organization is {name} affiliated with?",
                aff, pool_affiliation, rng, "Crews & Organizations",
                difficulty(prom, 0), src, exp), "char_affiliation", prom, saga)

        bounty = clean_bounty(f.get("bounty"))
        if bounty:
            emit(name, make_question(
                name, f"What is {name}'s known bounty?", bounty, pool_bounty,
                rng, "Bounties", difficulty(prom, 0), src, exp),
                "char_bounty", prom, saga)

        # Occupation self-selects for informative answers: the per-answer cap
        # limits generic values ("Pirate" covers ~400 characters) so the surviving
        # questions skew to distinctive roles (Doctor, Vice Admiral, Samurai...).
        occupation = primary(f.get("occupation"))
        if occupation:
            emit(name, make_question(
                name, f"What is {name}'s occupation?", occupation, pool_occupation,
                rng, "Characters", difficulty(prom, 0), src, exp),
                "char_occupation", prom, saga)

        origin = primary(f.get("origin"))
        if origin:
            emit(name, make_question(
                name, f"Where does {name} originate from?", origin, pool_origin,
                rng, "Characters", difficulty(prom, 1), src, exp),
                "char_origin", prom, saga)

        residence = primary(f.get("residence"))
        if residence:
            emit(name, make_question(
                name, f"Where does {name} reside?", residence, pool_residence,
                rng, "Geography", difficulty(prom, 1), src, exp),
                "char_residence", prom, saga)

        epithet = clean_epithet(f.get("epithet"))
        if epithet:
            emit(name, make_question(
                name, f"By what epithet is {name} known?", epithet, pool_epithet,
                rng, "Characters", difficulty(prom, 0), src, exp),
                "char_epithet", prom, saga)

    # --- Devil Fruit templates ----------------------------------------------
    for fr in fruits:
        f = fr["fields"]
        fruit_name = combined_df_name(f.get("ename"), f.get("rname") or fr["title"])
        prom = prominence(fr["title"], fr.get("article_len", 0))
        saga = saga_from_first(f.get("first"))
        src = fr["source"]
        exp = fr.get("summary")

        user = clean_name(f.get("user"))
        if user:
            emit(fr["title"], make_question(
                fr["title"], f"Who is the user of the {fruit_name}?", user,
                pool_df_user, rng, "Devil Fruits", difficulty(prom, 0), src, exp),
                "fruit_user", prom, saga)

        dtype = primary(f.get("type"))
        if dtype and dtype.lower() != "unknown":
            emit(fr["title"], make_question(
                fr["title"], f"What type of Devil Fruit is the {fruit_name}?",
                dtype, pool_df_type, rng, "Devil Fruits", difficulty(prom, 0), src,
                exp), "fruit_type", prom, saga, max_answer=MAX_PER_ANSWER_CLASS)

        meaning = clean_name(f.get("meaning"))
        if meaning:
            emit(fr["title"], make_question(
                fr["title"], f"What does the name of the {fruit_name} translate to?",
                meaning, pool_df_meaning, rng, "Devil Fruits", difficulty(prom, 1),
                src, exp), "fruit_meaning", prom, saga)

    # --- Location templates --------------------------------------------------
    for l in locs:
        f = l["fields"]
        prom = prominence(l["title"], l.get("article_len", 0))
        saga = saga_from_first(f.get("first"))
        exp = l.get("summary")
        region = primary(f.get("region"))
        if region:
            emit(l["title"], make_question(
                l["title"], f"In which region of the world is {l['title']} located?",
                region, pool_region, rng, "Geography", difficulty(prom, 1),
                l["source"], exp), "loc_region", prom, saga)

        # Repurposed from the old vague "Which faction is X affiliated with?"
        # (answers ranged over kingdoms, crews, families and even characters). We
        # now only ask when the affiliation is a *kingdom*, giving a crisp
        # "Which kingdom does X belong to?" with other kingdoms as distractors;
        # non-kingdom affiliations are dropped rather than asked vaguely.
        affil = primary(f.get("affiliation"))
        if is_kingdom(affil):
            emit(l["title"], make_question(
                l["title"], f"Which kingdom does {l['title']} belong to?",
                affil, pool_kingdom, rng, "Geography",
                difficulty(prom, 1), l["source"], exp), "loc_kingdom", prom, saga)

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
