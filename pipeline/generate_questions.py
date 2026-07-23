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

# There are only 11 sagas, so "which saga does X debut in?" would hit MAX_PER_ANSWER
# almost immediately. This looser cap lets the arc-debut template contribute a real
# slice of the bank without letting any one saga dominate it.
MAX_PER_ANSWER_SAGA = 14

# An "occupation" that only one character in the whole dump carries (e.g. "God of
# Skypiea", "Biwa hoshi") is really a one-off title, not a gradeable occupation, and
# it makes an odd-one-out distractor. We only ask/offer occupations shared by at
# least this many characters, so both answers and distractors are recognisable roles.
MIN_OCCUPATION_FREQ = 2


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


def bounty_amount(v) -> int | None:
    """Numeric value of a character's canonical bounty (``clean_bounty`` -> int), or
    None. Used to rank pirates by bounty for the "highest bounty?" template."""
    b = clean_bounty(v)
    if not b:
        return None
    digits = b.replace(",", "").split()[0]
    return int(digits) if digits.isdigit() else None


_NIHONGO = re.compile(r"\{\{Nihongo\|([^|}]*)")
_QUOTES = re.compile(r'["“”]')          # any double-quote mark (also mid-string)
# Dub-only variants ("Sky Punk (4Kids...)", "...in the edited dub"). Whole *list
# items* that are purely a dub note are skipped in favour of the canon epithet.
_DUB_NOTE = re.compile(r"4Kids|Funimation|\bVIZ\b|edited dub|English version|subs",
                       re.IGNORECASE)


def clean_epithet(v) -> str | None:
    """Extract a clean display epithet from the ``epithet`` field.

    Epithets are iconic ("Pirate Hunter", "Cat Burglar", "Fire Fist"). The parser
    resolves the wiki's ``{{Nihongo|...}}`` wrapper to a plain string ahead of us,
    so the field arrives as a quoted string or a list of them
    (``'"Chaser"'``, ``['"Sengoku the Buddha"', '"The Resourceful General"']``).
    We take the first canon entry, drop parenthetical dub/translation notes and the
    surrounding quotes, and skip list items that are *only* a dub variant so a
    character keeps their real epithet rather than a 4Kids rename.
    """
    for item in as_list(v):
        s = str(item)
        m = _NIHONGO.search(s)          # tolerate a still-wrapped value, just in case
        if m:
            s = m.group(1)
        if _DUB_NOTE.search(s) and "(" not in s:
            continue                    # a bare dub-only alt; prefer the next entry
        e = _QUOTES.sub("", strip_quals(s.split(";")[0])).strip(" ;,.")
        if e and len(e) > 2 and "{{" not in e and e.lower() not in (
                "former", "formerly", "n/a", "none", "unknown"):
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

# Titles of non-canon *works* (movies, games, novels, spin-off/parody manga, stage
# shows) as they appear in a ``first`` field. A chapter citation alongside one of
# these is a promotional cover/cameo tie-in, not a story debut: e.g. Uta
# ("Chapter 1055 (flashback); One Piece Film: Red"), Zephyr ("One Piece Film: Z;
# Chapter 691 (cover)"), the Odyssey game cast, and the Chin Piece / One Piece in
# Love / MiraBato / novel casts. Matched case-insensitively against ``first``.
_NON_CANON_WORK = re.compile(
    r"One Piece Film|Film:|\bMovie\b|Odyssey|One Piece Party|One Piece in Love|"
    r"One Piece Short|One Piece School|Chin Piece|Fischer's|Shokugeki no Sanji|"
    r"One Piece novel|\bnovel\b|Chopperman|MiraBato|Dream Adventure Log|Stampede|"
    r"Heart of Gold|Strong World|Dance Carnival|Live Attraction|Omake Manga Corner|"
    r"episode A\b|3D2Y|Glorious Island|Cross Epoch|Romance Dawn Story",
    re.IGNORECASE,
)

# A *clean* chapter citation — a bare "Chapter 907" clause, a genuine story debut.
# Deliberately excludes qualified forms the wiki uses for non-story appearances:
# "Chapter 691 (cover)", "Chapter 155 (mentioned)", "Chapter 1055 (flashback)",
# "Chapter 817 cover", and work-prefixed ones like "Chin Piece Chapter 1" or
# "novel HEROINES, Chapter 3" (none of which match ^Chapter <n>$).
_CLEAN_CHAPTER = re.compile(r"^Chapter\s+\d+\.?$")


