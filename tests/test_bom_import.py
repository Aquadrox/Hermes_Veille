"""Import de nomenclature (CSV / Excel) : lecture bornée, reconnaissance des colonnes par règles,
proposition modifiable, puis application dans l'interface, la CLI et le MCP."""
import io
import json
import os
import re
import zipfile

import pytest
from openpyxl import Workbook

from veille_core import bom_import, cli, paths, service
from veille_core import components as comp


def xlsx(*sheets) -> bytes:
    """xlsx en mémoire : sheets = (nom, lignes)."""
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets:
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


BOM_ROWS = [
    ["Nomenclature carte X — rév. B"],
    [],
    ["Repère", "Qté", "Désignation", "Fabricant", "Mfr. P/N", "Digi-Key Part Number", "Code article"],
    ["R1", 2, "Résistance", "Yageo", "RC0603FR-0710KL", "311-10.0KHRCT-ND", 100234],
    ["U1", 1, "Régulateur", "Texas Instruments", "LM317T", "296-LM317T-ND", 100235],
    ["U2", 1, "MCU", "STMicroelectronics", "STM32F103C8T6", "497-X-ND", 100236],
    [None, None, None, None, None, None, None],
    ["C9", 1, "Condensateur", None, "GRM188R71C104KA01D", None, 100237],
    ["U3", 1, "Doublon", "Texas Instruments", "LM317T", None, 100299],
]


def propose(data, filename="bom.xlsx", **kw):
    return bom_import.propose_options(bom_import.read_table(data, filename), **kw)


# --------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------
@pytest.mark.parametrize("sep", [";", ",", "\t"])
def test_csv_separators(sep):
    text = sep.join(["Manufacturer Part Number", "Manufacturer", "Internal P/N"]) + "\n" \
        + sep.join(["LM317T", "Texas Instruments", "R-1"]) + "\n"
    p = propose(text.encode(), "bom.csv")
    assert p["components"] == [{"mpn": "LM317T", "manufacturer": "Texas Instruments", "ref": "R-1"}]


def test_csv_encodings():
    text = "Référence fabricant;Fabricant;Réf. interne\nLM317T;Société Générale Électronique;R-é\n"
    for data in (text.encode("utf-8-sig"), text.encode("cp1252")):
        p = propose(data, "bom.csv")
        assert p["components"][0]["manufacturer"] == "Société Générale Électronique"
        assert p["mapping"]["mpn"]["confidence"] == "exacte"


def test_xlsx_title_rows_numbers_and_distributor_column():
    p = propose(xlsx(("BOM", BOM_ROWS)))
    assert p["header_row"] == 2  # lignes de titre ignorées
    cols = {f: p["columns"][m["col"]] for f, m in p["mapping"].items()}
    assert cols == {"mpn": "Mfr. P/N", "manufacturer": "Fabricant", "ref": "Code article"}  # pas Digi-Key
    assert p["components"][0] == {"mpn": "RC0603FR-0710KL", "manufacturer": "Yageo", "ref": "100234"}  # pas 100234.0
    assert len(p["components"]) == 3  # doublon LM317T fusionné
    assert p["rejected"] == [{"line": 8, "reason": "fabricant manquant ou invalide"}]


def test_xlsx_sheets_formulas_and_default_manufacturer():
    data = xlsx(("Vide", []), ("Liste", [["MPN", "Qté"], ["LM317T", "=1+1"], ["NE555P", 3]]))
    table = bom_import.read_table(data, "b.xlsx")
    assert table["sheets"] == ["Vide", "Liste"] and table["sheet"] == "Liste"  # première non vide
    p = bom_import.propose_options(table)
    assert p["error"].startswith("choisir la colonne du fabricant")
    p = bom_import.propose_options(table, default_manufacturer="Texas Instruments")
    assert [c["mpn"] for c in p["components"]] == ["LM317T", "NE555P"]
    with pytest.raises(bom_import.BomImportError, match="feuille inconnue"):
        bom_import.read_table(data, "b.xlsx", "Absente")
    with pytest.raises(bom_import.BomImportError, match="aucune donnée"):
        bom_import.read_table(data, "b.xlsx", "Vide")


