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
value); per-answer (≤6) and per-entity (≤4) caps stop one value/character
dominating. A field value only *one* entity in the dump claims is dropped from its
pool (`MIN_OCCUPATION_FREQ` / `MIN_ORIGIN_FREQ` / `MIN_REGION_FREQ`) — a one-off is
a gag or a real-world leak, not a gradeable value, and it made distractors like
"Tibet" that any player strikes out on sight.

Difficulty is still assigned here from `article_len`, but it is **no longer what
ships** — the calibration stage below overrides it. Output
`../wiki-data/questions.generated.json` (gitignored, regenerable).

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

It also merges `curated_questions.json` (agent-authored natural-language questions,
checked in) ahead of the generated bank, applies the calibrated difficulty overlay
and the subject portraits from `image_urls.json`, and runs two regression guards:
`golden.json` (hand-verified answers survived) and `golden_difficulty.json` (the
difficulty scale hasn't inverted).

**Everything the bank carries is applied here.** This step rewrites `questions.json`
from scratch, so a field bolted on by a later script survives only until the next
regeneration — that is how the portraits silently disappeared when the curated
questions were merged. New per-question data belongs in a checked-in overlay that
`validate.py` applies, not in a post-processing pass over the bank.

Latest full run: **3194 questions** — 1033 Characters, 555 Crews & Organizations,
535 Geography, 498 Devil Fruits, 297 Arcs & Story, 278 Bounties
(812 easy / 1575 medium / 807 hard).

## Subject portraits (`enrich_images.py`, opt-in network)

```powershell
py pipeline/enrich_images.py              # refresh pipeline/image_urls.json (Fandom API)
py pipeline/validate.py                   # then rebuild so the bank picks them up
```

The dump's infoboxes store a gallery key, not a filename, so portrait URLs can't be
derived offline. This is the pipeline's only network step: it looks up each subject's
lead image (`prop=pageimages`, polite delay, batched) and caches `{title: url}` to
`pipeline/image_urls.json` — **checked in**, so the build itself stays no-network. It
writes only the cache; `validate.py` attaches `image` to each question, keyed off the
`source` URL the question already carries.

URLs get `format=png` appended: Fandom's thumbnails serve WebP bytes from a `.png`
URL, which older iOS Safari can't decode. The app also requests them with
`referrerPolicy="no-referrer"` to get past the CDN's hotlink protection.

Latest run: **1188/1203 subjects** have an image → 3178/3194 questions carry one
(the other 15 wiki pages have no lead image).

## Difficulty calibration

Difficulty used to be authored twice, on two incompatible scales: the generator
computed it from page length, and six authoring agents each guessed their own
threshold for the curated file. The result was inverted at the boundary — obscure
subjects shipping as `easy`, headline plot facts shipping as `hard`. `DIFFICULTY.md`
is now the single rubric for both paths, and difficulty ships as an **overlay** so a
question's tier no longer depends on which half of the pipeline produced it.

```powershell
py pipeline/calibrate_signals.py --report   # mechanical signals, no LLM, no network
py pipeline/rate_questions.py --estimate    # scope + cost of the rating pass
py pipeline/rate_questions.py               # 3 blind raters (needs ANTHROPIC_API_KEY)
py pipeline/calibrate.py --review           # merge -> calibration/difficulty.json
```

| File | What |
|---|---|
| `DIFFICULTY.md` | the rubric — tiers by expected % correct, and the "this does not count as hard" list |
| `calibrate_signals.py` | subject prominence + guessability tells (stem leak, invented distractors, numeric ladders…) |
| `rate_questions.py` | three blind raters (completionist / casual watcher / test-design skeptic), cached by content hash |
| `calibrate.py` | median rater score − guessability penalty, clamped, cut into tiers |
| `calibration/` | `signals.json`, `ratings.json` (cache), `difficulty.json` (the overlay) — **checked in**, so CI needs neither the dump nor an API key |
| `golden_difficulty.json` | anchor questions pinned to defensible tiers; fails the build if the scale inverts again |

Without ratings the overlay holds each question's authored tier and applies signal
demotions only — deliberately *not* a prominence prior, since scoring from page
length is the original bug. An anchor only becomes binding once its question has
actually been rated, so `validate.py` reports the rest as pending rather than
failing the build over work that hasn't run.

## Full regeneration (one shot)

```powershell
py pipeline/parse_wiki.py; if ($?) { py pipeline/generate_questions.py }; if ($?) { py pipeline/validate.py }
```

Re-run `calibrate_signals.py` → `calibrate.py` → `validate.py` afterwards so the
difficulty overlay covers any newly generated questions, and `enrich_images.py` →
`validate.py` if the run introduced subjects the image cache has never seen (the
validate output says how many). The chain is idempotent: `validate.py`
stashes the original label as `authoredDifficulty`, so a second pass grades against
what was authored rather than compounding its own demotions.

## Next

- **Run the rating pass.** Five `golden_difficulty.json` anchors are still mis-tiered
  and can't be fixed mechanically — they need semantic judgement (see `--estimate`
  for the cost).
- **Distractor repair worklist:** `py pipeline/calibrate_signals.py --report` lists
  the questions whose option sets give the answer away. Most need real canon
  replacements, which is authoring work rather than a mechanical fix.
- **Arcs & Story** category has no source yet — arcs aren't captured as an infobox
  `kind`; needs a dedicated parse (arc/saga navboxes) before it can be generated.
