"""Références inconnues de Digi-Key (statut établi par la recherche web de l'agent) et remise à zéro
de la recherche d'une veille composants : fonctions pures, service, CLI, interface, MCP."""
import json
import re

import pytest
from test_components import FakeClient, cand, tools, ui, watch  # noqa: F401 - fixtures partagées

from veille_core import cli, paths, service, store
from veille_core import components as comp

UNKNOWN = {"error": "introuvable chez Digi-Key"}


def comps(text="LM317T;Texas Instruments\nBAV99;Nexperia\nBAV99;onsemi"):
    return comp.replace_components([], comp.parse_components(text))


def finalize_raw(v, raw):
    p = v.dir / "rapports" / "cand.json"
    p.write_text(json.dumps(raw))
    return service.components_finalize(v.slug, p)


# --------------------------------------------------------------------------
# Fonctions pures
# --------------------------------------------------------------------------
@pytest.mark.parametrize("a,b,same", [
    ("2N7002,215", "2N7002215", True),       # Nexperia : virgule de conditionnement
    ("74HC14D,653", "74hc14d653", True),
    ("lm 317t", "LM317T", True),
    ("LM317T-DG", "LM317T", False),          # lettres en plus : autre référence
    ("MAX232CPE+", "MAX232CPE", False),      # « + » sans plomb : significatif
    ("SMBJ6.0A", "SMBJ60A", False),          # point décimal : 6 V ≠ 60 V
    ("P6KE6.8CA", "p6ke6.8ca", True),
])
def test_loose_mpn(a, b, same):
    assert (comp.loose_mpn(a) == comp.loose_mpn(b)) is same


def test_validate_statuses_schema_and_matching():
    raw = {"notices": [], "statuses": [
        {"mpn": "LM317T", "status": "active", "url": "https://www.mouser.com/p/LM317T?utm_source=x"},
        {"mpn": "LM317T", "status": "EN PRODUCTION", "url": "https://a.org"},     # statut inconnu
        {"mpn": "LM317T", "status": "OBSOLETE"},                                  # sans fiche
        {"mpn": "LM317T", "status": "LTB", "url": "javascript:alert(1)"},         # fiche non http(s)
        {"mpn": "INCONNU", "status": "ACTIVE", "url": "https://a.org/x"},
        {"mpn": "BAV99", "status": "ACTIVE", "url": "https://a.org/b"},           # ambigu sans fabricant
        {"mpn": "BAV99", "manufacturer": "onsemi", "status": "UNKNOWN"},          # UNKNOWN : fiche facultative
        "texte",
    ]}
    found, rejected = comp.validate_statuses(raw, comps())
    assert found == {comp.key_of("LM317T", "Texas Instruments"): {"status": "ACTIVE",
                                                                  "url": "https://www.mouser.com/p/LM317T"},
                     comp.key_of("BAV99", "onsemi"): {"status": "UNKNOWN", "url": None}}
    assert len(rejected) == 6 and any("statut inconnu" in r for r in rejected)
    assert any("aucune fiche" in r for r in rejected) and any("INCONNU" in r for r in rejected)
    assert comp.validate_statuses({"notices": []}, comps()) == ({}, [])  # champ facultatif
    assert comp.validate_statuses([], comps()) == ({}, [])               # ancien format : liste d'avis
    # mal typé : rejeté sans bloquer l'enregistrement des avis
    assert comp.validate_statuses({"statuses": {"mpn": "LM317T"}}, comps()) == ({}, ["« statuses » : liste attendue"])
    found, rejected = comp.validate_statuses({"statuses": [{"mpn": "LM317T", "status": "UNKNOWN"}] * 502}, comps())
    assert len(found) == 1 and rejected == ["2 statut(s) au-delà de la limite de 500"]


