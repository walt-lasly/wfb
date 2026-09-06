# AGENTS.md

## Project overview

This repository builds a Hugo static site that aggregates several archive collections, not just one blog. The largest and central section is [content/fred-pohl](content/fred-pohl), but the site also includes additional archive sections such as [content/efanzines](content/efanzines), [content/fanac](content/fanac), [content/heinleinarchive](content/heinleinarchive), [content/n3f](content/n3f), [content/pipermail](content/pipermail), and [content/vice](content/vice).

The content is organized as page bundles under `content/`; each article lives in its own folder such as `content/fred-pohl/2009-01-05-sir-arthur-and-i/` and typically contains `index.en.md` and `index.ru.md`.

Primary site config: [hugo.yaml](hugo.yaml)
Main content sections: [content/fred-pohl](content/fred-pohl), [content/efanzines](content/efanzines), [content/fanac](content/fanac), [content/heinleinarchive](content/heinleinarchive), [content/n3f](content/n3f), [content/pipermail](content/pipermail), [content/vice](content/vice)
Publishing workflow: [.github/workflows/hugo.yml](.github/workflows/hugo.yml)

## Working conventions

- Prefer editing the source article in its page bundle directory; do not create ad hoc markdown files elsewhere unless the task specifically requires it.
- The canonical article file is usually `index.en.md`. Russian versions live alongside it as `index.ru.md`.
- Generated translation files such as `_ru.deepl.md`, `_ru.gemini.md`, and `_ru.yandex.md` are intermediate artifacts; they should not be treated as the main source of truth unless the workflow explicitly calls for them.
- Keep Hugo front matter consistent with existing posts. Typical keys include `title`, `date`, `categories`, `tags`, `next_post_url`, `next_post_title`, `prev_post_url`, `prev_post_title`, and `translated`.
- Internal links generally use site-relative paths like `/fred-pohl/...`, `/efanzines/...`, `/fanac/...`, `/heinleinarchive/...`, `/n3f/...`, `/pipermail/...`, or `/vice/...`.
- This repository is a multi-section archive: when changing links or navigation, check whether the target belongs to a different archive section, not only the Fred Pohl section.
- Preserve Hugo shortcodes and Markdown link syntax exactly when editing article text; many translation scripts intentionally protect shortcodes and URLs from machine translation.

## Repository-specific tools

- `python3 convert.py` converts archived HTML into Hugo page bundles for the main Fred Pohl archive.
- `python3 convert_efanzines.py` does the same for efanzines content.
- `python3 check_links.py` validates internal links and flags wrong `/posts/` prefixes and missing targets.
- `python3 find_dead_links.py` exists for additional link-health checks and should be used when working on URL integrity.
- `python3 translate_common.py` and the `translate_*.py` scripts are for machine translation workflows; they protect shortcode/URL tokens before translation and are not the place to add unrelated content rules.

## Build and validation

- Local site build: `hugo` or `hugo --minify`
- Local preview: `hugo server` from the repo root
- Link sanity check: `python3 check_links.py`
- Do not treat generated output under `public/` or `resources/_gen/` as source files.
- If a change affects article slugs, URLs, or navigation metadata, update related internal links and verify them with the link-check scripts.

## Content patterns

- The timeline is date-based; article directories use `YYYY-MM-DD-slug` naming.
- Long-running narrative entries are often split across a series with `prev_post_url`/`next_post_url` links; keep those links accurate when moving/editing content.
- Most articles are historical/archival blog posts with citations, figures, and references, so be careful preserving original names, titles, and archived URLs.

## When making changes

- Prefer the smallest change that preserves archive fidelity.
- Keep existing metadata style and title casing consistent with nearby posts.
- If adding a new post, follow the same page-bundle layout and date/slug conventions used elsewhere in the section.
- When modifying cross-links, check both the target slug and any `prev_post_url`/`next_post_url` metadata.

## Useful references

- [hugo.yaml](hugo.yaml)
- [check_links.py](check_links.py)
- [convert.py](convert.py)
- [convert_efanzines.py](convert_efanzines.py)
- [.github/workflows/hugo.yml](.github/workflows/hugo.yml)
