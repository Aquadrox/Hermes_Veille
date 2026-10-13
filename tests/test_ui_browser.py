"""Liste des composants dans un vrai navigateur : recherche, filtres de colonne et de type, détail au clic.

Exécuté si Playwright et Chromium sont disponibles (poste de développement), sauté sinon.
"""
import contextlib
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


@contextlib.contextmanager
def _browser(monkeypatch, start: str, **context):
    """Serveur de l'interface (thread) + Chromium connecté ; page ouverte sur `start`. À la fin : aucune erreur
    JS ni violation CSP. `context` : options du contexte (viewport mobile par défaut, tactile…)."""
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
                pg = browser.new_context(**{"viewport": {"width": 390, "height": 844}, **context}).new_page()  # PWA mobile
                errors = []
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not m.text.startswith(
                    "Failed to load resource") else None)  # échec réseau simulé : attendu
                base = f"http://localhost:{port}"
                pg.goto(base + "/login")
                pg.fill("input[name=password]", PASSWORD)
                pg.click("button")
                pg.goto(base + start)
                yield pg
                assert errors == []  # aucune erreur JS ni violation CSP
            finally:
                browser.close()
    finally:
        server.should_exit = True


@pytest.fixture
def page(watch, monkeypatch):  # noqa: F811
    with _browser(monkeypatch, f"/veilles/{watch.slug}") as pg:
        yield pg


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


