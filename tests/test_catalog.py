"""Base SQLite (db.py) : base des composants partagée entre veilles (catalog.py), carnet d'adresses
(contacts.py) ; repli alldatasheet d'une référence introuvable. Réseau, PDF et e-mail simulés."""
import contextlib
import json
import os
import sqlite3
import threading
import time

import pytest
from test_components import FakeClient, cand, finalize, tools, watch  # noqa: F401 - fixtures partagées

from veille_core import catalog, cli, contacts, db, paths, service, store
from veille_core import components as comp

BOM = "LM317T;Texas Instruments;R-1\nSTM32F103C8T6;STMicroelectronics\nNE555P;Texas Instruments"


def new_watch(name: str, bom: str = BOM, **kw):
    return service.create_veille(name=name, subject="s", schedule_expr=service.COMPONENTS_SCHEDULE,
                                 recipients=kw.pop("recipients", ["achats@example.org"]), brief="# b",
                                 kind="composants", components=comp.parse_components(bom), **kw)


@pytest.fixture
def known(watch, tools):  # noqa: F811
    """La veille `watch` sait des choses : statuts Digi-Key (dont un LTB) et un PCN trouvé sur le web."""
    service.components_scan(watch.slug, client=FakeClient({
        "LM317T": {"status": "Last Time Buy", "product_url": "https://www.digikey.com/p/1"},
        "NE555P": {"error": "introuvable chez Digi-Key"}}))
    finalize(watch, [cand(["STM32F103C8T6"], ntype="PCN", url="https://st.com/pcn/1", number="PCN-7",
                          title="Changement de boîtier")])
    return watch


# --------------------------------------------------------------------------
# Base SQLite
# --------------------------------------------------------------------------
def test_database_created_private_with_schema_and_seeded_book(env):
    service.create_veille(name="Veille A", subject="s", schedule_expr="0 8 * * 1", brief="# b",
                          recipients=["Rssi@Example.org", "dsi@example.org"])
    service.create_veille(name="Veille B", subject="s", schedule_expr="0 8 * * 1", brief="# b", paused=True,
                          recipients=[store.PLACEHOLDER_RECIPIENT])  # adresse factice : jamais proposée
    paths.database().unlink()  # base absente : recréée et amorcée avec les destinataires des veilles
    assert sorted(contacts.all_addresses(), key=str.lower) == ["dsi@example.org", "Rssi@Example.org"]
    assert paths.database().stat().st_mode & 0o777 == 0o600  # adresses : données personnelles
    with db.connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # aucun -wal / -shm permanent
        assert {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} == {
            "parts", "notices", "contacts"}


def test_database_created_by_root_is_given_back(env, monkeypatch):
    """`docker exec veille-hermes veillectl …` tourne en root : la base créée revient au propriétaire du dossier
    des veilles (hermes), sinon l'interface ne pourrait plus y écrire."""
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: calls.append((str(path), uid, gid)))
    with db.connect():
        pass
    st = paths.veilles_dir().stat()
    assert calls == [(str(paths.database()), st.st_uid, st.st_gid)]
    with db.connect():  # base existante : rien à rendre
        pass
    assert len(calls) == 1


@pytest.mark.parametrize("damage", ["garbage", "newer"])
def test_ensure_sets_aside_corrupt_or_newer_database(env, damage):
    with db.connect() as con, con:
        contacts.remember_in(con, ["a@example.org"])
    if damage == "garbage":
        paths.database().write_bytes(b"ceci n'est pas une base SQLite" * 100)
    else:
        with contextlib.closing(sqlite3.connect(paths.database())) as con:  # « with con » seul ne ferme pas
            con.execute("PRAGMA user_version = 99")
    state = db.ensure()
    assert state.startswith("base mise de côté") and "puis recréée" in state
    aside = list(paths.veilles_dir().glob(".veille.db.corrompue-*"))
    assert len(aside) == 1  # jamais effacée
    assert db.ensure() == "base ok" and contacts.all_addresses() == []


