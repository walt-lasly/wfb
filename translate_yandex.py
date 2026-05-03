#!/usr/bin/env python3
"""
translate_yandex.py — Translate English blog posts to Russian using Yandex Translate.

Usage:
    python3 translate_yandex.py --key YOUR_YANDEX_KEY [options]

Options:
    --key KEY         Yandex Cloud API key (required)
    --folder ID       Yandex Cloud folder ID (usually not needed with API key auth)
    --post FOLDER     Translate a single post folder only (e.g. 2009-02-28-josie)
    --from FOLDER     Start batch translation from this post folder (inclusive)
    --force           Re-translate posts that already have Russian content
    --dry-run         Show what would be translated without calling the API
    --delay SECS      Seconds to wait between API calls (default: 0.5)

Glossary:
    Reads glossary.txt (same file as translate.py) and sends it as an inline
    glossary with every request — no separate sync step required.

Getting an API key:
    1. Create a free account at console.yandex.cloud
    2. Create a service account (IAM → Service Accounts → Create)
    3. Grant it the role 'ai.translate.user'
    4. Create an API key for the service account (copy the key value)
    5. Free tier: 1,000,000 characters/month (no credit card required)
       Paid tier: ~$9 per 1M chars after that

Notes:
    - Yandex Translate supports inline glossaries per request (up to 50 pairs
      per call); pairs beyond 50 are silently truncated to the first 50
    - Hugo shortcodes {{< >}} and markdown link URLs are protected with
      XHOLD_N_X placeholder tokens so they pass through untouched
    - The API accepts up to ~10,000 characters per texts[] item; large post
      bodies are split on paragraph boundaries and merged back after translation
    - 429 / 503 responses are retried up to 3 times with exponential back-off
"""

import argparse
import re
import sys
import time
from pathlib import Path

from translate_common import (
    CONTENT_DIR,
    DEFAULT_SECTION,
    STUB_MARKER,
    get_content_dir,
    load_glossary_entries,
    split_frontmatter,
    write_translation,
)

try:
    import requests
except ImportError:
    print("Error: 'requests' not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

YANDEX_URL    = "https://translate.api.cloud.yandex.net/translate/v2/translate"
# Yandex hard limit per texts[] element; we stay a bit below to be safe
CHUNK_LIMIT   = 9_500
# Maximum glossary pairs per request (Yandex API limit)
GLOSSARY_LIMIT = 50

# ---------------------------------------------------------------------------
# Yandex-specific placeholder protection
# Yandex mangles plain-text tokens (XHOLD → translated word) and inserts
# spaces in markdown link syntax [text] (url).  We use HTML mode and convert
# markdown links to <a href> tags so Yandex treats them as proper HTML —
# href attribute values are never translated and the tag structure is preserved.
# Entire images and shortcodes are tokenised as <x id="N"/> opaque tags.
# ---------------------------------------------------------------------------

_YT_IMG_RE     = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)\)')   # whole image
_YT_SC_RE      = re.compile(r'\{\{[<%].*?[>%]\}\}', re.DOTALL)  # shortcodes
_YT_ANCHOR_RE  = re.compile(r'\{#[\w-]+\}')                       # heading anchors {#id}
_YT_LINK_RE    = re.compile(r'\[([^\]]*)\]\(([^)\s]+)\)')    # [text](url)
_YT_TOK_RE     = re.compile(r'<x id="(\d+)"\s*/>')
_YT_AHREF_RE   = re.compile(r'<a href="(\d+)">(.*?)</a>', re.DOTALL)


def _protect(text: str):
    """Protect images and shortcodes as <x id="N"/> tokens; links as <a href="N">."""
    slots = []

    def slot(val: str) -> str:
        n = len(slots)
        slots.append(val)
        return str(n)

    # Protect entire images as one opaque token (alt text is usually a filename)
    text = _YT_IMG_RE.sub(lambda m: f'<x id="{slot(m.group())}"/>', text)
    # Protect Hugo shortcodes as opaque tokens
    text = _YT_SC_RE.sub(lambda m: f'<x id="{slot(m.group())}"/>', text)
    # Protect Hugo heading anchor IDs {#some-id} — Yandex translates them
    text = _YT_ANCHOR_RE.sub(lambda m: f'<x id="{slot(m.group())}"/>', text)
    # Convert markdown links to HTML <a> so Yandex keeps href intact and
    # translates only the visible link text
    text = _YT_LINK_RE.sub(
        lambda m: f'<a href="{slot(m.group(2))}">{m.group(1)}</a>', text
    )
    return text, slots


