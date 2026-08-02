#!/usr/bin/env python3
"""Deterministic difficulty signals for the question bank — no LLM, no network.

Difficulty is a property of the whole item (stem + the four options), not of the
fact alone (see ``DIFFICULTY.md``). This module computes the mechanical half of
that judgement: how famous the subject is, and whether the item can be answered
*without knowing the answer*.

Two families of signal:

**Prominence** — how well known is the subject? Every question carries a ``source``
wiki URL, which maps to a title, which maps to ``article_len`` in ``facts.jsonl``.
Reuses ``generate_questions.prominence`` / ``is_must_know`` so the curated questions
are measured on exactly the scale the template bank already uses.

**Guessability** — each flag below is a way to get the item right by elimination:

* ``stem_leak``          the stem restates the answer's own words
* ``gloss_leak``         the stem translates the answer (``Fuku`` = clothing)
* ``lone_canon_option``  all three distractors are invented; only the answer is real
* ``unattested_option``  at least one distractor is invented
* ``meta_noise_option``  an option is a real-world / publishing token
* ``numeric_ladder``     all four options are numbers on a scale — a 1-in-4 guess
* ``synonym_cluster``    the options are register variants of each other
* ``answer_len_outlier`` the correct option is conspicuously the longest
* ``cross_domain``       the distractors are a different kind of thing than the answer

The output feeds ``calibrate.py``, which combines it with the blind rater panel.
Worth reading on its own first: ``--report`` prints the worst offenders, which is
also the repair worklist for broken option sets.

Usage:
    py pipeline/calibrate_signals.py --report
    py pipeline/calibrate_signals.py --report --flag lone_canon_option --limit 40
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote

from generate_questions import META_NOISE, is_must_know, prominence
from rate_questions import content_hash

REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE = Path(__file__).resolve().parent
WIKI_DATA_DIR = REPO_ROOT / "wiki-data"
DEFAULT_FACTS = WIKI_DATA_DIR / "facts.jsonl"
DEFAULT_DUMP = WIKI_DATA_DIR / "onepiece_pages_current.xml"
TITLE_CACHE = WIKI_DATA_DIR / "page_titles.txt"
DEFAULT_BANK = REPO_ROOT / "app" / "public" / "data" / "questions.json"
CALIB_DIR = PIPELINE / "calibration"
DEFAULT_OUT = CALIB_DIR / "signals.json"

# Flags that mean "answerable without knowing the fact". calibrate.py penalises a
# question's score for each of these; the rest are informational.
GUESSABILITY_FLAGS = {
    "stem_leak", "lone_canon_option", "weak_distractors", "meta_noise_option",
    "numeric_ladder", "answer_len_outlier",
}

# Words too common to count as a content token when checking whether the stem
# gives the answer away.
STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "what", "which", "who",
    "was", "were", "his", "her", "their", "they", "one", "piece", "fruit", "mi",
    "no", "of", "a", "an", "in", "on", "at", "to", "is", "are", "it", "by",
    "pirates", "pirate", "crew", "island", "kingdom", "berries", "years", "old",
}

_WORD = re.compile(r"[a-z]+")
# "1,234", "1,234 Berries", "8 years old", "10,000 meters", "16 divisions"
_NUMERIC = re.compile(r"^\s*([\d,.]+)\s*(.*)$")


def norm_key(s: str) -> str:
    """Same normalisation validate.py uses to key questions — keep them identical."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def words(s: str) -> set[str]:
    return {w for w in _WORD.findall(str(s).lower()) if len(w) >= 3 and w not in STOPWORDS}


def title_from_source(url: str) -> str:
    """``https://onepiece.fandom.com/wiki/Nico_Robin`` -> ``Nico Robin``."""
    tail = str(url).rsplit("/wiki/", 1)[-1]
    return unquote(tail).replace("_", " ").split("#")[0].strip()


# --------------------------------------------------------------------------- #
# Canon vocabulary
# --------------------------------------------------------------------------- #

_TITLE_TAG = re.compile(r"<title>([^<]+)</title>")
# Wiki namespaces that aren't in-world subjects.
_NS_PREFIX = re.compile(
    r"^(talk|user|project|file|image|mediawiki|template|help|category|forum|"
    r"board|module|gadget|blog|thread|user blog|message wall)[ _]*talk?:", re.I)


