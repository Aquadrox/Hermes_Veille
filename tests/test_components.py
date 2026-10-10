"""Veille composants : fonctions pures (analyse, validation, fusion, rendu) puis service, CLI,
interface et MCP. Le réseau (Digi-Key), le PDF et l'envoi d'e-mail sont simulés."""
import json
import re
import shutil
import textwrap
from pathlib import Path

import pytest

from veille_core import cli, render, service, store
from veille_core import components as comp

REPO = Path(__file__).resolve().parent.parent
BOM = "MPN\tFabricant\tRéf\nLM317T\tTexas Instruments\tR-001\nSTM32F103C8T6\tSTMicroelectronics\tU-12\nNE555P\tTexas Instruments\t\n"


def cand(mpns, ntype="EOL", url="https://www.ti.com/eol/9", **kw):
    return {"mpns": mpns, "type": ntype, "title": kw.pop("title", f"Avis {ntype}"),
            "sources": [{"url": url, "label": "TI"}], **kw}


# --------------------------------------------------------------------------
# Fonctions pures
# --------------------------------------------------------------------------
def test_parse_components_formats_header_comments_and_duplicates():
    text = "MPN;Fabricant;Réf\nLM317T ; Texas Instruments ; R-1\n# commentaire\n\nlm317t;texas instruments\nNE555P,TI,\n"
    out = comp.parse_components(text)
    assert [c["mpn"] for c in out] == ["LM317T", "NE555P"]
    assert out[0] == {"mpn": "LM317T", "manufacturer": "Texas Instruments", "ref": "R-1"}
    assert len(comp.parse_components(BOM)) == 3  # collage tableur (tabulations)


@pytest.mark.parametrize("line", ["LM317T", "LM317T;", "<script>;TI", "LM 317T\u0000;TI", "x" * 70 + ";TI"])
def test_parse_components_rejects_bad_lines(line):
    with pytest.raises(comp.ComponentsError, match="ligne 1"):
        comp.parse_components(line)


def test_parse_components_limit(monkeypatch):
    monkeypatch.setattr(comp, "MAX_COMPONENTS", 2)
    with pytest.raises(comp.ComponentsError, match="trop de composants"):
        comp.parse_components("A1;X\nA2;X\nA3;X")


def test_replace_components_keeps_state_of_kept_keys():
    old = comp.replace_components([], comp.parse_components("A1;X;r\nB2;Y"))
    old[0]["web_checked_at"] = "2026-01-01"
    new = comp.replace_components(old, comp.parse_components("a1;x;nouvelle-ref\nC3;Z"))
    assert new[0]["web_checked_at"] == "2026-01-01" and new[0]["ref"] == "nouvelle-ref"
    assert new[1]["web_checked_at"] is None and len(new) == 2
    assert comp.parse_components(comp.format_components(new)) == [
        {"mpn": "a1", "manufacturer": "x", "ref": "nouvelle-ref"}, {"mpn": "C3", "manufacturer": "Z", "ref": ""}]


@pytest.mark.parametrize("url,expected", [
    ("https://www.TI.com/eol/9/?utm_source=x&id=3#top", "https://www.ti.com/eol/9?id=3"),
    ("http://example.org", "http://example.org"),
    ("javascript:alert(1)", None),
    ("data:text/html,<b>", None),
    ("https://user:pw@example.org/x", None),
    ("https://example.org/a b", None),
    ("ftp://example.org/x", None),
    ("https://", None),
    (42, None),
])
def test_normalize_url(url, expected):
    assert comp.normalize_url(url) == expected


def test_validate_candidates_schema_and_matching():
    comps = comp.replace_components([], comp.parse_components("LM317T;Texas Instruments\nBAV99;Nexperia\nBAV99;onsemi"))
    raw = {"notices": [
        cand(["LM317T"], published="2026-13-45", ltb_date="2027-03-31", scope="bidon"),
        cand(["LM317T"], ntype="FOO"),
        cand(["LM317T"], url="javascript:alert(1)"),
        cand(["INCONNU"]),
        cand(["BAV99"]),                                    # ambigu, sans fabricant : rejeté
        cand(["BAV99"], manufacturer="Nexperia B.V."),       # désambiguïsé par le fabricant
        cand(["LM317T", "BAV99"], manufacturer="nexperia", ntype="PCN", url="https://x.org/pcn"),  # gamme
        "texte",
    ]}
    valid, rejected = comp.validate_candidates(raw, comps)
    assert [(v["mpn"], v["type"]) for v in valid] == [("LM317T", "EOL"), ("BAV99", "EOL"),
                                                      ("LM317T", "PCN"), ("BAV99", "PCN")]
    assert valid[0]["published"] == "" and valid[0]["ltb_date"] == "2027-03-31"
    assert valid[0]["scope"] == "référence" and valid[2]["scope"] == "gamme"
    assert valid[1]["manufacturer"] == "Nexperia"
    assert len(rejected) == 5
    with pytest.raises(comp.ComponentsError):
        comp.validate_candidates({"notices": "x"}, comps)


def test_validate_candidates_bounds_text():
    comps = comp.replace_components([], comp.parse_components("LM317T;TI"))
    valid, _ = comp.validate_candidates([cand(["LM317T"], title="x" * 5000, summary="ligne\n" * 500)], comps)
    assert len(valid[0]["title"]) == 200 and len(valid[0]["summary"]) == 600 and "\n" not in valid[0]["summary"]


def _valid(mpns="LM317T", **kw):
    comps = comp.replace_components([], comp.parse_components("LM317T;Texas Instruments\nNE555P;Texas Instruments"))
    return comp.validate_candidates([cand(mpns.split(","), **kw)], comps)[0]


def test_merge_is_incremental_and_merges_sources():
    notices = []
    assert len(comp.merge(notices, _valid(number="TI-9"), "2026-10-01")) == 1
    # même numéro, autre URL : pas nouveau, source ajoutée
    assert comp.merge(notices, _valid(number="ti-9", url="https://mouser.com/pcn/9", ltb_date="2027-01-01")) == []
    assert [s["url"] for s in notices[0]["sources"]] == ["https://www.ti.com/eol/9", "https://mouser.com/pcn/9"]
    assert notices[0]["ltb_date"] == "2027-01-01" and notices[0]["found_at"] == "2026-10-01"
    # même URL (au paramètre utm près), sans numéro : pas nouveau
    assert comp.merge(notices, _valid(url="https://www.ti.com/eol/9/?utm_campaign=x")) == []
    # même avis de gamme pour une AUTRE référence suivie : nouveau pour celle-ci
    assert len(comp.merge(notices, _valid("LM317T,NE555P", number="TI-9"))) == 1
    # doublons dans un même lot
    assert len(comp.merge(notices, _valid(url="https://a.org/1") + _valid(url="https://a.org/1"))) == 1
    assert len(notices) == 3 and len({n["id"] for n in notices}) == 3


def test_pending_and_mark_delivered():
    notices = []
    comp.merge(notices, _valid(), "2026-10-01")
    ids = {n["id"] for n in notices}
    assert comp.pending(notices, "veille-a") == notices
    comp.mark_delivered(notices, ids, "veille-a", "2026-10-05")
    assert comp.pending(notices, "veille-a", {"2026-10-05"}) == notices  # relance du même jour
    assert comp.pending(notices, "veille-a", {"2026-10-12"}) == []
    assert comp.pending(notices, "veille-b", {"2026-10-12"}) == notices
    assert comp.pending(notices, "veille-a") == []
    assert comp.delivery_days(notices, "veille-a") == {"2026-10-05"}
    comp.mark_delivered(notices, ids, "veille-a", "2026-10-12")
    assert notices[0]["delivered"]["veille-a"] == "2026-10-05"  # première livraison conservée


def test_batch_order_and_search_since():
    cs = [{"mpn": "A", "web_checked_at": "2026-09-01", "dk": None},
          {"mpn": "B", "web_checked_at": "2026-08-01", "dk": None},
          {"mpn": "C", "web_checked_at": None, "dk": None},
          {"mpn": "D", "web_checked_at": "2026-10-01", "dk": {"changed_at": "2026-10-05"}}]
    assert [c["mpn"] for c in comp.batch(cs, 10)] == ["C", "D", "B", "A"]
    assert [c["mpn"] for c in comp.batch(cs, 2)] == ["C", "D"]
    assert comp.search_since(cs[2]) is None and comp.search_since(cs[0]) == "2026-08-02"


def test_markdown_escapes_untrusted_fields():
    notices = []
    evil = _valid(title="<img src=http://evil/x> | ::: {.x}", number="[a](javascript:x)",
                  url="https://ti.com/a_(b)")
    evil[0]["sources"][0]["label"] = "TI](javascript:alert(1))"
    comp.merge(notices, evil)
    view = comp.digest(comp.replace_components([], comp.parse_components("LM317T;Texas Instruments;R|1")),
                       notices, notices)
    md = comp.to_markdown(view)
    assert not re.search(r"(?<!\\)<", md) and "\\<img" in md  # aucun « < » non échappé
    assert "](javascript" not in md and "\\]\\(javascript" in md
    assert "R\\|1" in md and "(https://ti.com/a_%28b%29)" in md
    assert "::: {.critical}" in md and comp.subject_prefix(view) == "[EOL ×1] "
    html = comp.to_html_email(view, "Titre <b>")
    assert "<img src=http://evil" not in html and "&lt;img" in html and "Titre &lt;b&gt;" in html
    assert 'href="https://ti.com/a_(b)"' in html
    assert "LM317T" in comp.to_text_email(view)