def test_run_end_updates_head_without_reload(data, watch, page, monkeypatch):  # noqa: F811
    """Fin d'une analyse (PWA mobile) : « en cours » devient « actif » et les boutons se réactivent,
    sans recharger la page (échange hors bande de l'en-tête, aucun JS en ligne)."""
    from veille_core import store
    pg = page
    state = {"pid": 4242}
    monkeypatch.setattr(service, "running_pid", lambda slug: state["pid"])
    store.runs_dir(watch.slug).mkdir(parents=True, exist_ok=True)
    (store.runs_dir(watch.slug) / "20261009-120000.log").write_text("analyse en cours…\n")
    pg.reload()
    badge = "#detail-head h1 .badge"
    assert pg.text_content(badge) == "en cours" and pg.is_disabled("#detail-head button.primary")
    pg.evaluate("window.notReloaded = true")  # disparaîtrait avec un rechargement
    state["pid"] = None
    pg.locator(f"{badge}:text-is('actif')").wait_for(timeout=10_000)  # suivi du journal : toutes les 5 s
    assert pg.evaluate("window.notReloaded") is True and not pg.is_disabled("#detail-head button.primary")
    assert pg.text_content("#run-log .badge") == "terminé" and pg.locator("#detail-head").count() == 1
    # le formulaire remplacé garde sa confirmation (écouteur délégué de app.js)
    dialogs = []
    pg.once("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    pg.click("#detail-head button.primary")
    assert dialogs and "Lancer l'analyse" in dialogs[0]


def test_export_button_downloads_xlsx(data, watch, page):  # noqa: F811
    pg = page
    with pg.expect_download() as dl:
        pg.click("#composants a.button[download]")
    assert dl.value.suggested_filename.endswith(f"-{watch.slug}.xlsx")
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")  # pas de défilement horizontal


def test_base_page_filters_and_detail_mobile(data, watch, monkeypatch):  # noqa: F811
    """Base des composants (PWA) : filtre de veille EXACT (« composants-carte-x » ne retient pas
    « composants-carte-x-2 »), filtre de statut, recherche, détail lu par sa clé (MPN avec « + ») ; filtres
    lisibles sur 2 colonnes, sans défilement horizontal ; aucune erreur JS ni violation CSP."""
    from veille_core import components as comp
    other = service.create_veille(name="Composants carte X 2", subject="s", schedule_expr=service.COMPONENTS_SCHEDULE,
                                  recipients=["a@example.org"], brief="# b", kind="composants",
                                  components=comp.parse_components("MAX232CPE+;Analog Devices\nLM317T;Texas Instruments"))
    service.components_scan(other.slug, client=FakeClient({
        "MAX232CPE+": {"status": "Obsolete", "product_url": None},
        "LM317T": {"status": "Last Time Buy", "product_url": "https://www.digikey.com/p/1"}}))
    with _browser(monkeypatch, "/composants") as pg:
        everything = ["MAX232CPE+", "LM317T", "STM32F103C8T6", "NE555P"]
        assert _visible(pg, everything) == everything and pg.text_content("#comp-count") == "4 / 4"
        pg.select_option("select[data-col='6']", watch.slug)
        assert _visible(pg, ["LM317T", "STM32F103C8T6", "NE555P"]) == ["LM317T", "STM32F103C8T6", "NE555P"]
        pg.select_option("select[data-col='6']", other.slug)
        assert _visible(pg, ["MAX232CPE+", "LM317T"]) == ["MAX232CPE+", "LM317T"]
        pg.select_option("select[data-col='6']", "")
        pg.select_option("select[data-col='2']", "Obsolète")  # « Obsolete » de Digi-Key, libellé commun
        assert _visible(pg, ["MAX232CPE+"]) == ["MAX232CPE+"]
        pg.select_option("select[data-col='2']", "Actif")  # valeur exacte, pas une sous-chaîne
        assert _visible(pg, ["STM32F103C8T6", "NE555P"]) == ["STM32F103C8T6", "NE555P"]
        pg.select_option("select[data-col='2']", "")
        lefts = "els => els.filter(e => e.offsetParent).map(e => Math.round(e.getBoundingClientRect().left))"
        for width in (320, 360, 390):  # Composant, Statut, Avis, Échéance, Veille : 2 colonnes sur tout téléphone
            pg.set_viewport_size({"width": width, "height": 844})
            filters = pg.eval_on_selector_all("tr.filters > th", lefts)
            assert len(filters) == 5 and len(set(filters)) == 2, (width, filters)
            assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width
        pg.set_viewport_size({"width": 390, "height": 844})
        pg.fill("#comp-search", "max232")
        assert _visible(pg, ["MAX232CPE+"]) == ["MAX232CPE+"]
        pg.locator("tr.comp .comp-toggle").first.click()
        detail = pg.locator("tr.detail:not([hidden])")
        detail.get_by_text("Analysé par").wait_for()
        assert "Analog Devices" in detail.text_content() and "Obsolete" in detail.text_content()
        assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_layout_full_width_on_desktop(data, page):
    """Grand écran : cartes pleine largeur (plus de colonne de 1 200 px), texte courant plafonné
    (~110 caractères) mais pas les encadrés ; mobile (PWA) : marges de 16 px, sans défilement horizontal."""
    pg = page
    metrics = """() => {
      const card = document.querySelector('#composants').getBoundingClientRect();
      const prose = document.querySelector('.card.danger p');  // texte courant
      const flash = document.querySelector('#composants p.flash');
      return {vw: document.documentElement.clientWidth, card: card.width, left: card.left,
              header: getComputedStyle(document.querySelector('header.top')).paddingLeft,
              prose: prose.getBoundingClientRect().width, proseMax: getComputedStyle(prose).maxWidth,
              flash: flash.getBoundingClientRect().width,
              overflow: document.documentElement.scrollWidth > window.innerWidth};
    }"""
    for width in (1280, 1920, 2560):
        pg.set_viewport_size({"width": width, "height": 900})
        m = pg.evaluate(metrics)
        assert m["card"] >= 0.9 * width and not m["overflow"], m
        assert m["left"] == pytest.approx(0.02 * width, abs=1), m  # marge proportionnelle (2vw)
        if width >= 1920:  # texte plafonné, encadré pleine largeur du bloc
            assert m["proseMax"] != "none" and m["prose"] < m["flash"] < m["card"], m
    pg.set_viewport_size({"width": 390, "height": 844})
    m = pg.evaluate(metrics)
    assert m["left"] == 16 and m["card"] == m["vw"] - 32 and m["header"] == "20px" and not m["overflow"], m


def test_slow_action_marks_button_busy_at_once(watch, page, monkeypatch):  # noqa: F811
    """Pause (CLI Hermes, ~1 s) : le bouton passe tout de suite « en cours » et ne se reclique pas ;
    l'historique Hermes, lui, arrive après la page."""
    from veille_core import hermes
    pg = page
    slow_pause = hermes.pause
    monkeypatch.setattr(hermes, "pause", lambda job_id: (time.sleep(1.5), slow_pause(job_id)))
    # état du bouton relevé pendant l'envoi (Playwright n'interroge pas une page qui navigue)
    pg.evaluate("""() => document.addEventListener('submit', e => setTimeout(() => sessionStorage.setItem(
        'btn', [e.submitter.textContent, e.submitter.classList.contains('busy'), e.submitter.disabled].join()), 50))""")
    pg.click("#detail-head button:text-is('Pause')")
    pg.wait_for_url("**?ok=paused", timeout=10_000)
    assert pg.evaluate("sessionStorage.getItem('btn')") == "Pause,true,true"
    assert pg.locator("#detail-head button:text-is('Reprendre')").is_enabled()
    pg.locator("section h2:text('Historique')").wait_for(timeout=5_000)  # fragment chargé (load)


def test_progress_panel_mobile(watch, page, monkeypatch):  # noqa: F811
    """Avancement d'une analyse sur mobile (PWA) : page de la veille puis tableau de bord, sans défilement
    horizontal ni erreur JS / CSP (vérifié par la fixture)."""
    from test_progress import tracked_run
    tracked_run(watch, monkeypatch)
    pg = page
    pg.reload()
    pg.locator(".progress").wait_for()
    assert "Étape 2/3 — Recherche web : 0 / 2 composant(s) · en cours : LM317T" in pg.text_content(".progress")
    assert pg.locator("ul.chips li.searching").count() == 1 and pg.locator("progress").count() == 1
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    pg.goto(pg.url.split("/veilles/")[0] + "/")
    assert "en cours : LM317T" in pg.text_content(f"#row-{watch.slug}")
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_reset_search_mobile(data, watch, page):  # noqa: F811
    """Réinitialiser la recherche en PWA mobile : confirmation (app.js, aucun JS en ligne), refus sans effet,
    puis acceptation ; avis effacés, message affiché, aucun défilement horizontal."""
    from veille_core import components as comp
    pg = page
    button = "#reinitialiser button.danger"
    pg.locator(button).scroll_into_view_if_needed()
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    dialogs = []
    pg.once("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    pg.click(button)
    pg.wait_for_timeout(300)  # laisse à une éventuelle soumission le temps de naviguer
    assert dialogs and "Avis détectés (2), statuts et dates de recherche seront effacés" in dialogs[0]
    assert len(comp.load_notices(watch.slug)) == 2  # refusé : rien n'a changé
    pg.once("dialog", lambda d: d.accept())
    pg.click(button)
    pg.wait_for_url("**?ok=reset", timeout=10_000)
    assert "Recherche réinitialisée" in pg.text_content(".flash") and comp.load_notices(watch.slug) == []
    assert pg.text_content("#comp-count") == "3 / 3"  # la liste des composants reste


# --------------------------------------------------------------------------
# Destinataires : puces, carnet d'adresses, glisser-déposer (tactile réel et souris)
# --------------------------------------------------------------------------
BOOK = ["achats@example.org", "jean.dupont@example.org", "marie.curie@labo.example", "rssi@example.org"]


@pytest.fixture
def form(env, monkeypatch):
    """Formulaire de création, PWA mobile TACTILE, carnet d'adresses rempli."""
    from veille_core import contacts
    contacts.remember(BOOK)
    with _browser(monkeypatch, "/veilles/new", has_touch=True, is_mobile=True) as pg:
        yield pg


def _recipients(pg) -> list[str]:
    """Champ réellement envoyé (zone de texte masquée, tenue à jour par app.js)."""
    return [a for a in pg.input_value("textarea[name=recipients]").split("\n") if a]


def _book(pg) -> list[str]:
    return pg.eval_on_selector_all("[data-book] li:not([hidden]) [data-email]", "els => els.map(e => e.dataset.email)")


def _center(pg, selector: str) -> tuple[float, float]:
    b = pg.locator(selector).bounding_box()
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2


def _touch_drag(pg, source: str, target: str, hold_ms: int = 500) -> None:
    """Glisser au doigt avec de vrais événements tactiles (CDP) : appui de `hold_ms` sur `source`, déplacement,
    relâché sur `target`. hold_ms = 0 : balayage (défilement)."""
    cdp = pg.context.new_cdp_session(pg)

    def touch(kind, x=0.0, y=0.0):
        cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [] if kind == "touchEnd" else [{"x": x, "y": y}]})
    (x, y), (tx, ty) = _center(pg, source), _center(pg, target)
    touch("touchStart", x, y)
    pg.wait_for_timeout(hold_ms)
    for i in range(1, 11):
        touch("touchMove", x + (tx - x) * i / 10, y + (ty - y) * i / 10)
        pg.wait_for_timeout(15)
    touch("touchEnd")
    pg.wait_for_timeout(100)


