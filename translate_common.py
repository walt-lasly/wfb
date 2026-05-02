"""
translate_common.py — Shared utilities for all translate_*.py scripts.

Provides:
  - Path constants  (SCRIPT_DIR, CONTENT_DIR, GLOSSARY_FILE, STUB_MARKER)
  - Placeholder protection  (protect / restore)
  - Glossary loading  (load_glossary_entries)
  - Front-matter parsing  (split_frontmatter)
  - Translator file helpers  (write_translation, backup_if_different_translator)
"""

import re
import sys
from pathlib import Path

try:
    import requests  # noqa: F401 — imported here so callers get a clear error early
except ImportError:
    print("Error: 'requests' not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

SCRIPT_DIR    = Path(__file__).parent
CONTENT_DIR   = SCRIPT_DIR / "content" / "posts"
GLOSSARY_FILE = SCRIPT_DIR / "glossary.txt"
STUB_MARKER   = "<!-- ПЕРЕВОД -->"

# ---------------------------------------------------------------------------
# Placeholder protection
# ---------------------------------------------------------------------------

# Hugo shortcodes: {{< ... >}} and {{% ... %}}
_SHORTCODE_RE = re.compile(r'\{\{[<%].*?[>%]\}\}', re.DOTALL)
# Markdown link / image URLs: [text](URL) — protect URL, leave text translatable
_LINK_URL_RE  = re.compile(r'(\[[^\]]*\])\(([^)\s]+)\)')


def protect(text: str):
    """Replace Hugo shortcodes and link URLs with XHOLD_N_X tokens.

    Returns (protected_text, slots) where slots[N] is the original value.
    Token format is all-caps with underscores — opaque to all translation APIs.
    """
    slots = []

    def slot(val: str) -> str:
        n = len(slots)
        slots.append(val)
        return f"XHOLD_{n}_X"

    # Protect shortcodes first (they may contain square brackets)
    text = _SHORTCODE_RE.sub(lambda m: slot(m.group()), text)
    # Protect link/image URLs (keep link text translatable)
    text = _LINK_URL_RE.sub(lambda m: m.group(1) + "(" + slot(m.group(2)) + ")", text)

    return text, slots


def restore(text: str, slots: list) -> str:
    """Restore XHOLD_N_X placeholders to their original values."""
    for n, val in enumerate(slots):
        text = text.replace(f"XHOLD_{n}_X", val)
    return text

# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------


def load_glossary_entries() -> dict:
    """Read glossary.txt and return {english: russian} dict."""
    if not GLOSSARY_FILE.exists():
        return {}
    entries = {}
    for line in GLOSSARY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            entries[parts[0].strip()] = parts[1].strip()
    return entries

# ---------------------------------------------------------------------------
# Front-matter parsing
# ---------------------------------------------------------------------------


def split_frontmatter(content: str):
    """Split a Hugo markdown file into (front_matter_str, body_str).

    Returns (None, content) if no front matter is found.
    """
    m = re.match(r'^---\n(.*?)\n---\n?(.*)', content, re.DOTALL)
    if not m:
        return None, content
    return m.group(1), m.group(2)

# ---------------------------------------------------------------------------
# Translator backup helpers
# ---------------------------------------------------------------------------

# Matches: <!-- translated by Gemini (gemini-2.5-flash-lite) --> or <!-- translated by DeepL -->
_TRANSLATOR_MARK_RE = re.compile(r'<!--\s*translated by (.+?)\s*-->')


def _translator_slug(mark_text: str) -> str:
    """Convert a translator name to a safe filename slug.

    e.g. 'Gemini (gemini-2.5-flash-lite)' → 'gemini'
    """
    first_word = mark_text.strip().split()[0]
    return re.sub(r'[^a-z0-9]', '', first_word.lower())


def backup_if_different_translator(ru_path: Path, own_mark: str) -> None:
    """If index.ru.md exists and was made by a different translator, back it up.

    Backup name: index.ru.<slug>.md  (e.g. index.ru.deepl.md)
    Does nothing if the file is missing, has no translator mark, or the mark
    matches own_mark.
    """
    if not ru_path.exists():
        return
    content = ru_path.read_text(encoding="utf-8")
    m = _TRANSLATOR_MARK_RE.search(content)
    if not m:
        return
    existing_mark = m.group(1).strip()
    own_slug      = _translator_slug(own_mark)
    existing_slug = _translator_slug(existing_mark)
    if existing_slug == own_slug:
        return
    backup_path = ru_path.parent / f"_ru.{existing_slug}.md"
    backup_path.write_text(content, encoding="utf-8")


def write_translation(content: str, named_path: Path, ru_path: Path,
                      own_mark: str, force: bool) -> None:
    """Write translation to named file and optionally update index.ru.md.

    Always writes ``named_path`` (e.g. _ru.deepl.md).
    Updates ``ru_path`` (index.ru.md) unless a *different* translator already
    owns it and ``force`` is False.
    """
    named_path.write_text(content, encoding="utf-8")
    if ru_path.exists() and not force:
        existing = ru_path.read_text(encoding="utf-8")
        m = _TRANSLATOR_MARK_RE.search(existing)
        if m and _translator_slug(m.group(1).strip()) != _translator_slug(own_mark):
            return  # different translator owns active file — leave it alone
    ru_path.write_text(content, encoding="utf-8")
