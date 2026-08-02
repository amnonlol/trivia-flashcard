#!/usr/bin/env python3
"""Refresh the subject portrait cache that ``validate.py`` overlays onto the bank.

The cached wiki *dump* is text-only and its infoboxes store just a gallery key,
not a filename — so image URLs can't be derived offline like the rest of the
pipeline. This opt-in step fetches each subject's lead image from Fandom's live
MediaWiki API (``prop=pageimages``) into ``pipeline/image_urls.json``, which is
checked in so the deterministic build (parse -> generate -> validate) stays
no-network per CLAUDE.md.

This script writes **only the cache**, never the bank. ``validate.py`` is what
attaches ``image`` to each question, because it rewrites ``questions.json`` from
scratch: portraits written here would be erased by the next regeneration — which
is exactly how they disappeared when the curated questions were merged.

How it works:

* every question already carries a ``source`` wiki URL; the page title is read
  straight from it, so questions map to subjects with no extra data;
* unique titles are looked up in batches against ``api.php`` (respecting a polite
  delay), caching ``{title: image_url_or_null}`` — a cached ``null`` means "asked,
  the page has no lead image", so it isn't re-fetched every run.

Usage:
    py pipeline/enrich_images.py            # look up any subjects not yet cached
    py pipeline/enrich_images.py --refresh  # ignore the cache, re-fetch all
    py pipeline/enrich_images.py --limit 50 # fetch at most N new titles (dev)
    py pipeline/validate.py                 # then rebuild the bank to apply them
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# validate.py owns attaching images to the bank, so it owns the URL/title helpers too.
from validate import title_from_source

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BANK = REPO_ROOT / "app" / "public" / "data" / "questions.json"
# Checked in (small, and the build must not need the network to reproduce the bank).
CACHE_PATH = Path(__file__).resolve().parent / "image_urls.json"

API = "https://onepiece.fandom.com/api.php"
USER_AGENT = (
    "OnePieceFlashCards-pipeline/0.1 "
    "(+https://github.com/; educational trivia project; contact via repo)"
)
THUMB_SIZE = 400        # px; a portrait large enough for the explainer panel
BATCH = 40              # titles per API call (MediaWiki allows up to 50)
DELAY = 0.5             # seconds between calls — be a polite guest


def fetch_images(titles: list[str]) -> dict[str, str | None]:
    """Look up the lead image URL for a batch of page titles."""
    params = {
        "action": "query",
        "format": "json",
        "prop": "pageimages",
        "piprop": "thumbnail",
        "pithumbsize": str(THUMB_SIZE),
        "titles": "|".join(titles),
        "redirects": "1",
    }
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    result: dict[str, str | None] = {t: None for t in titles}
    query = payload.get("query", {})
    # Map any redirected titles back to what we asked for.
    alias = {r["to"]: r["from"] for r in query.get("redirects", [])}
    alias.update({n["to"]: n["from"] for n in query.get("normalized", [])})
    for page in query.get("pages", {}).values():
        title = page.get("title")
        asked = alias.get(title, title)
        thumb = page.get("thumbnail", {}).get("source")
        if asked in result:
            result[asked] = thumb
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", type=Path, default=DEFAULT_BANK,
                    help="question bank to read subjects from (never modified)")
    ap.add_argument("--cache", type=Path, default=CACHE_PATH, help="title -> image-url cache to update")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and re-fetch every title")
    ap.add_argument("--limit", type=int, default=None, help="fetch at most N new titles this run")
    args = ap.parse_args(argv)

    if not args.inp.exists():
        raise SystemExit(f"question bank not found: {args.inp}\n  run: py pipeline/validate.py")

    questions = json.loads(args.inp.read_text(encoding="utf-8"))
    cache: dict[str, str | None] = {}
    if args.cache.exists() and not args.refresh:
        cache = json.loads(args.cache.read_text(encoding="utf-8"))

    # Unique subject titles across the bank.
    titles = sorted({
        t for q in questions
        if (t := title_from_source(str(q.get("source", ""))))
    })
    todo = [t for t in titles if t not in cache]
    if args.limit is not None:
        todo = todo[:args.limit]
    print(f"{len(titles)} subjects, {len(cache)} cached, fetching {len(todo)} ...")

    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        try:
            cache.update(fetch_images(batch))
        except Exception as exc:  # network hiccup: save progress and stop
            print(f"  fetch failed at batch {i // BATCH}: {exc}", file=sys.stderr)
            break
        got = sum(1 for t in batch if cache.get(t))
        print(f"  [{min(i + BATCH, len(todo)):5d}/{len(todo)}] +{got} images")
        args.cache.parent.mkdir(parents=True, exist_ok=True)
        args.cache.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        if i + BATCH < len(todo):
            time.sleep(DELAY)

    have = sum(1 for v in cache.values() if v)
    covered = sum(1 for q in questions
                  if cache.get(title_from_source(str(q.get("source", "")))))
    print(f"images known for {have}/{len(cache)} subjects -> {args.cache}")
    print(f"{covered}/{len(questions)} questions have a portrait waiting; "
          f"apply it with: py pipeline/validate.py")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