def test_digest_reminder_and_empty_view():
    notices = []
    comp.merge(notices, _valid(ntype="PCN", url="https://a.org/1"), "2026-01-01")
    comp.merge(notices, _valid(ntype="LTB", url="https://a.org/2"), "2026-02-01")
    comps = comp.replace_components([], comp.parse_components("LM317T;Texas Instruments"))
    view = comp.digest(comps, notices, [])
    assert [n["type"] for n in view["reminder"]] == ["LTB"] and comp.subject_prefix(view) == ""
    assert "Aucun nouvel avis" in comp.to_markdown(view) and "Aucun nouvel avis" in comp.to_text_email(view)
    assert comp.summary(comps, notices) == {"components": 1, "notices": 2, "critical": 1}
    assert [n["type"] for n in comp.for_display(notices)] == ["LTB", "PCN"]
    assert comp.for_display(notices, "PCN", "2026-01-01")[0]["type"] == "PCN"


def _demo_notices():
    notices = []
    comp.merge(notices, _valid(ntype="PCN", url="https://a.org/pcn"), "2026-01-01")
    comp.merge(notices, _valid(ntype="EOL", url="https://a.org/eol", ltb_date="2027-03-31"), "2026-02-01")
    comps = comp.replace_components([], comp.parse_components(
        "LM317T;Texas Instruments;R-1\nSTM32F103C8T6;STMicroelectronics\nNE555P;Texas Instruments"))
    valid, _ = comp.validate_candidates({"notices": [cand(["STM32F103C8T6"], ntype="NRND", url="https://st.com/n")]},
                                        comps)
    comp.merge(notices, valid, "2026-03-01")
    return comps, notices


def test_group_one_row_per_component():
    comps, notices = _demo_notices()
    rows = comp.group(notices, comps, include_quiet=True)
    assert [r["mpn"] for r in rows] == ["LM317T", "STM32F103C8T6", "NE555P"]  # critiques, sévérité, sans avis
    lm = rows[0]
    assert lm["types"] == ["EOL", "PCN"] and lm["critical"] and lm["deadline"] == "2027-03-31"
    assert lm["link"]["url"] == "https://a.org/eol" and lm["worst"]["type"] == "EOL" and lm["ref"] == "R-1"
    assert lm["found"] == "2026-02-01" and lm["tracked"]
    assert rows[2]["types"] == [] and rows[2]["link"] is None and not rows[2]["critical"]
    assert [r["mpn"] for r in comp.group(notices, comps)] == ["LM317T", "STM32F103C8T6"]  # sans les silencieux
    # composant retiré de la liste : ses avis restent visibles
    gone = comp.group(notices, comps[1:])
    assert gone[0]["mpn"] == "LM317T" and not gone[0]["tracked"] and gone[0]["ref"] == ""
    assert not gone[0]["critical"]  # un composant retiré n'est plus une alerte (comme summary / rappel)
    # ancre : stable, sûre, distincte
    keys = {r["key"] for r in rows}
    assert {comp.anchor(k) for k in keys} == {r["anchor"] for r in rows} and len({r["anchor"] for r in rows}) == 3
    assert all(re.fullmatch(r"c-[0-9a-f]{10}", r["anchor"]) for r in rows)
    assert comp.badge_class("PDN") == "eol" and comp.badge_class("NRND") == "nrnd"


def test_compact_markdown_links_each_row_to_its_detail():
    comps, notices = _demo_notices()
    view = comp.digest(comps, notices, notices)
    md = comp.to_markdown(view)
    lm = comp.anchor(comp.key_of("LM317T", "Texas Instruments"))
    assert md.count(f"](#{lm})") == 2  # tableau des nouveautés + rappel
    assert md.count(f"::: {{#{lm} .comp}}") == 1  # un seul bloc de détail
    assert "[Fin de vie]{.badge .eol} [Modification \\(PCN\\)]{.badge .pcn}" in md  # 2 avis -> 1 ligne
    assert md.index("Nouvelles fins de vie") < md.index("Autres nouveaux avis") < md.index("Rappel") \
        < md.index("Détail par composant")
    assert "réf. R\\-1" in md and "dernière cde 2027\\-03\\-31" in md
    text = comp.to_text_email(view)
    assert "- LM317T (Texas Instruments) : Fin de vie, Modification (PCN) — https://a.org/eol" in text
    html = comp.to_html_email(view, "T")
    assert html.count("<tr>") == 1 + 3 + 1 + 1  # en-têtes + lignes : nouveaux (2 tableaux) + rappel
    assert "background:#fef2f2;padding:6px" in html  # ligne critique teintée


@pytest.mark.skipif(not shutil.which("pandoc"), reason="pandoc absent")
def test_compact_markdown_through_pandoc():
    """La syntaxe produite (liens internes, badges, blocs) est bien comprise par pandoc."""
    import subprocess
    comps, notices = _demo_notices()
    notices[0]["title"] = "<img src=http://evil/x> | ::: {.x}"
    md = comp.to_markdown(comp.digest(comps, notices, notices), heading="#")
    out = subprocess.run(["pandoc", "-f", "markdown+fenced_divs+bracketed_spans+pipe_tables", "-t", "html5"],
                         input=md, capture_output=True, text=True, check=True).stdout
    lm = comp.anchor(comp.key_of("LM317T", "Texas Instruments"))
    assert f'href="#{lm}"' in out and f'<div id="{lm}" class="comp">' in out
    assert '<span class="badge eol">Fin de vie</span>' in out and "<img" not in out and "&lt;img" in out


def test_corrupted_files_are_errors_not_empty(env):
    v = service.create_veille(name="Composants", subject="", schedule_expr=service.COMPONENTS_SCHEDULE,
                              recipients=["a@example.org"], brief="# b\n", kind="composants")
    (v.dir / "notices.json").write_text("{pas du json")
    with pytest.raises(comp.ComponentsError, match="notices.json illisible"):
        comp.load_notices(v.slug)
    (v.dir / "components.json").write_text("[]")
    with pytest.raises(comp.ComponentsError, match="objet JSON"):
        comp.load_components(v.slug)


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------
class FakeClient:
    def __init__(self, answers, on_call=None):
        self.answers, self.on_call, self.calls = answers, on_call, 0

    def status(self, mpn, manufacturer):
        self.calls += 1
        if self.on_call:
            self.on_call(mpn)
        a = self.answers.get(mpn, {"status": "Active", "product_url": None})
        if isinstance(a, Exception):
            raise a
        return a


@pytest.fixture
def tools(env, tmp_path, monkeypatch):
    """Faux PDF (fichier > 20 Ko) et faux envoi Brevo qui enregistre ses arguments."""
    pdf = tmp_path / "fake_pdf.py"
    pdf.write_text(textwrap.dedent("""
        import sys
        a = sys.argv
        open(a[a.index("--out") + 1], "wb").write(b"%PDF" + b"x" * 30000)
        print("OK: PDF généré")
    """))
    brevo = tmp_path / "fake_brevo.py"
    brevo.write_text(textwrap.dedent("""
        import json, os, sys
        a = sys.argv[1:]
        if os.environ.get("FAKE_BREVO_FAIL"):
            print("HTTP ERROR 401"); sys.exit(1)
        html = open(a[a.index("--html-file") + 1]).read() if "--html-file" in a else None
        with open(os.environ["FAKE_BREVO_LOG"], "a") as fh:
            fh.write(json.dumps({"args": a, "html": html}) + "\\n")
        print("OK: email accepted\\nstatus: delivered")
    """))
    log = tmp_path / "mails.jsonl"
    monkeypatch.setenv("BUILD_REPORT", str(pdf))
    monkeypatch.setenv("BREVO_SCRIPT", str(brevo))
    monkeypatch.setenv("FAKE_BREVO_LOG", str(log))
    monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
    monkeypatch.delenv("DIGIKEY_CLIENT_SECRET", raising=False)

    def mails():
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return mails


@pytest.fixture
def watch(tools):
    return service.create_veille(name="Composants carte X", subject="BOM", schedule_expr=service.COMPONENTS_SCHEDULE,
                                 recipients=["achats@example.org"], brief=store.brief_templates()["composants"],
                                 kind="composants", components=comp.parse_components(BOM), batch_size=2)


def finalize(v, notices):
    p = v.dir / "rapports" / "cand.json"
    p.write_text(json.dumps({"notices": notices}))
    return service.components_finalize(v.slug, p)


def test_create_components_veille(watch):
    assert store.load(watch.slug).kind == "composants" and watch.schedule == "0 23 * * 0"
    data = comp.load_components(watch.slug)
    assert len(data["components"]) == 3 and data["batch"] == []
    prompt = (watch.dir / "job-prompt.md").read_text()
    assert f"veillectl components scan {watch.slug}" in prompt
    assert f"veillectl components finalize {watch.slug} {watch.dir}/rapports/D-pcn.json" in prompt
    assert "au plus 2)" in prompt and "${" not in prompt
    row = service.describe(watch)
    assert row["components"] == {"components": 3, "notices": 0, "critical": 0, "linked": []}


