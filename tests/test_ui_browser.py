"""Liste des composants dans un vrai navigateur : recherche, filtres de colonne et de type, détail au clic.

Exécuté si Playwright et Chromium sont disponibles (poste de développement), sauté sinon.
"""
import os
import socket
import threading
import time

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from test_components import FakeClient, cand, finalize, tools, watch  # noqa: E402,F401 - fixtures partagées
from test_ui import PASSWORD  # noqa: E402

from veille_core import service  # noqa: E402


def _chromium():
    for root, _dirs, files in os.walk(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")):
        if "chrome" in files and "chrome-linux" in root:
            return os.path.join(root, "chrome")
    return None


@pytest.fixture
def page(watch, monkeypatch):  # noqa: F811
    from veille_ui import auth
    monkeypatch.setenv("VEILLE_UI_PASSWORD_HASH", auth.hash_password(PASSWORD))
    monkeypatch.setenv("VEILLE_UI_SECRET", "s" * 64)
    auth._failures.clear()
    import uvicorn

    from veille_ui.app import create_app
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert server.started, "serveur de test non démarré"
        with playwright.sync_playwright() as p:
            try:
                browser = p.chromium.launch(executable_path=_chromium())
            except Exception as exc:  # noqa: BLE001 - navigateur absent : test sauté
                pytest.skip(f"Chromium indisponible : {exc}")
            try:
                pg = browser.new_page(viewport={"width": 390, "height": 844})  # PWA mobile
                errors = []
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith(
                    "Failed to load resource") else None)  # échec réseau simulé : attendu
                base = f"http://localhost:{port}"
                pg.goto(base + "/login")
                pg.fill("input[name=password]", PASSWORD)
                pg.click("button")
                pg.goto(f"{base}/veilles/{watch.slug}")
                yield pg
                assert errors == []  # aucune erreur JS ni violation CSP
            finally:
                browser.close()
    finally:
        server.should_exit = True


def _visible(pg, expected: list[str] | None = None) -> list[str]:
    """MPN des lignes affichées ; avec `expected`, attend que le filtrage (différé de 120 ms) l'atteigne."""
    def names():
        return pg.eval_on_selector_all("tr.comp:not([hidden]) .comp-toggle strong", "els => els.map(e => e.textContent)")
    deadline = time.monotonic() + 3
    while expected is not None and names() != expected and time.monotonic() < deadline:
        pg.wait_for_timeout(50)
    return names()


@pytest.fixture
def data(watch, tools):  # noqa: F811
    service.components_scan(watch.slug, client=FakeClient({"LM317T": {"status": "Last Time Buy",
                                                                      "product_url": "https://www.digikey.com/p/1"}}))
    finalize(watch, [cand(["STM32F103C8T6"], ntype="PCN", url="https://st.com/pcn/1",
                          title="Changement de boîtier TO-220", summary="Nouveau site d'assemblage")])


def test_search_column_filters_and_detail(data, page):
    pg = page
    assert _visible(pg, ["LM317T", "STM32F103C8T6", "NE555P"]) == ["LM317T", "STM32F103C8T6", "NE555P"] and pg.text_content("#comp-count") == "3 / 3"
    # recherche plein texte, temps réel, sans casse ni accents, sur le contenu des avis
    pg.fill("#comp-search", "boitier to-220")
    assert _visible(pg, ["STM32F103C8T6"]) == ["STM32F103C8T6"] and pg.text_content("#comp-count") == "1 / 3"
    pg.fill("#comp-search", "texas ltb")  # code du type d'avis, pas seulement son libellé
    assert _visible(pg, ["LM317T"]) == ["LM317T"]
    pg.fill("#comp-search", "introuvable")
    assert _visible(pg, []) == [] and pg.is_visible("#comp-empty")
    pg.fill("#comp-search", "")
    # filtre de type
    pg.select_option("select[data-types]", "critical")
    assert _visible(pg, ["LM317T"]) == ["LM317T"]
    pg.select_option("select[data-types]", "none")
    assert _visible(pg, ["NE555P"]) == ["NE555P"]
    pg.select_option("select[data-types]", "PCN")
    assert _visible(pg, ["STM32F103C8T6"]) == ["STM32F103C8T6"]
    pg.select_option("select[data-types]", "")
    # filtre de colonne (Composant)
    pg.fill("input[data-col='0']", "ne5")
    assert _visible(pg, ["NE555P"]) == ["NE555P"]
    pg.fill("input[data-col='0']", "")
    # détail : clic sur la ligne, chargé une fois, replié au second clic
    row = pg.locator("tr.comp", has_text="STM32F103C8T6")
    row.locator("td").nth(3).click()
    detail = pg.locator("tr.detail:not([hidden])")
    detail.get_by_text("Nouveau site d'assemblage").wait_for()
    assert row.locator(".comp-toggle").get_attribute("aria-expanded") == "true"
    row.locator(".comp-toggle").click()
    assert pg.locator("tr.detail:not([hidden])").count() == 0
    # un détail ouvert suit le filtrage de sa ligne
    row.locator(".comp-toggle").click()
    pg.fill("#comp-search", "lm317")
    assert _visible(pg, ["LM317T"]) == ["LM317T"] and pg.locator("tr.detail:not([hidden])").count() == 0
    pg.fill("#comp-search", "")
    assert len(_visible(pg, ["LM317T", "STM32F103C8T6", "NE555P"])) == 3
    assert pg.locator("tr.detail:not([hidden])").count() == 1
    assert pg.locator("tr.detail").count() == 1  # une seule ligne de détail créée, réutilisée


