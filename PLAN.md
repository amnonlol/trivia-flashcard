# One Piece Trivia Flash Cards — Implementation Plan

## 1. Goal

A mobile-friendly, hosted-online web app for practicing **One Piece trivia** via
**multiple-choice questions**. Users pick a category/difficulty, answer questions
against a timer, get scored, and improve over time (progress + weak-area review).
Installable on smartphones as a PWA.

## 2. Base repository (chosen infrastructure)

**[SafdarJamal/quiz-app](https://github.com/SafdarJamal/quiz-app)** — cloned into `./base-repo/`.

| Attribute | Value |
|---|---|
| Stars | ~434 |
| Stack | React 18 (Create React App) + Semantic UI + PWA |
| License | **MIT** (permissive — safe to fork & rebrand) |
| Data source | Open Trivia DB API (multiple-choice) → **we replace with local One Piece bank** |
| Hosting | Static build → GitHub Pages / Vercel / Netlify (free) |

### Why this one over the alternatives
- **Scholarsome** (776★, Angular + NestJS + MySQL, AGPL): more stars, but it's a
  heavy multi-user Quizlet-style *platform* needing a server + DB. Overkill for a
  trivia trainer and harder to host. AGPL is restrictive.
- **quiz-app** is almost exactly our target already: a React **PWA** that plays
  **multiple-choice** quizzes with category/difficulty/timer/score. It fetches
  questions from a trivia API — the only meaningful change is swapping that API for
  our own One Piece question bank. MIT license, trivially static-hostable.

### Key integration point (verified in code)
`src/components/Main/index.js → fetchData()` builds an Open Trivia DB URL and hands
`results` to `startQuiz()`. Question schema the whole app already expects:

```json
{
  "category": "Characters",
  "type": "multiple",
  "difficulty": "easy",
  "question": "What is Monkey D. Luffy's Devil Fruit?",
  "correct_answer": "Gomu Gomu no Mi",
  "incorrect_answers": ["Mera Mera no Mi", "Hito Hito no Mi", "Bara Bara no Mi"],
  "options": ["...shuffled..."]
}
```
**We just need to produce JSON in this shape.** That is the seam the whole plan
targets — no rewrite of the quiz engine required.

## 3. Target repository layout

```
One-Piece-flash-cards/
├── app/                      # the React PWA (promoted from base-repo)
│   ├── public/
│   │   ├── data/questions.json        # generated question bank (served statically)
│   │   └── manifest.json, icons       # rebranded PWA assets
│   └── src/…                          # modified quiz app
├── pipeline/                 # content generation (Python)
│   ├── download_wiki.py               # fetch + extract the Fandom XML dump
│   ├── parse_wiki.py                  # XML → structured facts (infoboxes)
│   ├── generate_questions.py          # facts → MCQ JSON (templates + distractors)
│   ├── enrich_llm.py                  # (optional) Claude-generated plot questions
│   ├── validate.py                    # schema + dedupe + sanity checks
│   └── requirements.txt
├── wiki-data/                # RAW dump + extracted XML (gitignored, large)
├── base-repo/               # original clone, kept for reference until Phase 1 done
├── PLAN.md
└── CLAUDE.md
```

## 4. Local copy of the One Piece wiki

Source: **onepiece.fandom.com** → `Special:Statistics` → "Current pages" XML dump
(`.7z`). This is a complete offline snapshot of article wikitext (no images, no
private data). ~hundreds of MB uncompressed.

- `download_wiki.py`: resolve the current dump URL from `Special:Statistics`,
  download the `.7z`, extract with `py7zr`, output `wiki-data/onepiece_pages.xml`.
- Fallback if dump is stale/missing: MediaWiki **`Special:Export`** for a curated
  list of high-value pages, or the `api.php?action=query&export` endpoint. Respect
  rate limits; cache locally so we never re-hit the network during dev.
- **License note:** Fandom content is CC-BY-SA. Keep a per-question `source` link
  back to the wiki page and an attribution line in the app footer.

## 5. From wiki → questions (the core work)

### 5a. Parse (`parse_wiki.py`)
- Stream the XML (`lxml.etree.iterparse`) → per-page `{title, wikitext}`.
- Parse **infobox templates** with `mwparserfromhell` to extract structured facts:
  characters (Devil Fruit, bounty, affiliation, occupation, origin, age, height,
  status, first appearance), Devil Fruits (type, user, meaning), crews, arcs, etc.
- Emit `wiki-data/facts.jsonl` — one normalized entity per line.

### 5b. Generate (`generate_questions.py`) — deterministic, no hallucination
Template-based MCQs from facts. Each template = (question text, correct field,
distractor strategy). Examples:
- "What is **{char}**'s Devil Fruit?" → distractors = other real Devil Fruit names.
- "What is **{char}**'s first bounty?" → distractors = other characters' bounties.
- "Which crew does **{char}** belong to?" → distractors = other crews.
- "Who ate the **{fruit}**?" → distractors = other characters.
- "In which arc did **{event}** happen?" → distractors = other arcs.

**Distractor rule:** always sample from the *same category* of real values so wrong
answers are plausible, and enforce uniqueness (no duplicate options, no
correct-answer leak). Attach `category`, `difficulty`, `source` (wiki URL).

**Difficulty heuristic:** main Straw Hats & headline facts = `easy`; supporting
characters = `medium`; obscure/one-off = `hard` (drive off a page-prominence proxy
such as page length / backlink count / appearance count).

### 5c. (Optional) LLM enrichment (`enrich_llm.py`)
Use the **Claude API** (`claude-sonnet-5` for volume, `claude-opus-4-8` for hard
sets) to generate plot/relationship questions from page summaries where templates
can't reach. Must run through `validate.py`; keep these in a separate tagged batch
so they can be toggled off if quality dips. Load the `claude-api` skill before
writing any API code.

### 5d. Validate (`validate.py`)
- JSON-schema check against the app's expected shape.
- Exactly 4 options, correct answer present exactly once, no near-duplicate options.
- Deduplicate questions; cap per-entity question count to avoid repetition.
- Output `app/public/data/questions.json` (or split per category for lazy loading).

## 6. App changes (React)

1. **Promote** `base-repo/` → `app/`; `npm install`; confirm baseline runs.
2. **Swap data source**: in `Main/index.js`, replace `fetchData()`'s API call with a
   one-time `fetch('/data/questions.json')` (cached), then filter by category +
   difficulty and sample `numOfQuestions` client-side. Keep the existing
   `startQuiz(results, time)` contract intact.
3. **Categories**: rewrite `src/constants/categories.js` to One Piece categories
   (Characters, Devil Fruits, Crews & Organizations, Arcs & Story, Bounties,
   Geography, Mixed).
4. **Spoiler safety**: add an optional "up to saga/arc" filter so users aren't
   spoiled past where they've read/watched.
5. **Progress & weak-area review** (localStorage): track seen/missed questions and
   per-category accuracy; add a "Review missed" mode (lightweight spaced repetition —
   resurface missed questions sooner).
6. **Rebrand**: title, theme colors, `manifest.json`, app icons, footer attribution.
7. **Keep**: timer (Countdown), scoring (`calculateScore`/`calculateGrade`), Result
   breakdown, offline handling, service worker.

## 7. Hosting

Static build (`npm run build`) deployed to **GitHub Pages** (repo already has a
`gh-pages` deploy script) or Vercel/Netlify. PWA manifest + service worker make it
installable on phones and usable offline once loaded.

## 8. Phased milestones

- **Phase 0 — Setup:** ✅ promote base-repo → `app/`, install, run baseline, init git.
- **Phase 1 — Data seam:** ✅ hand-made 32-question One Piece
  `questions.json`, app loads local JSON (`Main/fetchData` → cached
  `fetch('/data/questions.json')` + client-side filter/sample), One Piece
  categories, rebranded title/manifest/header, CC-BY-SA footer. **Shippable demo.**
- **Phase 2 — Wiki pipeline:** download dump, parse infoboxes → facts.
- **Phase 3 — Question generation:** templates + distractors → validated bank
  (target ≥500 questions across categories).
- **Phase 4 — App polish:** categories, spoiler filter, progress/weak-area review,
  PWA assets.
- **Phase 5 — Deploy:** CI + static hosting, custom domain optional.
- **Phase 6 — (Optional) LLM enrichment** for plot/relationship questions.

## 9. Open decisions (to confirm as we go)
- Single `questions.json` vs. per-category files (lazy load) — decide at Phase 3 by size.
- Whether to include anime-only / filler content or manga-canon only (spoiler scope).
- Whether to spend on Phase 6 LLM enrichment (cost vs. added variety).