def test_old_veille_json_without_kind_and_unchanged_prompt(env):
    v = service.create_veille(name="Veille Cyber", subject="s", schedule_expr="0 8 * * 1",
                              recipients=["a@example.org"], brief="# b\n")
    data = json.loads((v.dir / "veille.json").read_text())
    for k in ("kind", "components", "batch_size"):
        data.pop(k)
    (v.dir / "veille.json").write_text(json.dumps(data))
    old = store.load(v.slug)
    assert old.kind == "veille" and old.components is None and service.describe(old)["components"] is None
    prompt = render.render(old)
    assert "6 bis" not in prompt and '"value_label":"..."}]}]}.\n\n7. GÉNÈRE' in prompt
    assert not render.write(old)  # prompt identique à celui de la création : rien à réécrire


def test_link_veille_to_components(watch):
    comp.save_notices(watch.slug, [])
    notices = []
    comp.merge(notices, _valid(), "2026-01-01")
    comp.save_notices(watch.slug, notices)
    v = service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                              recipients=["a@example.org"], brief="# b\n", include_components=watch.slug)
    assert store.load(v.slug).components == watch.slug
    prompt = (v.dir / "job-prompt.md").read_text()
    assert (f"veillectl components chapter {watch.slug} --for {v.slug} --append {v.dir}/rapports/D-veille.md"
            in prompt)
    # les avis antérieurs au rattachement ne sont pas réexpédiés
    assert comp.pending(comp.load_notices(watch.slug), comp.chapter_channel(v.slug)) == []
    assert comp.delivery_days(comp.load_notices(watch.slug), comp.chapter_channel(v.slug)) == set()
    with pytest.raises(store.StoreError, match="seule une veille thématique"):
        service.update_veille(watch.slug, include_components=watch.slug)
    with pytest.raises(service.ServiceError, match="n'est pas une veille composants"):
        service.update_veille(v.slug, include_components=v.slug)
    with pytest.raises(store.StoreError, match="veille inconnue"):
        service.update_veille(v.slug, include_components="inconnue")
    service.update_veille(v.slug, include_components="")
    assert store.load(v.slug).components is None and "6 bis" not in (v.dir / "job-prompt.md").read_text()


def test_scan_without_digikey(watch):
    out = service.components_scan(watch.slug)
    assert out["digikey"] == {"configured": False, "checked": 0, "errors": 0}
    assert [b["mpn"] for b in out["batch"]] == ["LM317T", "STM32F103C8T6"]
    assert out["batch"][0]["since"] is None
    assert len(comp.load_components(watch.slug)["batch"]) == 2


def test_scan_with_digikey_statuses(watch):
    client = FakeClient({"LM317T": {"status": "Last Time Buy", "product_url": "https://www.digikey.com/p/1"},
                         "NE555P": {"error": "introuvable chez Digi-Key"}})
    out = service.components_scan(watch.slug, client=client)
    assert out["digikey"]["checked"] == 2 and out["digikey"]["errors"] == 1 and out["new_digikey_notices"] == 1
    n = comp.load_notices(watch.slug)[0]
    assert (n["type"], n["origin"], n["sources"][0]["url"]) == ("LTB", "digikey", "https://www.digikey.com/p/1")
    cs = {c["mpn"]: c for c in comp.load_components(watch.slug)["components"]}
    assert cs["LM317T"]["dk"]["changed_at"] == cs["LM317T"]["dk"]["checked_at"]
    assert "changed_at" not in cs["STM32F103C8T6"]["dk"]  # première lecture « Active » : pas un changement
    assert cs["NE555P"]["dk"]["error"] == "introuvable chez Digi-Key"
    assert out["batch"][0]["known"] == ["Statut Digi-Key : Last Time Buy"]
    # relance le même jour : aucune nouvelle requête, aucun doublon
    again = FakeClient({})
    service.components_scan(watch.slug, client=again)
    assert again.calls == 0 and len(comp.load_notices(watch.slug)) == 1


def test_scan_status_change_creates_notice_and_prioritises(watch, monkeypatch):
    service.components_scan(watch.slug, client=FakeClient({}))
    finalize(watch, [])  # LM317T et STM32 recherchés sur le web
    monkeypatch.setattr(comp, "today", lambda: "2099-01-01")
    out = service.components_scan(watch.slug, client=FakeClient({"STM32F103C8T6": {
        "status": "Obsolete", "product_url": None}}))
    assert out["new_digikey_notices"] == 1
    n = comp.load_notices(watch.slug)[0]
    assert n["type"] == "OBSOLETE" and n["sources"][0]["url"].startswith("https://www.digikey.com/en/products/result")
    # NE555P jamais recherché d'abord, puis STM32 (statut changé) avant LM317T
    assert [b["mpn"] for b in out["batch"]] == ["NE555P", "STM32F103C8T6"]


def test_scan_concurrent_edit_is_preserved(watch):
    def edit(mpn):
        if mpn == "LM317T":  # pendant le passage Digi-Key (hors verrou), l'interface modifie la liste
            service.remove_components(watch.slug, ["NE555P"])
            service.import_components(watch.slug, comp.parse_components("BC547;onsemi"))
    service.components_scan(watch.slug, client=FakeClient({}, on_call=edit))
    mpns = [c["mpn"] for c in comp.load_components(watch.slug)["components"]]
    assert mpns == ["LM317T", "STM32F103C8T6", "BC547"]


def test_scan_budget_and_api_error(watch):
    out = service.components_scan(watch.slug, client=FakeClient({}), budget_s=-1)
    assert "stopped" in out["digikey"] and out["digikey"]["checked"] == 0
    from veille_core import digikey
    out = service.components_scan(watch.slug, client=FakeClient({"LM317T": digikey.DigiKeyError("HTTP 500")}))
    assert out["digikey"]["error"] == "HTTP 500" and len(out["batch"]) == 2  # le lot web reste produit


def test_finalize_dedicated_mail_and_incremental(watch, tools):
    service.components_scan(watch.slug)
    out = finalize(watch, [cand(["LM317T"], number="TI-9", ltb_date="2027-03-31"),
                           cand(["STM32F103C8T6"], ntype="PCN", url="https://st.com/pcn/1",
                                title="<img src=http://evil/x>"),
                           cand(["LM317T"], url="javascript:alert(1)")])
    assert "OK: veille composants — 2 nouvel(s) avis (dont 1 fin(s) de vie" in out and "1 rejet(s)" in out
    cs = {c["mpn"]: c for c in comp.load_components(watch.slug)["components"]}
    assert cs["LM317T"]["web_checked_at"] and cs["NE555P"]["web_checked_at"] is None
    assert comp.load_components(watch.slug)["batch"] == []
    report = store.list_reports(watch.slug)[0]
    assert report["pdf"] and report["md"]
    md = store.report_path(watch.slug, report["md"]).read_text()
    assert "\\<img" in md and not re.search(r"(?<!\\)<", md)
    (mail,) = tools()
    args = mail["args"]
    assert args[args.index("--subject") + 1].startswith("[EOL ×1] Composants carte X")
    assert args[args.index("--bcc-file") + 1].endswith("recipients.txt") and "--attach" in args
    assert "&lt;img" in mail["html"] and "<img src=http://evil" not in mail["html"]
    # deuxième exécution : rien de nouveau, l'e-mail le dit
    service.components_scan(watch.slug)
    out = finalize(watch, [cand(["LM317T"], number="TI-9", url="https://mouser.com/x")])
    assert "0 nouvel(s) avis" in out
    args = tools()[-1]["args"]
    assert args[args.index("--subject") + 1].startswith("Composants carte X")
    assert "Aucun nouvel avis" in tools()[-1]["html"] and len(comp.load_notices(watch.slug)) == 2


def test_finalize_mail_failure_keeps_notices_pending(watch, tools, monkeypatch):
    monkeypatch.setenv("FAKE_BREVO_FAIL", "1")
    with pytest.raises(service.ServiceError, match="HTTP ERROR"):
        finalize(watch, [cand(["LM317T"])])
    assert len(comp.load_notices(watch.slug)) == 1  # enregistré malgré l'échec d'envoi
    monkeypatch.delenv("FAKE_BREVO_FAIL")
    finalize(watch, [])
    assert "[EOL ×1]" in tools()[-1]["args"][tools()[-1]["args"].index("--subject") + 1]


def test_finalize_linked_sends_no_mail(watch, tools):
    service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                          recipients=["a@example.org"], brief="# b\n", include_components=watch.slug)
    out = finalize(watch, [cand(["LM317T"])])
    assert "inclus dans la veille veille-indus" in out and tools() == []
    assert comp.load_notices(watch.slug)[0]["delivered"]["mail"] == "via veille-indus"


def test_finalize_invalid_inputs(watch):
    p = watch.dir / "rapports" / "bad.json"
    p.write_text("{oups")
    with pytest.raises(comp.ComponentsError, match="JSON invalide"):
        service.components_finalize(watch.slug, p)
    with pytest.raises(comp.ComponentsError, match="introuvable"):
        service.components_finalize(watch.slug, watch.dir / "absent.json")
    p.write_text(json.dumps({"notices": "x"}))
    with pytest.raises(comp.ComponentsError):
        service.components_finalize(watch.slug, p)
    assert not (watch.dir / "notices.json").exists()
    v = service.create_veille(name="Veille", subject="", schedule_expr="0 8 * * 1",
                              recipients=["a@example.org"], brief="# b\n")
    with pytest.raises(service.ServiceError, match="pas une veille composants"):
        service.components_finalize(v.slug, p)