def load_page_titles(dump: Path, cache: Path) -> set[str]:
    """Every article title in the dump, normalised — the widest canon vocabulary.

    ``facts.jsonl`` only holds pages that carry one of the four infobox templates, so
    swords, organisations, techniques and lore terms are missing from it — which made
    real distractors like ``Wado Ichimonji`` look invented. Page titles cover all of
    them, redirects included (aliases are useful here), so attestation stops
    depending on whether a subject happened to have an infobox.

    Streaming the 400 MB dump takes a while, so the result is cached next to it. No
    dump and no cache is not fatal: the caller falls back to the infobox vocabulary.
    """
    if cache.exists():
        return {t for t in cache.read_text(encoding="utf-8").splitlines() if t}
    if not dump.exists():
        return set()

    titles: set[str] = set()
    with dump.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "<title>" not in line:
                continue
            m = _TITLE_TAG.search(line)
            if m and not _NS_PREFIX.match(m.group(1)):
                titles.add(norm_key(m.group(1)))
    titles.discard("")
    cache.write_text("\n".join(sorted(titles)), encoding="utf-8")
    return titles


class Canon:
    """What counts as a real One Piece value, built from the wiki dump + the bank.

    An option is *attested* if it names any page in the dump, appears as some
    entity's infobox field value, or is the correct answer to a question in the bank
    (correct answers are canon by construction — they came from the dump or from a
    sourced hand-authored question). Distractors are deliberately **not** attested
    by their presence as distractors, which is the whole point: an invented option
    like ``Samu Samu no Mi`` stays unattested and gets flagged.
    """

    def __init__(self, facts_path: Path, bank: list[dict], page_titles: set[str] = frozenset()):
        self.kinds: dict[str, set[str]] = defaultdict(set)   # norm_key -> {kind}
        self.by_title: dict[str, dict] = {}                  # norm_key(title) -> fact
        self.article_len: dict[str, int] = {}
        self.pages: set[str] = set(page_titles)              # norm_key(title) of any page

        for line in facts_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            fact = json.loads(line)
            title, kind = str(fact["title"]), fact["kind"]
            key = norm_key(title)
            self.by_title[key] = fact
            self.article_len[key] = int(fact.get("article_len") or 0)
            self.kinds[key].add(kind)
            for field, value in (fact.get("fields") or {}).items():
                for item in value if isinstance(value, list) else [value]:
                    for part in re.split(r"[;,]", str(item)):
                        part = re.sub(r"\s*\([^()]*\)", "", part).strip(" ;,.")
                        if len(part) >= 2:
                            self.kinds[norm_key(part)].add(f"field:{field}")

        for q in bank:
            self.kinds[norm_key(q["correct_answer"])].add("answer")

    def attested(self, option: str) -> bool:
        key = norm_key(option)
        if key in self.kinds or key in self.pages:
            return True
        # "The Amazon Pirates" / "the Baratie": a leading article is an authoring
        # style choice, not part of the name.
        return re.sub(r"^the", "", key) in self.pages

    def is_entity(self, option: str) -> bool:
        """The option has its own wiki page — a thing a fan could have heard of."""
        return norm_key(option) in self.by_title or norm_key(option) in self.pages

    def kinds_of(self, option: str) -> set[str]:
        return self.kinds.get(norm_key(option), set())

    def fact_for(self, title: str) -> dict | None:
        return self.by_title.get(norm_key(title))


# --------------------------------------------------------------------------- #
# Individual signals
# --------------------------------------------------------------------------- #

def split_number(option: str):
    """``"10,000 meters"`` -> ``(10000.0, "meters")``; None when not numeric."""
    m = _NUMERIC.match(str(option))
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return value, m.group(2).strip().lower()


_NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "hundred",
}
_ARTICLES = {"a", "an", "the", "of", "no", "and"}


def is_number_word(option: str) -> bool:
    tokens = _WORD.findall(str(option).lower())
    return bool(tokens) and all(t in _NUMBER_WORDS or t in _ARTICLES for t in tokens)


