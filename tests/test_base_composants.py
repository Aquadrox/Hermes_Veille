"""Format « MPN ; Fabricant » (réf. interne retirée en 0.5.0, anciennes listes et anciens fichiers acceptés) et
base des composants consultable dans l'interface (page « Composants »). Réseau, PDF et e-mail simulés."""
import json
import re
import sqlite3
from urllib.parse import quote

import pytest
from test_catalog import _client, known, new_watch  # noqa: F401 - fixtures partagées
from test_components import FakeClient, _sheet, cand, finalize, tools, watch  # noqa: F401

from veille_core import bom_import, catalog, cli, db, paths, service
from veille_core import components as comp

LM = comp.key_of("LM317T", "Texas Instruments")


# --------------------------------------------------------------------------
# « MPN ; Fabricant » : la réf. interne n'est plus demandée, les anciennes listes restent valables
# --------------------------------------------------------------------------
@pytest.mark.parametrize("line", ["LM317T ; Texas Instruments ; R-001", "LM317T\tTexas Instruments\tR-001\tnote",
                                  "LM317T,Texas Instruments,R-001", "LM317T ; Texas Instruments ;",
                                  "LM317T;Texas Instruments"])
def test_old_and_new_list_formats_give_the_same_entry(line):
    assert comp.parse_components(line) == [{"mpn": "LM317T", "manufacturer": "Texas Instruments"}]


def test_list_is_written_back_in_the_new_format():
    text = "MPN;Fabricant;Réf. interne\nLM317T;Texas Instruments;R-001\nNE555P ; TI"
    assert comp.format_components(comp.parse_components(text)) == "LM317T ; Texas Instruments\nNE555P ; TI"
    with pytest.raises(comp.ComponentsError, match="format attendu « MPN ; Fabricant » : ligne 2"):
        comp.parse_components("LM317T;TI\nsans-fabricant")


def test_watch_saved_before_050_keeps_working(known, monkeypatch, capsys):  # noqa: F811
    """`components.json` d'avant la 0.5.0 (réf. internes enregistrées) : lu, affiché et exporté sans elles ;
    ré-enregistrer la même liste (ancien format compris) ne réécrit rien ; l'agent ne les reçoit plus."""
    path = known.dir / "components.json"
    data = json.loads(path.read_text())
    for i, c in enumerate(data["components"]):
        c["ref"] = f"REF-{i:03d}"
    path.write_text(json.dumps(data))
    before = path.read_text()
    service.update_veille(known.slug, components=comp.parse_components(
        "LM317T;Texas Instruments;AUTRE\nSTM32F103C8T6;STMicroelectronics\nNE555P;Texas Instruments"))
    assert path.read_text() == before  # rien à enregistrer : ni migration ni réécriture
    client, _ = _client(monkeypatch)
    page, form = client.get(f"/veilles/{known.slug}").text, client.get(f"/veilles/{known.slug}/edit").text
    assert "REF-0" not in page + form and "LM317T ; Texas Instruments\nSTM32F103C8T6" in form
    rows = list(_sheet(service.components_xlsx(known.slug)).iter_rows(values_only=True))
    assert not any("REF-0" in str(v) for row in rows for v in row)
    out = service.components_scan(known.slug, client=FakeClient({}))
    assert out["batch"] and all("ref" not in item for item in out["batch"])  # moins de tokens par lot
    assert cli.main(["components", "list", known.slug]) == 0 and "REF-0" not in capsys.readouterr().out


def test_internal_reference_column_is_never_taken_for_the_mpn():
    """La réf. interne n'est plus importée, mais sa colonne reste reconnue : quand le MPN n'a pas de libellé
    connu (repli sur la forme des valeurs), « R-001 » n'est jamais pris pour un MPN."""
    data = b"Code article;Composant;Fabricant\nR-001;LM317T;TI\nR-002;NE555P;TI\nR-003;BC547B;onsemi\n"
    p = bom_import.propose_options(bom_import.read_table(data, "b.csv"))
    assert p["header_row"] == 0 and set(p["mapping"]) == {"mpn", "manufacturer"}
    assert p["mapping"]["mpn"] == {"col": 1, "confidence": "probable"}
    assert p["components"] == [{"mpn": "LM317T", "manufacturer": "TI"}, {"mpn": "NE555P", "manufacturer": "TI"},
                               {"mpn": "BC547B", "manufacturer": "onsemi"}]