def test_recipients_widget_tap_keyboard_and_submit(form):
    """Saisie mobile : autocomplétion sur le carnet, toucher pour ajouter, séparateurs et collage, adresse
    invalide refusée, retrait (× et Retour arrière) ; la veille créée a exactement ces destinataires."""
    from veille_core import contacts, store
    pg = form
    assert not pg.is_visible("textarea[name=recipients]") and pg.is_visible(".rcpt-input")
    assert sorted(_book(pg)) == sorted(BOOK)
    pg.fill(".rcpt-input", "DUPONT")  # recherche sans casse
    assert _book(pg) == ["jean.dupont@example.org"]
    pg.press(".rcpt-input", "Enter")  # première suggestion ; jamais l'envoi du formulaire
    assert _recipients(pg) == ["jean.dupont@example.org"] and pg.input_value(".rcpt-input") == ""
    assert "jean.dupont@example.org" not in _book(pg)  # déjà destinataire : plus proposée
    pg.tap("[data-book] [data-email='marie.curie@labo.example']")
    pg.fill(".rcpt-input", "nouveau@example.org,")  # séparateur : puce créée
    pg.fill(".rcpt-input", "a@example.org; b@example.org ")  # collage de plusieurs adresses
    assert _recipients(pg) == ["jean.dupont@example.org", "marie.curie@labo.example", "nouveau@example.org",
                               "a@example.org", "b@example.org"]
    pg.fill(".rcpt-input", "pas-une-adresse")
    pg.press(".rcpt-input", "Enter")
    assert "Adresse invalide : pas-une-adresse" in pg.text_content(".rcpt-msg")
    assert pg.input_value(".rcpt-input") == "pas-une-adresse" and len(_recipients(pg)) == 5
    pg.fill(".rcpt-input", "")
    pg.press(".rcpt-input", "Backspace")  # champ vide : retire la dernière
    pg.tap(".rcpt-list [aria-label='Retirer nouveau@example.org']")
    assert _recipients(pg) == ["jean.dupont@example.org", "marie.curie@labo.example", "a@example.org"]
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    pg.fill("input[name=name]", "Veille Destinataires")
    pg.fill(".rcpt-input", "z@example.org")  # tapée sans Entrée : prise en compte à l'envoi
    pg.click("button.primary")
    pg.wait_for_url("**/veilles/veille-destinataires?ok=created")
    assert store.read_recipients("veille-destinataires") == [
        "jean.dupont@example.org", "marie.curie@labo.example", "a@example.org", "z@example.org"]
    assert {"a@example.org", "z@example.org"} <= set(contacts.all_addresses())  # carnet enrichi