def test_finalize_pdf_failure(watch, monkeypatch, tmp_path):
    bad = tmp_path / "bad_pdf.py"
    bad.write_text("import sys; print('pandoc error'); sys.exit(1)")
    monkeypatch.setenv("BUILD_REPORT", str(bad))
    with pytest.raises(service.ServiceError, match="PDF non généré"):
        finalize(watch, [cand(["LM317T"])])
    assert comp.pending(comp.load_notices(watch.slug), "report")  # sera dans le prochain rapport


def test_chapter(watch, tools):
    v = service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                              recipients=["a@example.org"], brief="# b\n", include_components=watch.slug)
    finalize(watch, [cand(["LM317T"], ltb_date="2027-03-31"), cand(["NE555P"], ntype="PCN", url="https://a.org/p")])
    md = v.dir / "rapports" / "2026-10-12-veille.md"
    md.write_text("# Rapport\n\nTexte.\n")
    assert service.components_chapter(watch.slug, v.slug, md)[0] == "[EOL ×1]"
    assert service.components_chapter(watch.slug, v.slug, md)[0] == "[EOL ×1]"  # relance : idempotent
    text = md.read_text()
    assert text.count(service.CHAPTER_MARK) == 1 and text.startswith("# Rapport\n\nTexte.")
    assert text.index("Nouvelles fins de vie") < text.index("Autres nouveaux avis")
    (v.dir / "rapports" / "2026-10-12-veille.pdf").write_bytes(b"%PDF")  # exécution réussie
    # la semaine suivante (rapport horodaté, comme le produit le prompt) : plus rien de nouveau,
    # mais le rappel des fins de vie reste
    md2 = v.dir / "rapports" / "2026-10-19-06h30-veille.md"
    md2.write_text("# Rapport\n")
    assert service.components_chapter(watch.slug, v.slug, md2)[0] == ""
    assert "Aucun nouvel avis" in md2.read_text() and "Rappel" in md2.read_text()


def test_chapter_delivery_day_is_the_report_day(watch, tools):
    """Une exécution commencée avant minuit (rapport 23h55) dont le chapitre est ajouté après minuit
    reste rattachée à son rapport : pas de double envoi au run suivant."""
    import veille_core.components as c_mod
    v = service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                              recipients=["a@example.org"], brief="# b\n", include_components=watch.slug)
    finalize(watch, [cand(["NE555P"], ntype="PCN", url="https://a.org/p")])
    rdir = v.dir / "rapports"
    md = rdir / "2099-01-04-23h55-veille.md"
    md.write_text("# r\n")
    orig = c_mod.today
    try:
        c_mod.today = lambda: "2099-01-05"  # la commande tourne après minuit
        service.components_chapter(watch.slug, v.slug, md)
    finally:
        c_mod.today = orig
    assert {n["delivered"][c_mod.chapter_channel(v.slug)] for n in comp.load_notices(watch.slug)} == {"2099-01-04"}
    (rdir / "2099-01-04-23h55-veille.pdf").write_bytes(b"%PDF")  # l'exécution a abouti
    md2 = rdir / "2099-01-11-23h55-veille.md"
    md2.write_text("# r\n")
    assert service.components_chapter(watch.slug, v.slug, md2)[0] == "" and "Aucun nouvel avis" in md2.read_text()


def test_chapter_path_validation(watch, tmp_path):
    v = service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                              recipients=["a@example.org"], brief="# b\n")
    outside = tmp_path / "2026-10-12-veille.md"
    outside.write_text("x")
    other = watch.dir / "rapports" / "2026-10-12-veille.md"
    other.write_text("x")
    for bad in (outside, other, v.dir / "rapports" / "../config.md", v.dir / "rapports" / "notes.md",
                v.dir / "rapports" / "2026-10-12-veille.md"):  # le dernier n'existe pas
        with pytest.raises(store.StoreError, match="rapport Markdown invalide"):
            service.components_chapter(watch.slug, v.slug, bad)
    ok = v.dir / "rapports" / "2026-10-12-veille.md"
    ok.write_text("# r\n")
    assert service.components_chapter("inconnue", v.slug, ok) is None and ok.read_text() == "# r\n"


def test_archive_components_unlinks_veilles(watch):
    v = service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                              recipients=["a@example.org"], brief="# b\n", include_components=watch.slug)
    service.archive(watch.slug)
    assert store.load(v.slug).components is None and "6 bis" not in (v.dir / "job-prompt.md").read_text()


def test_add_remove_and_notices_filter(watch):
    assert service.import_components(watch.slug, comp.parse_components("lm317t;texas instruments\nBC547;onsemi"))["added"] == 1
    assert service.remove_components(watch.slug, ["bc547", "ABSENT"]) == 1
    assert service.remove_components(watch.slug, ["ABSENT"]) == 0
    with pytest.raises(store.StoreError, match="type d'avis inconnu"):
        service.notices(watch.slug, "FOO")
    with pytest.raises(store.StoreError, match="seule une veille composants"):
        v = service.create_veille(name="Veille", subject="", schedule_expr="0 8 * * 1",
                                  recipients=["a@example.org"], brief="# b\n")
        service.update_veille(v.slug, components=[])
    with pytest.raises(store.StoreError, match="taille de lot"):
        service.update_veille(watch.slug, batch_size=0)
    service.update_veille(watch.slug, batch_size=50)
    assert store.load(watch.slug).batch_size == 50
    assert "au plus 50)" in (watch.dir / "job-prompt.md").read_text()


def test_unknown_kind_rejected(env):
    with pytest.raises(store.StoreError, match="type de veille inconnu"):
        service.create_veille(name="X y", subject="", schedule_expr="0 8 * * 1", recipients=["a@example.org"],
                              brief="# b\n", kind="autre")


@pytest.mark.skipif(not (shutil.which("pandoc") and shutil.which("weasyprint")), reason="pandoc/weasyprint absents")
def test_real_pdf_is_above_watchdog_threshold(watch, monkeypatch):
    monkeypatch.setenv("BUILD_REPORT", str(REPO / "skills/productivity/styled-pdf-report/scripts/build_report.py"))
    finalize(watch, [])  # aucun avis : le plus petit rapport possible
    pdf = store.report_path(watch.slug, store.list_reports(watch.slug)[0]["pdf"])
    from test_watchdog import watchdog
    assert pdf.stat().st_size > watchdog.COMPONENTS_MIN_SIZE  # seuil de plausibilité du chien de garde


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def test_cli_components(watch, tools, tmp_path, capsys):
    bom = tmp_path / "bom.csv"
    bom.write_text("BC547;onsemi;Q1\n")
    assert cli.main(["components", "add", watch.slug, "--file", str(bom), "BC557;onsemi"]) == 0
    assert "2 composant(s) ajouté(s)" in capsys.readouterr().out
    assert cli.main(["components", "remove", watch.slug, "BC557"]) == 0
    with pytest.raises(SystemExit):  # valeurs en trop : refusées hors « components »
        cli.main(["list", "en-trop"])
    with pytest.raises(SystemExit):  # option inconnue : refusée
        cli.main(["components", "list", watch.slug, "--inconnue"])
    assert cli.main(["components", "scan", watch.slug]) == 0
    assert json.loads(capsys.readouterr().out.split("✓ 1 composant(s) retiré(s)\n")[-1])["batch"]
    p = watch.dir / "rapports" / "cand.json"
    p.write_text(json.dumps({"notices": [cand(["BC547"], url="https://onsemi.com/pcn/1")]}))
    assert cli.main(["components", "finalize", watch.slug, str(p)]) == 0
    assert "OK: veille composants — 1 nouvel" in capsys.readouterr().out
    assert cli.main(["components", "list", watch.slug]) == 0
    assert "BC547" in capsys.readouterr().out
    assert cli.main(["components", "notices", watch.slug, "--type", "EOL"]) == 0
    assert "onsemi.com/pcn/1" in capsys.readouterr().out
    assert cli.main(["components", "finalize", watch.slug]) == 1
    assert cli.main(["components", "chapter", watch.slug]) == 1
    assert cli.main(["components", "add", watch.slug, "--file", str(tmp_path / "absent.csv")]) == 1
    v = service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                              recipients=["a@example.org"], brief="# b\n")
    assert cli.main(["edit", v.slug, "--include-components", watch.slug]) == 0
    md = v.dir / "rapports" / "2026-10-12-veille.md"
    md.write_text("# r\n")
    capsys.readouterr()
    assert cli.main(["components", "chapter", watch.slug, "--for", v.slug, "--append", str(md)]) == 0
    out = capsys.readouterr().out
    assert "✓ chapitre composants ajouté" in out and "PREFIXE_SUJET" not in out  # avis antérieurs au lien
    assert cli.main(["components", "chapter", "inconnue", "--for", v.slug, "--append", str(md)]) == 0
    assert "introuvable — chapitre ignoré" in capsys.readouterr().out
    assert cli.main(["show", watch.slug]) == 0 and "composants   :" in capsys.readouterr().out


