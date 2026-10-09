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
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=_chromium())
        except Exception as exc:  # noqa: BLE001 - navigateur absent : test sauté
            pytest.skip(f"Chromium indisponible : {exc}")
        pg = browser.new_page(viewport={"width": 390, "height": 844})  # PWA mobile
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        base = f"http://localhost:{port}"
        pg.goto(base + "/login")
        pg.fill("input[name=password]", PASSWORD)
        pg.click("button")
        pg.goto(f"{base}/veilles/{watch.slug}")
        yield pg
        assert errors == []  # aucune erreur JS ni violation CSP
        browser.close()
    server.should_exit = True


def _visible(pg) -> list[str]:
    return pg.eval_on_selector_all("tr.comp:not([hidden]) .comp-toggle strong", "els => els.map(e => e.textContent)")


@pytest.fixture
def data(watch, tools):  # noqa: F811
    service.components_scan(watch.slug, client=FakeClient({"LM317T": {"status": "Last Time Buy",
                                                                      "product_url": "https://www.digikey.com/p/1"}}))
    finalize(watch, [cand(["STM32F103C8T6"], ntype="PCN", url="https://st.com/pcn/1",
                          title="Changement de boîtier TO-220", summary="Nouveau site d'assemblage")])


def test_search_column_filters_and_detail(data, page):
    pg = page
    assert _visible(pg) == ["LM317T", "STM32F103C8T6", "NE555P"] and pg.text_content("#comp-count") == "3 / 3"
    # recherche plein texte, temps réel, sans casse ni accents, sur le contenu des avis
    pg.fill("#comp-search", "boitier to-220")
    assert _visible(pg) == ["STM32F103C8T6"] and pg.text_content("#comp-count") == "1 / 3"
    pg.fill("#comp-search", "texas ltb")  # code du type d'avis, pas seulement son libellé
    assert _visible(pg) == ["LM317T"]
    pg.fill("#comp-search", "introuvable")
    assert _visible(pg) == [] and pg.is_visible("#comp-empty")
    pg.fill("#comp-search", "")
    # filtre de type
    pg.select_option("select[data-types]", "critical")
    assert _visible(pg) == ["LM317T"]
    pg.select_option("select[data-types]", "none")
    assert _visible(pg) == ["NE555P"]
    pg.select_option("select[data-types]", "PCN")
    assert _visible(pg) == ["STM32F103C8T6"]
    pg.select_option("select[data-types]", "")
    # filtre de colonne (Composant)
    pg.fill("input[data-col='0']", "ne5")
    assert _visible(pg) == ["NE555P"]
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
    assert pg.locator("tr.detail:not([hidden])").count() == 0
    pg.fill("#comp-search", "")
    assert pg.locator("tr.detail:not([hidden])").count() == 1
