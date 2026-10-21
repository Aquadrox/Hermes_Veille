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

The Markdown is untrusted (written by an agent that reads the web): nothing it references is
ever fetched. Pandoc embeds no resource and WeasyPrint loads none (not even `data:` URIs), so
`<a rel="attachment" href="file:///…">` cannot leak a file, a `data:` attachment cannot smuggle
one into the emailed PDF, and a remote image cannot call out. Charts are inline SVG.
"""
import argparse
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SLOT_RE = re.compile(r'<div class="chart-slot">\s*</div>')

# Run by the interpreter of the installed `weasyprint` (the only one able to import it).
# WeasyPrint >= 68 takes a URLFetcher; 61-67 a function (Debian 13 ships 62.3).
RENDER_PDF = """
import sys
import weasyprint

try:
    from weasyprint.urls import URLFetcher
    fetcher = URLFetcher(allowed_protocols=())
except ImportError:
    def fetcher(url, *args, **kwargs):
        raise ValueError("ressource refusée : " + url[:80])

doc = weasyprint.HTML(filename=sys.argv[1], url_fetcher=fetcher).render()
# WeasyPrint 61 lit les pièces jointes (rel="attachment") sans passer par url_fetcher : retirées ici.
doc.metadata.attachments = []
for page in doc.pages:
    page.links = [link for link in page.links if link[0] != "attachment"]
doc.write_pdf(sys.argv[2])
"""


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


def css_string(value):
    """Value placed inside a CSS string literal: cannot close the string, nor the <style> block."""
    value = " ".join(str(value).split())
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\3c ")


def weasyprint_python():
    """Interpreter of the `weasyprint` command (its shebang), else the current one."""
    exe = shutil.which("weasyprint")
    if not exe:
        print("ERREUR: weasyprint introuvable", file=sys.stderr)
        raise SystemExit(2)
    try:
        with open(exe, "rb") as fh:
            first = fh.readline().decode("utf-8", "replace").strip()
    except OSError:
        first = ""
    return shlex.split(first[2:]) if first.startswith("#!") else [sys.executable]


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


def build(args, css_path, tmp):
    """Markdown -> HTML (pandoc) -> charts injected -> PDF (WeasyPrint), inside `tmp`."""
    header_path = os.path.join(tmp, "header.html")
    style_path = os.path.join(tmp, "style.html")
    html_path = os.path.join(tmp, "report.html")

    with open(css_path, encoding="utf-8") as f:
        css_text = f.read()
    css_text = css_text.replace("__HEADER__", css_string(args.header)).replace("__FOOTER__", css_string(args.footer))
    # Stylesheet inlined by us: pandoc's --embed-resources would also embed any file the body points to.
    with open(style_path, "w", encoding="utf-8") as f:
        f.write(f"<style>\n{css_text}\n</style>\n")
    with open(header_path, "w", encoding="utf-8") as f:
        f.write(build_header(args.title, args.subtitle, args.meta))

    cmd = [
        "pandoc", args.md,
        "-f", "markdown+fenced_divs+bracketed_spans+pipe_tables+definition_lists",
        "-t", "html5", "-s",
        "--include-in-header", style_path, "--metadata", "document-css=false",
        "--toc", "--toc-depth", str(args.toc_depth),
        "--include-before-body", header_path,
        "--metadata", f"pagetitle={args.title}",
        "-o", html_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("pandoc error:", r.stderr, file=sys.stderr)
        raise SystemExit(1)

    with open(html_path, encoding="utf-8") as f:
        html_doc = f.read()

    charts = []
    if args.data and os.path.exists(args.data):
        with open(args.data, encoding="utf-8") as f:
            charts = json.load(f).get("charts", [])
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

    r2 = subprocess.run([*weasyprint_python(), "-c", RENDER_PDF, html_path, args.out], capture_output=True, text=True)
    if r2.returncode != 0:
        print("weasyprint error:", r2.stderr, file=sys.stderr)
        raise SystemExit(1)


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

    with tempfile.TemporaryDirectory(prefix="report-build-") as tmp:
        build(args, css_path, tmp)

    print(f"OK: PDF généré -> {args.out}")


if __name__ == "__main__":
    main()