def is_name_like(option: str) -> bool:
    """The option is a name, not a sentence or a quantity.

    Attestation only means something for names: "She turned invisible" is a perfectly
    good distractor that no wiki page will ever be titled after, and counting it as
    invented would flag half the plot questions. Capitalisation is the cheap
    discriminator — proper nouns capitalise their content words, prose doesn't.
    """
    tokens = [t for t in str(option).split() if t.strip(".,'\"").lower() not in _ARTICLES]
    if not tokens or is_number_word(option):
        return False
    capitalised = sum(1 for t in tokens if t[:1].isupper() or t[:1].isdigit())
    return capitalised / len(tokens) >= 0.5


def is_numeric_ladder(options: list[str]) -> bool:
    """All four options are the same quantity at different magnitudes.

    ``60,000,000 / 30,000,000 / 100,000,000 / 50,000,000`` is a 1-in-4 guess with a
    knowledge-shaped stem. Requires a matching unit so a genuine "which of these
    named things" question with one numeric option doesn't trip it.
    """
    if all(is_number_word(o) for o in options):
        return True                        # "Two" / "Four" / "Five" / "Three"
    parsed = [split_number(o) for o in options]
    if any(p is None for p in parsed):
        return False
    return len({unit for _, unit in parsed}) == 1


def is_entity_like(options: list[str]) -> bool:
    """Every option is a short name — the precondition for the vocabulary signals."""
    return all(len(str(o).split()) <= 5 and is_name_like(o) for o in options)


def is_parallel_enumeration(options: list[str]) -> bool:
    """One template, one varying number: "Grove 1" / "Grove 79" / "Grove 44"…

    These look invented to the vocabulary check — no wiki page is titled "Grove 79" —
    but they are *correctly built* distractors: a player can't eliminate one by never
    having heard of it, which is precisely what makes the item hard. Excluded from the
    attestation signals so a well-made enumeration question isn't demoted for it.
    """
    stems = {re.sub(r"\d+", "", norm_key(o)) for o in options}
    return len(stems) == 1 and stems != {""} and any(re.search(r"\d", str(o)) for o in options)


def stem_leak(question: str, correct: str, incorrect: list[str]) -> bool:
    """The stem already contains what *distinguishes* the answer from the distractors.

    Only distinctive words count. "How many divisions did the crew have?" ->
    "16 divisions" shares "divisions" with every option, so the stem gives nothing
    away; "The **Kuja** Pirates travelled aboard her ship... what is the crew
    called?" -> "Kuja Pirates" leaves "kuja" after the shared word is removed, and
    that word is sitting in the stem.
    """
    shared = set.union(*(words(i) for i in incorrect)) if incorrect else set()
    distinctive = words(correct) - shared
    if not distinctive:
        return False
    return distinctive <= words(question)


def gloss_leak(question: str, correct: str, canon: Canon) -> bool:
    """The stem translates the answer.

    One Piece names are transliterated Japanese, and the wiki records the English
    gloss (``ename``/``meaning``). ``Fuku Fuku no Mi`` glosses to "Clothes-Clothes
    Fruit", so a stem about creating *clothing* restates the answer. Matched on a
    5-character prefix so clothes/clothing and stitch/stitching count as one word.

    Informational, not a guessability flag: the player sees the romaji option, not
    the gloss, so the leak only reaches a reader who happens to know the Japanese.
    It is still a question-quality tell worth surfacing to the repair pass, and the
    rater panel — which sees exactly what the player sees — judges the real impact.
    """
    fact = canon.fact_for(correct)
    if not fact:
        return False
    fields = fact.get("fields") or {}
    gloss = " ".join(str(fields.get(f, "")) for f in ("ename", "meaning", "translation"))
    gloss_words = {w for w in words(gloss) if len(w) >= 4}
    if not gloss_words:
        return False
    stem = str(question).lower()
    # The answer's own transliteration doesn't count as a leak of its gloss.
    own = words(correct)
    return any(w[:5] in stem for w in gloss_words if w not in own)


def option_similarity(options: list[str]) -> float:
    """Mean pairwise string similarity across the four options.

    Reported, never flagged. High similarity usually means the options are
    *parallel* ("Grove 1" / "Grove 79" / ...), which is good MCQ design, not a tell.
    The bad case — near-synonyms like ``Snake Princess`` / ``Serpent Queen`` —
    is semantic, invisible to string distance, and left to the rater panel.
    """
    keys = [norm_key(o) for o in options]
    ratios = [
        SequenceMatcher(None, keys[i], keys[j]).ratio()
        for i in range(len(keys)) for j in range(i + 1, len(keys))
    ]
    return sum(ratios) / len(ratios) if ratios else 0.0