def test_positional_fallback_keeps_legacy_files():
    p = propose(b"LM317T;Texas Instruments;R-1\nNE555P;Texas Instruments\n", "bom.txt")
    assert p["header_row"] is None
    assert [c["mpn"] for c in p["components"]] == ["LM317T", "NE555P"]
    p = propose(b"MPN;Fabricant;Ref\nLM317T;TI;R-1\n", "bom.csv")
    assert p["components"] == [{"mpn": "LM317T", "manufacturer": "TI", "ref": "R-1"}]


def test_value_based_mpn_guess_and_user_override():
    data = b"Reference;Marque;Commentaire\nLM317T;TI;x\nNE555P;TI;y\nSTM32F103C8T6;ST;z\n"
    p = propose(data, "b.csv")
    assert p["mapping"]["mpn"] == {"col": 0, "confidence": "probable"}  # forme des valeurs
    p = propose(data, "b.csv", columns={"mpn": "A", "manufacturer": "Marque", "ref": "3"})
    assert [p["mapping"][f]["confidence"] for f in ("mpn", "manufacturer", "ref")] == ["choisie"] * 3
    assert p["components"][0] == {"mpn": "LM317T", "manufacturer": "TI", "ref": "x"}
    for bad in ({"mpn": "Z"}, {"mpn": "inconnue"}, {"mpn": "99"}):
        with pytest.raises(bom_import.BomImportError, match="colonne"):
            propose(data, "b.csv", columns=bad)
    with pytest.raises(bom_import.BomImportError, match="ligne d'en-tête"):
        propose(data, "b.csv", header="abc")
    p = propose(data, "b.csv", header=0, columns={"mpn": "A", "manufacturer": "B", "ref": "0"})  # pas d'en-tête
    assert p["components"][0] == {"mpn": "Reference", "manufacturer": "Marque", "ref": ""}  # ligne 1 = donnée
    assert len(p["components"]) == 4 and p["mapping"]["ref"]["col"] is None


@pytest.mark.parametrize("label,field", [
    ("MPN", "mpn"), ("Manufacturer Part Number", "mpn"), ("Mfr PN", "mpn"), ("Réf. fabricant", "mpn"),
    ("Part Number", "mpn"), ("Fabricant", "manufacturer"), ("Marque", "manufacturer"), ("MFR", "manufacturer"),
    ("Réf. interne", "ref"), ("Code article", "ref"), ("Item #", "ref"), ("Internal Part Number", "ref"),
])
def test_synonyms(label, field):
    assert {f for f in bom_import.FIELDS if bom_import._match(label, f)} == {field}


@pytest.mark.parametrize("label", ["Digi-Key Part Number", "Mouser #", "Supplier Part Number", "Farnell code",
                                   "Désignation", "Quantité", "Référence"])
def test_ambiguous_or_distributor_labels_never_mapped(label):
    assert not any(bom_import._match(label, f) for f in bom_import.FIELDS)


@pytest.mark.parametrize("filename,match", [("bom.xls", "non pris en charge"), ("bom.ods", "non pris en charge"),
                                            ("bom.pdf", "format attendu")])
def test_unsupported_formats(filename, match):
    with pytest.raises(bom_import.BomImportError, match=match):
        bom_import.read_table(b"x", filename)


def test_column_names():
    assert [bom_import.column_name(i) for i in (0, 25, 26, 27, 701)] == ["A", "Z", "AA", "AB", "ZZ"]