def test_cli_create_components(env, tmp_path, capsys):
    bom = tmp_path / "bom.csv"
    bom.write_text(BOM)
    assert cli.main(["create", "--kind", "composants", "--name", "BOM X", "--schedule", "0 23 * * 0",
                     "--recipients", "a@example.org", "--components-file", str(bom), "--batch-size", "10"]) == 0
    v = store.load("bom-x")
    assert v.kind == "composants" and v.batch_size == 10
    assert store.read_brief("bom-x") == store.brief_templates()["composants"]
    assert len(comp.load_components("bom-x")["components"]) == 3


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
@pytest.fixture
def ui(watch, monkeypatch):
    from test_ui import PASSWORD, login  # noqa: F401 - mêmes réglages que test_ui

    from veille_ui import auth
    monkeypatch.setenv("VEILLE_UI_PASSWORD_HASH", auth.hash_password(PASSWORD))
    monkeypatch.setenv("VEILLE_UI_SECRET", "s" * 64)
    auth._failures.clear()
    from fastapi.testclient import TestClient

    from veille_ui.app import create_app
    client = TestClient(create_app(), base_url="http://localhost")
    return client, login(client)


def _rows_html(page: str) -> list[str]:
    return re.findall(r'<tr class="comp[^"]*"', page)


def test_ui_components_list_filters_and_detail(ui, watch):
    """Une ligne par composant (y compris sans avis), filtrable, détail chargé au clic."""
    client, _ = ui
    service.components_scan(watch.slug, client=FakeClient({"LM317T": {"status": "Last Time Buy",
                                                                      "product_url": "https://www.digikey.com/p/1"}}))
    finalize(watch, [cand(["STM32F103C8T6"], ntype="PCN", url="https://st.com/pcn/1",
                          title="<script>alert(1)</script>", summary="résumé"),
                     cand(["STM32F103C8T6"], ntype="NRND", url="https://st.com/nrnd/1")])
    r = client.get(f"/veilles/{watch.slug}")
    assert r.status_code == 200 and "script-src 'self'" in r.headers["Content-Security-Policy"]
    t = r.text
    assert "<script>alert" not in t and "&lt;script&gt;alert(1)&lt;/script&gt;" in t  # dans data-q, échappé
    assert t.index('id="composants"') < t.index('id="rapports"')
    assert 'id="fins-de-vie"' not in t and 'id="avis"' not in t  # 3 blocs fusionnés en une liste
    rows = _rows_html(t)
    assert len(rows) == 3 and rows[0] == '<tr class="comp crit"'  # LM317T (dernière commande) en tête
    assert 'data-types="NRND PCN"' in t  # 2 avis -> 1 ligne
    assert '<tr class="detail"' not in t  # détail créé et chargé au clic (app.js), pas par htmx :
    assert f'hx-get="/veilles/{watch.slug}/components/' not in t
    assert 'id="comp-search"' in t and t.count("data-col=") == 5 and "data-types aria-label" in t
    assert 'href="https://st.com/nrnd/1" target="_blank" rel="noopener noreferrer"' in t  # lien du pire avis
    assert "1 en fin de vie ou dernière commande" in t
    # CSP : aucun style, gestionnaire d'événement ni script en ligne
    assert "style=" not in t and not re.search(r"\son[a-z]+=", t)
    assert all("src=" in tag for tag in re.findall(r"<script[^>]*>", t))
    # détail au clic (htmx) : avis complets, échappés
    anchors = dict(re.findall(r'data-detail="/veilles/[^/]+/components/(c-[0-9a-f]{10})".*?<strong>([^<]+)<', t, re.S))
    assert set(anchors.values()) == {"LM317T", "STM32F103C8T6", "NE555P"}
    by_mpn = {m: a for a, m in anchors.items()}
    d = client.get(f"/veilles/{watch.slug}/components/{by_mpn['STM32F103C8T6']}")
    assert d.status_code == 200 and "<html" not in d.text  # fragment, pas une page
    assert "&lt;script&gt;" in d.text and "<script>alert" not in d.text and "résumé" in d.text
    assert d.text.index("Déconseillé") < d.text.index("Modification (PCN)")  # le plus critique d'abord
    d = client.get(f"/veilles/{watch.slug}/components/{by_mpn['LM317T']}").text
    assert 'href="https://www.digikey.com/p/1"' in d and "Last Time Buy" in d
    assert "Aucun avis détecté" in client.get(f"/veilles/{watch.slug}/components/{by_mpn['NE555P']}").text
    dash = client.get("/").text
    assert "1 en fin de vie" in dash and f'href="/veilles/{watch.slug}#composants"' in dash


@pytest.mark.parametrize("path", ["c-0000000000", "x", "c-ABCDEF0123", "c-00000000000", "..%2F..%2Fcron"])
def test_ui_component_detail_rejects_unknown(ui, watch, path):
    client, _ = ui
    assert client.get(f"/veilles/{watch.slug}/components/{path}").status_code == 404