def _has_non_canon_work(first) -> bool:
    """True if any ``first`` entry names a non-canon work (movie/game/novel/etc.)."""
    return any(_NON_CANON_WORK.search(str(item)) for item in as_list(first))


def _has_clean_chapter(first) -> bool:
    """True if ``first`` carries a bare "Chapter <n>" story debut (see
    ``_CLEAN_CHAPTER``). Splits on ``;`` so "Chapter 907; Episode 887" counts."""
    for item in as_list(first):
        for clause in str(item).split(";"):
            if _CLEAN_CHAPTER.match(clause.strip()):
                return True
    return False


def is_canon(rec: dict) -> bool:
    """True only for manga-canon entities.

    An entity is canon iff its ``first`` field cites a manga *Chapter* and its
    title isn't flagged as a non-canon variant. Anime-only fillers debut with an
    ``Episode``-only ``first`` (never a chapter), so ``saga_from_first`` returns
    None for them and they're dropped.

    Movies, games, novels and spin-off/parody manga are trickier: their original
    characters often get a promotional manga *cover* or *flashback* cameo, which
    cites a chapter and would otherwise pass. We drop an entity that names a
    non-canon work in ``first`` **unless** it also has a clean standalone chapter
    debut — that keeps canon figures who merely cameo'd in a film (e.g. Vice
    Admirals Gion and Tokikake, who debut in Chapter 907 but appear in Film Gold)
    while dropping the film/game/novel originals (Uta, Zephyr, Tesoro, the Odyssey
    and Chin Piece casts, ...).
    """
    title = rec["title"].lower()
    if any(marker in title for marker in _NON_CANON_TITLE):
        return False
    first = rec["fields"].get("first")
    if saga_from_first(first) is None:
        return False
    if _has_non_canon_work(first) and not _has_clean_chapter(first):
        return False
    return True


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


def sample_distractors(correct: str, pool, rng: random.Random, n: int = 3,
                       ctx_tier=None, ctx_saga=None):
    """Pick ``n`` distinct distractors from ``pool``, skipping near-dupes.

    ``pool`` is the deduped list of ``{"v", "tier", "saga"}`` entries for this
    field (see ``build_pool``). Candidates are shuffled for variety, then stably
    ordered so that wrong answers from the **same prominence tier** and the
    **same-or-earlier saga** as the subject come first — a famous East-Blue-era
    question gets famous, era-appropriate distractors instead of a random deep cut.
    Ordering is only a preference: the whole pool is still traversed, so volume is
    unchanged. Returns None if fewer than ``n`` plausible distinct distractors
    survive.
    """
    candidates = [p for p in pool if not near_dupe(p["v"], correct)]
    rng.shuffle(candidates)

    def closeness(p):
        # Prefer same prominence tier (smaller gap first). Unknown tiers are neutral.
        if ctx_tier is not None and p["tier"] is not None:
            tier_gap = abs(p["tier"] - ctx_tier)
        else:
            tier_gap = 0
        # Prefer distractors that already exist by the subject's debut saga; later
        # debuts are pushed back (and mildly penalised by how much later).
        if ctx_saga is not None and p["saga"] is not None:
            saga_gap = 0 if p["saga"] <= ctx_saga else (p["saga"] - ctx_saga)
        else:
            saga_gap = 0
        return (tier_gap, saga_gap)

    candidates.sort(key=closeness)  # stable: keeps the shuffle order within a tie
    chosen: list[str] = []
    for p in candidates:
        if any(near_dupe(p["v"], c) for c in chosen):
            continue
        chosen.append(p["v"])
        if len(chosen) == n:
            return chosen
    return None


def lead(fact: str, summary) -> str:
    """Compose an answer-specific explainer: a one-line statement of the fact just
    asked, followed by the subject's wiki lead for context.

    The old behaviour handed every question about a subject that subject's generic
    ``summary``, so all four Vegapunk questions showed the same "leading scientist
    of the SSG" blurb and none of them explained the actual answer. Prefixing the
    fact ("Vegapunk ate the Nomi Nomi no Mi.") makes the teaching aid specific to
    what was asked while keeping the lead for colour.
    """
    fact = fact.strip()
    if fact and fact[-1] not in ".!?":
        fact += "."
    summary = (summary or "").strip()
    return f"{fact} {summary}".strip() if summary else fact