def test_apply_statuses_records_and_creates_notices():
    cs = comps("LM317T;Texas Instruments\nNE555P;Texas Instruments")
    lm, ne = (comp.key_of(m, "Texas Instruments") for m in ("LM317T", "NE555P"))
    out = comp.apply_statuses(cs, {lm: {"status": "ACTIVE", "url": "https://a.org/lm"},
                                   ne: {"status": "OBSOLETE", "url": "https://www.mouser.com/p/ne"}}, "2026-10-10")
    assert cs[0]["web_status"] == {"status": "ACTIVE", "url": "https://a.org/lm", "checked_at": "2026-10-10"}
    (n,) = out  # seul le statut obsolète devient un avis
    assert (n["key"], n["type"], n["origin"], n["title"]) == (ne, "OBSOLETE", "statut-web",
                                                              "Statut constaté sur le web : Obsolète")
    assert n["sources"] == [{"url": "https://www.mouser.com/p/ne", "label": "www.mouser.com"}]
    assert comp.merge([], out)[0]["type"] == "OBSOLETE"  # format accepté par la fusion


def test_reset_state_keeps_the_list_only():
    cs = comps("LM317T;Texas Instruments;R-1")
    cs[0].update(added_at="2025-01-01", web_checked_at="2026-10-01", dk={"status": "Active"},
                 web_status={"status": "ACTIVE", "url": None, "checked_at": "2026-10-01"})
    assert comp.reset_state(cs) == [{"mpn": "LM317T", "manufacturer": "Texas Instruments",
                                     "added_at": "2025-01-01", "web_checked_at": None, "dk": None}]


def test_group_links_to_web_sheet_when_digikey_has_none():
    cs = comps("LM317T;Texas Instruments")
    cs[0]["dk"] = UNKNOWN
    cs[0]["web_status"] = {"status": "ACTIVE", "url": "https://www.lcsc.com/p/1", "checked_at": "2026-10-10"}
    (row,) = comp.group([], cs, include_quiet=True)
    assert row["link"] == {"url": "https://www.lcsc.com/p/1", "label": "www.lcsc.com"} and row["web"]["status"] == "ACTIVE"
    cs[0]["dk"] = {"status": "Active", "product_url": "https://www.digikey.com/p/1"}
    assert comp.group([], cs, include_quiet=True)[0]["link"]["label"] == "Digi-Key"  # Digi-Key d'abord


# --------------------------------------------------------------------------
# Service : du scan au finalize
# --------------------------------------------------------------------------
def test_scan_tells_the_agent_which_references_digikey_misses(watch):  # noqa: F811
    out = service.components_scan(watch.slug, client=FakeClient({"LM317T": UNKNOWN}))
    by = {b["mpn"]: b for b in out["batch"]}
    assert by["LM317T"]["digikey_error"] == "introuvable chez Digi-Key" and by["LM317T"]["digikey_status"] is None
    assert by["STM32F103C8T6"]["digikey_error"] is None
    prompt = (watch.dir / "job-prompt.md").read_text()
    assert "RÉFÉRENCE INCONNUE DE DIGI-KEY" in prompt and '"statuses":[{' in prompt and "${" not in prompt


def test_finalize_records_web_statuses(watch, tools):  # noqa: F811
    service.components_scan(watch.slug, client=FakeClient({"LM317T": UNKNOWN, "NE555P": UNKNOWN}))
    out = finalize_raw(watch, {"notices": [cand(["NE555P"], ntype="OBSOLETE", url="https://www.ti.com/p/ne555p")],
                               "statuses": [
                                   {"mpn": "LM317T", "status": "ACTIVE", "url": "https://www.ti.com/p/lm317t"},
                                   {"mpn": "NE555P", "status": "OBSOLETE", "url": "https://www.ti.com/p/ne555p"},
                                   {"mpn": "STM32F103C8T6", "status": "BIDON", "url": "https://st.com/x"}]})
    assert "2 statut(s) établi(s) sur le web, 1 rejet(s)" in out
    cs = {c["mpn"]: c for c in comp.load_components(watch.slug)["components"]}
    assert cs["LM317T"]["web_status"]["status"] == "ACTIVE" and cs["LM317T"]["dk"] == {
        **cs["LM317T"]["dk"], "error": "introuvable chez Digi-Key"}  # le constat Digi-Key reste
    assert "web_status" not in cs["STM32F103C8T6"]
    (n,) = comp.load_notices(watch.slug)  # avis de l'agent et avis du statut : même fiche, un seul avis
    assert n["type"] == "OBSOLETE" and n["mpn"] == "NE555P"
    # relance : aucun doublon
    finalize_raw(watch, {"notices": [], "statuses": [{"mpn": "NE555P", "status": "OBSOLETE",
                                                      "url": "https://www.ti.com/p/ne555p"}]})
    assert len(comp.load_notices(watch.slug)) == 1


