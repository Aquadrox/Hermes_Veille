---
name: styled-pdf-report
description: "Use when building a styled PDF report from Markdown."
version: 1.0.0
author: agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [pdf, report, markdown, pandoc, weasyprint, css, charts]
    related_skills: [pdf, grounded-citations]
---

# Styled PDF Report

Turn **enriched Markdown** into a polished, print-ready PDF: cover header, clickable table of contents, key-figure banner, standardised item cards with badges, callouts, comparative tables, timeline, glossary, numbered sources, running headers and page numbers — plus **inline SVG charts**. Pure CLI (pandoc + WeasyPrint): no LaTeX, no Python dependencies.

## When to Use

- Build a report / memo / digest / review that must be delivered as a **designed PDF** (badges, boxes, tables, charts, pagination) rather than plain text.
- Recurring reports whose layout must stay identical week to week.
- NOT for pixel-perfect web pages (use a headless browser) and NOT for editing existing PDFs (use the `pdf` skill).

## Prerequisites

- `pandoc` (>= 3.0) and `weasyprint` on PATH; `poppler-utils` (for `pdftoppm`) only to preview.
- Debian/Ubuntu: `sudo apt-get install -y pandoc weasyprint poppler-utils`
- Fonts: Liberation/DejaVu are enough (no font download needed).

## Files

- `scripts/build_report.py` — the builder (Markdown → styled PDF, injects SVG charts, substitutes CSS tokens).
- `assets/report.css` — the stylesheet, with two tokens: `__HEADER__` (running top-left) and `__FOOTER__` (bottom-left). Override the CSS with `--css`.

## Procedure

1. Write the report body in Markdown using the conventions below. Put `<div class="chart-slot"></div>` where a chart goes.
2. (Optional) Write a charts JSON:
   ```json
   {"charts": [{"title": "…", "source": "…", "bars": [{"label": "2026", "value": 4, "value_label": "4 Md$"}]}]}
   ```
3. Build:
   ```bash
   python3 <skill_dir>/scripts/build_report.py \
     --md body.md --out report.pdf \
     --title "Quarterly review" --subtitle "Q3 2026" \
     --meta "Period : Q3 | Read : ~10 min | Audience : engineering" \
     --header "Quarterly review · Platform" --footer "Internal" \
     --data charts.json
   ```
   Expect `OK: PDF généré -> …`.
4. Preview and check: `pdftoppm -png -r 80 report.pdf /tmp/pg` then inspect `/tmp/pg-N.png`.

## Markup Cheatsheet

`--title/--subtitle/--meta` build the cover header; `--meta` splits on `|`. The body uses pandoc *fenced divs* (`::: {.class}`) and *bracketed spans* (`[text]{.class}`):

```
::: {.keyfigures}
::: {.fig}
**+218 %**

label of the figure (source)
:::
:::
```
```
::: {.keymessages}
### Messages clés
1. **Message** — one sentence.
:::
```
(also `::: {.top3}` and `::: {.actions}`)
```
::: {.card}
### Actor — announcement title
[Production]{.badge .m-production} [High confidence]{.badge .c-high} [Press release]{.badge .s-pr}

**Fait.** 2 lines.

**Pourquoi ça compte.** impact.
:::
```
Badges: maturity `.m-annonce .m-demo .m-pilote .m-production` · confidence `.c-high .c-mid .c-low` · source `.s-prim .s-press .s-analyst .s-pr`.
```
::: {.callout .retain}
…
:::
::: {.callout .vigilance}
…
:::
```
```
::: {.timeline}
::: {.ev}
<span class="date">14–19 Sep</span> <span class="desc">IMTS 2026</span>
:::
:::
```
```
::: {.glossary}
xDT
: *Executable Digital Twin* — definition.
:::
```
```
::: {.sources}
1. <span class="src-num">[1]</span> [Media — « Title », 14/09/2026](https://…) · <span class="badge s-press">Press</span>
:::
```
Tables: standard pipe tables. Page break: `::: {.pagebreak}` or `<div class="pagebreak"></div>`.

## Common Pitfalls

- **Duplicate title.** If you pass `--metadata title=…`, pandoc's template prints a second visible `<h1 class="title">`. The builder sets only `pagetitle` — keep it that way; never add a `title` metadata on top of `--title`.
- **WeasyPrint has no flexbox `gap`.** Lay out columns with `display: inline-block` + `margin-right`, or `display: table-cell` — not `gap`.
- **The chart slot gains whitespace.** Pandoc rewrites `<div class="chart-slot"></div>` to `<div class="chart-slot">\n\n</div>`. Match it with the regex `<div class="chart-slot">\s*</div>`, never by exact string.
- **Double numbering on sources.** With explicit `[n]` markers, set `.sources ol { list-style: none; }` (already in the shipped CSS). Otherwise the `<ol>` marker doubles the `[n]`.
- **Never paste raw URLs** in the body — they break the layout. Use hyperlinked titles in the sources list and `[n]` anchors in the text.
- **`@page` text is not a CSS variable.** Running header/footer are baked into the stylesheet via `__HEADER__` / `__FOOTER__`, which the builder substitutes before calling pandoc. Edit the CSS or pass `--header/--footer`.
- **Charts**: `value_label` is optional; a min bar height of 3px keeps tiny values visible.

## Verification Checklist

- Builder prints `OK: PDF généré -> …` and the file is a valid `PDF document`.
- Render with `pdftoppm` and visually check: cover header present **once**, TOC present, key-figure banner aligned, badges rendered, tables and charts not clipped, running header shows the chapter, page numbers `n / N` present.
- No raw URL or double-numbered source in the annex.
