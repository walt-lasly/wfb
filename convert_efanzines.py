#!/usr/bin/env python3
"""
convert_efanzines.py — Convert saved efanzines.com HTML files into Hugo page bundles.

One HTML file (one fanzine issue) → one Hugo page bundle under content/<section>/.

Usage:
    python3 convert_efanzines.py
    python3 convert_efanzines.py --source /path/to/efanzines/ --section efanzines --force

Run from the fred-pohl-blog/ directory.
The default source directory is ../../Magazines/efanzines/ relative to this script.
"""

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR      = Path(__file__).parent
SITE_DIR        = SCRIPT_DIR
DEFAULT_SECTION = "efanzines"
# Two levels up from fred-pohl-blog/ → books/  →  books/Magazines/efanzines/
DEFAULT_SOURCE  = SCRIPT_DIR.parent.parent / "Magazines" / "efanzines"

MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# Decorative / layout images that should not be included in output
_SKIP_IMAGES    = {"grey2bg.jpg"}
_SKIP_PREFIXES  = ("Ilogo", "ilogo")


# ── Helpers ────────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[''`]", "", text)
    text = re.sub(r"[^\w\s-]", " ", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")[:80]


def _sc(value: str) -> str:
    """Escape a string for use inside a Hugo shortcode quoted attribute."""
    return value.replace('"', '&quot;')


def is_layout_image(fname: str) -> bool:
    if fname in _SKIP_IMAGES:
        return True
    low = fname.lower()
    return any(low.startswith(p.lower()) for p in _SKIP_PREFIXES)


def parse_month_year(text: str):
    """Return datetime(year, month, 1) for the first 'Month YYYY' found, or None."""
    m = re.search(r"\b([A-Za-z]+)\s+(\d{4})\b", text)
    if m:
        month = MONTHS_EN.get(m.group(1).lower())
        if month:
            return datetime(int(m.group(2)), month, 1)
    return None


# ── File parser ────────────────────────────────────────────────────────────────

def parse_efanzines_file(html_path: Path) -> dict:
    """
    Read one efanzines HTML file.  Returns:
      title         – cleaned page title
      date          – datetime extracted from the magazine header
      content_nodes – list of BS nodes to convert (header table excluded)
      img_root      – the <body> element (used to collect image filenames)
      files_dir     – Path to the companion _files/ folder, or None
    """
    # Open in binary mode so BeautifulSoup honours the charset from <meta>.
    with open(html_path, "rb") as f:
        raw_bytes = f.read()
    soup = BeautifulSoup(raw_bytes, "html.parser")

    # ── Source URL (from IE-style "saved from" comment) ────────────────────────
    # The browser-saved file starts with:
    #   <!-- saved from url=(NNNN)https://efanzines.com/... -->
    archive_url = ""
    m = re.search(
        rb"<!--\s*saved from url=\(\d+\)(https?://[^\s>]+)\s*-->",
        raw_bytes[:2048],
    )
    if m:
        archive_url = m.group(1).decode("ascii", errors="replace").strip()

    # ── Title ──────────────────────────────────────────────────────────────────
    title_el  = soup.find("title")
    raw_title = title_el.get_text(strip=True) if title_el else ""
    raw_title = re.sub(r"\s+", " ", raw_title).strip()
    # Windows-1252 em dash (0x97) sometimes survives as a replacement character
    # when the file was re-encoded; normalise it.
    raw_title = raw_title.replace("\ufffd", "—").replace("\ufffe", "—").replace("\ufffc", "—")
    title = raw_title or html_path.stem

    # ── Locate the content cell ────────────────────────────────────────────────
    body = soup.find("body")
    if not body:
        return {
            "title": title, "date": datetime(2000, 1, 1), "archive_url": archive_url,
            "content_nodes": [], "img_root": soup, "files_dir": None,
        }

    # The page layout is: <body> → <table width=650 align=center> → <tbody> →
    # <tr> → <td> → (header table, cover table, content…).
    outer_table = body.find("table")
    content_td  = outer_table.find("td") if outer_table else None
    content_root = content_td or body

    # Walk direct children; skip the first <table> (magazine vol/date/copyright
    # header) but capture its text for the date.
    date_obj       = None
    copyright_text = ""
    content_nodes  = []
    header_table_seen = False

    for child in content_root.children:
        name = getattr(child, "name", None)
        if name == "table" and not header_table_seen:
            header_table_seen = True
            if date_obj is None:
                date_obj = parse_month_year(child.get_text())
            # Extract copyright notice from the <small> cell in the header table.
            small_el = child.find("small")
            if small_el:
                copyright_text = inline_text(small_el).strip()
                copyright_text = re.sub(r"[ \t]*\n[ \t]*", " ", copyright_text)
                copyright_text = re.sub(r"  +", " ", copyright_text).strip()
            else:
                copyright_text = ""
            # Do NOT add the header table to content — it's layout/boilerplate.
            continue
        content_nodes.append(child)

    # Fallback: scan the full document for a "Month YYYY" pattern
    if date_obj is None:
        date_obj = parse_month_year(soup.get_text())

    # ── Companion _files/ folder ───────────────────────────────────────────────
    files_dir = html_path.parent / (html_path.stem + "_files")

    return {
        "title":         title,
        "date":          date_obj or datetime(2000, 1, 1),
        "archive_url":    archive_url,
        "copyright":     copyright_text,
        "content_nodes": content_nodes,
        "img_root":      body,
        "files_dir":     files_dir if files_dir.is_dir() else None,
    }


# ── Inline text renderer ───────────────────────────────────────────────────────

def inline_text(node, page_url: str = "") -> str:
    """Render the inline children of *node* to a Markdown string.

    page_url: the base URL of the source page (without fragment).  When an
    <a href> points to the same page (same URL + "#anchor"), it is converted
    to a local fragment link (#anchor) rather than an external URL.
    """
    parts = []
    for child in node.children:
        name = getattr(child, "name", None)
        if name is None:
            parts.append(str(child))
        elif name == "font":
            parts.append(inline_text(child, page_url))
        elif name in ("b", "strong"):
            inner = inline_text(child, page_url).strip()
            if inner:
                parts.append(f"**{inner}**")
        elif name in ("i", "em"):
            inner = inline_text(child, page_url).strip()
            if inner:
                parts.append(f"*{inner}*")
        elif name in ("small", "big", "span", "cite", "tt", "code"):
            parts.append(inline_text(child, page_url))
        elif name == "br":
            parts.append("  \n")
        elif name == "a":
            href        = child.get("href", "").strip()
            anchor_name = child.get("name", "")
            text        = inline_text(child, page_url).strip()
            if anchor_name:
                # Named anchor — just emit the visible text; anchors are handled
                # at the heading level.
                parts.append(text)
            else:
                # Convert same-page anchor links to local #fragment links
                if page_url and href.startswith(page_url + "#"):
                    href = href[len(page_url):]
                if href and text:
                    parts.append(f"[{text}]({href})")
                else:
                    parts.append(text)
        else:
            parts.append(inline_text(child, page_url))
    return "".join(parts)


def _font_heading_level(tag) -> int:
    """Return 2 (##) for <font size=5+>, 3 (###) for <font size=4>, else 0."""
    if getattr(tag, "name", None) == "font":
        try:
            size = int(tag.get("size", "0"))
            if size >= 5:
                return 2
            if size == 4:
                return 3
        except (ValueError, TypeError):
            pass
    return 0


# ── HTML → Markdown ────────────────────────────────────────────────────────────

def efanzines_html_to_markdown(content_nodes, page_url: str = "") -> str:
    """Convert a list of BeautifulSoup nodes (efanzines body) to Markdown.

    page_url: base URL of the source page (no fragment), used to convert
    same-page anchor hrefs to local #fragment links.
    """
    lines = []

    def process(node):
        name = getattr(node, "name", None)

        # ── NavigableString ────────────────────────────────────────────────────
        if name is None:
            text = str(node).strip()
            if text:
                lines.append(text)
            return

        # ── Ignored tags ───────────────────────────────────────────────────────
        if name in ("script", "style"):
            return

        # ── Table: extract images (and optional caption paras) ─────────────────
        # Tables in efanzines HTML are used purely for image layout, not for
        # article text.  We pull out images and any caption paragraphs.
        if name == "table":
            for img in node.find_all("img"):
                src   = img.get("src", "")
                fname = Path(src).name
                if not fname or is_layout_image(fname):
                    continue
                alt         = img.get("alt", "") or img.get("title", "")
                href_parent = img.find_parent("a", href=True)
                if href_parent:
                    full_href = href_parent.get("href", "")
                    lines.append(f"\n[![{alt}]({fname})]({full_href})\n")
                else:
                    lines.append(f"\n![{alt}]({fname})\n")
            # Caption paragraphs (text-only <p> elements inside image tables)
            for p in node.find_all("p"):
                if p.find("img"):
                    continue
                text = inline_text(p).strip()
                if text:
                    lines.append("\n" + text + "\n")
            return

        # ── Structural pass-through ────────────────────────────────────────────
        if name in ("tbody", "tr", "td", "div"):
            for child in node.children:
                process(child)
            return

        # ── Explicit heading tags (rare in this HTML, but handle defensively) ──
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(name[1])
            text  = node.get_text(strip=True)
            lines.append(f'\n{"#" * level} {text}\n')
            return

        # ── Paragraph ─────────────────────────────────────────────────────────
        if name == "p":
            # Look for a heading-size <font> anywhere within this <p>.
            # In efanzines HTML, <font size="5"> and <font size="4"> are used
            # exclusively for section headings; searching recursively handles
            # cases where the heading <font> is wrapped in an outer <font face>
            # or preceded by an anchor-only <b> tag.
            heading_font = node.find(
                lambda tag: tag.name == "font" and _font_heading_level(tag) > 0
            )
            if heading_font:
                level = _font_heading_level(heading_font)
                # Named anchor may appear anywhere in the <p> (including inside
                # a sibling <b> tag), so search the whole paragraph.
                anchor_tag = node.find("a", attrs={"name": True})
                anchor_id  = anchor_tag.get("name", "") if anchor_tag else ""
                # Use inline_text so spaces around <i>/<b> are preserved
                text = inline_text(heading_font, page_url).strip()
                # Remove <br>-induced newlines (headings must be single line)
                text = re.sub(r"[ \t]*\n[ \t]*", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                id_suffix = f" {{#{anchor_id}}}" if anchor_id else ""
                lines.append(f'\n{"#" * level} {text}{id_suffix}\n')
                return

            # Regular paragraph — render with inline markup preserved
            text = inline_text(node, page_url).strip()
            # Collapse runs of non-breaking spaces used for indentation
            text = re.sub(r"[\xa0\u00a0]{3,}", " ", text)
            text = re.sub(r"[ \t]{3,}", " ", text)
            if not text:
                return
            # '#' alone on a line is a typographic section divider in efanzines
            # (like a printer's ornament).  Convert to a horizontal rule so it
            # does not become an accidental H1 heading in Markdown.
            if text == "#":
                lines.append("\n---\n")
            else:
                lines.append("\n" + text + "\n")
            return

        # ── Standalone <font> heading (outside a <p>) ─────────────────────────
        if name == "font":
            level = _font_heading_level(node)
            if level:
                text = re.sub(r"\s+", " ", node.get_text(strip=True))
                lines.append(f'\n{"#" * level} {text}\n')
            else:
                for child in node.children:
                    process(child)
            return

        # ── Blockquote ─────────────────────────────────────────────────────────
        if name == "blockquote":
            start = len(lines)
            for child in node.children:
                process(child)
            end = len(lines)
            for i in range(start, end):
                if lines[i].strip():
                    lines[i] = "> " + lines[i].lstrip()
            lines.append("")
            return

        # ── Lists ──────────────────────────────────────────────────────────────
        if name in ("ul", "ol"):
            for li in node.find_all("li", recursive=False):
                lines.append("- " + li.get_text(strip=True))
            lines.append("")
            return

        # ── Standalone image ───────────────────────────────────────────────────
        if name == "img":
            src   = node.get("src", "")
            fname = Path(src).name
            if fname and not is_layout_image(fname):
                alt = node.get("alt", "") or node.get("title", "")
                lines.append(f"\n![{alt}]({fname})\n")
            return

        # ── Horizontal rule ────────────────────────────────────────────────────
        if name == "hr":
            lines.append("\n---\n")
            return

        # ── Default: recurse into children ────────────────────────────────────
        for child in node.children:
            process(child)

    for node in content_nodes:
        process(node)

    text = "\n".join(lines)
    text = re.sub(r"\n{4,}", "\n\n", text)
    return text.strip()


# ── Front matter builder ───────────────────────────────────────────────────────

def build_front_matter(title: str, date: datetime, translated: bool,
                        lang: str, archive_url: str = "") -> str:
    safe_title = title.replace('"', '&quot;')
    lines = [
        "---",
        f'title: "{safe_title}"',
        f'date: {date.strftime("%Y-%m-%d")}',
    ]
    if archive_url:
        lines.append(f'archive_url: "{archive_url}"')
    lines.append(f'translated: {"true" if translated else "false"}')
    lines.append("---")
    return "\n".join(lines) + "\n\n"


# ── Per-file converter ─────────────────────────────────────────────────────────

def convert_file(html_path: Path, content_dir: Path, force: bool = False):
    print(f"  Processing: {html_path.name}")

    data   = parse_efanzines_file(html_path)
    title  = data["title"]
    date   = data["date"]
    slug   = slugify(title)
    folder = f"{date.strftime('%Y-%m-%d')}-{slug}"

    dest_dir = content_dir / folder

    if dest_dir.exists() and not force:
        print(f"    ↳ Skipping (already exists): {folder}")
        return

    dest_dir.mkdir(parents=True, exist_ok=True)

    # ── Copy images ────────────────────────────────────────────────────────────
    if data["files_dir"]:
        used = {
            Path(img.get("src", "")).name
            for img in data["img_root"].find_all("img")
            if img.get("src", "")
        }
        for img_file in data["files_dir"].iterdir():
            if img_file.name not in used or is_layout_image(img_file.name):
                continue
            shutil.copy2(img_file, dest_dir / img_file.name)

    # ── Convert body to Markdown ────────────────────────────────────────────────
    archive_url = data.get("archive_url", "")
    body_md = efanzines_html_to_markdown(data["content_nodes"], page_url=archive_url)

    # ── Prepend copyright notice as a blockquote ──────────────────────────────
    if data.get("copyright"):
        body_md = "> " + data["copyright"] + "\n\n" + body_md

    # ── Write index.en.md ──────────────────────────────────────────────────────
    archive_url = data.get("archive_url", "")
    fm_en = build_front_matter(title=title, date=date, translated=False, lang="en",
                               archive_url=archive_url)
    (dest_dir / "index.en.md").write_text(fm_en + body_md, encoding="utf-8")

    # ── Write index.ru.md stub ─────────────────────────────────────────
    ru_path = dest_dir / "index.ru.md"
    if not ru_path.exists() or force:
        fm_ru = build_front_matter(title=title, date=date, translated=False, lang="ru",
                                   archive_url=archive_url)
        stub = (
            "<!-- ПЕРЕВОД -->\n"
            "<!-- Замените эту строку переводом статьи. -->\n"
        )
        ru_path.write_text(fm_ru + stub, encoding="utf-8")

    print(f"    ↳ Created: {folder}/")


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert efanzines.com HTML files to Hugo page bundles."
    )
    parser.add_argument(
        "--source", metavar="DIR", type=Path, default=DEFAULT_SOURCE,
        help=f"Directory containing .html files (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--section", metavar="NAME", default=DEFAULT_SECTION,
        help=f"Hugo content section to write into (default: {DEFAULT_SECTION})",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing output folders.",
    )
    args = parser.parse_args()

    content_dir = SITE_DIR / "content" / args.section

    if not args.source.is_dir():
        print(f"Error: source directory not found: {args.source}", file=sys.stderr)
        sys.exit(1)

    content_dir.mkdir(parents=True, exist_ok=True)

    html_files = sorted(args.source.glob("*.html"))
    if not html_files:
        print(f"No .html files found in {args.source}")
        sys.exit(0)

    print(f"Found {len(html_files)} HTML file(s) in {args.source}\n")

    for html_path in html_files:
        convert_file(html_path, content_dir=content_dir, force=args.force)

    print(f"\nDone. Output in: {content_dir}")


if __name__ == "__main__":
    main()
