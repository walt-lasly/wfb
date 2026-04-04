from bs4 import BeautifulSoup
from pathlib import Path
import re

archive = Path("/home/styx/Документы/books/Фред Пол/blog")

for fpath in sorted(archive.glob("*.html")):
    html = fpath.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    print(f"\n{'='*60}")
    print(f"FILE: {fpath.name}")

    # Canonical link
    canonical = soup.find("link", rel="canonical")
    print(f"  canonical: {canonical['href'] if canonical else 'NONE'}")

    # og:url
    og = soup.find("meta", property="og:url")
    print(f"  og:url: {og['content'] if og else 'NONE'}")

    rt = soup.find(id="reader-title")
    rt_text = rt.get_text() if rt else "NONE"
    print(f"  reader-title: {rt_text!r}")

    # <title>
    t = soup.find("title")
    t_text = t.get_text() if t else "NONE"
    print(f"  <title>: {t_text!r}")

    # All hrefs with thewaythefutureblogs.com (not images)
    post_urls = []
    for a in soup.find_all("a", href=re.compile(r"thewaythefutureblogs\.com/\d{4}/\d{2}/")):
        post_urls.append(a["href"])
    print(f"  post hrefs ({len(post_urls)}): {post_urls[:3]}")