def answer_len_outlier(correct: str, incorrect: list[str]) -> bool:
    """The correct option is a conspicuously longer *description* — the oldest MCQ tell.

    Two guards keep this honest. It measures against the *longest* distractor, not the
    mean, because a single long distractor is enough to hide the answer. And it only
    applies to prose answers: "Roronoa Zoro" beside "Nami" / "Usopp" / "Sanji" trips
    any pure length ratio, but nobody picks it for being longer — the tell is a
    specific, qualified *phrase* against vaguer alternatives, not a longer name.
    """
    if len(str(correct).split()) < 4:
        return False
    others = [len(str(i)) for i in incorrect]
    return bool(others) and len(str(correct)) >= 1.6 * max(others)


def cross_domain(correct: str, incorrect: list[str], canon: Canon) -> bool:
    """The distractors are a different kind of thing than the answer.

    ``Geppo`` (a Rokushiki technique) against three Haki names: the stem already
    says "one of the six Rokushiki", so the odd-one-out is the answer. Detected by
    comparing what the dump knows each option *as* — requires every option to be
    attested, so it never fires on invented names (``lone_canon_option`` covers those).

    Informational, not a guessability flag: the technique-vs-Haki case it was written
    for isn't visible here at all (neither has an infobox, so neither is typed), while
    the one item it does fire on — Im against the Five Elders — is a *well-made*
    question whose distractors are exactly the right kind of thing. Kept because a
    real kind mismatch is worth seeing in the report; the rater panel judges the rest.
    """
    ck = canon.kinds_of(correct)
    dk = [canon.kinds_of(i) for i in incorrect]
    if not ck or any(not k for k in dk):
        return False
    if any(ck & k for k in dk):
        return False                       # shares a kind with at least one distractor
    shared = set.intersection(*dk)         # ...but the distractors agree among themselves
    return bool(shared)


# --------------------------------------------------------------------------- #
# Per-question signal record
# --------------------------------------------------------------------------- #