@pytest.fixture
def two_rows_max(monkeypatch):
    from veille_ui import app as app_mod
    monkeypatch.setitem(app_mod.templates.env.globals, "COMP_ROWS_SHOWN", 2)


def test_rows_capped_then_show_all(data, two_rows_max, page):
    """Mise en page légère : au plus COMP_ROWS_SHOWN lignes, « tout afficher » sur demande."""
    pg = page
    assert _visible(pg, ["LM317T", "STM32F103C8T6"]) == ["LM317T", "STM32F103C8T6"]
    assert pg.text_content("#comp-count") == "3 / 3" and "1 autre(s)" in pg.text_content("#comp-more")
    pg.fill("#comp-search", "ne555")  # la recherche porte sur toute la liste, pas seulement l'affichée
    assert _visible(pg, ["NE555P"]) == ["NE555P"] and not pg.is_visible("#comp-more")
    pg.fill("#comp-search", "")
    assert _visible(pg, ["LM317T", "STM32F103C8T6"]) == ["LM317T", "STM32F103C8T6"]
    pg.click("#comp-more button")
    assert _visible(pg, ["LM317T", "STM32F103C8T6", "NE555P"]) == ["LM317T", "STM32F103C8T6", "NE555P"]


def test_detail_network_failure_can_be_retried(data, page):
    pg = page
    pg.route("**/components/c-*", lambda route: route.abort())  # hors ligne
    row = pg.locator("tr.comp", has_text="STM32F103C8T6")
    row.locator(".comp-toggle").click()
    pg.get_by_text("Détail indisponible").wait_for()
    pg.unroute("**/components/c-*")
    row.locator(".comp-toggle").click()  # replier
    row.locator(".comp-toggle").click()  # rouvrir : nouvel essai
    pg.get_by_text("Nouveau site d'assemblage").wait_for()


def test_settings_page_mobile(page, monkeypatch):
    """Page Paramètres en PWA mobile : sans JS en ligne (CSP), confirmation, aucun défilement horizontal."""
    for k in ("ALERT_EMAIL", "BREVO_SENDER_EMAIL"):  # indépendant de l'environnement du poste
        monkeypatch.delenv(k, raising=False)
    pg = page
    pg.click("nav a[href='/settings']")
    pg.get_by_text("aucune : le chien de garde ne peut prévenir personne").wait_for()
    pg.fill("textarea[name=alert_emails]", "admin@example.org\nrssi@example.org")
    pg.click("button.primary")
    pg.get_by_text("Paramètres enregistrés").wait_for()
    assert "admin@example.org, rssi@example.org" in pg.text_content("#alertes")
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    dialogs = []
    pg.once("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))  # data-confirm (app.js) : refusé
    pg.click("#alertes .actions button")
    pg.wait_for_timeout(300)  # laisse à une éventuelle soumission le temps de naviguer
    assert dialogs == ["Envoyer un e-mail de test à admin@example.org, rssi@example.org ?"]
    assert pg.url.endswith("/settings?ok=settings-saved") and "Paramètres enregistrés" in pg.content()