# --------------------------------------------------------------------------
# Sécurité
# --------------------------------------------------------------------------
def test_size_and_row_limits(monkeypatch):
    with pytest.raises(bom_import.BomImportError, match="trop volumineux"):
        bom_import.read_table(b"x" * (bom_import.MAX_BYTES + 1), "b.csv")
    monkeypatch.setattr(bom_import, "MAX_ROWS", 3)
    with pytest.raises(bom_import.BomImportError, match="trop long"):
        bom_import.read_table(b"a;b\n" * 5, "b.csv")
    # Feuille « étirée » : des milliers de lignes vides ne comptent pas
    monkeypatch.setattr(bom_import, "MAX_EMPTY_RUN", 5)
    table = bom_import.read_table(b"MPN;Fabricant\nLM317T;TI\n" + b";\n" * 50 + b"NE555P;TI\n", "b.csv")
    assert len(table["rows"]) == 2


def test_cells_and_columns_bounded(monkeypatch):
    monkeypatch.setattr(bom_import, "MAX_COLS", 2)
    table = bom_import.read_table(("MPN;Fabricant;X\n" + "A" * 500 + ";TI;Z\n").encode(), "b.csv")
    assert table["rows"][1] == ["A" * bom_import.MAX_CELL, "TI"]


def test_zip_bomb_rejected(monkeypatch):
    data = xlsx(("BOM", BOM_ROWS))
    monkeypatch.setattr(bom_import, "MAX_UNZIPPED", 1000)
    with pytest.raises(bom_import.BomImportError, match="trop volumineux"):
        bom_import.read_table(data, "b.xlsx")
    with pytest.raises(bom_import.BomImportError, match="illisible"):
        bom_import.read_table(b"PK\x03\x04 pas une archive", "b.xlsx")


def _rewrite_sheet(data: bytes, transform) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for item in src.infolist():
            raw = src.read(item.filename)
            z.writestr(item, transform(raw.decode()).encode() if item.filename == "xl/worksheets/sheet1.xml" else raw)
    return out.getvalue()


def test_billion_laughs_blocked_by_defusedxml():
    """Entités XML imbriquées dans la feuille : refusé par defusedxml, jamais développé en mémoire."""
    from openpyxl import load_workbook
    base = xlsx(("BOM", [["MPN", "Fabricant"], ["LM317T", "TI"]]))
    assert bom_import.read_table(_rewrite_sheet(base, lambda x: x), "b.xlsx")["rows"][1] == ["LM317T", "TI"]
    ents = '<!ENTITY lol "lol">' + "".join(
        f'<!ENTITY lol{i} "{("&lol" + (str(i - 1) if i > 1 else "") + ";") * 10}">' for i in range(1, 9))
    evil = _rewrite_sheet(base, lambda x: f'<?xml version="1.0"?><!DOCTYPE worksheet [{ents}]>'
                          + x.replace("<t>LM317T</t>", "<t>&lol8;</t>"))
    with pytest.raises(bom_import.BomImportError, match="illisible|refusé"):
        bom_import.read_table(evil, "b.xlsx")
    with pytest.raises(ValueError) as exc:  # la cause est bien defusedxml (openpyxl l'utilise)
        load_workbook(io.BytesIO(evil), read_only=True)
    assert type(exc.value.__context__).__name__ == "EntitiesForbidden"


def test_import_path_confined(env, tmp_path):
    root = paths.imports_dir()
    root.mkdir()
    (root / "bom carte v3.xlsx").write_bytes(b"x")
    assert bom_import.import_path("bom carte v3.xlsx") == (root / "bom carte v3.xlsx").resolve()
    outside = tmp_path / "secret.csv"
    outside.write_text("x")
    (root / "lien.csv").symlink_to(outside)
    for bad in ("../secret.csv", str(outside), "lien.csv", ".ui-0123.json", "absent.csv", "a/b.csv", "x.json", 3):
        with pytest.raises(bom_import.BomImportError):
            bom_import.import_path(bad)