def signals_for(q: dict, canon: Canon) -> dict:
    question, correct = str(q["question"]), str(q["correct_answer"])
    incorrect = [str(i) for i in q["incorrect_answers"]]
    options = [correct] + incorrect

    subject = title_from_source(q.get("source", ""))
    subj_key = norm_key(subject)
    art_len = canon.article_len.get(subj_key)
    prom = prominence(subject, art_len) if art_len is not None else None
    if prom is None and is_must_know(subject):
        prom = 2                            # known name whose page we failed to match

    flags: list[str] = []
    ladder = is_numeric_ladder(options)
    # Numbers are never "attested" (the dump lists Robin's bounty, not the age 6),
    # and a number can't leak through the stem's wording, so the vocabulary-based
    # signals only apply to name-shaped options.
    named = is_entity_like(options) and not ladder and not is_parallel_enumeration(options)
    unattested = [o for o in incorrect if not canon.attested(o)] if named else []

    if named and stem_leak(question, correct, incorrect):
        flags.append("stem_leak")
    if named and gloss_leak(question, correct, canon):
        flags.append("gloss_leak")
    if named and len(unattested) == 3 and canon.is_entity(correct):
        # Only the answer is a real, page-worthy thing: a player who recognises the
        # name picks it without knowing the fact being asked.
        flags.append("lone_canon_option")
    elif len(unattested) >= 2:
        # Two invented distractors collapse the item toward a coin flip.
        flags.append("weak_distractors")
    elif unattested:
        flags.append("unattested_option")
    if any(norm_key(o) in META_NOISE for o in options):
        flags.append("meta_noise_option")
    if ladder:
        flags.append("numeric_ladder")
    similarity = option_similarity(options)
    if answer_len_outlier(correct, incorrect):
        flags.append("answer_len_outlier")
    if cross_domain(correct, incorrect, canon):
        flags.append("cross_domain")

    return {
        "question": question,
        # Identity of the *rateable item* — question plus option set, the same hash
        # rate_questions.py caches verdicts under. Carried here so calibrate.py can
        # tell a rating of this item from a rating of an earlier version of it: repair
        # a distractor and the hash moves, which is what drops the stale verdict.
        "hash": content_hash(question, options),
        "category": q.get("category"),
        # The label the question shipped with, never the calibrated one — a bank that
        # has already been through the overlay carries both, and reading the overlaid
        # value here would make the whole chain compound its own demotions on re-runs.
        "authored_difficulty": q.get("authoredDifficulty", q.get("difficulty")),
        "subject": subject,
        "article_len": art_len,
        "prominence": prom,
        "must_know": is_must_know(subject),
        "flags": flags,
        "guessability_flags": [f for f in flags if f in GUESSABILITY_FLAGS],
        "unattested_options": unattested,
        "option_similarity": round(similarity, 3),
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def print_report(records: dict[str, dict], flag_filter: str | None, limit: int) -> None:
    by_flag: dict[str, list[dict]] = defaultdict(list)
    for rec in records.values():
        for flag in rec["flags"]:
            by_flag[flag].append(rec)

    print("\nflag counts (question may carry several):")
    for flag, recs in sorted(by_flag.items(), key=lambda kv: -len(kv[1])):
        split = Counter(r["authored_difficulty"] for r in recs)
        marker = "*" if flag in GUESSABILITY_FLAGS else " "
        print(f"  {marker} {flag:20s} {len(recs):5d}   authored: {dict(split)}")
    print("  (* = counts as guessable; the rest are informational)")

    unmatched = sum(1 for r in records.values() if r["prominence"] is None)
    print(f"\nprominence: {dict(Counter(r['prominence'] for r in records.values()))}"
          f"  ({unmatched} unmatched subjects)")

    print("\nauthored difficulty vs. guessability:")
    for diff in ("easy", "medium", "hard"):
        recs = [r for r in records.values() if r["authored_difficulty"] == diff]
        flagged = [r for r in recs if r["guessability_flags"]]
        pct = 100 * len(flagged) / len(recs) if recs else 0
        print(f"  {diff:7s} {len(recs):5d} total, {len(flagged):5d} guessable ({pct:.0f}%)")

    if flag_filter:
        chosen = by_flag.get(flag_filter, [])
        print(f"\n--- {flag_filter}: {len(chosen)} questions ---")
    else:
        # The repair worklist: hard-labelled questions that are mechanically gettable.
        chosen = sorted(
            (r for r in records.values()
             if r["authored_difficulty"] == "hard" and r["guessability_flags"]),
            key=lambda r: -len(r["guessability_flags"]))
        print(f"\n--- worst offenders: 'hard' but guessable ({len(chosen)}) ---")

    for rec in chosen[:limit]:
        print(f"\n  [{rec['authored_difficulty']}|{rec['category']}] {rec['question']}")
        print(f"    flags: {', '.join(rec['flags'])}"
              f"   prominence={rec['prominence']}  subject={rec['subject']!r}")
        if rec["unattested_options"]:
            print(f"    unattested: {rec['unattested_options']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", type=Path, default=DEFAULT_BANK, help="question bank to analyse")
    ap.add_argument("--facts", type=Path, default=DEFAULT_FACTS, help="parsed wiki facts")
    ap.add_argument("--dump", type=Path, default=DEFAULT_DUMP, help="wiki XML dump (page titles)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="signals output")
    ap.add_argument("--report", action="store_true", help="print the human-readable report")
    ap.add_argument("--flag", help="report only questions carrying this flag")
    ap.add_argument("--limit", type=int, default=25, help="examples to print")
    args = ap.parse_args(argv)

    for path in (args.bank, args.facts):
        if not path.exists():
            raise SystemExit(f"not found: {path}")

    bank = json.loads(args.bank.read_text(encoding="utf-8"))
    titles = load_page_titles(args.dump, TITLE_CACHE)
    if not titles:
        print(f"warning: no page titles ({args.dump} missing) — attestation will be "
              f"limited to infobox subjects and will over-report invented distractors")
    canon = Canon(args.facts, bank, titles)
    print(f"canon vocabulary: {len(canon.kinds)} infobox values + {len(titles)} page "
          f"titles, from {len(canon.by_title)} entities + {len(bank)} answers")

    records = {norm_key(q["question"]): signals_for(q, canon) for q in bank}
    flagged = sum(1 for r in records.values() if r["guessability_flags"])
    print(f"scored {len(records)} questions: {flagged} carry a guessability flag")

    if args.report:
        print_report(records, args.flag, args.limit)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote signals for {len(records)} questions -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
