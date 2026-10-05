#!/usr/bin/env python3
"""Build a styled PDF report from enriched Markdown.

Markdown -> styled HTML (pandoc) -> PDF (WeasyPrint), with inline SVG charts.
Standard library only (pandoc + weasyprint are external CLIs).

Usage:
  build_report.py --md body.md --out out.pdf --title "Title" \
      --subtitle "Subtitle" --meta "A | B | C" \
      [--header "running header"] [--footer "running footer"] \
      [--accent "#0f766e"] [--data charts.json] [--css custom.css] [--toc-depth 2]

The body may contain <div class="chart-slot"></div> placeholders; each is replaced, in
order, by a chart described in --data. The stylesheet tokens __HEADER__ / __FOOTER__ are
substituted from --header / --footer.
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SLOT_RE = re.compile(r'<div class="chart-slot">\s*</div>')


def find_css(explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    for cand in (
        os.path.join(HERE, "assets", "report.css"),
        os.path.join(HERE, "..", "assets", "report.css"),
        os.path.join(HERE, "report.css"),
    ):
        if os.path.exists(cand):
            return os.path.abspath(cand)
    return os.path.join(HERE, "assets", "report.css")


def esc(value):
    return html.escape(str(value), quote=True)


def bar_chart_svg(bars, accent="#0f766e", width=660, height=200):
    """Vertical bar chart as inline SVG (no dependency)."""
    pad_l, pad_r, pad_t, pad_b = 8, 8, 24, 32
    n = max(len(bars), 1)
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    maxv = max((abs(float(b.get("value", 0))) for b in bars), default=1.0) or 1.0
    gap = 16
    bar_w = max((plot_w - gap * (n + 1)) / n, 6)
    font = "Liberation Sans,sans-serif"
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}">'
    ]
    out.append(
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width - pad_r}" '
        f'y2="{pad_t + plot_h}" stroke="#cbd5e1"/>'
    )
    for i, b in enumerate(bars):
        v = float(b.get("value", 0))
        h = max((abs(v) / maxv) * plot_h, 3.0)
        x = pad_l + gap * (i + 1) + bar_w * i
        y = pad_t + plot_h - h
        out.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
            f'fill="{accent}" rx="2"/>'
        )
        lbl = b.get("value_label") or f"{v:g}"
        out.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{y - 5:.1f}" text-anchor="middle" '
            f'font-family="{font}" font-size="12" font-weight="700" '
            f'fill="{accent}">{esc(lbl)}</text>'
        )
        out.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{pad_t + plot_h + 15:.1f}" text-anchor="middle" '
            f'font-family="{font}" font-size="11" fill="#475569">{esc(b.get("label", ""))}</text>'
        )
    out.append("</svg>")
    return "".join(out)


def chart_block(chart, accent):
    svg = bar_chart_svg(chart.get("bars", []), accent=accent)
    title = f'<div class="chart-title">{esc(chart["title"])}</div>' if chart.get("title") else ""
    src = f'<div class="src">Source : {esc(chart["source"])}</div>' if chart.get("source") else ""
    return f'<div class="chart">{title}{svg}{src}</div>'


def build_header(title, subtitle, meta):
    metas = "".join(f"<span>{esc(m.strip())}</span>" for m in meta.split("|") if m.strip()) if meta else ""
    sub = f'<div class="subtitle">{esc(subtitle)}</div>' if subtitle else ""
    return f'<div class="doc-header"><h1>{esc(title)}</h1>{sub}<div class="meta">{metas}</div></div>'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--meta", default="")
    ap.add_argument("--header", default="")
    ap.add_argument("--footer", default="")
    ap.add_argument("--accent", default="#0f766e")
    ap.add_argument("--data", default=None)
    ap.add_argument("--css", default=None)
    ap.add_argument("--toc-depth", default="2")
    args = ap.parse_args()

    if not os.path.exists(args.md):
        print(f"ERREUR: introuvable {args.md}", file=sys.stderr)
        sys.exit(2)

    css_path = find_css(args.css)
    if not os.path.exists(css_path):
        print(f"ERREUR: feuille de style introuvable ({css_path})", file=sys.stderr)
        sys.exit(2)

    tmp = tempfile.mkdtemp(prefix="report-build-")
    header_path = os.path.join(tmp, "header.html")
    css_out = os.path.join(tmp, "report.css")
    html_path = os.path.join(tmp, "report.html")

    with open(css_path, encoding="utf-8") as f:
        css_text = f.read()
    css_text = css_text.replace("__HEADER__", args.header).replace("__FOOTER__", args.footer)
    with open(css_out, "w", encoding="utf-8") as f:
        f.write(css_text)
    with open(header_path, "w", encoding="utf-8") as f:
        f.write(build_header(args.title, args.subtitle, args.meta))

    cmd = [
        "pandoc", args.md,
        "-f", "markdown+fenced_divs+bracketed_spans+pipe_tables+definition_lists",
        "-t", "html5", "-s", "--embed-resources",
        "--css", css_out, "--toc", "--toc-depth", str(args.toc_depth),
        "--include-before-body", header_path,
        "--metadata", f"pagetitle={args.title}",
        "-o", html_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("pandoc error:", r.stderr, file=sys.stderr)
        sys.exit(1)

    with open(html_path, encoding="utf-8") as f:
        html_doc = f.read()

    charts = []
    if args.data and os.path.exists(args.data):
        charts = json.load(open(args.data, encoding="utf-8")).get("charts", [])
    n_slots = len(SLOT_RE.findall(html_doc))
    if n_slots < len(charts):
        for c in charts[n_slots:]:
            print(f"avertissement: pas de chart-slot pour '{c.get('title', '')}'", file=sys.stderr)
    state = {"i": 0}

    def _repl(_m):
        if state["i"] < len(charts):
            block = chart_block(charts[state["i"]], args.accent)
            state["i"] += 1
            return block
        return ""

    html_doc = SLOT_RE.sub(_repl, html_doc)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_doc)

    r2 = subprocess.run(["weasyprint", html_path, args.out], capture_output=True, text=True)
    if r2.returncode != 0:
        print("weasyprint error:", r2.stderr, file=sys.stderr)
        sys.exit(1)

    print(f"OK: PDF généré -> {args.out}")


if __name__ == "__main__":
    main()