def test_stash_bound_to_session_and_expiring(env, monkeypatch):
    token = bom_import.stash(b"MPN;Fabricant\nLM317T;TI\n", "bom.csv", "session-a")
    f = paths.imports_dir() / f".ui-{token}.json"
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert bom_import.unstash(token, "session-a") == ("bom.csv", b"MPN;Fabricant\nLM317T;TI\n")
    for tok, sess in ((token, "session-b"), ("../../etc/passwd", "session-a"), ("ab" * 16, "session-a")):
        with pytest.raises(bom_import.BomImportError, match="expirée"):
            bom_import.unstash(tok, sess)
    os.utime(f, (1, 1))  # vieux de plus d'une heure
    with pytest.raises(bom_import.BomImportError, match="expirée"):
        bom_import.unstash(token, "session-a")
    bom_import.stash(b"x", "y.csv", "session-a")  # chaque dépôt purge les anciens
    assert not f.exists()


# --------------------------------------------------------------------------
# Liste combinée et service
# --------------------------------------------------------------------------
def test_combine_modes():
    cur = comp.parse_components("A1;X\nB2;Y")
    new = comp.parse_components("B2;Y\nC3;Z")
    merged, st = comp.combine(cur, new, "add")
    assert [c["mpn"] for c in merged] == ["A1", "B2", "C3"] and st == {"added": 1, "existing": 1, "removed": 0}
    merged, st = comp.combine(cur, new, "replace")
    assert [c["mpn"] for c in merged] == ["B2", "C3"] and st == {"added": 1, "existing": 1, "removed": 1}
    with pytest.raises(comp.ComponentsError, match="mode d'import"):
        comp.combine(cur, new, "merge")


@pytest.fixture
def watch(env):
    return service.create_veille(name="BOM", subject="", schedule_expr=service.COMPONENTS_SCHEDULE,
                                 recipients=["a@example.org"], brief="# b\n", kind="composants",
                                 components=comp.parse_components("LM317T;Texas Instruments;OLD\nBC547;onsemi"))