def _restore(text: str, slots: list) -> str:
    """Restore <a href> links and <x id="N"/> tokens back to markdown."""
    # Convert <a href="N">text</a> → [text](original_url)
    text = _YT_AHREF_RE.sub(
        lambda m: f'[{m.group(2)}]({slots[int(m.group(1))]})', text
    )
    return _YT_TOK_RE.sub(lambda m: slots[int(m.group(1))], text)

# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------

def build_glossary_config(entries: dict) -> dict:
    """Build Yandex glossaryConfig payload from an already-filtered entries dict."""
    if not entries:
        return {}
    pairs = [
        {"sourceText": en, "translatedText": ru}
        for en, ru in list(entries.items())[:GLOSSARY_LIMIT]
    ]
    return {"glossaryConfig": {"glossaryData": {"glossaryPairs": pairs}}}


def filter_glossary_for_text(entries: dict, text: str) -> dict:
    """Return the subset of entries whose English term appears in text.

    Matching is case-insensitive and whole-word so that e.g. "Dune" does not
    match "Dunedain".  Result is capped at GLOSSARY_LIMIT entries.
    """
    text_lower = text.lower()
    matched = {}
    for en, ru in entries.items():
        # Use a simple case-insensitive substring check first (fast path),
        # then confirm with a word-boundary regex to avoid false positives.
        if en.lower() in text_lower:
            pattern = r'(?i)(?<![\w])' + re.escape(en) + r'(?![\w])'
            if re.search(pattern, text):
                matched[en] = ru
                if len(matched) >= GLOSSARY_LIMIT:
                    break
    return matched


# ---------------------------------------------------------------------------
# Yandex Translate API
# ---------------------------------------------------------------------------