def test_concurrent_writers_wait_instead_of_failing(env):
    """Un écrivain (CLI du job) attend qu'un autre (interface) ait fini, au lieu d'échouer (« database is locked »)."""
    with db.connect():
        pass
    started = threading.Event()

    def hold():
        with db.connect() as con, con:
            con.execute("INSERT INTO contacts VALUES ('a@example.org', 1, 'x')")
            started.set()
            time.sleep(0.5)
    t = threading.Thread(target=hold)
    t.start()
    started.wait()
    contacts.remember(["b@example.org"])  # bloqué ~0,5 s, puis réussit
    t.join()
    assert set(contacts.all_addresses()) == {"a@example.org", "b@example.org"}


# --------------------------------------------------------------------------
# Base des composants
# --------------------------------------------------------------------------
def test_new_watch_adopts_known_statuses_and_notices(known, tools):  # noqa: F811
    """Une nouvelle veille reprend ce qu'une autre sait déjà : statuts, date de recherche web, avis (date de
    détection d'origine) ; ils partent dans son PREMIER e-mail (jamais livrés sur ses canaux)."""
    original = {n["key"]: n for n in comp.load_notices(known.slug)}
    other = new_watch("Composants carte Y", "lm317t;texas instruments\nSTM32F103C8T6;STMicroelectronics\nBC547;onsemi")
    data = {c["mpn"]: c for c in comp.load_components(other.slug)["components"]}
    assert data["lm317t"]["dk"]["status"] == "Last Time Buy"  # orthographe de CETTE veille conservée
    assert data["STM32F103C8T6"]["web_checked_at"] == comp.today() and data["BC547"]["dk"] is None
    notices = comp.load_notices(other.slug)
    assert sorted(n["type"] for n in notices) == ["LTB", "PCN"]
    for n in notices:
        assert n["delivered"] == {} and n["found_at"] == original[n["key"]]["found_at"]
        assert n["id"] == original[n["key"]]["id"]
    assert next(n for n in notices if n["type"] == "LTB")["mpn"] == "lm317t"
    assert "2 référence(s) déjà connue(s), 2 avis repris" in json.dumps(service.read_audit(), ensure_ascii=False)
    # premier passage de la nouvelle veille : les avis repris sont dans son e-mail
    finalize(other, [])
    mail = tools()[-1]
    assert "Last Time Buy" in mail["html"] and "Changement de boîtier" in mail["html"]
    assert "[EOL ×1]" in mail["args"][mail["args"].index("--subject") + 1]


def test_adding_components_adopts_once_and_never_from_itself(known, tools):  # noqa: F811
    other = new_watch("Composants carte Y", "BC547;onsemi")
    st = service.import_components(other.slug, comp.parse_components("NE555P;Texas Instruments\nLM317T;Texas Instruments"))
    assert st["added"] == 2 and st["known"] == 2 and st["notices_known"] == 1
    ne555 = next(c for c in comp.load_components(other.slug)["components"] if c["mpn"] == "NE555P")
    assert ne555["dk"]["error"] == "introuvable chez Digi-Key"
    # réimport des mêmes références : rien de nouveau, aucun doublon
    st = service.import_components(other.slug, comp.parse_components("LM317T;Texas Instruments"))
    assert st["added"] == 0 and st["known"] == 0 and len(comp.load_notices(other.slug)) == 1
    # une veille ne « reprend » jamais sa propre contribution (STM32 : connue de `known` seule)
    stm32 = comp.key_of("STM32F103C8T6", "STMicroelectronics")
    assert catalog.knowledge([stm32], known.slug) == {} and stm32 in catalog.knowledge([stm32], other.slug)


def test_known_state_of_kept_reference_is_not_overwritten(known, tools):  # noqa: F811
    other = new_watch("Composants carte Y", "LM317T;Texas Instruments")
    data = comp.load_components(other.slug)
    data["components"][0]["dk"]["status"] = "Obsolete"  # état propre à cette veille, plus récent
    comp.save_components(other.slug, data)
    # remplacement de la liste : LM317T reste (état gardé), seules les nouvelles références sont reprises
    service.update_veille(other.slug, components=comp.parse_components("LM317T;Texas Instruments\nNE555P;Texas Instruments"))
    data = {c["mpn"]: c for c in comp.load_components(other.slug)["components"]}
    assert data["LM317T"]["dk"]["status"] == "Obsolete" and data["NE555P"]["dk"]["error"]