def test_service_import_add_and_replace_keeps_state(watch):
    data = comp.load_components(watch.slug)
    data["components"][0]["web_checked_at"] = "2026-01-01"
    comp.save_components(watch.slug, data)
    items = comp.parse_components("LM317T;Texas Instruments;NEW\nNE555P;Texas Instruments")
    assert service.import_components(watch.slug, items, "add") == {"added": 1, "existing": 1, "removed": 0, "total": 3}
    st = service.import_components(watch.slug, items, "replace")
    assert st == {"added": 0, "existing": 2, "removed": 1, "total": 2}
    cs = comp.load_components(watch.slug)["components"]
    assert [c["mpn"] for c in cs] == ["LM317T", "NE555P"] and cs[0]["web_checked_at"] == "2026-01-01"
    assert cs[0]["ref"] == "NEW"  # remplacement : la réf. du fichier fait foi
    # remplacement à taille égale : bien enregistré (régression : seul le nombre était comparé)
    service.import_components(watch.slug, comp.parse_components("X1;Y\nX2;Y"), "replace")
    assert [c["mpn"] for c in comp.load_components(watch.slug)["components"]] == ["X1", "X2"]
    assert "components-import-replace" in json.dumps(service.read_audit())


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def test_cli_import(watch, tmp_path, capsys):
    f = tmp_path / "bom.xlsx"
    f.write_bytes(xlsx(("BOM", BOM_ROWS)))
    assert cli.main(["components", "import", watch.slug, str(f), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "MPN (référence fabricant)" in out and "E « Mfr. P/N »" in out and "ligne 8 :" in out
    assert "simulation" in out and len(comp.load_components(watch.slug)["components"]) == 2
    assert cli.main(["components", "import", watch.slug, str(f), "--default-manufacturer", "Murata"]) == 0
    assert "3 ajouté(s), 1 déjà présent(s), 0 retiré(s) — 5 composant(s)" in capsys.readouterr().out
    assert cli.main(["components", "import", watch.slug, str(f), "--replace", "--mpn", "E",
                     "--manufacturer", "Fabricant", "--ref", "0", "--default-manufacturer", "Murata"]) == 0
    assert "1 retiré(s)" in capsys.readouterr().out  # BC547 absent du fichier
    assert cli.main(["components", "import", watch.slug, str(f), "--mpn", "Désignation"]) == 0
    assert cli.main(["components", "import", watch.slug, str(f), "--manufacturer", "", "--dry-run"]) == 1
    assert cli.main(["components", "import", watch.slug]) == 1
    assert cli.main(["components", "import", watch.slug, str(tmp_path / "absent.xlsx")]) == 1


def test_cli_components_file_accepts_excel(env, tmp_path):
    good = tmp_path / "bom.xlsx"
    good.write_bytes(xlsx(("BOM", [["Mfr. P/N", "Fabricant"], ["LM317T", "TI"], ["NE555P", "TI"]])))
    assert cli.main(["create", "--kind", "composants", "--name", "BOM Y", "--schedule", "0 23 * * 0",
                     "--recipients", "a@example.org", "--components-file", str(good)]) == 0
    assert len(comp.load_components("bom-y")["components"]) == 2
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(xlsx(("BOM", BOM_ROWS)))  # ligne 8 sans fabricant : refus explicite
    assert cli.main(["edit", "bom-y", "--components-file", str(bad)]) == 1
    assert len(comp.load_components("bom-y")["components"]) == 2


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
@pytest.fixture
def ui(env, monkeypatch):
    from fastapi.testclient import TestClient
    from test_ui import PASSWORD, login

    from veille_ui import auth
    monkeypatch.setenv("VEILLE_UI_PASSWORD_HASH", auth.hash_password(PASSWORD))
    monkeypatch.setenv("VEILLE_UI_SECRET", "s" * 64)
    auth._failures.clear()
    from veille_ui.app import create_app
    client = TestClient(create_app(), base_url="http://localhost")
    csrf = login(client)
    return client, csrf, {"X-CSRF-Token": csrf, "HX-Request": "true"}


def upload(client, headers, data=None, name="bom.xlsx"):
    r = client.post("/fragments/import", files={"import_file": (name, data or xlsx(("BOM", BOM_ROWS)))},
                    headers=headers)
    token = re.search(r'name="import_token" value="([0-9a-f]{32})"', r.text)
    return r, token.group(1) if token else None


def test_ui_upload_proposal_and_csp(ui):
    client, _, h = ui
    form = client.get("/veilles/new?kind=composants").text
    assert 'name="import_file"' in form and 'id="import-panel"' in form and 'id="components-field"' in form
    r, token = upload(client, h)
    assert r.status_code == 200 and token
    t = r.text
    assert "3 composants valides" in t and "l. 8 : fabricant manquant" in t
    assert re.search(r'name="import_mpn">.*?<option value="5" selected>E — Mfr\. P/N', t, re.S)
    assert re.search(r'<option value="3" selected>Ligne 3', t) and "style=" not in t
    assert "Repère | Qté" in t  # choix de la ligne d'en-tête lisible


def test_ui_upload_errors_and_csrf(ui):
    client, _, h = ui
    r = client.post("/fragments/import", files={"import_file": ("bom.xlsx", b"x")})
    assert r.status_code == 403  # CSRF exigé
    r, token = upload(client, h, b"x", "bom.xls")
    assert r.status_code == 400 and token is None and "non pris en charge" in r.text
    r = client.post("/fragments/import", data={"x": "1"}, headers=h)
    assert r.status_code == 400 and "Choisir un fichier" in r.text
    r = client.post("/fragments/import/preview", data={"import_token": "f" * 32}, headers=h)
    assert r.status_code == 400 and "expirée" in r.text


def test_ui_preview_remap_and_sheet_change(ui):
    client, _, h = ui
    data = xlsx(("BOM", BOM_ROWS), ("Autre", [["MPN", "Marque"], ["NE555P", "TI"]]))
    _, token = upload(client, h, data)
    base = {"import_token": token, "import_sheet": "BOM", "import_header": "3", "import_mpn": "5",
            "import_manufacturer": "4", "import_ref": "7"}
    r = client.post("/fragments/import/preview", data={**base, "import_default_mfr": "Murata"}, headers=h)
    assert "4 composants valides" in r.text and "badge ok\">choisie" in r.text
    r = client.post("/fragments/import/preview", data={**base, "import_sheet": "Autre"},
                    headers={**h, "HX-Trigger-Name": "import_sheet"})
    assert "1 composant valide" in r.text and 'selected>Autre' in r.text  # détection relancée
    r = client.post("/fragments/import/preview", data={**base, "import_mpn": ""}, headers=h)
    assert "choisir la colonne du MPN" in r.text


def test_ui_apply_add_replace_then_create(ui):
    client, csrf, h = ui
    _, token = upload(client, h)
    base = {"import_token": token, "import_sheet": "BOM", "import_header": "3", "import_mpn": "5",
            "import_manufacturer": "4", "import_ref": "7", "import_default_mfr": "Murata"}
    r = client.post("/fragments/import/apply", data={**base, "import_mode": "add",
                                                     "components": "LM317T ; Texas Instruments ; OLD\nBC547;onsemi"},
                    headers=h)
    text = re.search(r"<textarea[^>]*>(.*?)</textarea>", r.text, re.S).group(1)
    assert text.splitlines()[:2] == ["LM317T ; Texas Instruments ; OLD", "BC547 ; onsemi"]
    assert "3 ajouté(s), 1 déjà présent(s)" in r.text
    r = client.post("/fragments/import/apply", data={**base, "import_mode": "replace", "components": text}, headers=h)
    replaced = re.search(r"<textarea[^>]*>(.*?)</textarea>", r.text, re.S).group(1)
    assert "BC547" not in replaced and "1 retiré(s)" in r.text
    r = client.post("/fragments/import/apply", data={**base, "components": "ligne invalide"}, headers=h)
    assert r.status_code == 400 and "ligne invalide" in r.text  # la saisie de l'utilisateur est conservée
    from test_ui import create_form
    r = client.post("/veilles/new", data=create_form(csrf, name="BOM Z", kind="composants", components=replaced,
                                                     freq_kind="weekly", dow="0", time="23:00"), follow_redirects=False)
    assert r.status_code == 303 and len(comp.load_components("bom-z")["components"]) == 4


# --------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------
def test_mcp_preview_and_import(watch, monkeypatch, tmp_path):
    pytest.importorskip("mcp")
    from test_mcp import call, data, tools

    from veille_mcp.server import build_server
    srv = build_server()
    t = tools(srv)
    assert t["preview_component_import"].annotations.read_only_hint is True
    assert t["import_components"].annotations.read_only_hint is False
    monkeypatch.setenv("VEILLE_HOST_DATA", "/srv/veille-hermes/data")
    r = call(srv, "preview_component_import", {"filename": "bom.xlsx"})
    assert r.is_error and "/srv/veille-hermes/data/imports" in r.content[0].text
    paths.imports_dir().mkdir()
    (paths.imports_dir() / "bom.xlsx").write_bytes(xlsx(("BOM", BOM_ROWS)))
    p = data(call(srv, "preview_component_import", {"filename": "bom.xlsx"}))
    assert p["mapping"]["mpn"] == {"column": "E : Mfr. P/N", "confidence": "exacte"}
    assert p["valid"] == 3 and p["rejected_count"] == 1 and p["header_row"] == 3
    out = data(call(srv, "import_components", {"slug": watch.slug, "filename": "bom.xlsx",
                                               "default_manufacturer": "Murata"}))
    assert out == {"slug": watch.slug, "added": 3, "existing": 1, "removed": 0, "total": 5, "ignored_rows": 0}
    r = call(srv, "import_components", {"slug": watch.slug, "filename": "bom.xlsx", "mpn_column": ""})
    assert r.is_error and "MPN" in r.content[0].text
    r = call(srv, "preview_component_import", {"filename": "../veilles/.audit.log"})
    assert r.is_error and "nom de fichier invalide" in r.content[0].text

