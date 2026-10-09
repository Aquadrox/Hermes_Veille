import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "skills"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


brevo = load("brevo_send", SKILLS / "email/brevo-email/scripts/brevo_send.py")
builder = load("build_report", SKILLS / "productivity/styled-pdf-report/scripts/build_report.py")


def args(**kw):
    base = dict(to=None, to_file=None, bcc=None, bcc_file=None, subject="S", text="T", html=None,
                body_file=None, cc=None, reply_to=None, attach=[])
    base.update(kw)
    return argparse.Namespace(**base)


def test_html_file_body(tmp_path):
    f = tmp_path / "d.html"
    f.write_text("<h1>" + "x" * 200_000 + "</h1>")  # au-delà de la limite d'un argument (128 Ko)
    payload, _ = brevo.build_payload(args(to="a@x.fr", text="fallback", html_file=str(f)), "me@x.fr", None)
    assert payload["htmlContent"].startswith("<h1>") and payload["textContent"] == "fallback"
    with pytest.raises(ValueError, match="--html-file"):
        brevo.build_payload(args(to="a@x.fr", text=None), "me@x.fr", None)


def test_to_file_reads_all_lines(tmp_path):
    f = tmp_path / "r.txt"
    f.write_text("a@x.fr\n\n# commentaire\nb@y.fr\nA@x.fr\n")
    payload, real = brevo.build_payload(args(to_file=str(f)), "me@x.fr", None)
    assert [t["email"] for t in payload["to"]] == ["a@x.fr", "b@y.fr"]
    assert "bcc" not in payload


def test_bcc_only_uses_sender_as_to(tmp_path):
    f = tmp_path / "r.txt"
    f.write_text("a@x.fr\nb@y.fr\n")
    payload, real = brevo.build_payload(args(bcc_file=str(f)), "me@x.fr", "Moi")
    assert payload["to"] == [{"email": "me@x.fr"}]
    assert [b["email"] for b in payload["bcc"]] == ["a@x.fr", "b@y.fr"]
    assert real == ["a@x.fr", "b@y.fr"]  # --verify suit un vrai destinataire
    assert payload["sender"] == {"name": "Moi", "email": "me@x.fr"}


def test_no_recipient_is_an_error():
    with pytest.raises(ValueError):
        brevo.build_payload(args(), "me@x.fr", None)


def test_main_sends_and_reads_env_file(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("BREVO_API_KEY='k'\nBREVO_SENDER_EMAIL=me@x.fr\n")
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.delenv("BREVO_SENDER_EMAIL", raising=False)
    calls = []
    monkeypatch.setattr(brevo, "_api", lambda m, p, k, payload=None: calls.append((m, p, k, payload)) or {"messageId": "<1>"})
    att = tmp_path / "r.pdf"
    att.write_bytes(b"%PDF")
    assert brevo.main(["--bcc", "a@x.fr", "--subject", "S", "--text", "T", "--attach", str(att),
                       "--env-file", str(env)]) == 0
    method, path, key, payload = calls[0]
    assert (method, path, key) == ("POST", "/smtp/email", "k")
    assert payload["attachment"][0]["name"] == "r.pdf"
    assert "(+1 bcc)" in capsys.readouterr().out


def test_bar_chart_svg_escapes_labels():
    svg = builder.bar_chart_svg([{"label": "<b>", "value": 3}, {"label": "B", "value": 0}])
    assert "&lt;b&gt;" in svg and svg.count("<rect") == 2


@pytest.mark.skipif(not (shutil.which("pandoc") and shutil.which("weasyprint")),
                    reason="pandoc/weasyprint absents (couvert par le smoke test de l'image)")
def test_build_pdf(tmp_path):
    md = tmp_path / "r.md"
    md.write_text("## Titre\n\n<div class=\"chart-slot\"></div>\n")
    data = tmp_path / "c.json"
    data.write_text(json.dumps({"charts": [{"title": "T", "bars": [{"label": "A", "value": 1}]}]}))
    out = tmp_path / "r.pdf"
    subprocess.run([sys.executable, str(SKILLS / "productivity/styled-pdf-report/scripts/build_report.py"),
                    "--md", str(md), "--out", str(out), "--title", "X", "--data", str(data)], check=True)
    assert out.read_bytes().startswith(b"%PDF")