def test_reset_removes_contribution_archive_keeps_it(known, tools):  # noqa: F811
    keys = [comp.key_of("LM317T", "Texas Instruments")]
    assert catalog.knowledge(keys, "autre")[keys[0]]["dk"]["status"] == "Last Time Buy"
    service.reset_components(known.slug)  # données jugées fausses : elles ne doivent plus se propager
    assert catalog.knowledge(keys, "autre") == {}
    service.components_scan(known.slug, client=FakeClient({"LM317T": {"status": "Last Time Buy", "product_url": None}}))
    dest = service.archive(known.slug)
    assert catalog.knowledge(keys, "autre")[keys[0]]["dk"]["status"] == "Last Time Buy"
    other = new_watch("Composants carte X", "LM317T;Texas Instruments")  # même slug que l'archivée
    assert other.slug == known.slug and [n["type"] for n in comp.load_notices(other.slug)] == ["LTB"]
    with db.connect() as con:
        assert {r[0] for r in con.execute("SELECT DISTINCT source FROM parts")} == {dest.name, other.slug}


def test_rebuild_from_files(known, tools):  # noqa: F811
    """La base n'est qu'un index : supprimée, elle se reconstruit à l'identique depuis les fichiers des veilles
    actives et archivées ; une copie de réinitialisation ne revient pas ; une veille illisible est signalée."""
    archived = new_watch("Carte archivée", "BC547;onsemi")
    service.components_scan(archived.slug, client=FakeClient({"BC547": {"status": "Obsolete", "product_url": None}}))
    service.archive(archived.slug)
    service.reset_components(known.slug)  # copie -reinit- dans l'archive : jamais réindexée
    service.components_scan(known.slug, client=FakeClient({"LM317T": {"status": "Last Time Buy", "product_url": None}}))
    expected = catalog.stats()
    assert expected == {"parts": 4, "notices": 2, "sources": 2}
    paths.database().unlink()
    out = service.rebuild_catalog()
    assert out["database"] == "base ok" and out["skipped"] == []
    assert {k: out[k] for k in ("parts", "notices", "sources")} == expected
    stm32 = comp.key_of("STM32F103C8T6", "STMicroelectronics")  # son PCN, effacé par la remise à zéro, ne revient pas
    assert catalog.knowledge([stm32], "autre")[stm32]["notices"] == []
    (known.dir / "notices.json").write_text("{corrompu")
    out = service.rebuild_catalog()
    assert len(out["skipped"]) == 1 and out["skipped"][0].startswith(known.slug)
    assert "catalog-rebuild" in json.dumps(service.read_audit())