def test_ui_component_detail_other_veilles_and_login(ui, watch):
    client, _ = ui
    anchor = comp.anchor(comp.key_of("LM317T", "Texas Instruments"))
    assert client.get(f"/veilles/{watch.slug}/components/{anchor}").status_code == 200
    assert client.get(f"/veilles/inconnue/components/{anchor}").status_code == 404
    v = service.create_veille(name="Veille", subject="", schedule_expr="0 8 * * 1",
                              recipients=["a@example.org"], brief="# b\n")
    assert client.get(f"/veilles/{v.slug}/components/{anchor}").status_code == 404  # pas une veille composants
    client.cookies.clear()
    r = client.get(f"/veilles/{watch.slug}/components/{anchor}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_ui_lists_every_component_without_cap(ui, watch):
    """Filtrage côté navigateur : la liste doit être complète (plus de plafond à 100 avis)."""
    client, _ = ui
    service.import_components(watch.slug, [{"mpn": f"R{i:03d}", "manufacturer": "Yageo", "ref": ""} for i in range(150)])
    finalize(watch, [cand(["LM317T"], ntype="PCN", url=f"https://a.org/{i}") for i in range(3)])
    t = client.get(f"/veilles/{watch.slug}").text
    assert len(_rows_html(t)) == 153 and "153 / 153" in t


def test_ui_removed_component_keeps_its_notices(ui, watch):
    client, _ = ui
    finalize(watch, [cand(["NE555P"], url="https://a.org/eol")])
    service.remove_components(watch.slug, ["NE555P"])
    t = client.get(f"/veilles/{watch.slug}").text
    assert len(_rows_html(t)) == 3 and "(retiré)" in t
    assert '<tr class="comp crit"' not in t and "en fin de vie ou dernière commande" not in t  # plus une alerte
    d = client.get(f"/veilles/{watch.slug}/components/{comp.anchor(comp.key_of('NE555P', 'Texas Instruments'))}")
    assert d.status_code == 200 and "retiré de la liste suivie" in d.text and "https://a.org/eol" in d.text


def test_ui_empty_component_list(ui, watch):
    client, _ = ui
    service.remove_components(watch.slug, ["LM317T", "STM32F103C8T6", "NE555P"])
    t = client.get(f"/veilles/{watch.slug}").text
    assert "Aucun composant suivi" in t and not _rows_html(t) and "0 / 0" in t


def test_ui_create_and_edit_components(ui):
    client, csrf = ui
    from test_ui import create_form
    form = create_form(csrf, name="BOM carte Y", kind="composants", components="LM317T;TI;R1\nNE555P\tTI\t",
                       batch_size="25", freq_kind="weekly", dow="0", time="23:00")
    r = client.post("/veilles/new", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/veilles/bom-carte-y?ok=created"
    v = store.load("bom-carte-y")
    assert v.kind == "composants" and v.batch_size == 25 and v.schedule == "0 23 * * 0"
    t = client.get("/veilles/bom-carte-y/edit").text
    assert "LM317T ; TI ; R1" in t and 'name="kind" value="composants"' in t
    bad = create_form(csrf, name="BOM carte Z", kind="composants", components="sans-fabricant")
    r = client.post("/veilles/new", data=bad)
    assert r.status_code == 400 and "ligne 1" in r.text and not store.exists("bom-carte-z")
    form = create_form(csrf, name="BOM carte Y", components="BC547;onsemi", batch_size="25", kind="veille")
    r = client.post("/veilles/bom-carte-y/edit", data=form, follow_redirects=False)
    assert r.status_code == 303  # le type reste « composants » malgré le champ falsifié
    assert [c["mpn"] for c in comp.load_components("bom-carte-y")["components"]] == ["BC547"]
    t = client.get("/veilles/new?kind=composants").text
    assert "Nouvelle veille composants" in t and "Brief de départ" not in t
    assert "?template=composants" not in client.get("/veilles/new").text


def test_ui_veille_includes_components(ui, watch):
    client, csrf = ui
    from test_ui import create_form
    r = client.post("/veilles/new", data=create_form(csrf, include_components=watch.slug), follow_redirects=False)
    assert r.status_code == 303
    assert store.load("veille-cyber").components == watch.slug
    t = client.get("/veilles/veille-cyber").text
    assert f'href="/veilles/{watch.slug}"' in t
    assert f'<option value="{watch.slug}" selected>' in client.get("/veilles/veille-cyber/edit").text
    assert "veille-cyber" in client.get(f"/veilles/{watch.slug}").text  # livraison : chapitre


# --------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------
def test_mcp_components_tools(env, tools):
    pytest.importorskip("mcp")
    from test_mcp import call, data

    from veille_mcp.server import build_server
    srv = build_server()
    v = data(call(srv, "create_veille", {"name": "BOM Z", "subject": "carte Z", "recipients": ["a@example.org"],
                                         "kind": "composants", "components_list": ["LM317T;Texas Instruments;R1"]}))
    assert v["kind"] == "composants" and v["cron"] == "0 23 * * 0" and v["components"]["components"] == 1
    assert data(call(srv, "add_components", {"slug": "bom-z", "components_list": ["NE555P;Texas Instruments"]})) == {
        "slug": "bom-z", "added": 1, "total": 2, "known": 0, "notices_known": 0}
    assert data(call(srv, "remove_components", {"slug": "bom-z", "mpns": ["NE555P"]}))["total"] == 1
    r = call(srv, "add_components", {"slug": "bom-z", "components_list": ["mauvaise ligne"]})
    assert r.is_error and "ligne 1" in r.content[0].text
    finalize(store.load("bom-z"), [cand(["LM317T"], ltb_date="2027-03-31"),
                                   cand(["LM317T"], ntype="PCN", url="https://a.org/p")])
    rows = data(call(srv, "list_notices", {}))
    assert [n["type"] for n in rows] == ["EOL", "PCN"] and rows[0]["critical"] is True
    assert rows[0]["sources"][0]["url"] == "https://www.ti.com/eol/9" and rows[0]["veille"] == "bom-z"
    assert data(call(srv, "list_notices", {"slug": "bom-z", "type": "PCN"}))[0]["type"] == "PCN"
    comps = data(call(srv, "list_components", {"slug": "bom-z"}))
    assert comps[0]["worst_notice"] == "EOL"
    w = data(call(srv, "create_veille", {"name": "Veille A", "subject": "a", "recipients": ["a@example.org"],
                                         "time": "07:30"}))
    assert w["cron"] == "30 7 * * 1" and w["kind"] == "veille"
    w = data(call(srv, "update_veille", {"slug": "veille-a", "include_components": "bom-z"}))
    assert w["include_components"] == "bom-z"
    r = call(srv, "list_components", {"slug": "veille-a"})
    assert r.is_error and "pas une veille composants" in r.content[0].text


def test_digikey_answers_are_not_trusted_for_links(watch):
    service.components_scan(watch.slug, client=FakeClient({"LM317T": {"status": "Obsolete", "product_url": None}}))
    url = comp.load_notices(watch.slug)[0]["sources"][0]["url"]
    assert re.match(r"^https://www\.digikey\.com/en/products/result\?keywords=LM317T$", url)



# --------------------------------------------------------------------------
# Correctifs issus de la revue
# --------------------------------------------------------------------------
def test_escalation_on_same_link_is_reported(watch):
    """NRND puis « Obsolete » sur la même fiche Digi-Key : l'aggravation est signalée."""
    url = "https://www.digikey.com/p/1"
    service.components_scan(watch.slug, client=FakeClient({"LM317T": {"status": "Not For New Designs",
                                                                      "product_url": url}}))
    import veille_core.components as c_mod
    orig = c_mod.today
    try:
        c_mod.today = lambda: "2099-01-01"
        out = service.components_scan(watch.slug, client=FakeClient({"LM317T": {"status": "Obsolete",
                                                                                "product_url": url}}))
    finally:
        c_mod.today = orig
    assert out["new_digikey_notices"] == 1
    assert [n["type"] for n in comp.load_notices(watch.slug)] == ["NRND", "OBSOLETE"]
    # même chose pour le web : PCN puis EOL citant la même fiche distributeur
    notices = []
    comp.merge(notices, _valid(ntype="PCN", url="https://mouser.com/p/LM317T"))
    assert len(comp.merge(notices, _valid(ntype="EOL", url="https://mouser.com/p/LM317T"))) == 1


@pytest.mark.parametrize("bad", [{"sources": 5}, {"sources": "https://x.org"}, {"mpns": 5}, {"mpns": "LM317T"}])
def test_malformed_candidate_is_rejected_not_crashing(bad):
    comps = comp.replace_components([], comp.parse_components("LM317T;TI"))
    valid, rejected = comp.validate_candidates([{**cand(["LM317T"]), **bad}, cand(["LM317T"])], comps)
    assert len(valid) == 1 and "doivent être des listes" in rejected[0]


def test_chapter_reincludes_notices_of_failed_run(watch, tools):
    import veille_core.components as c_mod
    v = service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                              recipients=["a@example.org"], brief="# b\n", include_components=watch.slug)
    finalize(watch, [cand(["NE555P"], ntype="PCN", url="https://a.org/p")])
    rdir = v.dir / "rapports"
    orig = c_mod.today
    try:
        c_mod.today = lambda: "2099-01-05"
        md = rdir / "2099-01-05-veille.md"
        md.write_text("# r\n")
        service.components_chapter(watch.slug, v.slug, md)  # puis l'exécution échoue : pas de PDF
        c_mod.today = lambda: "2099-01-12"
        md2 = rdir / "2099-01-12-veille.md"
        md2.write_text("# r\n")
        service.components_chapter(watch.slug, v.slug, md2)
        assert "Autres nouveaux avis" in md2.read_text()  # réinclus
        (rdir / "2099-01-12-06h30-veille.pdf").write_bytes(b"%PDF")  # rapport produit (nom horodaté)
        c_mod.today = lambda: "2099-01-19"
        md3 = rdir / "2099-01-19-veille.md"
        md3.write_text("# r\n")
        service.components_chapter(watch.slug, v.slug, md3)
        assert "Aucun nouvel avis" in md3.read_text()
    finally:
        c_mod.today = orig


def test_chapter_requires_link_and_channel_namespace(watch, tools):
    report = service.create_veille(name="Report", slug="report", subject="s", schedule_expr="30 6 * * 1",
                                   recipients=["a@example.org"], brief="# b\n")
    md = report.dir / "rapports" / "2026-10-12-veille.md"
    md.write_text("# r\n")
    with pytest.raises(store.StoreError, match="n'inclut pas"):
        service.components_chapter(watch.slug, "report", md)
    finalize(watch, [cand(["LM317T"])])  # livré sur le canal « report » de la veille composants
    service.update_veille("report", include_components=watch.slug)
    comp.save_notices(watch.slug, [{**n, "delivered": {"report": "2026-01-01"}}
                                   for n in comp.load_notices(watch.slug)])
    assert service.components_chapter(watch.slug, "report", md)[0] == "[EOL ×1]"  # pas de collision


def test_header_and_short_manufacturer_names():
    assert comp.parse_components("# export\nPart Number;Manufacturer\nLM317T;TI") == [
        {"mpn": "LM317T", "manufacturer": "TI", "ref": ""}]
    assert comp.same_manufacturer("TI", "ti") and not comp.same_manufacturer("ON", "Infineon")
    assert comp.same_manufacturer("onsemi", "ON Semiconductor onsemi")


def test_digikey_fallback_link_keeps_special_characters(tools):
    v = service.create_veille(name="BOM", subject="", schedule_expr=service.COMPONENTS_SCHEDULE,
                              recipients=["a@example.org"], brief="# b\n", kind="composants",
                              components=comp.parse_components("LM317T#PBF;TI\nAD8+X;Analog Devices"))
    service.components_scan(v.slug, client=FakeClient({m: {"status": "Obsolete", "product_url": None}
                                                       for m in ("LM317T#PBF", "AD8+X")}))
    urls = sorted(n["sources"][0]["url"] for n in comp.load_notices(v.slug))
    assert urls == ["https://www.digikey.com/en/products/result?keywords=AD8%2BX",
                    "https://www.digikey.com/en/products/result?keywords=LM317T%23PBF"]


def test_ui_corrupted_components_file(ui, watch):
    client, _ = ui
    (watch.dir / "components.json").write_text("{oups")
    for url in (f"/veilles/{watch.slug}", f"/veilles/{watch.slug}/edit"):
        r = client.get(url)
        assert r.status_code == 500 and "components.json illisible" in r.text


# --------------------------------------------------------------------------
# Lancement depuis l'interface et le MCP (analyse complète, statuts Digi-Key à la demande)
# --------------------------------------------------------------------------
@pytest.fixture
def with_keys(monkeypatch):
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "client-id-abcd")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "client-secret-abcd")


@pytest.fixture
def spawned(monkeypatch):
    from veille_core import hermes
    calls = []

    def fake(cmd, log_path, env=None):
        calls.append({"cmd": cmd, "log": log_path, "env": env})
        return 999_999  # PID inexistant : le lancement est aussitôt considéré terminé
    monkeypatch.setattr(hermes, "spawn_detached", fake)
    return calls


