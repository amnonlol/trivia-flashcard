#!/usr/bin/env python3
"""Download and extract the One Piece wiki (onepiece.fandom.com) as a local,
text-only snapshot of article wikitext.

Fandom publishes a "current pages" XML dump per wiki (linked from
``Special:Statistics``). It contains only wikitext — no images, no binaries.
Images survive as ``[[File:...]]`` links inside the wikitext, which is exactly
what we want: clone the text, keep only a *reference* to each picture.

Output (under ``wiki-data/``, gitignored):
    onepiece_pages_current.xml.7z   the raw compressed dump (kept for re-extract)
    onepiece_pages_current.xml      the extracted MediaWiki XML

Usage (from repo root or anywhere):
    py pipeline/download_wiki.py                 # download + extract
    py pipeline/download_wiki.py --force         # re-download even if cached
    py pipeline/download_wiki.py --no-extract    # just fetch the .7z
    py pipeline/download_wiki.py --wiki onepiece # a different Fandom wiki

The dump is cached; per CLAUDE.md we never re-hit the network during normal dev.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# Fandom S3 dump layout: .../<first letter>/<first two letters>/<dbname>_pages_current.xml.7z
DUMP_HOST = "https://s3.amazonaws.com/wikia_xml_dumps"
DEFAULT_WIKI = "onepiece"

# wiki-data/ lives at the repo root, one level up from pipeline/.
REPO_ROOT = Path(__file__).resolve().parent.parent
WIKI_DATA_DIR = REPO_ROOT / "wiki-data"

USER_AGENT = (
    "OnePieceFlashCards-pipeline/0.1 "
    "(+https://github.com/; educational trivia project; contact via repo)"
)
CHUNK = 1 << 20  # 1 MiB


def dump_url(wiki: str) -> str:
    """Build the S3 dump URL for a Fandom wiki dbname (e.g. 'onepiece')."""
    w = wiki.lower()
    return f"{DUMP_HOST}/{w[0]}/{w[:2]}/{w}_pages_current.xml.7z"


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _open(url: str, offset: int = 0):
    """Open a URL with our UA, optionally resuming from `offset` bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if offset:
        req.add_header("Range", f"bytes={offset}-")
    return urllib.request.urlopen(req, timeout=60)


def download(url: str, dest: Path, force: bool = False) -> Path:
    """Stream `url` to `dest` with a progress bar, resume, and md5 verify.

    Returns the path to the downloaded file. Skips the network entirely if a
    complete, md5-matching copy is already cached (unless `force`).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    # Peek at the remote to learn total size + the dump's published md5.
    try:
        head = _open(url)
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"ERROR: dump URL returned HTTP {e.code}.\n  {url}\n"
            "The Fandom dump may have moved. Check "
            "https://onepiece.fandom.com/wiki/Special:Statistics for the current "
            "'Database download' link, or fall back to Special:Export."
        )
    total = int(head.headers.get("Content-Length", 0))
    remote_md5 = head.headers.get("x-amz-meta-md5")  # Fandom/S3 exposes this
    head.close()

    if dest.exists() and not force:
        if remote_md5 and _md5(dest) == remote_md5:
            print(f"cached & verified: {dest.name} ({_human(dest.stat().st_size)})")
            return dest
        if not remote_md5:
            print(f"cached (md5 unavailable, trusting existing): {dest.name}")
            return dest
        print("cached copy failed md5 check — re-downloading.")

    # Resume a partial download if the server supports ranges.
    offset = part.stat().st_size if part.exists() and not force else 0
    if force and part.exists():
        part.unlink()

    mode = "ab" if offset else "wb"
    try:
        resp = _open(url, offset=offset)
    except urllib.error.HTTPError:
        offset, mode = 0, "wb"  # range not honored → start over
        resp = _open(url)

    got = offset
    started = time.time()
    print(f"downloading {url}\n  -> {part}  ({_human(total)})")
    with part.open(mode) as f:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                pct = got / total * 100
                rate = got / max(time.time() - started, 1e-6)
                sys.stdout.write(
                    f"\r  {pct:5.1f}%  {_human(got)}/{_human(total)}  {_human(rate)}/s   "
                )
                sys.stdout.flush()
    resp.close()
    sys.stdout.write("\n")

    if total and got != total:
        raise SystemExit(f"ERROR: incomplete download ({got}/{total} bytes). Re-run to resume.")

    if remote_md5:
        actual = _md5(part)
        if actual != remote_md5:
            raise SystemExit(
                f"ERROR: md5 mismatch (got {actual}, expected {remote_md5}). "
                "File corrupt — re-run with --force."
            )
        print(f"md5 verified: {remote_md5}")

    part.replace(dest)
    print(f"saved: {dest} ({_human(dest.stat().st_size)})")
    return dest


def extract(archive: Path) -> Path:
    """Extract the single .xml member of the .7z dump next to it."""
    try:
        import py7zr
    except ImportError:
        raise SystemExit(
            "py7zr is required to extract the dump.\n"
            "  py -m pip install -r pipeline/requirements.txt"
        )

    print(f"extracting {archive.name} ...")
    with py7zr.SevenZipFile(archive, mode="r") as z:
        names = z.getnames()
        z.extractall(path=archive.parent)
    xmls = [archive.parent / n for n in names if n.lower().endswith(".xml")]
    if not xmls:
        raise SystemExit(f"no .xml member found in {archive.name} (got {names}).")
    out = xmls[0]
    print(f"extracted: {out} ({_human(out.stat().st_size)})")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wiki", default=DEFAULT_WIKI, help="Fandom wiki dbname (default: onepiece)")
    p.add_argument("--url", help="explicit dump URL (overrides --wiki)")
    p.add_argument("--force", action="store_true", help="re-download even if cached")
    p.add_argument("--no-extract", action="store_true", help="download the .7z only")
    args = p.parse_args(argv)

    url = args.url or dump_url(args.wiki)
    archive = WIKI_DATA_DIR / Path(url).name

    download(url, archive, force=args.force)
    if not args.no_extract:
        extract(archive)
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