def test_web_status_cited_with_another_sheet_is_not_a_new_notice(watch, tools):  # noqa: F811
    """Le statut est redemandé à chaque rotation : la même obsolescence citée avec une autre fiche ne
    redéclenche ni avis ni e-mail « [EOL ×1] ». Une aggravation (NRND -> OBSOLETE) en est un."""
    def status(ntype, url):
        return finalize_raw(watch, {"notices": [], "statuses": [{"mpn": "NE555P", "status": ntype, "url": url}]})
    assert "1 nouvel(s) avis" in status("OBSOLETE", "https://www.mouser.com/p/ne555p")
    assert "0 nouvel(s) avis" in status("OBSOLETE", "https://fr.farnell.com/p/ne555p")
    (n,) = comp.load_notices(watch.slug)
    assert len(n["sources"]) == 2  # la nouvelle fiche complète l'avis connu
    status("NRND", "https://a.org/nrnd")
    assert [x["type"] for x in comp.load_notices(watch.slug)] == ["OBSOLETE", "NRND"]


def test_unknown_reference_is_searched_once_then_rotates(watch, tools, monkeypatch):  # noqa: F811
    """Une référence que Digi-Key ne trouve plus passe une fois en priorité, même si l'agent ne rend aucun
    statut valide : le lot ne se fige jamais sur elle (les autres références restent recherchées)."""
    clock = {"day": 9}
    monkeypatch.setattr(comp, "today", lambda: f"2026-10-{clock['day']:02d}")
    monkeypatch.setattr(store, "report_stamp", lambda: f"2026-10-{clock['day']:02d}-23h00")
    service.update_veille(watch.slug, batch_size=1)
    lots = []
    for answers in ({}, {}, {}, {"NE555P": UNKNOWN}, {"NE555P": UNKNOWN}, {"NE555P": UNKNOWN}):
        clock["day"] += 1  # une exécution par jour : scan puis finalize
        out = service.components_scan(watch.slug, client=FakeClient(answers))
        lots.append(out["batch"][0]["mpn"])
        finalize_raw(watch, {"notices": [], "statuses": [{"mpn": "NE555P", "status": "EOL"}]})  # statut rejeté
    # 3 jamais vérifiées, puis NE555P (devenue introuvable) en priorité une fois, puis la rotation reprend
    assert lots == ["LM317T", "STM32F103C8T6", "NE555P", "NE555P", "LM317T", "STM32F103C8T6"]
    ne = next(c for c in comp.load_components(watch.slug)["components"] if c["mpn"] == "NE555P")
    assert ne["dk"]["changed_at"] == "2026-10-13" and "web_status" not in ne


def test_web_status_dropped_once_digikey_finds_the_reference(watch, tools):  # noqa: F811
    service.components_scan(watch.slug, client=FakeClient({"NE555P": UNKNOWN}))
    finalize_raw(watch, {"notices": [], "statuses": [{"mpn": "NE555P", "status": "ACTIVE", "url": "https://a.org/p"}]})
    ne = next(c for c in comp.load_components(watch.slug)["components"] if c["mpn"] == "NE555P")
    assert ne["web_status"]["status"] == "ACTIVE"
    ne["dk"]["checked_at"] = "2000-01-01"  # revérifié aujourd'hui
    data = comp.load_components(watch.slug)
    data["components"] = [ne if c["mpn"] == "NE555P" else c for c in data["components"]]
    comp.save_components(watch.slug, data)
    service.components_scan(watch.slug, client=FakeClient({"NE555P": {"status": "Active",
                                                                      "product_url": "https://www.digikey.com/p/9"}}))
    ne = next(c for c in comp.load_components(watch.slug)["components"] if c["mpn"] == "NE555P")
    assert "web_status" not in ne and ne["dk"]["status"] == "Active" and ne["dk"]["error"] is None


