#!/usr/bin/env python3
"""Parse the local One Piece wiki XML dump into structured facts.

Phase 3, step 1. Streams ``wiki-data/onepiece_pages_current.xml`` with
``lxml.iterparse`` and pulls the infobox templates out of each page's wikitext
with ``mwparserfromhell``, cleaning wiki markup down to plain values.

Two wrinkles specific to onepiece.fandom.com:

* Minor characters carry an inline ``{{Char Box}}`` on their ns-0 article.
* Major characters (all Straw Hats, headline villains — the best trivia fodder)
  hide the infobox inside ``Template:<Name> Tabs Top`` (namespace 10). Those Char
  Boxes carry a ``root =`` param naming the real article, and fold the character's
  Devil Fruit in as ``dfname``/``dftype``/``dfmeaning`` fields. So we scan ns 10
  too and trust ``root`` for the entity title.

Every infobox value is wrapped in citation templates (``{{Qref}}``), wikilinks,
``{{Nihongo}}``/``{{W}}``/``{{B}}`` templates, ``<br/>`` and ``'' ''`` markup.
``clean_field`` strips all of that to plain text, keeping display text and the
Berry-amount digits, and splits ``<br/>``-separated values into a list.

Output (under ``wiki-data/``, gitignored):
    facts.jsonl   one normalized entity per line:
        {"title", "kind", "source", "article_len", "tabbed", "fields": {...}}

Usage:
    py pipeline/parse_wiki.py
    py pipeline/parse_wiki.py --limit 500     # parse only the first N pages (dev)
    py pipeline/parse_wiki.py --out other.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

from lxml import etree
import mwparserfromhell

REPO_ROOT = Path(__file__).resolve().parent.parent
WIKI_DATA_DIR = REPO_ROOT / "wiki-data"
DEFAULT_XML = WIKI_DATA_DIR / "onepiece_pages_current.xml"
DEFAULT_OUT = WIKI_DATA_DIR / "facts.jsonl"

# MediaWiki export schema namespace (see wiki-dump-source memory).
NS = "{http://www.mediawiki.org/xml/export-0.11/}"

WIKI_BASE = "https://onepiece.fandom.com/wiki/"

# Infobox template name (lowercased) -> the entity kind we tag it with.
INFOBOX_KINDS = {
    "char box": "character",
    "devil fruit box": "devil_fruit",
    "island box": "location",
    "chapter box": "chapter",
    "episode box": "episode",
}

# Citation / footnote templates: drop them and everything they contain.
CITATION_TEMPLATES = {"qref", "qref/ext", "web ref", "webref", "refn", "ref", "sbs ref"}

# Layout / styling params that carry no trivia value — omit from the output.
SKIP_PARAMS = {
    "root", "colorscheme", "color scheme", "image", "imagename", "imagesize",
    "backcolor", "textcolor", "dfbackcolor", "dftextcolor", "border",
    "caption", "name", "title", "tab", "tabs",
}


def render_template(t: mwparserfromhell.nodes.Template) -> str:
    """Render a single (leaf) template to the plain text it should contribute.

    Only the handful of templates that actually appear inside infobox values need
    special handling; everything else falls back to its last positional argument
    (usually the display text) or empty.
    """
    name = str(t.name).strip().lower()
    if name in CITATION_TEMPLATES:
        return ""  # citations add no readable value
    positional = [str(p.value) for p in t.params if not p.showkey]
    if name == "b":
        return ""  # Berry sign; the amount follows as plain text
    if name in ("nihongo", "nihongo foot", "ruby", "lang"):
        return positional[0] if positional else ""  # keep the (English/first) reading
    if name in ("w", "sort", "wp", "wikipedia"):
        return positional[-1] if positional else ""  # link display text
    if name == "-":
        return ""
    return positional[-1] if positional else ""


def _is_leaf_template(t: mwparserfromhell.nodes.Template) -> bool:
    """True when ``t`` has no nested template (so it's safe to render now).

    Detected by scanning its param values for ``{{`` — the ``Template`` node has
    no ``filter_templates`` method in mwparserfromhell 0.7.x, so the older
    ``len(t.filter_templates()) == 1`` check raised ``AttributeError`` and silently
    disabled all template rendering.
    """
    return not any("{{" in str(p.value) for p in t.params)


def _resolve_templates(code: mwparserfromhell.wikicode.Wikicode) -> None:
    """Replace every template in ``code`` with its rendered text, innermost first."""
    for _ in range(12):
        leaves = [t for t in code.filter_templates() if _is_leaf_template(t)]
        if not leaves:
            break
        for t in leaves:
            try:
                code.replace(t, render_template(t))
            except ValueError:
                pass  # already removed as part of an enclosing node


_FILE_LINK = re.compile(r"\[\[(?:File|Image|Category|Media):[^\[\]]*\]\]", re.IGNORECASE)
_WIKILINK = re.compile(r"\[\[(?:[^|\]]*\|)?([^\[\]]+)\]\]")
_EXT_LINK = re.compile(r"\[(?:https?:)?//[^\s\]]+\s+([^\]]+)\]")
_BR = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_STRAY_TMPL = re.compile(r"\{\{[^{}]*\}\}")
_WS = re.compile(r"[ \t ]+")


def clean_field(raw: str):
    """Clean one infobox value to plain text.

    Returns a ``str`` for a single value, or a ``list[str]`` when the source used
    ``<br/>`` / newlines to list several (e.g. ages across timeskips). Returns
    ``None`` when nothing readable is left.
    """
    try:
        code = mwparserfromhell.parse(raw)
        _resolve_templates(code)
        text = str(code)
    except Exception:
        text = raw

    text = _COMMENT.sub("", text)
    text = _BR.sub("\n", text)
    text = _FILE_LINK.sub("", text)
    # Flatten wikilinks (run twice for the occasional nested link).
    text = _WIKILINK.sub(r"\1", text)
    text = _WIKILINK.sub(r"\1", text)
    text = _EXT_LINK.sub(r"\1", text)
    text = _TAG.sub("", text)
    text = _STRAY_TMPL.sub("", text)
    text = text.replace("'''", "").replace("''", "")
    text = text.replace("&nbsp;", " ").replace("[[", "").replace("]]", "")

    segments = []
    for line in text.split("\n"):
        line = _WS.sub(" ", line).strip()
        line = re.sub(r"^-{2,}\s*", "", line)  # strip leading MediaWiki <hr> (----)
        line = line.strip(";,").strip()
        if line:
            segments.append(line)
    if not segments:
        return None
    return segments[0] if len(segments) == 1 else segments


# Lead-paragraph summary extraction. The first prose paragraph of an article is a
# clean, sourced, one-sentence "who/what is this" — used by the app to explain a
# question after a wrong / "I don't know" answer.
_HEADING = re.compile(r"(?m)^=+.*?=+\s*$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
# A sentence still ending on a connective/orphan word means its final noun was an
# unrenderable template (a citation, an empty templated link) — trim that clause.
_DANGLING = re.compile(
    r"[,;]?\s+(?:a|an|the|of|to|in|on|for|with|and|as|by|from|into|being|"
    r"is|was|were|are|known|called|named|serving|designated|nicknamed)$",
    re.IGNORECASE,
)
_LEAD_WINDOW = 12000       # only the top of the article can hold the lead
_SUMMARY_MAX = 240         # cap so the app panel stays a couple of lines


# Hatnote / cross-reference templates that sit above the lead. They're short and
# single-line (so the size test below misses them) but render to link text like
# "Straw Hat Luffy (Disambiguation)" that would masquerade as the lead sentence.
_HATNOTE_TEMPLATES = {
    "you may", "for", "about", "main", "distinguish", "redirect", "see also",
    "seealso", "otheruses", "merge", "note", "spoiler", "expand", "cleanup",
}


def _is_block_template(t: mwparserfromhell.nodes.Template) -> bool:
    """A layout/hatnote template (infobox, quote, cross-reference) above the lead.

    Kept distinct from the *inline* templates that carry sentence content
    (``{{Nihongo|Rubber Human|…}}``): those are short, single-line and not
    hatnotes, so they survive to ``clean_field`` and the sentence keeps its words.
    """
    if str(t.name).strip().lower() in _HATNOTE_TEMPLATES:
        return True
    body = str(t)
    return "\n" in body or len(body) > 120


def _tidy_sentence(sent: str) -> str:
    """Trim spacing artifacts and any dangling trailing clause from a sentence."""
    sent = re.sub(r"\s+([.,;:!?])", r"\1", sent)   # drop space before punctuation
    sent = re.sub(r"\(\s*\)", "", sent)            # empty "()"
    sent = _WS.sub(" ", sent).strip(" ,;.")
    prev = None
    while prev != sent and _DANGLING.search(sent):
        prev = sent
        cut = max(sent.rfind(","), sent.rfind(";"))
        sent = sent[:cut].strip(" ,;") if cut != -1 else ""
    return sent


def lead_summary(text: str) -> str | None:
    """Extract the article's first prose paragraph as a short plain-text summary.

    Removes the leading infobox/quote block templates, takes the first real
    paragraph (the bolded ``'''Name''' ... is ...`` lead), renders inline templates
    and flattens wiki markup via ``clean_field``, and keeps one or two sentences.
    Returns ``None`` when no prose lead can be found (list/gallery pages).
    """
    window = text[:_LEAD_WINDOW]
    if "'''" not in window:            # no bolded lead -> not a prose article
        return None

    window = _COMMENT.sub("", window)
    try:
        code = mwparserfromhell.parse(window)
        for t in code.filter_templates(recursive=False):
            if _is_block_template(t):
                try:
                    code.remove(t)
                except ValueError:
                    pass
        window = str(code)
    except Exception:
        pass
    window = _FILE_LINK.sub("", window)
    window = _HEADING.sub("", window)

    # Try successive paragraphs until one yields a real lead (an early block might
    # still clean down to a stray link or an indented hatnote line).
    for block in re.split(r"\n\s*\n", window):
        raw = block.strip()
        if raw.startswith(":") or raw.startswith(";"):
            continue
        if len(re.sub(r"[^A-Za-z]", "", raw)) < 20:
            continue

        value = clean_field(raw)
        if isinstance(value, list):
            value = " ".join(value)
        value = _WS.sub(" ", str(value)).strip()

        sentences = [s for s in
                     (_tidy_sentence(s) for s in _SENTENCE.split(value)) if s]
        if not sentences:
            continue
        summary = sentences[0]
        if len(summary) < 120 and len(sentences) > 1:
            summary = f"{summary}. {sentences[1]}"
        if len(re.sub(r"[^A-Za-z]", "", summary)) < 20:
            continue
        if len(summary) > _SUMMARY_MAX:
            return summary[:_SUMMARY_MAX].rsplit(" ", 1)[0].rstrip(" ,;") + "…"
        return summary + "."
    return None


def extract_fields(t: mwparserfromhell.nodes.Template) -> dict:
    """Clean every content param of an infobox template into a fields dict."""
    fields = {}
    for p in t.params:
        key = str(p.name).strip().lower()
        if key in SKIP_PARAMS:
            continue
        value = clean_field(str(p.value))
        if value is not None:
            fields[key] = value
    return fields


def param(t: mwparserfromhell.nodes.Template, key: str):
    return t.get(key).value if t.has(key) else None


def source_url(title: str) -> str:
    return WIKI_BASE + quote(title.replace(" ", "_"), safe="/:()#',!&")


def _non_empty(fields: dict) -> int:
    return sum(1 for v in fields.values() if v)


def parse(xml_path: Path, limit: int | None = None):
    """Yield nothing; return (records, article_lens). Single streaming pass."""
    records: dict[tuple[str, str], dict] = {}  # (title, kind) -> record
    article_lens: dict[str, int] = {}
    article_leads: dict[str, str] = {}         # ns-0 title -> lead summary
    n_pages = 0

    ctx = etree.iterparse(str(xml_path), events=("end",), tag=NS + "page")
    for _, page in ctx:
        n_pages += 1
        title = page.findtext(NS + "title") or ""
        ns = page.findtext(NS + "ns") or ""
        text = page.findtext(f"{NS}revision/{NS}text") or ""

        if ns == "0":
            article_lens[title] = len(text)
            # A tabbed major character's prose lives on this ns-0 article while
            # its infobox sits in ns-10; stash the lead here and attach by title.
            summary = lead_summary(text)
            if summary:
                article_leads[title] = summary

        # Cheap pre-filter: skip pages with no infobox template at all.
        if ns in ("0", "10") and "Box" in text:
            try:
                code = mwparserfromhell.parse(text)
                templates = code.filter_templates()
            except Exception:
                templates = []
            for t in templates:
                name = str(t.name).strip().lower()
                kind = INFOBOX_KINDS.get(name)
                if not kind:
                    continue

                root = param(t, "root")
                if root is not None:
                    entity = clean_field(str(root))
                    entity = entity if isinstance(entity, str) else (entity[0] if entity else None)
                    tabbed = True
                elif ns == "0":
                    entity = title
                    tabbed = False
                else:
                    continue  # ns-10 template with no root: a definition/doc page

                if not entity or "{{{" in entity:
                    continue

                fields = extract_fields(t)
                if not fields:
                    continue

                rec = {
                    "title": entity,
                    "kind": kind,
                    "source": source_url(entity),
                    "tabbed": tabbed,
                    "fields": fields,
                }
                key = (entity, kind)
                # Keep the richest instance if an entity appears more than once.
                if key not in records or _non_empty(fields) > _non_empty(records[key]["fields"]):
                    records[key] = rec

        page.clear()
        while page.getprevious() is not None:
            del page.getparent()[0]

        if limit and n_pages >= limit:
            break

    # Attach the article-length prominence proxy (drives difficulty later) and
    # the lead-paragraph summary (the app's post-answer explainer).
    for rec in records.values():
        rec["article_len"] = article_lens.get(rec["title"], 0)
        summary = article_leads.get(rec["title"])
        if summary:
            rec["summary"] = summary

    return list(records.values()), n_pages


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xml", type=Path, default=DEFAULT_XML, help="path to the pages XML dump")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output facts.jsonl path")
    ap.add_argument("--limit", type=int, default=None, help="parse only the first N pages (dev)")
    args = ap.parse_args(argv)

    if not args.xml.exists():
        raise SystemExit(f"XML dump not found: {args.xml}\n  run: py pipeline/download_wiki.py")

    print(f"parsing {args.xml} ...")
    records, n_pages = parse(args.xml, limit=args.limit)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    kinds: dict[str, int] = {}
    with args.out.open("w", encoding="utf-8") as f:
        for rec in records:
            kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"scanned {n_pages} pages -> {len(records)} entities")
    for kind, count in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print(f"  {count:6d}  {kind}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