def test_recipients_drag_and_drop_touch(form):
    """Glisser-déposer au doigt (PWA) : appui long carnet -> destinataires, puis l'inverse ; un balayage rapide
    fait défiler la page sans rien ajouter."""
    pg = form
    pg.locator("[data-recipients]").scroll_into_view_if_needed()
    _touch_drag(pg, "[data-book] [data-email='rssi@example.org']", ".rcpt-box")
    assert _recipients(pg) == ["rssi@example.org"] and pg.locator(".rcpt-ghost").count() == 0
    before = pg.evaluate("scrollY")
    _touch_drag(pg, "[data-book] [data-email='achats@example.org']", ".rcpt-box", hold_ms=0)
    assert _recipients(pg) == ["rssi@example.org"] and pg.evaluate("scrollY") != before  # défilement
    pg.locator("[data-recipients]").scroll_into_view_if_needed()
    _touch_drag(pg, ".rcpt-list [data-email='rssi@example.org'] span", "[data-book]")
    assert _recipients(pg) == [] and "rssi@example.org" in _book(pg)


def test_recipients_drag_and_drop_mouse_and_errors(env, monkeypatch):
    """Bureau : glisser à la souris (sans double ajout par le clic qui suit) ; envoi sans destinataire bloqué
    côté client ; adresse invalide renvoyée par le serveur signalée en rouge, saisie conservée."""
    from veille_core import contacts
    contacts.remember(BOOK)
    with _browser(monkeypatch, "/veilles/new", viewport={"width": 1280, "height": 900}) as pg:
        (x, y), (tx, ty) = _center(pg, "[data-book] [data-email='achats@example.org']"), _center(pg, ".rcpt-box")
        pg.mouse.move(x, y)
        pg.mouse.down()
        pg.mouse.move(tx, ty, steps=8)
        assert pg.locator(".rcpt-box.rcpt-over").count() == 1  # zone cible signalée
        pg.mouse.up()
        assert _recipients(pg) == ["achats@example.org"]
        pg.click("[data-book] [data-email='rssi@example.org']")  # clic simple : ajout
        assert _recipients(pg) == ["achats@example.org", "rssi@example.org"]
        pg.click(".rcpt-list [aria-label='Retirer achats@example.org']")
        pg.click(".rcpt-list [aria-label='Retirer rssi@example.org']")
        pg.fill("input[name=name]", "Veille Vide")
        pg.click("button.primary")
        pg.wait_for_timeout(300)
        assert pg.url.endswith("/veilles/new") and pg.evaluate(
            "document.querySelector('.rcpt-input').validationMessage") == "Au moins un destinataire est requis."
        pg.fill(".rcpt-input", "ok@example.org pas@valide")
        pg.click("button.primary")  # le serveur juge : 400, formulaire réaffiché avec la saisie
        pg.get_by_text("adresse(s) invalide(s) : pas@valide").wait_for()
        assert _recipients(pg) == ["ok@example.org", "pas@valide"]
        assert pg.locator(".rcpt-list .rcpt-bad").text_content().startswith("pas@valide")


