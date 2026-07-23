#!/usr/bin/env python3
"""Attach a portrait ``image`` URL to each question (optional, network step).

The cached wiki *dump* is text-only and its infoboxes store just a gallery key,
not a filename — so image URLs can't be derived offline like the rest of the
pipeline. This opt-in step fetches each subject's lead image from Fandom's live
MediaWiki API (``prop=pageimages``) and writes the URL back into the question
bank, so the app can show a picture beside the post-answer explainer.

It is deliberately *separate* from the deterministic build (parse -> generate ->
validate stays no-network per CLAUDE.md). Run it only when you want images, and
it caches every lookup so re-runs are cheap and mostly offline.

How it works:

* every question already carries a ``source`` wiki URL; the page title is read
  straight from it, so questions map to subjects with no extra data;
* unique titles are looked up in batches against ``api.php`` (respecting a polite
  delay), caching ``{title: image_url_or_null}`` to ``wiki-data/image_urls.json``;
* each question gets ``image`` set from its subject's cached URL (absent when the
  page has no image — the app renders fine without one).

Usage:
    py pipeline/enrich_images.py                 # enrich app/public/data/questions.json in place
    py pipeline/enrich_images.py --refresh       # ignore the cache, re-fetch all
    py pipeline/enrich_images.py --limit 50      # fetch at most N new titles (dev)
    py pipeline/enrich_images.py --in x --out y  # explicit paths
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BANK = REPO_ROOT / "app" / "public" / "data" / "questions.json"
CACHE_PATH = REPO_ROOT / "wiki-data" / "image_urls.json"

API = "https://onepiece.fandom.com/api.php"
USER_AGENT = (
    "OnePieceFlashCards-pipeline/0.1 "
    "(+https://github.com/; educational trivia project; contact via repo)"
)
THUMB_SIZE = 400        # px; a portrait large enough for the explainer panel
BATCH = 40              # titles per API call (MediaWiki allows up to 50)
DELAY = 0.5             # seconds between calls — be a polite guest


def force_png(url: str) -> str:
    """Force Fandom's thumbnailer to serve a real PNG.

    Fandom's ``static.wikia.nocookie.net`` thumbnails are content-negotiated:
    the URL ends in ``.png`` but the bytes are **always WebP**, regardless of the
    browser's ``Accept`` header. Desktop browsers decode WebP fine, but any client
    that can't (notably older iOS Safari) fails to render and the app's ``onError``
    hides the image — so portraits silently vanish on some phones. Appending
    ``format=png`` makes the thumbnailer emit genuine PNG bytes everywhere.
    """
    if not url or "nocookie.net" not in url:
        return url
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if any(k == "format" for k, _ in query):
        return url
    query.append(("format", "png"))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def title_from_source(source: str) -> str | None:
    """Recover the wiki page title from a question's ``source`` URL."""
    marker = "/wiki/"
    if marker not in source:
        return None
    slug = source.split(marker, 1)[1]
    return urllib.parse.unquote(slug).replace("_", " ")


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
    ap.add_argument("--in", dest="inp", type=Path, default=DEFAULT_BANK, help="question bank to enrich")
    ap.add_argument("--out", type=Path, default=None, help="output path (default: overwrite --in)")
    ap.add_argument("--cache", type=Path, default=CACHE_PATH, help="title -> image-url cache")
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

    # Merge cached URLs into the bank (only set/remove the key we own).
    enriched = 0
    for q in questions:
        title = title_from_source(str(q.get("source", "")))
        url = cache.get(title) if title else None
        if url:
            q["image"] = force_png(url)
            enriched += 1
        else:
            q.pop("image", None)

    out = args.out or args.inp
    out.write_text(json.dumps(questions, ensure_ascii=False, indent=1), encoding="utf-8")
    have = sum(1 for v in cache.values() if v)
    print(f"images known for {have}/{len(cache)} subjects; "
          f"{enriched}/{len(questions)} questions now carry an image -> {out}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