def test_mcp_without_internal_reference(known):  # noqa: F811
    pytest.importorskip("mcp")
    from test_mcp import call, data
    from test_mcp import tools as mcp_tools

    from veille_mcp.server import build_server
    srv = build_server()
    t = mcp_tools(srv)
    assert all("ref_column" not in t[n].input_schema["properties"] for n in ("preview_component_import",
                                                                             "import_components"))
    assert all("ref" not in r for r in data(call(srv, "list_components", {"slug": known.slug})))
    # agent resté sur l'ancien schéma : `ref_column` est ignoré, l'import se fait
    paths.imports_dir().mkdir(parents=True, exist_ok=True)
    (paths.imports_dir() / "bom.csv").write_text("MPN;Fabricant;Réf. interne\nBC547;onsemi;R-9\n")
    out = data(call(srv, "import_components", {"slug": known.slug, "filename": "bom.csv", "ref_column": "C"}))
    assert out["added"] == 1 and comp.load_components(known.slug)["components"][-1]["mpn"] == "BC547"


# --------------------------------------------------------------------------
# Base des composants : lecture
# --------------------------------------------------------------------------
def _part(mpn: str, status: str, day: str) -> dict:
    return {"mpn": mpn, "manufacturer": "Texas Instruments", "dk": {"status": status, "checked_at": day},
            "web_checked_at": day}


def _notice(c: dict, found: str) -> dict:
    n = comp.status_notice(c, "OBSOLETE", "Statut Digi-Key : Obsolete", "https://www.digikey.com/p/1", "Digi-Key",
                           "digikey")
    return comp.merge([], [n], found)[0]


def test_browse_and_rows_merge_every_watch(env):
    """Une ligne par référence, toutes veilles confondues : statut le plus récent (quel que soit l'ordre des
    veilles), avis commun une seule fois à sa première détection, veilles d'origine ; une référence jamais
    vérifiée n'y est pas."""
    lm_b, lm_a = _part("LM317T", "Obsolete", "2026-10-01"), _part("LM317T", "Active", "2026-09-01")
    catalog.sync("carte-b", [lm_b], [_notice(lm_b, "2026-10-01")])
    catalog.sync("carte-a", [lm_a, _part("NE555P", "Active", "2026-09-01")], [_notice(lm_a, "2026-09-15")])
    catalog.sync("carte-c", [{"mpn": "BC547", "manufacturer": "onsemi"}], [])
    known_ = catalog.browse()
    assert set(known_) == {LM, comp.key_of("NE555P", "Texas Instruments")}
    assert known_[LM]["sources"] == {"carte-a", "carte-b"} and known_[LM]["dk"]["status"] == "Obsolete"
    assert (known_[LM]["mpn"], known_[LM]["manufacturer"], len(known_[LM]["notices"])) == ("LM317T",
                                                                                          "Texas Instruments", 2)
    assert set(catalog.browse(LM)) == {LM} and catalog.browse("inconnue|X") == {}
    rows = {r["mpn"]: r for r in service.catalog_rows()}
    assert set(rows) == {"LM317T", "NE555P"} and rows["LM317T"]["watches"] == ["carte-a", "carte-b"]
    assert len(rows["LM317T"]["notices"]) == 1 and rows["LM317T"]["found"] == "2026-09-15"
    assert rows["LM317T"]["critical"] and comp.status_label(rows["LM317T"]) == "Obsolète"
    assert rows["NE555P"]["notices"] == [] and rows["NE555P"]["watches"] == ["carte-a"]
    assert [r["mpn"] for r in service.catalog_rows(LM)] == ["LM317T"]


