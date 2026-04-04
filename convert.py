#!/usr/bin/env python3
"""
convert.py — Convert archived Fred Pohl blog HTML files into Hugo page bundles.

Usage:
    python3 convert.py

Run from the fred-pohl-blog/ directory.  The script reads every .html file in
../blog/ (the archive folder), builds a page bundle under content/posts/, and
copies images from the companion _files/ folder.

Outputs per entry:
  content/posts/<YYYY-MM-DD-slug>/
      index.en.md          — English original with YAML front matter
      index.ru.md          — Russian stub (translated: false)
      <images>             — copied from the _files/ companion folder

Re-running is safe: existing folders are skipped unless --force is passed.
"""

import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
SITE_DIR     = SCRIPT_DIR          # script lives in fred-pohl-blog/
ARCHIVE_DIR  = SCRIPT_DIR.parent / "blog"   # raw HTML files
CONTENT_DIR  = SITE_DIR / "content" / "posts"

# ── Month name → number (for parsing "January 7, 2009") ──────────────────────
MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Produce a URL-safe slug from a title string."""
    text = text.lower()
    text = re.sub(r"[''`]", "", text)        # drop apostrophes
    text = re.sub(r"[^\w\s-]", " ", text)   # non-word → space
    text = re.sub(r"[\s_]+", "-", text)      # spaces → dash
    text = re.sub(r"-{2,}", "-", text)       # collapse multiple dashes
    return text.strip("-")[:80]              # max 80 chars


def parse_date(date_str: str):
    """
    Parse strings like:
      "January 7, 2009 at 8:09 am"
      "January 19, 2009 at 8:25 am"
    Returns a datetime or None.
    """
    m = re.search(
        r"(\w+)\s+(\d{1,2}),\s+(\d{4})",
        date_str,
        re.IGNORECASE,
    )
    if not m:
        return None
    month_name, day, year = m.group(1).lower(), int(m.group(2)), int(m.group(3))
    month = MONTHS_EN.get(month_name)
    if not month:
        return None
    return datetime(year, month, day)


def extract_text_links(elements):
    """Return list of plain text link labels from a list of <a> tags."""
    return [a.get_text(strip=True) for a in elements]


def yaml_list(items: list) -> str:
    if not items:
        return "[]"
    parts = ", ".join(f'"{i}"' for i in items)
    return f"[{parts}]"


def image_markdown(src: str, alt: str, caption: str = "") -> str:
    if caption:
        return f'{{{{< figure src="{src}" alt="{alt}" caption="{caption}" >}}}}'
    return f'![{alt}]({src})'


def html_to_markdown(content_div, slug_dir: Path) -> str:
    """
    Walk the content div and produce Markdown text.
    Images are referenced relative to the page bundle folder.
    wp-caption divs become figure/caption blocks.
    """
    lines = []

    def process_node(node):
        if hasattr(node, "name"):
            tag = node.name

            if tag in ("script", "style"):
                return

            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                level = int(tag[1])
                lines.append("\n" + "#" * level + " " + node.get_text(strip=True) + "\n")

            elif tag == "p":
                # Check if this paragraph is inside a wp-caption (handled below)
                if node.parent and "wp-caption" in (node.parent.get("class") or []):
                    return
                text = node.get_text()
                if text.strip():
                    lines.append("\n" + text.strip() + "\n")
                else:
                    # Paragraph contains only non-text children (e.g. <img> or <a><img></a>)
                    for child in node.children:
                        process_node(child)

            elif tag == "blockquote":
                inner = node.get_text().strip()
                for bline in inner.splitlines():
                    lines.append("> " + bline)
                lines.append("")

            elif tag in ("ul", "ol"):
                for li in node.find_all("li", recursive=False):
                    lines.append("- " + li.get_text(strip=True))
                lines.append("")

            elif tag == "img":
                src = node.get("src", "")
                alt = node.get("alt", "")
                title = node.get("title", "")
                fname = Path(src).name
                # Skip Wayback Machine toolbar / UI artefact images
                if any(skip in fname.lower() for skip in
                       ("wayback", "toolbar", "banner", "header-gg", "_tb_")):
                    return
                lines.append(f"\n![{alt or title}]({fname})\n")

            elif tag == "div" and "wp-caption" in (node.get("class") or []):
                # WordPress image caption block
                img = node.find("img")
                caption_p = node.find("p", class_="wp-caption-text")
                caption_text = caption_p.get_text(strip=True) if caption_p else ""
                if img:
                    src = img.get("src", "")
                    alt = img.get("alt", "") or img.get("title", "")
                    fname = Path(src).name
                    if caption_text:
                        lines.append(f'\n{{{{< figure src="{fname}" alt="{alt}" caption="{caption_text}" >}}}}\n')
                    else:
                        lines.append(f"\n![{alt}]({fname})\n")

            elif tag in ("em", "i"):
                pass  # handled by get_text at parent level

            elif tag in ("br",):
                lines.append("  ")

            elif tag in ("hr",):
                lines.append("\n---\n")

            else:
                if hasattr(node, "children"):
                    for child in node.children:
                        process_node(child)
        # NavigableString — skip; text is extracted at the element level above


    for child in content_div.children:
        process_node(child)

    # Collapse excessive blank lines
    text = "\n".join(lines)
    text = re.sub(r"\n{4,}", "\n\n", text)
    return text.strip()


def parse_html_file(html_path: Path):
    """
    Parse one archived HTML file and return a dict with:
      title, date, categories, tags, archive_url, body_html, image_src_dir
    """
    with open(html_path, encoding="utf-8", errors="replace") as f:
        soup = BeautifulSoup(f, "html.parser")

    result = {}
    # ── Archive URL ────────────────────────────────────────────────────────────────
    # Extracted FIRST, before any decompose() calls, because the most reliable
    # source is the Wayback Calendar sparkline links inside the toolbar — they
    # are empty-text <a href> tags that all point to different snapshots of the
    # CURRENT page.  After the toolbar is stripped those links are gone, and the
    # first remaining archive link would be a navigation link to a different post.
    archive_url = ""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.match(
            r"(https://web\.archive\.org/web/(\d{14})/https?://(?:www\.)?thewaythefutureblogs\.com/\d{4}/\d{2}/[^/#\s]+)",
            href,
        )
        if m and not href.rstrip("/").endswith(("#", "#close", "#expand")):
            archive_url = m.group(1)
            if not archive_url.endswith("/"):
                archive_url += "/"
            break
    result["archive_url"] = archive_url

    # ── Strip Wayback Machine toolbar before any parsing ─────────────────────
    # The toolbar lives in <div id="wm-ipp-base"> and contains no article content.
    # Remove it (and any sibling toolbar divs) so they don’t pollute the output.
    for wm_id in ("wm-ipp-base", "wm-ipp", "wm-toolbar", "wm-ipp-sparkline",
                  "wm-ipp-print", "wm-ipp-inside"):
        el = soup.find(id=wm_id)
        if el:
            el.decompose()
    # Also strip any <script> tags that reference wayback/archive machinery
    for script in soup.find_all("script"):
        src = script.get("src", "")
        if "archive.org" in src or "wayback" in src.lower():
            script.decompose()
    # ── Title ──────────────────────────────────────────────────────────────────
    title_el = soup.find(id="reader-title") or soup.find("title")
    raw_title = title_el.get_text() if title_el else html_path.stem
    # Collapse whitespace then strip "The Way the Future Blogs » Blog Archive »" prefix
    raw_title = re.sub(r"[\s\u00a0]+", " ", raw_title).strip()
    title = re.sub(r"^.*»\s*Blog Archive\s*»\s*", "", raw_title, flags=re.DOTALL).strip()
    if not title:
        title = html_path.stem
    result["title"] = title

    # ── Footer metadata: date, categories, tags ────────────────────────────────
    # WordPress always renders a <p class="post-meta"> with:
    #   rel="category tag" + href containing /category/ → category
    #   rel="tag"          + href containing /tag/      → tag
    #   other links (trackback, RSS, leave-a-response)  → ignored

    # Locate the footer element — try the known WP class first, then scan
    footer_el = soup.find("p", class_="post-meta")
    if not footer_el:
        # Fallback: deepest element that contains the posted-on marker text
        candidates = [el for el in soup.find_all(True)
                      if "This entry was posted on" in el.get_text()]
        footer_el = candidates[-1] if candidates else None

    date_obj = None
    categories = []
    tags = []

    if footer_el:
        date_obj = parse_date(footer_el.get_text())
        for a in footer_el.find_all("a"):
            href = a.get("href", "")
            rels = a.get("rel", [])
            # Use URL path as primary discriminator; rel as secondary
            if "/category/" in href or ("category" in rels and "tag" in rels):
                categories.append(a.get_text(strip=True))
            elif "/tag/" in href or (rels == ["tag"]):
                tags.append(a.get_text(strip=True))

    # Fallback date: try filename prefix (format: 2009_01 NNN Title)
    if not date_obj:
        m = re.match(r"(\d{4})_(\d{2})", html_path.name)
        if m:
            date_obj = datetime(int(m.group(1)), int(m.group(2)), 1)

    result["date"] = date_obj or datetime(2009, 1, 1)
    result["categories"] = categories
    result["tags"] = tags

    # ── Body content ───────────────────────────────────────────────────────────
    # Reader-mode wraps text in #readability-page-1; also strip the WP post-meta
    # footer ("This entry was posted…") from the body — it belongs in front matter.
    body_div = soup.find(id="readability-page-1")
    if not body_div:
        body_div = soup.find("div", class_="moz-reader-content")
    if not body_div:
        body_div = soup.find("body")
    if body_div:
        for pm in body_div.find_all("p", class_="post-meta"):
            pm.decompose()
        for el in body_div.find_all(True, id=re.compile(r"^wm-")):
            el.decompose()
        # Remove sidebar
        sidebar = body_div.find(id="sidebar")
        if sidebar:
            sidebar.decompose()
        # Remove "Leave a Reply" form and everything after it
        respond = body_div.find(id="respond")
        if respond:
            for sibling in list(respond.find_next_siblings()):
                sibling.decompose()
            respond.decompose()
    result["body_div"] = body_div

    # ── Companion _files/ directory ────────────────────────────────────────────
    stem = html_path.stem
    files_dir = html_path.parent / (stem + "_files")
    result["files_dir"] = files_dir if files_dir.is_dir() else None

    return result


def build_front_matter(title: str, date: datetime, categories: list,
                        tags: list, archive_url: str, translated: bool,
                        lang: str) -> str:
    lines = ["---"]
    lines.append(f'title: "{title}"')
    lines.append(f'date: {date.strftime("%Y-%m-%d")}')
    lines.append(f'categories: {yaml_list(categories)}')
    lines.append(f'tags: {yaml_list(tags)}')
    if archive_url:
        lines.append(f'archive_url: "{archive_url}"')
    lines.append(f'translated: {"true" if translated else "false"}')
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def convert_file(html_path: Path, force: bool = False):
    print(f"  Processing: {html_path.name}")

    data = parse_html_file(html_path)

    date      = data["date"]
    title     = data["title"]
    slug      = slugify(title)
    folder    = f"{date.strftime('%Y-%m-%d')}-{slug}"
    dest_dir  = CONTENT_DIR / folder

    if dest_dir.exists() and not force:
        print(f"    ↳ Skipping (already exists): {folder}")
        return

    dest_dir.mkdir(parents=True, exist_ok=True)

    # ── Copy images ────────────────────────────────────────────────────────────
    if data["files_dir"]:
        for img_file in data["files_dir"].iterdir():
            if img_file.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"):
                # Skip Wayback Machine toolbar artefacts
                if any(skip in img_file.name.lower() for skip in
                       ("wayback", "toolbar", "banner", "header-gg", "_tb_")):
                    continue
                shutil.copy2(img_file, dest_dir / img_file.name)

    # ── Produce Markdown body ──────────────────────────────────────────────────
    body_md = ""
    if data["body_div"]:
        body_md = html_to_markdown(data["body_div"], dest_dir)

    # ── Write index.en.md ─────────────────────────────────────────────────────
    fm_en = build_front_matter(
        title       = title,
        date        = date,
        categories  = data["categories"],
        tags        = data["tags"],
        archive_url = data["archive_url"],
        translated  = False,
        lang        = "en",
    )
    en_path = dest_dir / "index.en.md"
    en_path.write_text(fm_en + body_md, encoding="utf-8")

    # ── Write index.ru.md (stub) ───────────────────────────────────────────────
    ru_path = dest_dir / "index.ru.md"
    if not ru_path.exists() or force:
        fm_ru = build_front_matter(
            title       = title,   # placeholder — translator will fill in
            date        = date,
            categories  = data["categories"],
            tags        = data["tags"],
            archive_url = data["archive_url"],
            translated  = False,
            lang        = "ru",
        )
        stub_body = (
            "<!-- ПЕРЕВОД -->\n"
            "<!-- Замените эту строку переводом статьи. -->\n"
            "<!-- Когда перевод готов, установите translated: true в front matter выше. -->\n"
        )
        ru_path.write_text(fm_ru + stub_body, encoding="utf-8")

    print(f"    ↳ Created: {folder}/")


def main():
    parser = argparse.ArgumentParser(description="Convert archived HTML to Hugo page bundles.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output folders.")
    args = parser.parse_args()

    if not ARCHIVE_DIR.is_dir():
        print(f"Error: archive directory not found: {ARCHIVE_DIR}", file=sys.stderr)
        sys.exit(1)

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)

    html_files = sorted(ARCHIVE_DIR.glob("*.html"))
    if not html_files:
        print(f"No .html files found in {ARCHIVE_DIR}")
        sys.exit(0)

    print(f"Found {len(html_files)} HTML file(s) in {ARCHIVE_DIR}\n")
    for html_path in html_files:
        convert_file(html_path, force=args.force)

    print(f"\nDone. Output in: {CONTENT_DIR}")


if __name__ == "__main__":
    main()