def make_question(subject_seed, question, correct, pool, rng_master,
                  category, diff, source, explainer=None, image=None,
                  ctx_tier=None, ctx_saga=None):
    # Never build a question around a meta/real-world value (e.g. a card-game
    # mascot whose "origin" parsed to "Bandai").
    if is_noise(correct):
        return None
    rng = random.Random(f"{SEED}:{subject_seed}:{question}")
    distractors = sample_distractors(correct, pool, rng,
                                     ctx_tier=ctx_tier, ctx_saga=ctx_saga)
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

    Each entry is a dict ``{"v", "tier", "saga"}`` carrying the value plus the
    prominence tier and debut-saga order of the entity it came from, so
    ``sample_distractors`` can prefer era-/tier-appropriate wrong answers. First
    occurrence of a value wins (records aren't prominence-sorted yet at pool-build
    time, but the tier is only a sampling *preference*, not a hard filter).

    Meta/real-world noise (see ``META_NOISE``) is dropped so it can never surface
    as a distractor.
    """
    seen = {}
    for rec in records:
        val = extract(rec)
        if not val or is_noise(val):
            continue
        k = norm_key(val)
        if k in seen:
            continue
        saga = saga_from_first(rec.get("fields", {}).get("first"))
        seen[k] = {
            "v": val,
            "tier": prominence(rec.get("title", ""), rec.get("article_len", 0)),
            "saga": saga["order"] if saga else None,
        }
    return list(seen.values())


def saga_pool():
    """Distractor pool of every saga name (for arc-debut questions). Tier/saga are
    left ``None`` so sampling treats all sagas as equally plausible distractors."""
    return [{"v": name, "tier": None, "saga": None} for _, _, name in SAGA_BOUNDS]


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
    pool_region = build_pool(locs, lambda l: primary(l["fields"].get("region")))
    pool_residence = build_pool(chars, lambda c: primary(c["fields"].get("residence")))
    pool_epithet = build_pool(chars, lambda c: clean_epithet(c["fields"].get("epithet")))

    # Character-name pool: the distractor source for the reverse/relational templates
    # whose *answer is a character* (reverse-epithet, crew membership, bounty ranking).
    pool_name = build_pool(chars, lambda c: c["title"])
    pool_saga = saga_pool()

    # Occupation frequency: an occupation only one character carries is really a
    # one-off title, not a gradeable role. Restrict the occupation pool (answers and
    # distractors) to occupations shared by >= MIN_OCCUPATION_FREQ characters.
    occ_freq = Counter(
        norm_key(primary(c["fields"].get("occupation")))
        for c in chars if primary(c["fields"].get("occupation"))
    )
    pool_occupation = [p for p in pool_occupation
                       if occ_freq[norm_key(p["v"])] >= MIN_OCCUPATION_FREQ]

    # Crew-membership index: norm(affiliation) -> set of member norm-names, built from
    # *every* affiliation a character lists (not just the primary) so a former member
    # is never offered as a wrong "member of crew X" distractor.
    members_by_crew: dict[str, set[str]] = defaultdict(set)
    for c in chars:
        for item in as_list(c["fields"].get("affiliation")):
            for clause in re.split(r"[;,]", strip_quals(item)):
                key = norm_key(clause)
                if key:
                    members_by_crew[key].add(norm_key(c["title"]))

    # Bounty-holder name pool: the distractor source for the reverse-bounty template
    # ("which character has a bounty of X?"). Drawing wrong answers only from other
    # bounty-carrying characters keeps them plausibly bounty-worthy pirates.
    pool_bounty_names = build_pool(
        [c for c in chars if bounty_amount(c["fields"].get("bounty")) is not None],
        lambda c: c["title"],
    )

    def within_saga(items, limit):
        """Keep only pool items debuting no later than ``limit`` (spoiler bound for
        name-answer templates, whose distractors are themselves characters). Items of
        unknown debut are kept — they can't be gated and are rarely late-saga."""
        if limit is None:
            return items
        return [p for p in items if p["saga"] is None or p["saga"] <= limit]

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
    # depth 0 = headline fact, depth 1 = deep cut (see difficulty()). Every call
    # passes ctx_tier/ctx_saga so distractors are era- and fame-appropriate, and a
    # fact-specific explainer (see lead()) so the post-answer teaching aid explains
    # the actual answer rather than repeating the subject's generic wiki lead.
    for c in chars:
        name = c["title"]
        f = c["fields"]
        prom = prominence(name, c.get("article_len", 0))
        saga = saga_from_first(f.get("first"))
        so = saga["order"] if saga else None
        src = c["source"]
        exp = c.get("summary")  # subject's wiki lead — context after the fact line

        df = combined_df_name(f.get("dfename"), f.get("dfname"))
        if df:
            emit(name, make_question(
                name, f"Which Devil Fruit did {name} eat?", df, pool_df_name,
                rng, "Devil Fruits", difficulty(prom, 0), src,
                lead(f"{name}'s Devil Fruit is the {df}", exp),
                ctx_tier=prom, ctx_saga=so), "char_df", prom, saga)

        aff = primary(f.get("affiliation"))
        if aff:
            emit(name, make_question(
                name, f"Which crew or organization is {name} affiliated with?",
                aff, pool_affiliation, rng, "Crews & Organizations",
                difficulty(prom, 0), src,
                lead(f"{name} is affiliated with the {aff}", exp),
                ctx_tier=prom, ctx_saga=so), "char_affiliation", prom, saga)

        bounty = clean_bounty(f.get("bounty"))
        if bounty:
            emit(name, make_question(
                name, f"What is {name}'s known bounty?", bounty, pool_bounty,
                rng, "Bounties", difficulty(prom, 0), src,
                lead(f"{name}'s known bounty is {bounty}", exp),
                ctx_tier=prom, ctx_saga=so), "char_bounty", prom, saga)

        # Occupation self-selects for informative answers: the per-answer cap limits
        # generic values ("Pirate" covers ~400 characters), and the pool is already
        # filtered to occupations >= 2 characters share, so the surviving questions
        # skew to distinctive, gradeable roles (Doctor, Vice Admiral, Samurai...).
        occupation = primary(f.get("occupation"))
        if occupation and occ_freq[norm_key(occupation)] >= MIN_OCCUPATION_FREQ:
            emit(name, make_question(
                name, f"What is {name}'s occupation?", occupation, pool_occupation,
                rng, "Characters", difficulty(prom, 0), src,
                lead(f"{name}'s occupation is {occupation}", exp),
                ctx_tier=prom, ctx_saga=so), "char_occupation", prom, saga)

        origin = primary(f.get("origin"))
        if origin:
            emit(name, make_question(
                name, f"Where does {name} originate from?", origin, pool_origin,
                rng, "Characters", difficulty(prom, 1), src,
                lead(f"{name} originates from {origin}", exp),
                ctx_tier=prom, ctx_saga=so), "char_origin", prom, saga)

        residence = primary(f.get("residence"))
        if residence:
            emit(name, make_question(
                name, f"Where does {name} reside?", residence, pool_residence,
                rng, "Geography", difficulty(prom, 1), src,
                lead(f"{name} resides in {residence}", exp),
                ctx_tier=prom, ctx_saga=so), "char_residence", prom, saga)

        epithet = clean_epithet(f.get("epithet"))
        if epithet:
            emit(name, make_question(
                name, f"By what epithet is {name} known?", epithet, pool_epithet,
                rng, "Characters", difficulty(prom, 0), src,
                lead(f'{name} is known as "{epithet}"', exp),
                ctx_tier=prom, ctx_saga=so), "char_epithet", prom, saga)

        # Reverse-epithet: the same fact asked the other way. Iconic and only worth
        # asking for recognisable subjects, so gate on prominence. Distractors are
        # other characters, bounded to the subject's debut saga so a later-arc name
        # can't leak as a wrong answer. Skip epithets that embed the character's own
        # name ("Pirate Hunter Zoro", "Kaidou of the Beasts") — they'd give the
        # answer away in this direction (the forward question still asks them).
        name_tokens = {t for t in re.findall(r"[A-Za-z]+", name) if len(t) >= 3}
        epithet_gives_name = bool(epithet) and any(
            t.lower() in epithet.lower() for t in name_tokens)
        if epithet and prom >= 1 and not epithet_gives_name:
            emit(name, make_question(
                name, f"Which character is known by the epithet \"{epithet}\"?",
                name, within_saga(pool_name, so), rng, "Characters",
                difficulty(prom, 0), src,
                lead(f'"{epithet}" is the epithet of {name}', exp),
                ctx_tier=prom, ctx_saga=so), "epithet_reverse", prom, saga)

        # Crew membership (relational): "which of these is a member of X?". Answer is
        # the subject; distractors are characters who are *not* in that crew (checked
        # against every affiliation they list), bounded to the subject's debut saga.
        if aff and prom >= 1:
            non_members = [p for p in within_saga(pool_name, so)
                           if norm_key(p["v"]) not in members_by_crew[norm_key(aff)]]
            emit(name, make_question(
                name, f"Which of these characters is a member of the {aff}?",
                name, non_members, rng, "Crews & Organizations",
                difficulty(prom, 1), src,
                lead(f"{name} is a member of the {aff}", exp),
                ctx_tier=prom, ctx_saga=so), "crew_member", prom, saga)

        # Reverse-bounty (bounty -> character): the amount is the prompt, other
        # bounty-carrying pirates are the distractors (saga-bounded). Unlike a
        # generic "who has the highest bounty?" its text is unique per amount, so it
        # doesn't collapse under duplicate-text dedupe.
        if bounty and prom >= 1:
            emit(name, make_question(
                name, f"Which character has a known bounty of {bounty}?", name,
                within_saga(pool_bounty_names, so), rng, "Bounties",
                difficulty(prom, 1), src,
                lead(f"{name} has a known bounty of {bounty}", exp),
                ctx_tier=prom, ctx_saga=so), "bounty_reverse", prom, saga)

        # Arc debut (Arcs & Story): when a recognisable character first appears. The
        # answer is a saga name; the saga distractor pool covers every saga.
        if saga and prom >= 1:
            emit(name, make_question(
                name, f"In which saga does {name} first appear?", saga["name"],
                pool_saga, rng, "Arcs & Story", difficulty(prom, 1), src,
                lead(f"{name} first appears in the {saga['name']} Saga", exp)),
                "char_saga", prom, saga, max_answer=MAX_PER_ANSWER_SAGA)

    # --- Devil Fruit templates ----------------------------------------------
    for fr in fruits:
        f = fr["fields"]
        fruit_name = combined_df_name(f.get("ename"), f.get("rname") or fr["title"])
        prom = prominence(fr["title"], fr.get("article_len", 0))
        saga = saga_from_first(f.get("first"))
        src = fr["source"]
        exp = fr.get("summary")

        so = saga["order"] if saga else None

        user = clean_name(f.get("user"))
        if user:
            emit(fr["title"], make_question(
                fr["title"], f"Who is the user of the {fruit_name}?", user,
                pool_df_user, rng, "Devil Fruits", difficulty(prom, 0), src,
                lead(f"The {fruit_name} is eaten by {user}", exp),
                ctx_tier=prom, ctx_saga=so), "fruit_user", prom, saga)


    # --- Location templates --------------------------------------------------
    for l in locs:
        f = l["fields"]
        title = l["title"]
        prom = prominence(title, l.get("article_len", 0))
        saga = saga_from_first(f.get("first"))
        so = saga["order"] if saga else None
        exp = l.get("summary")
        region = primary(f.get("region"))
        if region:
            emit(title, make_question(
                title, f"In which region of the world is {title} located?",
                region, pool_region, rng, "Geography", difficulty(prom, 1),
                l["source"], lead(f"{title} is located in {region}", exp),
                ctx_tier=prom, ctx_saga=so), "loc_region", prom, saga)

        # Repurposed from the old vague "Which faction is X affiliated with?"
        # (answers ranged over kingdoms, crews, families and even characters). We
        # now only ask when the affiliation is a *kingdom*, giving a crisp
        # "Which kingdom does X belong to?" with other kingdoms as distractors;
        # non-kingdom affiliations are dropped rather than asked vaguely.
        affil = primary(f.get("affiliation"))
        if is_kingdom(affil):
            emit(title, make_question(
                title, f"Which kingdom does {title} belong to?",
                affil, pool_kingdom, rng, "Geography",
                difficulty(prom, 1), l["source"],
                lead(f"{title} belongs to the {affil}", exp),
                ctx_tier=prom, ctx_saga=so), "loc_kingdom", prom, saga)

        # Arc debut of a recognisable location (Arcs & Story).
        if saga and prom >= 1:
            emit(title, make_question(
                title, f"In which saga does {title} first appear?", saga["name"],
                pool_saga, rng, "Arcs & Story", difficulty(prom, 1), l["source"],
                lead(f"{title} first appears in the {saga['name']} Saga", exp)),
                "loc_saga", prom, saga, max_answer=MAX_PER_ANSWER_SAGA)

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
