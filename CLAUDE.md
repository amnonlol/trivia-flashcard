# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A **One Piece trivia flash-card web app**: mobile-friendly, hosted online, plays
**multiple-choice** questions so users get better at One Piece trivia. Installable
as a PWA. See `PLAN.md` for the full implementation plan and current phase.

## Architecture (two halves)

1. **`app/`** — the web app. A React 18 PWA forked from
   [SafdarJamal/quiz-app](https://github.com/SafdarJamal/quiz-app) (MIT).
   It plays multiple-choice quizzes with category/difficulty/timer/scoring.
   The original fetched questions from the Open Trivia DB API; **we serve our own
   local One Piece question bank instead** (`app/public/data/questions.json`).
2. **`pipeline/`** — Python content pipeline that turns a local copy of the One
   Piece wiki into that question bank: download dump → parse infoboxes → generate
   MCQs → validate.

`base-repo/` is the pristine upstream clone, kept for reference until Phase 0
promotes it to `app/`. Don't edit `base-repo/`.

## The one schema that matters

Every question — hand-written, template-generated, or LLM-generated — MUST match the
shape the quiz engine already expects (from upstream `Quiz/mock.json`):

```json
{
  "category": "Characters",
  "type": "multiple",
  "difficulty": "easy",
  "question": "What is Monkey D. Luffy's Devil Fruit?",
  "correct_answer": "Gomu Gomu no Mi",
  "incorrect_answers": ["Mera Mera no Mi", "Hito Hito no Mi", "Bara Bara no Mi"],
  "options": ["Hito Hito no Mi", "Gomu Gomu no Mi", "Bara Bara no Mi", "Mera Mera no Mi"],
  "source": "https://onepiece.fandom.com/wiki/Monkey_D._Luffy"
}
```
Rules: exactly 4 `options`; `correct_answer` appears in `options` exactly once;
no duplicate/near-duplicate options; distractors sampled from the **same category**
of real values so they're plausible. `source` links back to the wiki (CC-BY-SA
attribution). The app shuffles `options` at load, but the pipeline should still
pre-shuffle and validate.

## Where to make changes

- **Wire the app to local data:** `app/src/components/Main/index.js → fetchData()`.
  Replace the API `fetch` with a cached `fetch('/data/questions.json')`, then filter
  by category/difficulty and sample `numOfQuestions`. Preserve the
  `startQuiz(results, time)` contract — don't rewrite the quiz engine.
- **Categories:** `app/src/constants/categories.js`.
- **Question generation logic:** `pipeline/generate_questions.py` (templates +
  distractor sampling). Keep it **deterministic** — no hallucinated facts.
- **Never hand-edit** `app/public/data/questions.json`; regenerate it via the
  pipeline so it stays reproducible.

## Content / data sources

- One Piece wiki: **onepiece.fandom.com** → `Special:Statistics` → "Current pages"
  XML dump (`.7z`). Cache under `wiki-data/` (gitignored — large). Never re-hit the
  network during normal dev; work from the cached dump.
- Fandom content is **CC-BY-SA**: keep per-question `source` links and a footer
  attribution in the app.
- Be mindful of **spoilers**: prefer a saga/arc scope filter; note whether a batch is
  manga-canon vs. anime/filler.

## Conventions

- **Commands run on Windows / PowerShell** (Bash tool available for POSIX scripts).
- App: Node + Create React App. `cd app && npm start` (dev), `npm run build` (prod),
  `npm test`. Deploy target is a **static host** (GitHub Pages / Vercel / Netlify) —
  keep the build free of server-side dependencies.
- Pipeline: Python 3. Deps in `pipeline/requirements.txt`
  (`lxml`, `mwparserfromhell`, `py7zr`, plus `anthropic` for optional Phase 6).
- If writing Claude API code (Phase 6 LLM enrichment), **load the `claude-api`
  skill first**; default to the latest models (`claude-sonnet-5`, `claude-opus-4-8`).

## Guardrails

- Don't add a backend/database — this must stay a static PWA (that was the whole
  reason for choosing this base over Scholarsome).
- Keep questions verifiable and sourced; when generating, prefer template extraction
  over free-form LLM output, and always run `pipeline/validate.py`.
- This is not yet a git repo — initialize one before the first commit (Phase 0).
