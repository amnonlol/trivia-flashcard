# pipeline/ — One Piece wiki → question bank

Python content pipeline that turns a local, text-only copy of
[onepiece.fandom.com](https://onepiece.fandom.com) into the app's
`app/public/data/questions.json`. See `../PLAN.md` for the phased plan.

## Setup (virtualenv)

From the repo root:

```powershell
py -m venv pipeline/.venv                                   # create (once)
pipeline/.venv/Scripts/python.exe -m pip install -r pipeline/requirements.txt
```

Then run everything with the venv's interpreter, e.g.
`pipeline/.venv/Scripts/python.exe pipeline/download_wiki.py`.
Or activate it first (`pipeline\.venv\Scripts\Activate.ps1`) and just use `python`.
`.venv/` is gitignored.

## Phase 2 — local wiki copy (done by `download_wiki.py`)

```powershell
py pipeline/download_wiki.py            # download + extract (cached after first run)
py pipeline/download_wiki.py --force    # re-download even if cached
py pipeline/download_wiki.py --no-extract
```

Fetches Fandom's **"current pages" XML dump** — a complete snapshot of article
**wikitext only**. No images or binaries are downloaded; pictures survive as
`[[File:...]]` / template image links, i.e. text pointers we can resolve to URLs
later. The download resumes on interruption and is verified against the dump's
published MD5.

Outputs (under `../wiki-data/`, **gitignored** — large, regenerable):

| File | Size | What |
|---|---|---|
| `onepiece_pages_current.xml.7z` | ~61 MB | raw compressed dump (kept for re-extract) |
| `onepiece_pages_current.xml`    | ~411 MB | extracted MediaWiki XML — **294k pages, ~14.7k articles** |

Per CLAUDE.md: work from this cache; don't re-hit the network during normal dev.

## Phase 3 — parse infoboxes → facts (done by `parse_wiki.py`)

```powershell
py pipeline/parse_wiki.py                 # full dump -> wiki-data/facts.jsonl
py pipeline/parse_wiki.py --limit 20000   # first N pages only (dev)
```

Streams the XML (`lxml.iterparse`) and pulls infobox templates out of each page
with `mwparserfromhell`, cleaning wiki markup (`{{Qref}}` citations, `[[links]]`,
`{{Nihongo}}`/`{{W}}`/`{{B}}`, `<br/>`, `'' ''`) down to plain values. Scans both
article (ns 0) and template (ns 10) namespaces — major characters (Straw Hats,
headline villains) hide their `{{Char Box}}` inside `Template:<Name> Tabs Top`,
keyed by a `root =` param that names the real article.

Infoboxes extracted → `kind`: `Char Box`→character, `Devil Fruit Box`→devil_fruit,
`Island Box`→location, `Chapter Box`→chapter, `Episode Box`→episode.

Output `../wiki-data/facts.jsonl` (**gitignored**, regenerable), one entity per line:

```json
{"title": "Monkey D. Luffy", "kind": "character", "tabbed": true,
 "source": "https://onepiece.fandom.com/wiki/Monkey_D._Luffy", "article_len": 32264,
 "fields": {"bounty": ["3,000,000,000", "1,500,000,000", …], "origin": "East Blue (Foosha Village)",
            "dfname": ["Gomu Gomu no Mi", …], "occupation": "Pirate Captain; Emperor; …", …}}
```

`fields` values are a `str` (single value) or `list[str]` when the wiki listed
several with `<br/>` (e.g. bounty history, newest first; ages across timeskips).
`article_len` is the prominence proxy for the difficulty heuristic. Latest full
run: **5398 entities** — 2375 character, 1218 episode, 1203 chapter, 394 location,
208 devil_fruit.

## Phase 3 — facts → questions (done by `generate_questions.py`)

```powershell
py pipeline/generate_questions.py         # facts.jsonl -> wiki-data/questions.generated.json
```

Deterministic, **no hallucination**: every question and its correct answer come
straight from an infobox field, and distractors are sampled from *real* values of
the same field on other entities (so wrong answers are plausible and never
invented). Options are pre-shuffled with a per-question seed for reproducibility;
the app reshuffles at load anyway.

Templates (field → question, distractors from the same field's pool):

| Template | Category | Correct field |
|---|---|---|
| Which Devil Fruit did *X* eat? | Devil Fruits | character `dfename` |
| Who is the user of the *fruit*? | Devil Fruits | devil_fruit `user` |
| What type is the *fruit*? (Paramecia/Zoan/Logia…) | Devil Fruits | devil_fruit `type` |
| Which crew/org is *X* affiliated with? | Crews & Organizations | character `affiliation` |
| What is *X*'s known bounty? | Bounties | character `bounty` (official numbers only) |
| Where does *X* originate from? | Characters | character `origin` |
| In which region is *place* located? | Geography | location `region` |

Values are normalised first (lists/translation notes/`;`-history → one clean
value); difficulty comes from `article_len` (easy ≥20k, medium ≥6k, else hard);
per-answer (≤6) and per-entity (≤4) caps stop one value/character dominating.
Output `../wiki-data/questions.generated.json` (gitignored, regenerable).

## Phase 3 — validate → app bank (done by `validate.py`)

```powershell
py pipeline/validate.py                   # -> app/public/data/questions.json
py pipeline/validate.py --check-only      # validate without writing
```

Enforces the exact quiz-engine schema and drops any malformed or duplicate
question (fatal-exit only if the input is missing or *nothing* survives): required
keys, `type == "multiple"`, known category (kept in sync with
`app/src/constants/categories.js`), exactly 4 options, `correct_answer` present
exactly once, no near-duplicate options, `onepiece.fandom.com` source URL.

Latest full run: **1949 questions** written — 1059 Crews & Organizations,
460 Devil Fruits, 220 Bounties, 139 Geography, 71 Characters
(424 easy / 622 medium / 903 hard), across 1565 unique wiki sources.

## Full regeneration (one shot)

```powershell
py pipeline/parse_wiki.py; if ($?) { py pipeline/generate_questions.py }; if ($?) { py pipeline/validate.py }
```

## Next

- **Arcs & Story** category has no source yet — arcs aren't captured as an infobox
  `kind`; needs a dedicated parse (arc/saga navboxes) before it can be generated.
- Optional Phase 6 LLM enrichment for plot/relationship questions (`enrich_llm.py`).