# --------------------------------------------------------------------------
# Remise à zéro
# --------------------------------------------------------------------------
@pytest.fixture
def searched(watch, tools):  # noqa: F811
    """Veille composants qui a déjà tout un historique : statuts, avis livrés, statut web, rapport."""
    service.components_scan(watch.slug, client=FakeClient({"LM317T": {"status": "Last Time Buy",
                                                                      "product_url": "https://www.digikey.com/p/1"},
                                                           "NE555P": UNKNOWN}))
    finalize_raw(watch, {"notices": [cand(["STM32F103C8T6"], ntype="PCN", url="https://st.com/pcn/1")],
                         "statuses": [{"mpn": "NE555P", "status": "ACTIVE", "url": "https://www.ti.com/p/ne555p"}]})
    assert len(comp.load_notices(watch.slug)) == 2 and store.list_reports(watch.slug)
    return watch


def test_reset_starts_over_and_keeps_a_copy(searched):
    before = {n: (searched.dir / n).read_text() for n in ("components.json", "notices.json")}
    added = [c["added_at"] for c in comp.load_components(searched.slug)["components"]]
    dest = service.reset_components(searched.slug, actor="test")
    data = comp.load_components(searched.slug)
    assert comp.load_notices(searched.slug) == [] and data["batch"] == []
    assert [c["mpn"] for c in data["components"]] == ["LM317T", "STM32F103C8T6", "NE555P"]
    assert all(c["dk"] is None and c["web_checked_at"] is None and "web_status" not in c for c in data["components"])
    assert [c["added_at"] for c in data["components"]] == added
    # copie préalable intacte dans les archives, rapports et destinataires conservés
    assert dest.parent == paths.archive_dir() and dest.name.startswith(f"{searched.slug}-reinit-")
    assert dest.stat().st_mode & 0o777 == 0o755  # comme les autres dossiers du volume
    assert {n: (dest / n).read_text() for n in before} == before
    assert store.list_reports(searched.slug) and store.read_recipients(searched.slug) == ["achats@example.org"]
    assert any(a["action"] == "components-reset" and a["actor"] == "test" for a in service.read_audit())
    # l'analyse suivante revérifie tout : Digi-Key de nouveau interrogé, lot « jamais vérifiés »
    client = FakeClient({})
    out = service.components_scan(searched.slug, client=client)
    assert client.calls == 3 and all(b["since"] is None and b["known"] == [] for b in out["batch"])


def test_two_resets_never_overwrite_the_first_copy(searched):
    first = service.reset_components(searched.slug)
    second = service.reset_components(searched.slug)
    assert first != second and len(json.loads((first / "notices.json").read_text())["notices"]) == 2
    assert json.loads((second / "notices.json").read_text())["notices"] == []


def test_reset_refused_while_running_or_on_bad_input(searched, monkeypatch):
    before = (searched.dir / "notices.json").read_text()
    with monkeypatch.context() as m:
        m.setattr(service, "running_pid", lambda slug: 4242)
        with pytest.raises(service.ServiceError, match="lancement est en cours"):
            service.reset_components(searched.slug)
    v = service.create_veille(name="Veille", subject="", schedule_expr="0 8 * * 1",
                              recipients=["a@example.org"], brief="# b\n")
    with pytest.raises(service.ServiceError, match="pas une veille composants"):
        service.reset_components(v.slug)
    for name in ("notices.json", "components.json"):
        good = (searched.dir / name).read_text()
        (searched.dir / name).write_text("{corrompu")
        with pytest.raises(comp.ComponentsError, match=f"{name} illisible"):
            service.reset_components(searched.slug)
        assert (searched.dir / name).read_text() == "{corrompu"  # rien touché
        assert not paths.archive_dir().exists() or not any(paths.archive_dir().iterdir())
        (searched.dir / name).write_text(good)
    assert (searched.dir / "notices.json").read_text() == before


def test_cli_reset(searched, capsys):
    assert cli.main(["components", "reset", searched.slug]) == 0
    out = capsys.readouterr().out
    assert out.startswith("✓ recherche réinitialisée") and str(paths.archive_dir()) in out
    assert comp.load_notices(searched.slug) == []