def _yandex_call(texts: list, api_key: str, folder_id: str,
                 glossary_config: dict) -> list:
    """Single call to Yandex Translate v2. Returns list of translated strings."""
    payload = {
        "texts": texts,
        "sourceLanguageCode": "en",
        "targetLanguageCode": "ru",
        "format": "HTML",
        "speller": True,
    }
    if folder_id:
        payload["folderId"] = folder_id
    payload.update(glossary_config)

    r = requests.post(
        YANDEX_URL,
        headers={
            "Authorization": f"Api-Key {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    if r.status_code in (429, 503):
        raise OSError(f"{r.status_code} {r.text[:200]}")
    r.raise_for_status()
    return [t["text"] for t in r.json()["translations"]]


def yandex_translate(texts: list, api_key: str, folder_id: str,
                     glossary_config: dict) -> list:
    """Translate a list of strings, retrying up to 3 times on 429/503."""
    backoff = 5.0
    last_err = None
    for attempt in range(3):
        try:
            return _yandex_call(texts, api_key, folder_id, glossary_config)
        except OSError as e:
            last_err = e
            if "limit on units" in str(e):
                raise  # hard hourly quota exhausted, no point retrying
            print(f" [rate-limited, waiting {backoff:.0f}s…]", end="", flush=True)
            time.sleep(backoff)
            backoff *= 2  # 5 → 10 → 20
    raise last_err


# ---------------------------------------------------------------------------
# Chunking for large texts
# ---------------------------------------------------------------------------

def split_into_chunks(text: str, limit: int = CHUNK_LIMIT) -> list:
    """Split text on blank-line paragraph boundaries into chunks ≤ limit chars."""
    paragraphs = re.split(r'\n{2,}', text)
    chunks = []
    current = ""
    for para in paragraphs:
        sep = "\n\n" if current else ""
        if len(current) + len(sep) + len(para) <= limit:
            current += sep + para
        else:
            if current:
                chunks.append(current)
            # If a single paragraph exceeds limit, hard-split on newlines
            if len(para) > limit:
                lines = para.split("\n")
                current = ""
                for line in lines:
                    sep2 = "\n" if current else ""
                    if len(current) + len(sep2) + len(line) <= limit:
                        current += sep2 + line
                    else:
                        if current:
                            chunks.append(current)
                        current = line
            else:
                current = para
    if current:
        chunks.append(current)
    return chunks or [""]


def translate_long_text(text: str, api_key: str, folder_id: str,
                        glossary_config: dict, delay: float,
                        progress_cb=None) -> str:
    """Split large text into chunks, translate each, and rejoin."""
    if len(text) <= CHUNK_LIMIT:
        if progress_cb:
            progress_cb(1, 1)
        return yandex_translate([text], api_key, folder_id, glossary_config)[0]

    chunks = split_into_chunks(text)
    total  = len(chunks)
    results = []
    for i, chunk in enumerate(chunks):
        if i > 0:
            time.sleep(delay)
        if progress_cb:
            progress_cb(i + 1, total)
        results.append(
            yandex_translate([chunk], api_key, folder_id, glossary_config)[0]
        )
    return "\n\n".join(results)


# ---------------------------------------------------------------------------
# Per-post translation
# ---------------------------------------------------------------------------

def translate_post(post_dir: Path, api_key: str, folder_id: str,
                   all_glossary: dict, force: bool, dry_run: bool,
                   delay: float) -> str:
    """Translate one post's index.en.md → index.ru.md.

    Returns: 'ok' | 'skip-done' | 'skip-no-en' | 'skip-bad-format' |
             'dry-run' | 'quota: <msg>' | 'error: <msg>'
    """
    en_path    = post_dir / "index.en.md"
    ru_path    = post_dir / "index.ru.md"
    named_path = post_dir / "_ru.yandex.md"

    if not en_path.exists():
        return "skip-no-en"

    # Skip if Yandex already translated this post unless --force
    if named_path.exists() and not force:
        if STUB_MARKER not in named_path.read_text(encoding="utf-8"):
            return "skip-done"

    en_text = en_path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(en_text)
    if fm is None:
        return "skip-bad-format"

    if dry_run:
        return "dry-run"

    # Extract English title from front matter
    title_m = re.search(r'^title: "(.+)"$', fm, re.MULTILINE)
    en_title = title_m.group(1) if title_m else ""

    # Build a per-post glossary from only the terms that appear in this post
    search_text = (en_title + " " + body) if en_title else body
    matched = filter_glossary_for_text(all_glossary, search_text)
    glossary_config = build_glossary_config(matched)

    # Protect Hugo shortcodes, images and link URLs before translation
    protected_body, slots = _protect(body)

    # Print post name + size before starting so the user sees progress
    print(f"  …  {post_dir.name}  ({len(protected_body):,} chars)", end="", flush=True)

    def _chunk_cb(current: int, total: int):
        if total > 1:
            print(f"\r  …  {post_dir.name}  chunk {current}/{total}   ", end="", flush=True)

    try:
        ru_body = translate_long_text(
            protected_body, api_key, folder_id, glossary_config, delay,
            progress_cb=_chunk_cb,
        )
    except requests.HTTPError as e:
        code = e.response.status_code
        body_text = e.response.text[:120]
        if code in (402, 429) or "quota" in body_text.lower():
            return f"quota: {code} {body_text}"
        return f"error: {code} {body_text}"
    except OSError as e:
        msg = str(e)
        if msg.startswith("429") or "limit on units" in msg:
            return f"quota: {msg[:120]}"
        return f"error: {msg}"
    except Exception as e:
        return f"error: {e}"

    ru_body = _restore(ru_body, slots)

    # Translate title separately
    ru_title = en_title
    if en_title:
        time.sleep(delay)
        try:
            ru_title = yandex_translate(
                [en_title], api_key, folder_id, glossary_config
            )[0].strip()
        except requests.HTTPError as e:
            return f"error-title: {e.response.status_code} {e.response.text[:80]}"
        except OSError as e:
            msg = str(e)
            if msg.startswith("429") or "limit on units" in msg:
                return f"quota: {msg[:120]}"
            return f"error-title: {msg}"
        except Exception as e:
            return f"error-title: {e}"

    # Rebuild front matter with translated title + translator tag
    ru_fm = fm
    if en_title and ru_title and ru_title != en_title:
        ru_title_safe = ru_title.replace('"', '&quot;')
        ru_fm = ru_fm.replace(f'title: "{en_title}"', f'title: "{ru_title_safe}"')
    # Set/update translator field
    ru_fm = re.sub(r'\ntranslator:.*', '', ru_fm)
    ru_fm += '\ntranslator: "Yandex"'

    write_translation(
        f"---\n{ru_fm}\n---\n<!-- translated by Yandex Translate -->\n{ru_body}",
        named_path, ru_path, "Yandex Translate", force,
    )
    return "ok"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Translate Fred Pohl blog posts EN→RU via Yandex Translate.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--key",    required=True, metavar="KEY",
                        help="Yandex Cloud API key")
    parser.add_argument("--folder", default="", metavar="ID",
                        help="Yandex Cloud folder ID (optional with API key auth)")
    parser.add_argument("--post",   metavar="FOLDER",
                        help="Translate a single post folder (e.g. 2009-02-28-josie)")
    parser.add_argument("--from",   metavar="FOLDER", dest="from_post",
                        help="Start batch from this post folder, inclusive")
    parser.add_argument("--force",  action="store_true",
                        help="Re-translate posts that already have Russian content")
    parser.add_argument("--dry-run", action="store_true",
                        help="List posts that would be translated without calling API")
    parser.add_argument("--section", metavar="NAME", default=DEFAULT_SECTION,
                        help=f"Content section to translate (default: {DEFAULT_SECTION})")
    parser.add_argument("--delay",  type=float, default=0.5, metavar="SECS",
                        help="Seconds to wait between API calls (default: 0.5)")
    parser.add_argument("--verbose", "-v", dest="verbose", action="store_true", default=True,
                        help="Show skipped posts (default: on)")
    parser.add_argument("--quiet",   "-q", dest="verbose", action="store_false",
                        help="Hide skipped posts")
    args = parser.parse_args()

    glossary_entries = load_glossary_entries()
    print(f"Loaded {len(glossary_entries)} glossary entries (filtered per post)\n")

    if args.post:
        post_dirs = [get_content_dir(args.section) / args.post]
    else:
        post_dirs = sorted(d for d in get_content_dir(args.section).iterdir() if d.is_dir())
        if args.from_post:
            from_name = Path(args.from_post).name
            matching = [d for d in post_dirs if d.name >= from_name]
            if not matching:
                print(f"Error: no post folder >= '{from_name}' found.", file=sys.stderr)
                sys.exit(1)
            skipped_count = len(post_dirs) - len(matching)
            print(f"Skipping {skipped_count} posts before '{matching[0].name}'")
            post_dirs = matching

    ok = skipped = errors = 0

    for post_dir in post_dirs:
        if not post_dir.is_dir():
            continue

        status = translate_post(
            post_dir, args.key, args.folder, glossary_entries,
            args.force, args.dry_run, args.delay,
        )

        if status == "ok":
            print(f"\r  ✓  {post_dir.name}                              ")
            ok += 1
            time.sleep(args.delay)
        elif status == "dry-run":
            print(f"  ~  {post_dir.name}  (would translate)")
            ok += 1
        elif status.startswith("skip"):
            if args.verbose:
                reason = status[len("skip-"):].replace("-", " ")
                print(f"  ~  {post_dir.name}  ({reason})")
            skipped += 1
        elif status.startswith("quota"):
            detail = status[len("quota:"):].strip()
            print(f"\n  ✗  Quota exceeded at '{post_dir.name}'.")
            if detail:
                print(f"     API error: {detail}")
            print(f"     Resume with: --from {post_dir.name}")
            print(f"\nDone: {ok} translated, {skipped} skipped, then quota hit.")
            sys.exit(1)
        else:
            print(f"\r  ✗  {post_dir.name}: {status}                    ")
            errors += 1

    action = "would translate" if args.dry_run else "translated"
    print(f"\nDone: {ok} {action}, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    main()
