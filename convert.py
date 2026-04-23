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


def _sc(value: str) -> str:
    """Escape a value for use inside a Hugo shortcode quoted attribute."""
    return value.replace('"', '&quot;')


def image_markdown(src: str, alt: str, caption: str = "") -> str:
    if caption:
        return f'{{{{< figure src="{_sc(src)}" alt="{_sc(alt)}" caption="{_sc(caption)}" >}}}}'
    return f'![{alt}]({src})'


def html_to_markdown(content_div, slug_dir: Path) -> str:
    """
    Walk the content div and produce Markdown text.
    Images are referenced relative to the page bundle folder.
    wp-caption divs become figure/caption blocks.
    """
    lines = []

    def inline_text(node) -> str:
        """Render inline content of a node to a Markdown string, preserving bold/italic.

        Italic runs are merged: consecutive <i>/<em> tags, bridge NavigableStrings
        between them, and embedded <a> links are collapsed into a single *...* span
        to avoid broken CommonMark emphasis from adjacent *A* plaintext *B* patterns.
        """
        parts = []
        children = list(node.children)
        i = 0
        while i < len(children):
            child = children[i]
            name = getattr(child, "name", None)

            if name in ("i", "em"):
                # Start an italic group. Greedily consume:
                #   - consecutive <i>/<em> tags
                #   - NavigableStrings that bridge two italic tags
                #   - <a> links (rendered as **bold** inline)
                buf = child.get_text()
                j = i + 1
                while j < len(children):
                    nc = children[j]
                    nn = getattr(nc, "name", None)
                    if nn in ("i", "em"):
                        buf += nc.get_text()
                        j += 1
                    elif nn is None:
                        # Include a bridge NavigableString only when the next
                        # sibling is another italic tag (keeps the span unified).
                        if (j + 1 < len(children) and
                                getattr(children[j + 1], "name", None) in ("i", "em")):
                            buf += str(nc)
                            j += 1
                        else:
                            break
                    elif nn == "a":
                        # Embed link as **bold** inside the italic span
                        lt = nc.get_text().strip()
                        if lt:
                            buf += f" **{lt}** "
                        j += 1
                    else:
                        break
                stripped = buf.strip()
                if stripped:
                    lead = buf[: len(buf) - len(buf.lstrip())]
                    trail = buf[len(buf.rstrip()) :]
                    parts.append(f"{lead}*{stripped}*{trail}")
                i = j
                continue

            elif name is None:
                parts.append(str(child))

            elif name in ("b", "strong"):
                inner = child.get_text()
                stripped = inner.strip()
                if stripped:
                    lead = inner[: len(inner) - len(inner.lstrip())]
                    trail = inner[len(inner.rstrip()) :]
                    parts.append(f"{lead}**{stripped}**{trail}")

            elif name == "br":
                parts.append("  \n")

            elif name == "p":
                # <li><p>...</p></li> — recurse into the paragraph
                parts.append(inline_text(child))

            elif name == "a":
                text = child.get_text()
                if text.strip():
                    parts.append(f"**{text.strip()}**")
                else:
                    parts.append(text)

            else:
                parts.append(child.get_text())

            i += 1

        return "".join(parts)

    def process_node(node):
        if not hasattr(node, "name"):
            # NavigableString — emit plain text when inside a recursed context
            text = str(node).strip()
            if text:
                lines.append(text)
            return
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
                # Only treat as image-containing if there's a real (non-pixel) image.
                # Tracking pixels are 1×1; skip them by checking width/height attrs.
                real_imgs = [
                    img for img in node.find_all("img")
                    if not (img.get("width") == "1" and img.get("height") == "1")
                    and Path(img.get("src", "")).name not in ("ir",)
                ]
                if real_imgs or node.find("object"):
                    lines.append("")
                    for child in node.children:
                        process_node(child)
                    lines.append("")
                elif node.find(["b", "strong", "em", "i"]):
                    # Paragraph with inline markup — use inline_text to preserve bold/italic
                    text = inline_text(node).strip()
                    if text:
                        lines.append("\n" + text + "\n")
                else:
                    text = node.get_text()
                    if text.strip():
                        lines.append("\n" + text.strip() + "\n")

            elif tag == "blockquote":
                # Detect poem blockquotes: contain <br> tags (verse line breaks).
                # Title is the first <strong> or <b> child; stanzas are <p> with <br>,
                # or (fallback) direct NavigableString/inline children separated by <br>.
                if node.find("br"):
                    # Extract title
                    title_el = node.find(["strong", "b"])
                    title = title_el.get_text(strip=True) if title_el else ""
                    # Collect stanzas from <p> children (standard structure)
                    stanzas = []
                    for p in node.find_all("p"):
                        stanza_lines = []
                        for child in p.children:
                            name = getattr(child, "name", None)
                            if name == "br":
                                continue  # line separator
                            elif name:
                                line = child.get_text().strip()  # inline tag
                            else:
                                line = str(child).strip()  # NavigableString
                            if line:
                                stanza_lines.append(line)
                        if stanza_lines:
                            stanzas.append("\n".join(stanza_lines))
                    # Fallback: lines are direct blockquote children (NavigableStrings
                    # separated by <br>), not wrapped in <p> tags.
                    if not stanzas:
                        stanza_lines = []
                        for child in node.children:
                            name = getattr(child, "name", None)
                            if name == "br":
                                continue
                            elif name in (None,):  # NavigableString
                                line = str(child).strip()
                                if line:
                                    stanza_lines.append(line)
                            elif name == "p":
                                pass  # skip empty trailing <p></p>
                            elif name:
                                line = child.get_text().strip()
                                if line:
                                    stanza_lines.append(line)
                        if stanza_lines:
                            stanzas.append("\n".join(stanza_lines))
                    poem_body = "\n\n".join(stanzas)
                    title_attr = f' title="{title}"' if title else ""
                    print(f"      ♦ Poem found: ({title or '(untitled)'})")
                    lines.append(f'\n{{{{< poem{title_attr} >}}}}\n{poem_body}\n{{{{< /poem >}}}}\n')
                else:
                    # Regular prose blockquote — recurse so images are preserved.
                    start = len(lines)
                    for child in node.children:
                        process_node(child)
                    end = len(lines)
                    for i in range(start, end):
                        if lines[i].strip():
                            lines[i] = "> " + lines[i].lstrip()
                    lines.append("")

            elif tag in ("ul", "ol"):
                for li in node.find_all("li", recursive=False):
                    if li.find(["b", "strong", "em", "i"]):
                        lines.append("- " + inline_text(li).strip())
                    else:
                        lines.append("- " + li.get_text(strip=True))
                lines.append("")

            elif tag == "object":
                # YouTube embed: <object><param name="movie" value="...youtube.../v/VIDEO_ID&...">
                movie_param = node.find("param", attrs={"name": "movie"})
                if movie_param:
                    val = movie_param.get("value", "")
                    m = re.search(r"youtube(?:-nocookie)?\.com/v/([A-Za-z0-9_-]+)", val)
                    if m:
                        print(f"      ▶ YouTube found: {m.group(1)}")
                        lines.append(f'\n{{{{< youtube {m.group(1)} >}}}}\n')
                        return
                # Not a YouTube object — recurse normally
                for child in node.children:
                    process_node(child)

            elif tag == "embed":
                return  # always inside <object>, handled above

            elif tag == "img":
                src = node.get("src", "")
                alt = node.get("alt", "")
                title = node.get("title", "")
                fname = Path(src).name
                # Skip tiny tracking pixels: 1×1 images with no dimension attributes
                # are detected by width/height attrs. Also skip known non-image names.
                w = node.get("width", "")
                h = node.get("height", "")
                if w == "1" and h == "1":
                    return
                # Skip files with no extension that also look like tracking pixels
                # (e.g. bare 'ir' files from Amazon)
                if not Path(fname).suffix and fname in ("ir",):
                    return
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
                        lines.append(f'\n{{{{< figure src="{_sc(fname)}" alt="{_sc(alt)}" caption="{_sc(caption_text)}" >}}}}\n')
                    else:
                        lines.append(f"\n![{alt}]({fname})\n")

            elif tag == "div":
                # Detect centred poem div: has an inline style with "width:" (e.g.
                # "width:20em") AND contains a <p> with ≥3 <br> verse-line breaks.
                # The width requirement distinguishes small poem containers from
                # general content wrappers like <div class="post-entry">.
                div_style = node.get("style", "").replace(" ", "")
                has_width = "width:" in div_style
                verse_p = (
                    next(
                        (p for p in node.find_all("p", recursive=False)
                         if len(p.find_all("br")) >= 3),
                        None,
                    )
                    if has_width else None
                )
                if verse_p:
                    # Extract title from <strong>/<b> or a bold-styled <span>
                    title_el = node.find(["strong", "b"])
                    if not title_el:
                        for span in node.find_all("span", recursive=False):
                            style = span.get("style", "").replace(" ", "")
                            if "font-weight:bold" in style:
                                title_el = span
                                break
                    title = title_el.get_text(strip=True) if title_el else ""
                    # Collect verse lines (br = line separator)
                    stanza_lines = []
                    for child in verse_p.children:
                        name = getattr(child, "name", None)
                        if name == "br":
                            continue
                        line = child.get_text().strip() if name else str(child).strip()
                        if line:
                            stanza_lines.append(line)
                    poem_body = "\n".join(stanza_lines)
                    # Append attribution line (—Author) from last <p><em> child
                    for p in node.find_all("p", recursive=False):
                        em = p.find("em")
                        if em:
                            attr_text = em.get_text().strip()
                            if attr_text.startswith("—") or attr_text.startswith("--"):
                                poem_body += f"\n\n{attr_text}"
                                break
                    title_attr = f' title="{title}"' if title else ""
                    print(f"      ♦ Poem found: ({title or '(untitled)'})")
                    lines.append(f'\n{{{{< poem{title_attr} >}}}}\n{poem_body}\n{{{{< /poem >}}}}\n')
                else:
                    # Regular div — recurse into children
                    for child in node.children:
                        process_node(child)

            elif tag in ("strong", "b"):
                # Bold — wrap in ** when encountered during recursion
                inner = node.get_text()
                stripped = inner.strip()
                if stripped:
                    lead = inner[: len(inner) - len(inner.lstrip())]
                    trail = inner[len(inner.rstrip()) :]
                    lines.append(f"{lead}**{stripped}**{trail}")

            elif tag in ("em", "i"):
                # Italic — wrap in * when encountered during recursion
                inner = node.get_text()
                stripped = inner.strip()
                if stripped:
                    lead = inner[: len(inner) - len(inner.lstrip())]
                    trail = inner[len(inner.rstrip()) :]
                    lines.append(f"{lead}*{stripped}*{trail}")

            elif tag in ("br",):
                lines.append("  ")

            elif tag in ("hr",):
                lines.append("\n---\n")

            elif tag == "a":
                text = node.get_text()
                if text.strip():
                    lines.append(f"**{text.strip()}**")
                else:
                    if hasattr(node, "children"):
                        for child in node.children:
                            process_node(child)

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
    # 1. Standard WordPress entry-title heading (most posts)
    title_el = (soup.find("h1", class_="entry-title") or
                soup.find("h2", class_="entry-title"))
    # 2. First plain <h2> not marked as a widget title (older theme pages)
    if not title_el:
        for h2 in soup.find_all("h2"):
            cls = h2.get("class") or []
            if "widgettitle" not in cls and "widget-title" not in cls:
                title_el = h2
                break
    if title_el:
        title = re.sub(r"[\s\u00a0]+", " ", title_el.get_text()).strip()
    else:
        # 3. Fall back to id="reader-title" or page <title>
        fb_el = soup.find(id="reader-title") or soup.find("title")
        raw_title = fb_el.get_text() if fb_el else html_path.stem
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