def test_check_statuses_now(watch, with_keys, spawned, monkeypatch):
    log = service.check_statuses_now(watch.slug, actor="test")
    (call,) = spawned
    assert call["cmd"][1:] == ["-m", "veille_core", "components", "scan", watch.slug, "--status-only"]
    assert str(REPO) in call["env"]["PYTHONPATH"] and call["log"].name == log
    assert store.RUN_LOG_RE.match(log)
    assert "components-check" in json.dumps(service.read_audit())
    monkeypatch.setattr(service, "running_pid", lambda slug: 42)
    with pytest.raises(service.ServiceError, match="déjà en cours"):
        service.check_statuses_now(watch.slug)


def test_check_statuses_now_requires_keys_and_components_kind(watch, spawned, monkeypatch):
    monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
    with pytest.raises(service.ServiceError, match="page Sources"):
        service.check_statuses_now(watch.slug)
    v = service.create_veille(name="Veille", subject="", schedule_expr="0 8 * * 1",
                              recipients=["a@example.org"], brief="# b\n")
    with pytest.raises(service.ServiceError, match="pas une veille composants"):
        service.check_statuses_now(v.slug)
    assert spawned == []


def test_status_only_scan_keeps_planned_batch(watch):
    service.components_scan(watch.slug)  # étape 1 d'une exécution planifiée : lot enregistré
    planned = comp.load_components(watch.slug)["batch"]
    out = service.components_scan(watch.slug, client=FakeClient({"NE555P": {"status": "Obsolete",
                                                                            "product_url": None}}),
                                  plan_batch=False)
    assert out["batch"] == [] and comp.load_components(watch.slug)["batch"] == planned
    assert out["new_digikey_notices"] == 1


def test_cli_status_only(watch, monkeypatch, capsys, with_keys):
    from veille_core import digikey
    monkeypatch.setattr(digikey, "Client", lambda *a, **k: FakeClient({"NE555P": {"error": "introuvable"}}))
    assert cli.main(["components", "scan", watch.slug, "--status-only"]) == 0
    assert "✓ Digi-Key : 2 statut(s) lu(s), 1 référence(s) non trouvée(s)" in capsys.readouterr().out
    assert comp.load_components(watch.slug)["batch"] == []


def test_detached_command_line_really_runs(watch, monkeypatch):
    """La commande lancée par l'interface s'exécute telle quelle (module, PYTHONPATH, option)."""
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not k.startswith("DIGIKEY_")}
    env["PYTHONPATH"] = str(REPO)
    out = subprocess.run([sys.executable, "-m", "veille_core", "components", "scan", watch.slug, "--status-only"],
                         cwd="/", env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "clés Digi-Key absentes" in out.stdout


def test_ui_launch_buttons_and_confirmations(ui, watch, with_keys, spawned):
    client, csrf = ui
    t = client.get(f"/veilles/{watch.slug}").text
    assert "Vérifier les statuts Digi-Key" in t and "Lancer l&#39;analyse" in t
    assert "Un e-mail récapitulatif sera envoyé aux destinataires." in t
    r = client.post(f"/veilles/{watch.slug}/check-status", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/veilles/{watch.slug}?ok=checking"
    assert len(spawned) == 1
    service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                          recipients=["a@example.org"], brief="# b\n", include_components=watch.slug)
    t = client.get(f"/veilles/{watch.slug}").text
    assert "Aucun e-mail : les nouveaux avis partiront dans le prochain e-mail de veille-indus." in t
    dash = client.get("/").text
    assert "Aucun e-mail : les nouveaux avis partiront" in dash and "Lancer « Veille Indus » maintenant ?" in dash
    assert "Vérifier les statuts Digi-Key" not in client.get("/veilles/veille-indus").text


def test_ui_check_status_without_keys_shows_error(ui, watch, monkeypatch):
    client, csrf = ui
    monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
    r = client.post(f"/veilles/{watch.slug}/check-status", data={"csrf": csrf})
    assert r.status_code == 400 and "page Sources" in r.text


def test_mcp_check_component_statuses(watch, with_keys, spawned):
    pytest.importorskip("mcp")
    from test_mcp import call, data, tools

    from veille_mcp.server import build_server
    srv = build_server()
    t = tools(srv)["check_component_statuses"]
    assert t.annotations.read_only_hint is False and t.annotations.open_world_hint is False
    out = data(call(srv, "check_component_statuses", {"slug": watch.slug}))
    assert out["started"] is True and spawned[0]["cmd"][-1] == "--status-only"
    assert "AUCUN" in tools(srv)["run_veille_now"].description


# --------------------------------------------------------------------------
# Rapports horodatés (AAAA-MM-JJ-HHhMM) : plusieurs exécutions le même jour ne s'écrasent plus
# --------------------------------------------------------------------------
def test_two_runs_the_same_day_keep_both_reports(watch, tools, monkeypatch):
    stamps = iter(["2026-10-09-09h05", "2026-10-09-14h30"])
    monkeypatch.setattr(store, "report_stamp", lambda: next(stamps))
    finalize(watch, [cand(["LM317T"])])
    finalize(watch, [])
    reps = store.list_reports(watch.slug)
    assert [(r["stamp"], r["date"], r["time"]) for r in reps] == [
        ("2026-10-09-14h30", "2026-10-09", "14:30"), ("2026-10-09-09h05", "2026-10-09", "09:05")]
    assert all(r["pdf"] == f"{r['stamp']}-veille.pdf" and r["md"] == f"{r['stamp']}-veille.md" for r in reps)
    args = tools()[-1]["args"]
    assert args[args.index("--attach") + 1].endswith("2026-10-09-14h30-veille.pdf")


def test_report_names_whitelist_and_legacy(env):
    import datetime as dt
    assert store.report_stamp(dt.datetime(2026, 10, 9, 7, 5)) == "2026-10-09-07h05"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{2}h\d{2}", store.report_stamp())
    ok = ["2026-10-09-14h30-veille.pdf", "2026-10-09-14h30-veille.md", "2026-10-09-veille.pdf"]
    bad = ["2026-10-09-14:30-veille.pdf", "2026-10-09-1430-veille.pdf", "../2026-10-09-veille.pdf",
           "2026-10-09-14h30-veille.pdf.exe", "2026-10-09-14h3-veille.pdf", "2026-10-09-14h30-charts.json"]
    assert all(store.REPORT_RE.match(n) for n in ok) and not any(store.REPORT_RE.match(n) for n in bad)
    v = service.create_veille(name="Veille", subject="", schedule_expr="0 8 * * 1",
                              recipients=["a@example.org"], brief="# b\n")
    rdir = v.dir / "rapports"
    for name in ("2026-10-08-veille.pdf", "2026-10-09-veille.pdf", "2026-10-09-08h00-veille.pdf",
                 "2026-10-09-08h00-veille.md", "2026-10-09-18h45-veille.pdf", "2026-10-09-18h45-charts.json"):
        (rdir / name).write_bytes(b"%PDF")
    reps = store.list_reports(v.slug)
    assert [r["stamp"] for r in reps] == ["2026-10-09-18h45", "2026-10-09-08h00", "2026-10-09", "2026-10-08"]
    assert reps[1]["md"] and reps[2]["time"] == "" and reps[2]["date"] == "2026-10-09"
    for name in bad:
        with pytest.raises(store.StoreError, match="nom de rapport invalide"):
            store.report_path(v.slug, name)


def test_prompts_fix_the_timestamp_once(watch):
    v = service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                              recipients=["a@example.org"], brief="# b\n", include_components=watch.slug)
    for x in (watch, v):
        prompt = (x.dir / "job-prompt.md").read_text()
        assert "`date +%Y-%m-%d-%Hh%M`" in prompt and "UNE SEULE FOIS" in prompt and "${" not in prompt
    assert f"--append {v.dir}/rapports/D-veille.md" in (v.dir / "job-prompt.md").read_text()


# --------------------------------------------------------------------------
# Export Excel (liste complète) et pièce jointe des e-mails
# --------------------------------------------------------------------------
def _sheet(data: bytes):
    from io import BytesIO

    from openpyxl import load_workbook
    return load_workbook(BytesIO(data))["Composants"]


def _sheet_xml(data: bytes) -> str:
    import zipfile
    from io import BytesIO
    with zipfile.ZipFile(BytesIO(data)) as z:
        return z.read("xl/worksheets/sheet1.xml").decode()


def test_xlsx_full_list_critical_first_and_safe_cells(watch, tools):
    """Toutes les références (y compris sans avis ou retirées), critiques en tête ; un texte venu du
    web n'est jamais une formule, les caractères de contrôle n'empêchent pas l'export."""
    service.components_scan(watch.slug, client=FakeClient({"LM317T": {"status": "Last Time Buy",
                                                                      "product_url": "https://www.digikey.com/p/1"}}))
    finalize(watch, [cand(["STM32F103C8T6"], ntype="PCN", url="https://st.com/pcn/1",
                          title='=HYPERLINK("http://evil/x","clic")\x02', ltb_date="2027-03-31"),
                     cand(["NE555P"], ntype="EOL", url="https://www.ti.com/eol/9", number="TI-9")])
    service.remove_components(watch.slug, ["NE555P"])  # retiré : ses avis restent, plus une alerte
    data = service.components_xlsx(watch.slug)
    ws = _sheet(data)
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0][:4] == ("Composant", "Fabricant", "Réf. interne", "Critique")
    assert [r[0] for r in rows[1:]] == ["LM317T", "NE555P", "STM32F103C8T6"]
    lm, ne, stm = rows[1:]
    assert lm[3] == "Oui" and lm[8] == "Last Time Buy" and lm[13] is None and lm[2] == "R-001"
    assert ne[3] is None and ne[13] == "retiré" and rows[0][10] == "Statut web" and lm[10] is None
    assert stm[5] == '=HYPERLINK("http://evil/x","clic")' and stm[6] == as_read(2027, 3, 31)
    assert ws.cell(4, 6).data_type == "s" and "<f>" not in _sheet_xml(data)  # texte, jamais formule
    assert ws.cell(4, 13).hyperlink.target == "https://st.com/pcn/1"
    assert ws.cell(2, 1).fill.fgColor.rgb.endswith("FEF2F2") and ws.freeze_panes == "A2" and ws.auto_filter.ref
    assert ws.page_setup.orientation == "landscape" and ws.sheet_properties.pageSetUpPr.fitToPage


