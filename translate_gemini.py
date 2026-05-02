#!/usr/bin/env python3
"""
translate_gemini.py — Translate English blog posts to Russian using Google Gemini.

Usage:
    python3 translate_gemini.py --key YOUR_GEMINI_KEY [options]

Options:
    --key KEY       Google Gemini API key (required)
    --model MODEL   Gemini model name (default: gemini-2.0-flash-lite)
    --post FOLDER   Translate a single post folder only (e.g. 2009-02-28-josie)
    --from FOLDER   Start batch translation from this post folder (inclusive)
    --force         Re-translate posts that already have Russian content
    --dry-run       Show what would be translated without calling the API
    --delay SECS    Seconds to wait between API calls (default: 2.0)

Glossary:
    Reads glossary.txt (same file as translate.py) and injects the term list
    directly into the translation system prompt — no API sync step required.

Notes:
    - Working free-tier models (May 2026): gemini-2.5-flash-lite, gemini-2.5-flash
      (gemini-2.0-* models have limit:0 on the free tier)
    - Free tier: ~1500 req/day; with two calls per post --delay 2 is sufficient
    - 429 RESOURCE_EXHAUSTED is retried up to 3 times with increasing back-off;
      after 3 consecutive 429s the error is printed and the script exits cleanly
    - Hugo shortcodes {{< >}} and markdown link URLs are protected with
      XHOLD_N_X placeholder tokens so they survive translation unchanged
    - Gemini's 1M-token context window handles even the largest posts without chunking
"""

import argparse
import re
import sys
import time
from pathlib import Path

from translate_common import (
    CONTENT_DIR,
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

DEFAULT_MODEL    = "gemini-2.5-flash-lite"
GEMINI_BASE_URL  = "https://generativelanguage.googleapis.com/v1beta/models"

_SYSTEM_PROMPT_TEMPLATE = """\
You are a professional translator specialising in science fiction literature and memoir writing.
Translate the user's text from English to Russian.

Strict rules:
1. Use informal, casual Russian (разговорный авторский стиль); never use formal вы-constructions
2. Preserve ALL Markdown formatting exactly — headers (#, ##), bold (**text**), italic (*text*), \
unordered and ordered lists, code spans, blockquotes, horizontal rules, blank lines
3. Do NOT translate, modify, or split placeholder tokens that look like XHOLD_0_X — \
copy them verbatim into the output at the exact same position
4. Output the Russian translation ONLY — no preamble, no explanations, no commentary
5. Preserve paragraph breaks and blank lines exactly as in the source
6. Keep proper names in their original form unless a well-established Russian equivalent exists

Terminology glossary (always use these specific translations, case-insensitively):
{glossary}"""


def build_system_prompt(entries: dict) -> str:
    if entries:
        glossary_text = "\n".join(f"  {en} → {ru}" for en, ru in entries.items())
    else:
        glossary_text = "  (no glossary loaded)"
    return _SYSTEM_PROMPT_TEMPLATE.format(glossary=glossary_text)


# ---------------------------------------------------------------------------
# Gemini API
# ---------------------------------------------------------------------------

class QuotaError(Exception):
    """Raised when Gemini returns 429 RESOURCE_EXHAUSTED — hard quota, do not retry."""
    def __init__(self, body: str):
        self.body = body
        super().__init__(f"429 RESOURCE_EXHAUSTED: {body[:200]}")


class ServiceError(Exception):
    """Raised when Gemini returns 503 Service Unavailable — transient, worth retrying."""
    def __init__(self, body: str):
        self.body = body
        super().__init__(f"503 Service Unavailable: {body[:200]}")


# Keep alias so old catch sites still compile during transition
RateLimitError = QuotaError


def _gemini_call(text: str, api_key: str, model: str, system_prompt: str) -> str:
    """Single call to the Gemini generateContent REST endpoint."""
    url = f"{GEMINI_BASE_URL}/{model}:generateContent?key={api_key}"
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "temperature": 0.1,   # low temperature → consistent, predictable translation
            "candidateCount": 1,
        },
    }
    r = requests.post(url, json=payload, timeout=120)
    if r.status_code == 429:
        raise QuotaError(r.text)
    if r.status_code == 503:
        raise ServiceError(r.text)
    r.raise_for_status()
    data = r.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError(f"Gemini returned no candidates: {r.text[:300]}")
    finish_reason = candidates[0].get("finishReason", "")
    if finish_reason not in ("STOP", ""):
        raise ValueError(f"Unexpected finishReason '{finish_reason}': {r.text[:300]}")
    return candidates[0]["content"]["parts"][0]["text"]