def test_ui_reset_button_confirmation_and_csrf(ui, searched, monkeypatch):  # noqa: F811
    client, csrf = ui
    t = client.get(f"/veilles/{searched.slug}").text
    form = re.search(r'<form method="post" action="/veilles/[^"]+/reset-search" data-confirm="([^"]+)">', t)
    assert form and "Avis détectés (2), statuts et dates de recherche seront effacés" in form.group(1)
    assert "Réinitialiser la recherche</button>" in t and 'id="reinitialiser"' in t
    assert client.post(f"/veilles/{searched.slug}/reset-search", data={}).status_code == 403  # sans jeton CSRF
    assert len(comp.load_notices(searched.slug)) == 2
    r = client.post(f"/veilles/{searched.slug}/reset-search", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/veilles/{searched.slug}?ok=reset"
    page = client.get(r.headers["location"]).text
    assert "Recherche réinitialisée (copie archivée)" in page and comp.load_notices(searched.slug) == []
    monkeypatch.setattr(service, "running_pid", lambda slug: 4242)
    t = client.get(f"/veilles/{searched.slug}").text
    assert re.search(r'<button class="danger" disabled>Réinitialiser la recherche', t)
    r = client.post(f"/veilles/{searched.slug}/reset-search", data={"csrf": csrf})
    assert r.status_code == 400 and "lancement est en cours" in r.text


def test_ui_reset_with_corrupted_file_shows_the_error(ui, searched):  # noqa: F811
    client, csrf = ui
    (searched.dir / "notices.json").write_text("{corrompu")
    r = client.post(f"/veilles/{searched.slug}/reset-search", data={"csrf": csrf})
    assert r.status_code == 500 and "notices.json illisible" in r.text


def test_ui_reset_absent_from_thematic_veille(ui):  # noqa: F811
    client, csrf = ui
    v = service.create_veille(name="Veille", subject="", schedule_expr="0 8 * * 1",
                              recipients=["a@example.org"], brief="# b\n")
    assert "reset-search" not in client.get(f"/veilles/{v.slug}").text
    r = client.post(f"/veilles/{v.slug}/reset-search", data={"csrf": csrf})
    assert r.status_code == 400 and "pas une veille composants" in r.text


def test_ui_shows_web_status_detail_search_and_excel(ui, searched):  # noqa: F811
    client, _ = ui
    t = client.get(f"/veilles/{searched.slug}").text
    assert '<td class="opt">Actif</td>' in t and '<option value="Actif">Actif</option>' in t  # colonne et filtre
    ne = comp.anchor(comp.key_of("NE555P", "Texas Instruments"))
    d = client.get(f"/veilles/{searched.slug}/components/{ne}").text
    assert "introuvable chez Digi-Key" in d and "Statut constaté sur le web" in d
    assert 'href="https://www.ti.com/p/ne555p" target="_blank" rel="noopener noreferrer">Actif</a>' in d
    from test_components import _sheet
    rows = {r[0]: r for r in _sheet(service.components_xlsx(searched.slug)).iter_rows(values_only=True)}
    assert rows["NE555P"][7] == "introuvable chez Digi-Key" and rows["NE555P"][9] == "Actif"
    assert rows["NE555P"][13] == "https://www.ti.com/p/ne555p"  # lien : la fiche trouvée sur le web


def test_mcp_reset_requires_the_slug_twice(searched):
    pytest.importorskip("mcp")
    from test_mcp import call, data

    from veille_mcp.server import build_server
    srv = build_server()
    r = call(srv, "reset_component_search", {"slug": searched.slug, "confirm_slug": "autre"})
    assert r.is_error and "confirmation invalide" in r.content[0].text and len(comp.load_notices(searched.slug)) == 2
    out = data(call(srv, "reset_component_search", {"slug": searched.slug, "confirm_slug": searched.slug}))
    assert out["reset"] is True and out["backup"].startswith(str(paths.archive_dir()))
    assert comp.load_notices(searched.slug) == []
    (row,) = [c for c in data(call(srv, "list_components", {"slug": searched.slug})) if c["mpn"] == "NE555P"]
    assert row["web_status"] is None and row["digikey_error"] is None