def test_xlsx_unsafe_link_illegal_chars_and_odd_values():
    """Lien non http(s) jamais cliquable ; caractères interdits en XML (contrôles, U+FFFE/U+FFFF)
    retirés ; valeurs inattendues (None, nombre, « date » hors colonne de date) sans erreur."""
    comps = [{"mpn": "2026-01-01", "manufacturer": "X", "ref": 42,
              "dk": {"status": "Obsolete\x01\uffff", "checked_at": "2026-13-45",
                     "product_url": "javascript:alert(1)"}, "web_checked_at": "2026-10-01\n"}]
    data = comp.to_xlsx(comps, [])
    ws = _sheet(data)  # relu sans erreur : fichier bien formé
    assert [c.value for c in ws[2]][:3] == ["2026-01-01", "X", "42"] and ws.cell(2, 1).data_type == "s"
    assert ws.cell(2, 9).value == "Obsolete" and ws.cell(2, 10).value == "2026-13-45"  # date invalide : texte
    assert ws.cell(2, 12).value == "2026-10-01\n" and ws.cell(2, 12).data_type == "s"
    assert ws.cell(2, 13).value is None and ws.cell(2, 13).hyperlink is None
    assert _sheet(comp.to_xlsx([], [])).max_row == 1  # liste vide : en-tête seul


def as_read(*a):
    import datetime as dt
    return dt.datetime(*a)  # openpyxl relit les dates en datetime


def test_finalize_attaches_xlsx_snapshot(watch, tools):
    service.components_scan(watch.slug)
    finalize(watch, [cand(["LM317T"])])
    (mail,) = tools()
    args = mail["args"]
    attached = [args[i + 1] for i, a in enumerate(args) if a == "--attach"]
    assert len(attached) == 2 and attached[0].endswith("-veille.pdf")
    xlsx = Path(attached[1])
    stamp = store.list_reports(watch.slug)[0]["stamp"]
    assert xlsx.name == f"{stamp}-{watch.slug}.xlsx" and xlsx.parent == watch.dir / "rapports"
    assert [r[0] for r in _sheet(xlsx.read_bytes()).iter_rows(min_row=2, values_only=True)] == [
        "LM317T", "NE555P", "STM32F103C8T6"]
    assert len(store.list_reports(watch.slug)) == 1  # l'Excel ne se mêle pas à la liste des rapports
    assert "fichier Excel joint" in mail["html"] and "fichier Excel joint" in args[args.index("--text") + 1]
    # exécution suivante : seul le dernier export est gardé (sauvegardes bornées)
    old = xlsx.with_name(f"2020-01-01-{watch.slug}.xlsx")
    xlsx.rename(old)
    (watch.dir / "rapports" / f"2020-01-01-autre-{watch.slug}.xlsx").write_bytes(b"x")  # autre veille : intact
    finalize(watch, [])
    left = sorted(p.name for p in (watch.dir / "rapports").glob("*.xlsx"))
    assert old.name not in left and len(left) == 2 and f"2020-01-01-autre-{watch.slug}.xlsx" in left


def test_xlsx_failure_never_blocks_the_mail(watch, tools, monkeypatch):
    """L'Excel est une pièce jointe de confort : son échec est tracé, l'e-mail et le chapitre partent."""
    def boom(*a):
        raise TypeError("donnée inattendue")
    monkeypatch.setattr(comp, "to_xlsx", boom)
    finalize(watch, [cand(["LM317T"])])
    args = tools()[-1]["args"]
    assert args.count("--attach") == 1 and "Excel" not in tools()[-1]["html"]
    assert comp.pending(comp.load_notices(watch.slug), "mail") == []  # livré
    assert any(a["action"] == "components-xlsx" and "donnée inattendue" in a["detail"] for a in service.read_audit())
    v = service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                              recipients=["a@example.org"], brief="# b\n", include_components=watch.slug)
    md = v.dir / "rapports" / "2026-10-12-veille.md"
    md.write_text("# r\n")
    assert service.components_chapter(watch.slug, v.slug, md)[1] is None
    assert cli.main(["components", "chapter", watch.slug, "--for", v.slug, "--append", str(md)]) == 0


def test_chapter_writes_xlsx_and_prompt_attaches_it(watch, tools, capsys):
    v = service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                              recipients=["a@example.org"], brief="# b\n", include_components=watch.slug)
    finalize(watch, [cand(["LM317T"])])
    md = v.dir / "rapports" / "2026-10-12-06h30-veille.md"
    md.write_text("# r\n")
    prefix, xlsx = service.components_chapter(watch.slug, v.slug, md)
    assert prefix == "[EOL ×1]" and xlsx == v.dir / "rapports" / f"2026-10-12-06h30-{watch.slug}.xlsx"
    assert _sheet(xlsx.read_bytes()).max_row == 4
    assert cli.main(["components", "chapter", watch.slug, "--for", v.slug, "--append", str(md)]) == 0
    assert f"PIECE_JOINTE={xlsx}" in capsys.readouterr().out  # relance : même fichier, remplacé
    assert sorted(p.name for p in (v.dir / "rapports").glob("*.xlsx")) == [xlsx.name]
    assert not list((watch.dir / "rapports").glob("*.xlsx"))  # rattachée : rien chez la veille composants
    prompt = (v.dir / "job-prompt.md").read_text()
    assert "PIECE_JOINTE=<chemin>" in prompt and "--attach <chemin>" in prompt


def test_ui_export_xlsx(ui, watch):
    client, _ = ui
    page = client.get(f"/veilles/{watch.slug}").text
    assert f'href="/veilles/{watch.slug}/components.xlsx" download' in page
    r = client.get(f"/veilles/{watch.slug}/components.xlsx")
    assert r.status_code == 200 and r.headers["content-type"] == comp.XLSX_TYPE
    assert re.fullmatch(rf'attachment; filename="\d{{4}}-\d{{2}}-\d{{2}}-{watch.slug}\.xlsx"',
                        r.headers["content-disposition"])
    assert r.headers["cache-control"] == "no-store" and _sheet(r.content).max_row == 4
    other = service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                                  recipients=["a@example.org"], brief="# b\n")
    assert client.get(f"/veilles/{other.slug}/components.xlsx").status_code == 404
    assert client.get("/veilles/inconnue/components.xlsx").status_code == 404
    (watch.dir / "notices.json").write_text("{oups")
    assert client.get(f"/veilles/{watch.slug}/components.xlsx").status_code == 500
    client.cookies.clear()
    assert client.get(f"/veilles/{watch.slug}/components.xlsx", follow_redirects=False).status_code == 303


def test_ui_run_end_updates_head_without_reload(ui, watch, monkeypatch):
    """Le suivi du journal (`poll=1`) renvoie l'en-tête à jour (hors bande) quand le lancement se termine."""
    client, _ = ui
    log = "20261009-120000.log"
    store.runs_dir(watch.slug).mkdir(parents=True, exist_ok=True)
    (store.runs_dir(watch.slug) / log).write_text("analyse…\n")
    running = {"pid": 4242}
    monkeypatch.setattr(service, "running_pid", lambda slug: running["pid"])
    page = client.get(f"/veilles/{watch.slug}").text
    assert 'id="detail-head"' in page and ">en cours</span>" in page and "hx-swap-oob" not in page
    assert f'runs/{log}?poll=1" hx-trigger="load"' in page  # course : fini avant le premier chargement
    url = f"/veilles/{watch.slug}/runs/{log}"
    r = client.get(url, params={"poll": 1}).text
    assert f'hx-get="{url}?poll=1"' in r and "detail-head" not in r  # en cours : journal seul
    running["pid"] = None
    r = client.get(url, params={"poll": 1}).text
    head = r[r.index('id="detail-head"'):]
    assert 'hx-swap-oob="true"' in head and ">actif</span>" in head and "disabled" not in head
    assert "hx-get" not in r[:r.index('id="detail-head"')]  # le suivi s'arrête
    assert "detail-head" not in client.get(url).text  # journal ouvert seul : pas d'en-tête
    assert "recharger</a> pour voir le nouveau rapport" in r
    page = client.get(f"/veilles/{watch.slug}").text  # page rendue « actif » : pas de suivi de fin
    assert f'hx-get="{url}" hx-trigger="load"' in page
    # veille archivée pendant le suivi : 286, htmx arrête d'interroger
    service.archive(watch.slug)
    assert client.get(url, params={"poll": 1}).status_code == 286
    assert client.get(url).status_code == 404
