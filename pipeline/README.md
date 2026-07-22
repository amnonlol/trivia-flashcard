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

## Next (Phase 3, not built yet)

- `parse_wiki.py` — stream the XML (`lxml.iterparse`), parse infobox templates
  with `mwparserfromhell` → `wiki-data/facts.jsonl` (one entity per line).
- `generate_questions.py` — deterministic template MCQs + same-category distractors.
- `validate.py` — schema / 4-options / dedupe → `app/public/data/questions.json`.
