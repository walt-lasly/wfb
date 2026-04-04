"""Restructure convert.py: move footer metadata extraction before body decompose."""
from pathlib import Path

src = Path("/home/styx/Документы/books/Фред Пол/fred-pohl-blog/convert.py")
text = src.read_text(encoding="utf-8")

# The two blocks to swap are clearly delimited by their first comment lines.
BODY_START = '    # ── Body content ──'
FOOTER_START = '    # ── Footer metadata: date, categories, tags ──'
URL_START = '    # ── Archive URL ──'

i_body   = text.index(BODY_START)
i_footer = text.index(FOOTER_START)
i_url    = text.index(URL_START)

before  = text[:i_body]          # everything before body block
body_block   = text[i_body:i_footer]    # body block (currently first)
footer_block = text[i_footer:i_url]     # footer block (currently second)
after   = text[i_url:]           # archive URL onwards

# Swap: footer first, body second — but also patch the body block comments
body_block_new = body_block.replace(
    "# ── Body content ──",
    "# ── Body content ──",
).replace(
    "# Reader-mode wraps text in #readability-page-1; also strip the WP post-meta\n    # footer (\u201cThis entry was posted\u2026\u201d) from the body \u2014 it belongs in front matter.",
    "# Strip the WP post-meta footer AFTER reading it above (decompose is in-place).",
).replace(
    "# Remove the post-meta footer from the body so it doesn\u2019t appear in the text\n    if body_div:\n        for pm in body_div.find_all(\"p\", class_=\"post-meta\"):\n            pm.decompose()\n        # Also strip any leftover Wayback comment/navigation links\n        for el in body_div.find_all([\"div\", \"p\", \"ul\"], id=re.compile(r\"wm-\")):\n            el.decompose()",
    "if body_div:\n        for pm in body_div.find_all(\"p\", class_=\"post-meta\"):\n            pm.decompose()\n        for el in body_div.find_all(True, id=re.compile(r\"^wm-\")):\n            el.decompose()",
)

footer_block_new = footer_block.replace(
    "# 1. Locate the footer element \u2014 try the known WP class first, then scan\n    footer_el",
    "# Locate the footer element \u2014 try the known WP class first, then scan\n    footer_el",
).replace(
    "            # else: trackback / leave-a-response / RSS \u2192 skip\n",
    "",
)

result = before + footer_block_new + body_block_new + after
src.write_text(result, encoding="utf-8")
print("Done. Swapped footer metadata extraction before body decompose.")
print(f"Byte count: {len(result)}")
