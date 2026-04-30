#!/usr/bin/env python3
"""Scan all archive HTML files for internal blog links that don't resolve in the link map."""
import re, sys
from pathlib import Path
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from convert import extract_blog_path, parse_html_file, slugify

ARCHIVE_DIR = Path(__file__).parent.parent / "blog"
html_files = sorted(ARCHIVE_DIR.glob("*.html"))

# Build link_map (same logic as main())
link_map = {}
for html_path in html_files:
    try:
        data = parse_html_file(html_path)
        url_key = extract_blog_path(data["archive_url"])
        if url_key:
            date = data["date"]
            folder = f"{date.strftime('%Y-%m-%d')}-{slugify(data['title'])}"
            link_map[url_key] = f"/posts/{folder}/"
            alias_m = re.match(r"^(.*)-(\d+)$", url_key)
            if alias_m:
                clean_key = alias_m.group(1)
                if clean_key not in link_map:
                    link_map[clean_key] = f"/posts/{folder}/"
    except Exception:
        pass

print(f"Link map: {len(link_map)} keys\n")

# Scan every file for dead internal links
dead = []
for html_path in html_files:
    try:
        with open(html_path, encoding="utf-8", errors="replace") as f:
            soup = BeautifulSoup(f, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            key = extract_blog_path(href)
            if key and key not in link_map:
                text = a.get_text(strip=True)[:80]
                dead.append((html_path.name, key, text))
    except Exception:
        pass

if not dead:
    print("No dead internal links found.")
else:
    print(f"Dead internal links ({len(dead)}):\n")
    for fname, key, text in sorted(dead, key=lambda x: x[1]):
        print(f"  slug: {key}")
        print(f"  file: {fname}")
        print(f"  text: {text!r}")
        print()