def test_base_unavailable_is_reported_never_a_crash(known, monkeypatch):  # noqa: F811
    client, _ = _client(monkeypatch)

    def broken(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(db, "connect", broken)
    r = client.get("/composants")
    assert r.status_code == 503 and "momentanément indisponible" in r.text
    assert client.get("/composants/detail", params={"key": LM}).status_code == 503
    assert client.get("/composants.xlsx").status_code == 503
    assert service.catalog_rows() is None and service.catalog_xlsx() is None
    assert "db-error" not in json.dumps(service.read_audit())  # lecture d'affichage : journal non inondé


# --------------------------------------------------------------------------
# Base des composants : interface
# --------------------------------------------------------------------------
@pytest.fixture
def base(known, tools):  # noqa: F811
    """Deux veilles qui partagent LM317T ; la seconde, archivée, a seule BC547."""
    other = new_watch("Carte B", "LM317T;Texas Instruments\nBC547;onsemi")
    service.components_scan(other.slug, client=FakeClient({
        "LM317T": {"status": "Last Time Buy", "product_url": "https://www.digikey.com/p/1"},
        "BC547": {"status": "Obsolete", "product_url": None}}))
    return known, service.archive(other.slug).name


def test_page_lists_every_analysed_reference(base, monkeypatch):
    watch_, archived = base
    client, _ = _client(monkeypatch)
    t = client.get("/composants").text
    assert '<a href="/composants">Composants</a>' in t  # menu
    assert len(re.findall(r'<tr class="comp[^"]*"', t)) == 4  # LM317T (2 veilles), STM32, NE555P, BC547
    assert f'<option value="{archived}">carte-b (archivée le 20' in t
    assert f'<option value="{watch_.slug}">{watch_.slug}</option>' in t
    assert f'data-tags="{archived}|{watch_.slug}"' in t  # LM317T : ses deux veilles, filtre exact
    # statuts Digi-Key et web sous un même libellé : un seul choix par statut
    assert '<option value="Dernière commande">Dernière commande</option>' in t and '<option value="Obsolète">' in t
    assert '<option value="Obsolete">' not in t and 'data-q="Last Time Buy ' in t  # le terme Digi-Key reste cherchable
    assert f'data-detail="/composants/detail?key={quote(LM)}"' in t
    assert "style=" not in t and not re.search(r"<script(?![^>]*\bsrc=)", t)  # CSP : rien en ligne
    assert 'href="/composants"' in client.get("/health").text and 'href="/composants"' in client.get(
        f"/veilles/{watch_.slug}").text
    client.cookies.clear()
    assert client.get("/composants", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/composants/detail", params={"key": LM}, follow_redirects=False).status_code == 303


def test_detail_is_read_by_key(base, monkeypatch):
    watch_, archived = base
    client, _ = _client(monkeypatch)
    d = client.get("/composants/detail", params={"key": LM}, headers={"HX-Request": "true"})
    assert d.status_code == 200 and "Analysé par" in d.text and "Last Time Buy" in d.text
    assert f'<a href="/veilles/{watch_.slug}">{watch_.slug}</a>' in d.text and "carte-b (archivée le 20" in d.text
    for bad in ("", "inconnue|X", "x" * 201):
        assert client.get("/composants/detail", params={"key": bad}).status_code == 404


def test_export_names_the_watches_of_each_reference(base, monkeypatch):
    watch_, archived = base
    client, _ = _client(monkeypatch)
    r = client.get("/composants.xlsx")
    assert r.status_code == 200 and r.headers["content-type"] == comp.XLSX_TYPE
    assert re.fullmatch(r'attachment; filename="\d{4}-\d{2}-\d{2}-base-composants\.xlsx"',
                        r.headers["content-disposition"])
    rows = list(_sheet(r.content).iter_rows(values_only=True))
    assert rows[0][-1] == "Veilles" and len(rows[0]) == 15
    by = {row[0]: row for row in rows[1:]}
    label = catalog.archived_label(archived)  # comme dans l'interface
    assert label.startswith("carte-b (archivée le 20")
    assert by["LM317T"][-1] == f"{label}, {watch_.slug}" and by["BC547"][-1] == label


def test_empty_base(env, monkeypatch):
    client, _ = _client(monkeypatch)
    t = client.get("/composants").text
    assert "Aucune référence analysée" in t and "/composants.xlsx" not in t and "0 / 0" in t


def test_untrusted_texts_are_escaped(known, tools, monkeypatch):  # noqa: F811
    finalize(known, [cand(["NE555P"], ntype="EOL", url="https://www.ti.com/eol/555",
                          title="<img src=x onerror=alert(1)>", summary="<script>alert(1)</script>")])
    client, _ = _client(monkeypatch)
    t = client.get("/composants").text
    assert "<img src=x" not in t and "&lt;img src=x onerror=alert(1)&gt;" in t and "<script>alert" not in t
    d = client.get("/composants/detail", params={"key": comp.key_of("NE555P", "Texas Instruments")}).text
    assert "<script>alert" not in d and "&lt;script&gt;alert(1)&lt;/script&gt;" in d


@pytest.mark.parametrize("dk,web,label", [
    ({"status": "Obsolete"}, {}, "Obsolète"),  # libellé commun aux statuts Digi-Key et web
    ({"status": "Preliminary"}, {}, "Preliminary"),  # statut Digi-Key sans équivalent : tel quel
    ({"status": "Active"}, {"status": "OBSOLETE"}, "Actif"),  # Digi-Key la connaît : il fait foi
    # Digi-Key ne la trouve plus : la recherche web l'emporte sur son ancien statut, conservé à défaut
    ({"status": "Active", "error": "introuvable chez Digi-Key"}, {"status": "UNKNOWN"}, "Introuvable"),
    ({"status": "Active", "error": "introuvable chez Digi-Key"}, {}, "Actif"),
    ({"error": "introuvable chez Digi-Key"}, {"status": "ACTIVE", "datasheet": "https://www.alldatasheet.com/x"},
     "Actif (alldatasheet)"),
    ({}, {}, ""),
])
def test_status_label(dk, web, label):
    assert comp.status_label({"dk": dk, "web": web}) == label


def test_stale_digikey_status_and_web_status_stay_searchable(known, monkeypatch):  # noqa: F811
    """NE555P : Digi-Key ne la trouve pas, la recherche web la dit introuvable. Colonne et filtre « Introuvable »
    (pas un ancien statut Digi-Key) ; « introuvable » se cherche comme avant."""
    data = comp.load_components(known.slug)
    ne = next(c for c in data["components"] if c["mpn"] == "NE555P")
    ne["dk"]["status"] = "Active"  # dernier statut connu avant que Digi-Key ne la perde
    ne["web_status"] = {"status": "UNKNOWN", "url": None, "checked_at": "2026-10-10"}
    comp.save_components(known.slug, data)
    client, _ = _client(monkeypatch)
    t = client.get(f"/veilles/{known.slug}").text
    row = re.search(r'<tr class="comp"[^>]*data-q="Active Introuvable"[^>]*>.*?</tr>', t, re.S).group(0)
    assert "NE555P" in row and '<td class="opt">Introuvable</td>' in row
    assert '<option value="Introuvable">Introuvable</option>' in t


def test_malformed_notice_of_an_archive_never_breaks_the_base(known, tools):  # noqa: F811
    """Avis retouché à la main dans une veille archivée (sans type, source sans lien) : ignoré par la page, l'export
    et la reprise, au lieu d'une erreur 500 pour toutes les veilles."""
    other = new_watch("Carte B", "BC547;onsemi")
    service.components_scan(other.slug, client=FakeClient({"BC547": {"status": "Obsolete", "product_url": None}}))
    dest = service.archive(other.slug)
    notices = json.loads((dest / "notices.json").read_text())["notices"]
    bad = [{k: v for k, v in notices[0].items() if k != "type"} | {"id": "x1"},
           {**notices[0], "id": "x2", "sources": [{"label": "sans lien"}]}, {**notices[0], "id": "x3", "found_at": None}]
    (dest / "notices.json").write_text(json.dumps({"notices": notices + bad}))
    service.rebuild_catalog()
    bc = comp.key_of("BC547", "onsemi")
    assert len(catalog.browse(bc)[bc]["notices"]) == 1  # seul l'avis bien formé
    assert [r["mpn"] for r in service.catalog_rows() if r["mpn"] == "BC547"] == ["BC547"]
    assert service.catalog_xlsx()
    assert len(comp.load_notices(new_watch("Carte C", "BC547;onsemi").slug)) == 1  # reprise : idem


def test_reads_are_one_snapshot_and_leave_no_transaction(known, monkeypatch):  # noqa: F811
    """`parts` et `notices` lues dans une même transaction de lecture, refermée ensuite (les écrivains
    n'attendent que la durée de la lecture)."""
    seen = []
    real = catalog._gather

    def spy(con, *a):
        seen.append(con.in_transaction)
        return real(con, *a)
    monkeypatch.setattr(catalog, "_gather", spy)
    assert catalog.browse() and catalog.knowledge([LM], "autre")
    assert seen == [True, True]
    service.import_components(known.slug, comp.parse_components("BC547;onsemi"))  # écriture possible ensuite


def test_cli_still_accepts_the_legacy_ref_option(known, tmp_path, capsys):  # noqa: F811
    f = tmp_path / "bom.csv"
    f.write_text("MPN;Fabricant;Réf. interne\nBC547;onsemi;R-9\n")
    assert cli.main(["components", "import", known.slug, str(f), "--ref", "C"]) == 0  # acceptée, ignorée
    assert "1 ajouté(s)" in capsys.readouterr().out