def gemini_translate(text: str, api_key: str, model: str,
                     system_prompt: str) -> str:
    """Translate text via Gemini. 503 is retried up to 3 times; 429 exits immediately."""
    backoff = 15.0
    last_err = None
    for attempt in range(3):
        try:
            return _gemini_call(text, api_key, model, system_prompt)
        except QuotaError:
            raise  # hard quota — no point waiting, propagate immediately
        except ServiceError as e:
            last_err = e
            print(f" [overloaded, waiting {backoff:.0f}s\u2026]", end="", flush=True)
            time.sleep(backoff)
            backoff *= 2  # 15 → 30 → 60
    raise last_err


# ---------------------------------------------------------------------------
# Per-post translation
# ---------------------------------------------------------------------------

def translate_post(post_dir: Path, api_key: str, model: str,
                   system_prompt: str, force: bool, dry_run: bool,
                   delay: float) -> str:
    """Translate one post's index.en.md → index.ru.md.

    Returns: 'ok' | 'skip-done' | 'skip-no-en' | 'skip-bad-format' |
             'dry-run' | 'error: <msg>' | 'quota: <msg>'
    """
    en_path    = post_dir / "index.en.md"
    ru_path    = post_dir / "index.ru.md"
    named_path = post_dir / "_ru.gemini.md"

    if not en_path.exists():
        return "skip-no-en"

    # Skip if Gemini already translated this post unless --force
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

    # Protect Hugo shortcodes and link URLs before translation
    protected_body, slots = protect(body)

    try:
        ru_body = gemini_translate(protected_body, api_key, model, system_prompt)
    except QuotaError as e:
        return f"quota: {e.body[:120]}"
    except ServiceError as e:
        return f"error: 503 {e.body[:120]}"
    except requests.HTTPError as e:
        return f"error: {e.response.status_code} {e.response.text[:120]}"
    except Exception as e:
        return f"error: {e}"

    ru_body = restore(ru_body, slots)

    # Translate title separately (provides better focus for short strings)
    ru_title = en_title
    if en_title:
        time.sleep(delay)
        try:
            raw = gemini_translate(en_title, api_key, model, system_prompt)
            # Strip any spurious surrounding quotes Gemini may add
            ru_title = raw.strip().strip('"').strip("«»").strip()
        except QuotaError as e:
            return f"quota: {e.body[:120]}"
        except Exception as e:
            return f"error-title: {e}"

    # Rebuild front matter with translated title + translator tag
    ru_fm = fm
    if en_title and ru_title and ru_title != en_title:
        ru_title_safe = ru_title.replace('"', '&quot;')
        ru_fm = ru_fm.replace(f'title: "{en_title}"', f'title: "{ru_title_safe}"')
    # Set/update translator field
    ru_fm = re.sub(r'\ntranslator:.*', '', ru_fm)
    ru_fm += f'\ntranslator: "Gemini"'

    write_translation(
        f"---\n{ru_fm}\n---\n<!-- translated by Gemini ({model}) -->\n{ru_body}",
        named_path, ru_path, f"Gemini ({model})", force,
    )
    return "ok"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Translate Fred Pohl blog posts EN→RU via Google Gemini.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--key",     required=True, metavar="KEY",
                        help="Google Gemini API key")
    parser.add_argument("--model",   default=DEFAULT_MODEL, metavar="MODEL",
                        help=f"Gemini model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--post",    metavar="FOLDER",
                        help="Translate a single post folder (e.g. 2009-02-28-josie)")
    parser.add_argument("--from",    metavar="FOLDER", dest="from_post",
                        help="Start batch from this post folder, inclusive")
    parser.add_argument("--force",   action="store_true",
                        help="Re-translate posts that already have Russian content")
    parser.add_argument("--dry-run", action="store_true",
                        help="List posts that would be translated without calling API")
    parser.add_argument("--delay",   type=float, default=2.0, metavar="SECS",
                        help="Seconds to wait between API calls (default: 2.0)")
    parser.add_argument("--verbose", "-v", dest="verbose", action="store_true", default=True,
                        help="Show skipped posts (default: on)")
    parser.add_argument("--quiet",   "-q", dest="verbose", action="store_false",
                        help="Hide skipped posts")
    args = parser.parse_args()

    glossary_entries = load_glossary_entries()
    system_prompt = build_system_prompt(glossary_entries)
    print(f"Loaded {len(glossary_entries)} glossary entries")
    print(f"Model: {args.model}\n")

    if args.post:
        post_dirs = [CONTENT_DIR / args.post]
    else:
        post_dirs = sorted(d for d in CONTENT_DIR.iterdir() if d.is_dir())
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
            post_dir, args.key, args.model, system_prompt,
            args.force, args.dry_run, args.delay,
        )

        if status == "ok":
            print(f"  ✓  {post_dir.name}")
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
                print(f"     API error: {detail[:200]}")
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