def test_recipients_without_javascript(env, monkeypatch):
    """Sans JavaScript : la zone de texte d'origine, le carnet masqué ; la création fonctionne comme avant."""
    from veille_core import contacts, store
    contacts.remember(BOOK)
    with _browser(monkeypatch, "/veilles/new", java_script_enabled=False) as pg:
        assert pg.is_visible("textarea[name=recipients]") and not pg.is_visible("[data-book]")
        pg.fill("input[name=name]", "Veille Sans JS")
        pg.fill("textarea[name=recipients]", "a@example.org\nb@example.org")
        pg.click("button.primary")
        pg.wait_for_url("**/veilles/veille-sans-js?ok=created")
        assert store.read_recipients("veille-sans-js") == ["a@example.org", "b@example.org"]


def test_recipients_second_finger_during_drag(form):
    """Un second doigt pendant un glisser est ignoré : au relâchement, ni fantôme ni état de glisser ne restent
    (régression relevée en revue : `drag` écrasé, fantôme orphelin jusqu'au rechargement)."""
    pg = form
    pg.locator("[data-recipients]").scroll_into_view_if_needed()
    cdp = pg.context.new_cdp_session(pg)
    (x, y), (x2, y2) = (_center(pg, f"[data-book] [data-email='{a}']") for a in ("rssi@example.org", "achats@example.org"))
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y, "id": 1}]})
    pg.wait_for_timeout(500)  # appui long : glisser commencé
    assert pg.locator(".rcpt-ghost").count() == 1
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [
        {"x": x, "y": y, "id": 1}, {"x": x2, "y": y2, "id": 2}]})
    pg.wait_for_timeout(500)  # l'appui long du second doigt ne démarre rien
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    pg.wait_for_timeout(200)
    assert pg.locator(".rcpt-ghost, .rcpt-held, .rcpt-dragging").count() == 0 and _recipients(pg) == []