def convert_file(html_path: Path, force: bool = False, used_folders: set = None):
    print(f"  Processing: {html_path.name}")

    data = parse_html_file(html_path)

    date      = data["date"]
    title     = data["title"]
    slug      = slugify(title)
    folder    = f"{date.strftime('%Y-%m-%d')}-{slug}"

    # Disambiguate when two different source files produce the same folder name.
    # Use the file's sequential number from the filename as a suffix.
    if used_folders is not None and folder in used_folders:
        num_m = re.match(r'^(\d+)', html_path.stem)
        suffix = num_m.group(1) if num_m else html_path.stem
        folder = f"{folder}-{suffix}"
        print(f"    ↳ Slug collision — renamed to: {folder}")
    if used_folders is not None:
        used_folders.add(folder)

    dest_dir  = CONTENT_DIR / folder

    if dest_dir.exists() and not force:
        print(f"    ↳ Skipping (already exists): {folder}")
        return

    dest_dir.mkdir(parents=True, exist_ok=True)

    # ── Copy images ────────────────────────────────────────────────────────────
    if data["files_dir"] and data["body_div"]:
        # Collect every filename referenced in an <img> tag so we copy exactly
        # what is used — including extension-less files like "5216748" (JPEG).
        used = {
            Path(img.get("src", "")).name
            for img in data["body_div"].find_all("img")
            if img.get("src", "")
        }
        for img_file in data["files_dir"].iterdir():
            if img_file.name not in used:
                continue
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
    used_folders: set = set()
    for html_path in html_files:
        convert_file(html_path, force=args.force, used_folders=used_folders)

    print(f"\nDone. Output in: {CONTENT_DIR}")


if __name__ == "__main__":
    main()
