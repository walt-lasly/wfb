#!/usr/bin/env python3
"""
translate.py — Translate English blog posts to Russian using the DeepL API.

Usage:
    python3 translate.py --key YOUR_DEEPL_KEY [options]

Options:
    --key KEY           DeepL API authentication key (required, ends in :fx for free tier)
    --pro               Use DeepL Pro API endpoint (default: free-tier api-free.deepl.com)
    --post FOLDER       Translate a single post folder only (e.g. 2009-02-28-josie)
    --from FOLDER       Start batch translation from this post folder (inclusive)
    --force             Re-translate posts that already have Russian content
    --dry-run           Show what would be translated without calling the API
    --sync-glossary     Upload/replace the glossary from glossary.txt to DeepL and exit

Glossary:
    Edit glossary.txt to control how specific terms are translated.
    Each non-empty line: English term<TAB>Russian term
    Lines starting with # are comments.
    Run --sync-glossary once after editing the file to push changes to DeepL.

Notes:
    - Free-tier key ends in ':fx'
    - Free tier: 500,000 characters/month, rate limit ~3 req/s
    - Posts whose index.ru.md no longer contains the stub comment are skipped
      unless --force is passed.
    - Hugo shortcodes {{< >}} and markdown link URLs are protected from
      translation using placeholder tokens.
"""

import argparse
import re
import sys
import time
from pathlib import Path

from translate_common import (
    CONTENT_DIR,
    GLOSSARY_FILE,
    STUB_MARKER,
    load_glossary_entries,
    protect,
    restore,
    split_frontmatter,
    write_translation,
)

try:
    import requests