def test_database_failure_never_blocks_the_files(known, tools, monkeypatch):  # noqa: F811
    """Base indisponible : l'analyse, la création et l'ajout réussissent (les fichiers font foi), l'échec est
    tracé ; l'index se reconstruit au démarrage."""
    def broken(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(db, "connect", broken)
    out = finalize(known, [cand(["NE555P"], ntype="EOL", url="https://www.ti.com/eol/555")])
    assert out.startswith("OK: veille composants — 1 nouvel(s) avis")
    other = new_watch("Composants carte Y", "LM317T;Texas Instruments")
    assert comp.load_notices(other.slug) == [] and service.address_book() == []
    assert service.catalog_stats() is None
    audit = json.dumps(service.read_audit(), ensure_ascii=False)
    assert "db-error" in audit and "disk I/O error" in audit
    with pytest.raises(service.ServiceError, match="carnet d'adresses indisponible"):
        service.forget_contact("a@example.org")


def test_failed_creation_leaves_no_contribution(known, tools, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_HERMES_FAIL", "1")
    with pytest.raises(Exception):  # noqa: B017 - job Hermes refusé
        new_watch("Composants carte Y", "LM317T;Texas Instruments")
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM parts WHERE source = 'composants-carte-y'").fetchone()[0] == 0


def test_cli_catalog(known, capsys):
    assert cli.main(["catalog", "rebuild"]) == 0
    out = capsys.readouterr().out
    assert "base ok" in out and "3 référence(s), 2 avis, 1 veille(s)" in out
    assert cli.main(["catalog", "stats"]) == 0 and "3 référence(s)" in capsys.readouterr().out
    other = new_watch("Composants carte Y", "BC547;onsemi")
    assert cli.main(["components", "add", other.slug, "LM317T;Texas Instruments"]) == 0
    assert "1 composant(s) ajouté(s) ; base : 1 déjà connu(s), 1 avis repris" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Carnet d'adresses
# --------------------------------------------------------------------------
def test_address_book_remember_order_and_forget(env):
    v = service.create_veille(name="Veille A", subject="s", schedule_expr="0 8 * * 1", brief="# b",
                              recipients=["a@example.org", "b@example.org"])
    service.create_veille(name="Veille B", subject="s", schedule_expr="0 8 * * 1", brief="# b",
                          recipients=["B@example.org", "c@example.org"])
    book = contacts.all_addresses()
    assert book[0] == "b@example.org" and sorted(book) == ["a@example.org", "b@example.org", "c@example.org"]  # b : 2 fois
    assert service.forget_contact("A@EXAMPLE.ORG") is True and service.forget_contact("a@example.org") is False
    # une modification qui garde « a » ne la remet pas dans le carnet ; une adresse ajoutée y entre
    service.update_veille(v.slug, recipients=["a@example.org", "b@example.org", "d@example.org"])
    assert "a@example.org" not in contacts.all_addresses() and "d@example.org" in contacts.all_addresses()
    audit = json.dumps(service.read_audit(), ensure_ascii=False)
    assert "contacts-forget" in audit and "example.org" not in audit.lower()  # jamais d'adresse dans l'audit


def test_address_book_ignores_invalid_and_placeholder(env):
    contacts.remember(["ok@example.org", "pas une adresse", store.PLACEHOLDER_RECIPIENT, None, " ok@example.org "])
    assert contacts.all_addresses() == ["ok@example.org"]


# --------------------------------------------------------------------------
# Repli alldatasheet
# --------------------------------------------------------------------------
COMPS = [{"mpn": "NE555P", "manufacturer": "Texas Instruments"}, {"mpn": "ESP32-WROOM-32E", "manufacturer": "Espressif"}]
ADS = "https://www.alldatasheet.com/datasheet-pdf/pdf/1/TI/NE555P.html"


def test_validate_statuses_alldatasheet_never_proves_a_status():
    raw = {"statuses": [
        {"mpn": "NE555P", "manufacturer": "TI", "status": "ACTIVE", "url": "https://www.ti.com/product/NE555",
         "datasheet": ADS, "maker": "https://www.ti.com"},
        {"mpn": "ESP32-WROOM-32E", "status": "OBSOLETE", "url": ADS},
    ]}
    found, rejected = comp.validate_statuses(raw, COMPS)
    assert found == {comp.key_of("NE555P", "Texas Instruments"): {
        "status": "ACTIVE", "url": "https://www.ti.com/product/NE555", "datasheet": ADS, "maker": "https://www.ti.com"}}
    assert rejected == ["statut #2 : alldatasheet ne fait pas foi pour un statut (fiche du fabricant attendue)"]
    # UNKNOWN : la page alldatasheet citée comme fiche devient la fiche technique consultée
    found, _ = comp.validate_statuses({"statuses": [{"mpn": "ESP32-WROOM-32E", "status": "UNKNOWN", "url": ADS,
                                                     "maker": "https://www.espressif.com"}]}, COMPS)
    assert found[comp.key_of("ESP32-WROOM-32E", "Espressif")] == {
        "status": "UNKNOWN", "url": None, "datasheet": ADS, "maker": "https://www.espressif.com"}
    # champs du repli mal formés : ignorés, le statut reste
    found, rejected = comp.validate_statuses({"statuses": [
        {"mpn": "NE555P", "status": "NRND", "url": "https://www.ti.com/p", "datasheet": "https://www.ti.com/ds",
         "maker": "javascript:alert(1)"}]}, COMPS)
    assert found[comp.key_of("NE555P", "Texas Instruments")] == {"status": "NRND", "url": "https://www.ti.com/p"}
    assert rejected == []


def test_alldatasheet_is_never_a_notice_source():
    valid, rejected = comp.validate_candidates({"notices": [
        cand(["NE555P"], url=ADS),
        {**cand(["NE555P"], ntype="PCN", url=ADS), "sources": [{"url": ADS}, {"url": "https://www.ti.com/pcn/1"}]},
    ]}, COMPS)
    assert rejected == ["#1 : alldatasheet n'est jamais une source d'avis : lien du fabricant attendu"]
    assert [s["url"] for s in valid[0]["sources"]] == ["https://www.ti.com/pcn/1"]


def test_web_status_keeps_maker_site_and_label():
    c = {"mpn": "NE555P", "manufacturer": "Texas Instruments",
         "web_status": {"status": "UNKNOWN", "url": None, "maker": "https://www.ti.com", "checked_at": "2026-01-01"}}
    comp.apply_statuses([c], {comp.key_of("NE555P", "Texas Instruments"): {"status": "ACTIVE", "url": "https://www.ti.com/p",
                                                                          "datasheet": ADS}}, "2026-02-01")
    assert c["web_status"] == {"status": "ACTIVE", "url": "https://www.ti.com/p", "datasheet": ADS,
                               "maker": "https://www.ti.com", "checked_at": "2026-02-01"}
    assert comp.web_status_label(c["web_status"]) == "Actif (alldatasheet)"
    assert comp.web_status_label({"status": "NRND"}) == "Déconseillé (NRND)" and comp.web_status_label({}) == ""


def test_merge_keeps_original_detection_date_only_when_not_imposed():
    notices = []
    imposed = comp.merge(notices, [{**comp.status_notice(COMPS[0] | {"ref": ""}, "LTB", "t", "https://x.org/1", "x", "web"),
                                    "found_at": "2020-01-01"}], "2026-03-03")
    kept = comp.merge(notices, [{**comp.status_notice(COMPS[1] | {"ref": ""}, "LTB", "t", "https://x.org/2", "x", "web"),
                                 "found_at": "2020-01-01"}])
    assert imposed[0]["found_at"] == "2026-03-03" and kept[0]["found_at"] == "2020-01-01"


def test_scan_offers_alldatasheet_fallback_to_the_agent(watch, tools):  # noqa: F811
    """Référence inconnue de Digi-Key : le lot donne à l'agent la recherche alldatasheet (MPN encodé) et ce qu'un
    passage précédent a établi ; une référence connue de Digi-Key n'a pas ces champs."""
    data = comp.load_components(watch.slug)
    data["components"].append(comp.replace_components([], [{"mpn": "SN74HC#00+/A", "manufacturer": "TI", "ref": ""}])[0])
    data["components"][-1]["web_status"] = {"status": "UNKNOWN", "url": None, "maker": "https://www.ti.com"}
    comp.save_components(watch.slug, data)
    service.update_veille(watch.slug, batch_size=10)
    out = service.components_scan(watch.slug, client=FakeClient({"SN74HC#00+/A": {"error": "introuvable chez Digi-Key"}}))
    lot = {b["mpn"]: b for b in out["batch"]}
    assert lot["SN74HC#00+/A"]["alldatasheet"] == (
        "https://www.alldatasheet.com/view.jsp?Searchword=SN74HC%2300%2B%2FA")
    assert lot["SN74HC#00+/A"]["web_status"] == "UNKNOWN" and lot["SN74HC#00+/A"]["maker"] == "https://www.ti.com"
    assert "alldatasheet" not in lot["LM317T"] and "maker" not in lot["LM317T"]
    prompt = (watch.dir / "job-prompt.md").read_text()
    assert "REPLI ALLDATASHEET" in prompt and "Direct Link" in prompt and "${" not in prompt


# --------------------------------------------------------------------------
# Interface et MCP
# --------------------------------------------------------------------------
def _client(monkeypatch):
    from fastapi.testclient import TestClient
    from test_ui import PASSWORD, login

    from veille_ui import auth
    from veille_ui.app import create_app
    monkeypatch.setenv("VEILLE_UI_PASSWORD_HASH", auth.hash_password(PASSWORD))
    monkeypatch.setenv("VEILLE_UI_SECRET", "s" * 64)
    auth._failures.clear()
    client = TestClient(create_app(), base_url="http://localhost")
    return client, login(client)


def test_ui_form_offers_address_book_and_reprise_messages(known, tools, monkeypatch):  # noqa: F811
    from test_ui import create_form
    client, csrf = _client(monkeypatch)
    page = client.get("/veilles/new").text
    assert '<ul class="rcpt-book" data-book hidden' in page and 'data-email="achats@example.org"' in page
    assert 'name="recipients"' in page  # champ envoyé inchangé (et seule interface sans JavaScript)
    form = create_form(csrf, name="Carte Y", kind="composants", components="LM317T;Texas Instruments",
                       recipients="achats@example.org\nnouveau@example.org")
    r = client.post("/veilles/new", data=form, follow_redirects=False)
    assert r.headers["location"] == "/veilles/carte-y?ok=created-known"
    assert "Avis déjà connus d&#39;autres veilles repris" in client.get(r.headers["location"]).text
    assert "nouveau@example.org" in contacts.all_addresses()
    form.update(components="LM317T;Texas Instruments\nSTM32F103C8T6;STMicroelectronics")
    r = client.post("/veilles/carte-y/edit", data=form, follow_redirects=False)
    assert r.headers["location"] == "/veilles/carte-y?ok=updated-known"
    form.update(components="LM317T;Texas Instruments\nBC547;onsemi")
    assert client.post("/veilles/carte-y/edit", data=form, follow_redirects=False).headers["location"].endswith("ok=updated")
    r = client.post("/veilles/new", data=create_form(csrf, name="Sans base"), follow_redirects=False)
    assert r.headers["location"] == "/veilles/sans-base?ok=created"  # veille thématique : message habituel


def test_ui_settings_forget_contact(known, monkeypatch):
    client, csrf = _client(monkeypatch)
    page = client.get("/settings").text
    assert 'id="carnet"' in page and 'value="achats@example.org"' in page
    assert client.post("/settings/forget", data={"email": "achats@example.org"}).status_code == 403  # sans CSRF
    r = client.post("/settings/forget", data={"csrf": csrf, "email": "achats@example.org"}, follow_redirects=False)
    assert r.headers["location"] == "/settings?ok=contact-forgotten#carnet" and contacts.all_addresses() == []
    r = client.post("/settings/forget", data={"csrf": csrf, "email": "achats@example.org"}, follow_redirects=False)
    assert r.headers["location"].startswith("/settings?ok=settings-unchanged")
    assert "Vide : il se remplit" in client.get("/settings").text
    assert store.read_recipients(known.slug) == ["achats@example.org"]  # toujours destinataire


def test_ui_health_detail_and_export_show_fallback(known, tools, monkeypatch):  # noqa: F811
    client, _ = _client(monkeypatch)
    assert "3 référence(s) connue(s), 2 avis" in client.get("/health").text
    data = comp.load_components(known.slug)
    ne555 = next(c for c in data["components"] if c["mpn"] == "NE555P")
    ne555["web_status"] = {"status": "ACTIVE", "url": "https://www.ti.com/product/NE555P", "datasheet": ADS,
                           "maker": "https://www.ti.com", "checked_at": "2026-10-01"}
    comp.save_components(known.slug, data)
    detail = client.get(f"/veilles/{known.slug}/components/{comp.anchor(comp.key_of('NE555P', 'Texas Instruments'))}").text
    assert f'href="{ADS}"' in detail and ">alldatasheet<" in detail and ">site du fabricant<" in detail
    assert "Actif (alldatasheet)" in client.get(f"/veilles/{known.slug}").text  # recherche de la liste
    from io import BytesIO

    from openpyxl import load_workbook
    ws = load_workbook(BytesIO(client.get(f"/veilles/{known.slug}/components.xlsx").content)).active
    col = [c.value for c in ws[1]].index("Statut web")
    assert "Actif (alldatasheet)" in [row[col] for row in ws.iter_rows(min_row=2, values_only=True)]


def test_mcp_list_components_reports_fallback(known):
    pytest.importorskip("mcp")
    from test_mcp import call, data

    from veille_mcp.server import build_server
    d = comp.load_components(known.slug)
    d["components"][2]["web_status"] = {"status": "UNKNOWN", "url": None, "datasheet": ADS, "maker": "https://www.ti.com"}
    comp.save_components(known.slug, d)
    rows = {r["mpn"]: r for r in data(call(build_server(), "list_components", {"slug": known.slug}))}
    assert rows["NE555P"]["web_status_datasheet"] == ADS and rows["NE555P"]["web_status_maker"] == "https://www.ti.com"
    assert rows["LM317T"]["web_status_datasheet"] is None