except ImportError:
    print("Error: 'requests' not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)


def deepl_translate(text: str, api_key: str, pro: bool,
                    glossary_id: str = "") -> str:
    """Call DeepL REST API to translate text from English to Russian."""
    base = "https://api.deepl.com" if pro else "https://api-free.deepl.com"
    data = {
        "text": text,
        "source_lang": "EN",
        "target_lang": "RU",
        # Fred Pohl's blog is casual first-person memoir; avoid formal вы-constructions
        "formality": "prefer_less",
        # Respect original formatting (punctuation, capitalisation at sentence boundaries)
        "preserve_formatting": "1",
        # Default split (punctuation + newlines) keeps paragraph/heading structure intact
        # "nonewlines" was causing all text to merge onto one line
    }
    if glossary_id:
        data["glossary_id"] = glossary_id
    r = requests.post(
        f"{base}/v2/translate",
        headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
        data=data,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["translations"][0]["text"]


def sync_glossary(api_key: str, pro: bool):
    """Upload glossary.txt to DeepL, replacing any existing EN→RU glossary."""
    base = "https://api.deepl.com" if pro else "https://api-free.deepl.com"
    headers = {"Authorization": f"DeepL-Auth-Key {api_key}"}

    entries = load_glossary_entries()
    if not entries:
        print("glossary.txt is empty or missing — nothing to sync.")
        return

    # Delete existing EN→RU glossaries with the same name
    r = requests.get(f"{base}/v2/glossaries", headers=headers, timeout=10)
    r.raise_for_status()
    for g in r.json().get("glossaries", []):
        if g["name"] == "fred-pohl-blog" and g["source_lang"] == "en" and g["target_lang"] == "ru":
            gid = g.get("glossary_id") or g.get("id")
            requests.delete(f"{base}/v2/glossaries/{gid}", headers=headers, timeout=10)
            print(f"  Deleted old glossary {gid}")

    # Build TSV content
    tsv = "\n".join(f"{en}\t{ru}" for en, ru in entries.items())

    r = requests.post(
        f"{base}/v2/glossaries",
        headers=headers,
        json={
            "name": "fred-pohl-blog",
            "source_lang": "en",
            "target_lang": "ru",
            "entries": tsv,
            "entries_format": "tsv",
        },
        timeout=15,
    )
    r.raise_for_status()
    gid = r.json()["glossary_id"]
    print(f"  Glossary uploaded: {len(entries)} entries, id={gid}")


def get_glossary_id(api_key: str, pro: bool) -> str:
    """Return the DeepL glossary ID for 'fred-pohl-blog' (EN→RU), or empty string."""
    base = "https://api.deepl.com" if pro else "https://api-free.deepl.com"
    try:
        r = requests.get(
            f"{base}/v2/glossaries",
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            timeout=10,
        )
        r.raise_for_status()
        for g in r.json().get("glossaries", []):
            if (g["name"] == "fred-pohl-blog"
                    and g["source_lang"] == "en"
                    and g["target_lang"] == "ru"
                    and g.get("ready", False)):
                return g["glossary_id"]
    except Exception:
        pass
    return ""


def translate_post(post_dir: Path, api_key: str, force: bool, pro: bool,
                   dry_run: bool, glossary_id: str = "") -> str:
    """Translate one post directory's index.en.md → index.ru.md.

    Returns a short status string: 'ok', 'skip-done', 'skip-no-en',
    'skip-bad-format', 'dry-run', or 'error: <message>'.
    """
    en_path    = post_dir / "index.en.md"
    ru_path    = post_dir / "index.ru.md"
    named_path = post_dir / "_ru.deepl.md"

    if not en_path.exists():
        return "skip-no-en"

    # Skip if DeepL already translated this post unless --force
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

    # Protect body (shortcodes + URLs) before sending to DeepL
    protected_body, slots = protect(body)

    try:
        ru_body = deepl_translate(protected_body, api_key, pro, glossary_id)
    except requests.HTTPError as e:
        return f"error: {e.response.status_code} {e.response.text[:120]}"
    except Exception as e:
        return f"error: {e}"

    ru_body = restore(ru_body, slots)

    # Translate title separately
    ru_title = en_title
    if en_title:
        time.sleep(0.35)  # rate limit: ~3 req/s on free tier
        try:
            ru_title = deepl_translate(en_title, api_key, pro, glossary_id)
        except Exception as e:
            return f"error-title: {e}"

    # Build Russian front matter — same as English except translated title + translator tag
    ru_fm = fm
    if en_title and ru_title != en_title:
        # Escape any double-quotes in the translated title
        ru_title_safe = ru_title.replace('"', '&quot;')
        ru_fm = ru_fm.replace(f'title: "{en_title}"', f'title: "{ru_title_safe}"')
    # Set/update translator field
    ru_fm = re.sub(r'\ntranslator:.*', '', ru_fm)
    ru_fm += '\ntranslator: "DeepL"'

    write_translation(
        f"---\n{ru_fm}\n---\n<!-- translated by DeepL -->\n{ru_body}",
        named_path, ru_path, "DeepL", force,
    )
    return "ok"


def main():
    parser = argparse.ArgumentParser(
        description="Translate Fred Pohl blog posts EN→RU via DeepL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--key",     required=True, metavar="KEY",
                        help="DeepL API authentication key")
    parser.add_argument("--pro",     action="store_true",
                        help="Use DeepL Pro endpoint (default: free tier)")
    parser.add_argument("--sync-glossary", action="store_true",
                        help="Upload glossary.txt to DeepL and exit")
    parser.add_argument("--post",    metavar="FOLDER",
                        help="Translate a single post folder (e.g. 2009-02-28-josie)")
    parser.add_argument("--from",    metavar="FOLDER", dest="from_post",
                        help="Start batch from this post folder, inclusive (e.g. 2009-06-01-some-post)")
    parser.add_argument("--force",   action="store_true",
                        help="Re-translate posts that already have Russian content")
    parser.add_argument("--dry-run", action="store_true",
                        help="List posts that would be translated without calling API")
    parser.add_argument("--verbose", "-v", dest="verbose", action="store_true", default=True,
                        help="Show skipped posts (default: on)")
    parser.add_argument("--quiet",   "-q", dest="verbose", action="store_false",
                        help="Hide skipped posts")
    args = parser.parse_args()

    if args.sync_glossary:
        sync_glossary(args.key, args.pro)
        return

    # Load glossary ID once (if a glossary has been synced)
    glossary_id = get_glossary_id(args.key, args.pro)
    if glossary_id:
        print(f"Using glossary: {glossary_id}\n")

    if args.post:
        post_dirs = [CONTENT_DIR / args.post]
    else:
        post_dirs = sorted(d for d in CONTENT_DIR.iterdir() if d.is_dir())
        if args.from_post:
            # Drop everything before the specified folder (by sorted name)
            from_name = Path(args.from_post).name  # tolerate trailing slash
            matching = [d for d in post_dirs if d.name >= from_name]
            if not matching:
                print(f"Error: no post folder >= '{from_name}' found.", file=sys.stderr)
                sys.exit(1)
            skipped_count = len(post_dirs) - len(matching)
            print(f"Skipping {skipped_count} posts before '{matching[0].name}'")
            post_dirs = matching

    total = len(post_dirs)
    ok = skipped = errors = 0

    for post_dir in post_dirs:
        if not post_dir.is_dir():
            continue

        status = translate_post(post_dir, args.key, args.force, args.pro,
                                args.dry_run, glossary_id)

        if status == "ok":
            print(f"  ✓  {post_dir.name}")
            ok += 1
            time.sleep(0.35)  # free-tier rate limit
        elif status == "dry-run":
            print(f"  ~  {post_dir.name}  (would translate)")
            ok += 1
        elif status.startswith("skip"):
            if args.verbose:
                reason = status[len("skip-"):].replace("-", " ")
                print(f"  ~  {post_dir.name}  ({reason})")
            skipped += 1
        elif "456" in status or "Quota Exceeded" in status:
            print(f"\n  ✗  Quota exceeded at '{post_dir.name}'.")
            print(f"     Resume with: --from {post_dir.name}")
            print(f"\nDone: {ok} translated, {skipped} skipped, then quota hit.")
            sys.exit(1)
        else:
            print(f"  ✗  {post_dir.name}: {status}")
            errors += 1

    action = "would translate" if args.dry_run else "translated"
    print(f"\nDone: {ok} {action}, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    main()
