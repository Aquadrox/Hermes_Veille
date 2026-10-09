import re

import pytest
from fastapi.testclient import TestClient

from veille_core import hermes, service, store
from veille_ui import auth

PASSWORD = "un-mot-de-passe-solide"


@pytest.fixture
def client(env, monkeypatch):
    monkeypatch.setenv("VEILLE_UI_PASSWORD_HASH", auth.hash_password(PASSWORD))
    monkeypatch.setenv("VEILLE_UI_SECRET", "s" * 64)
    auth._failures.clear()
    from veille_ui.app import create_app

    return TestClient(create_app(), base_url="http://localhost")


def login(client):
    r = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/").text
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def create_form(csrf, **kw):
    data = {"csrf": csrf, "name": "Veille Cyber", "subject": "cybersécurité industrielle",
            "freq_kind": "weekly", "time": "07:30", "dow": "2", "day": "1", "cron_expr": "",
            "recipients": "a@example.org\nb@example.org", "brief": "# Brief\n\nTexte.",
            "pdf_title": "", "pdf_header": "", "pdf_footer": "Interne", "pdf_audience": "RSSI",
            "pdf_accent": "#123456", "model": ""}
    data.update(kw)
    return data


def test_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/veilles/x/reports/2026-01-01-veille.pdf", follow_redirects=False).status_code == 303
    assert client.get("/healthz").text == "ok"
    r = client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


def test_bad_host_rejected(client):
    assert client.get("/login", headers={"Host": "evil.example"}).status_code == 400


def test_wrong_password_and_throttle(client):
    for _ in range(auth.MAX_FAILURES):
        assert client.post("/login", data={"password": "non"}).status_code == 401
    assert client.post("/login", data={"password": PASSWORD}).status_code == 429


def test_security_headers(client):
    r = client.get("/login")
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"


def test_csrf_required(client):
    login(client)
    r = client.post("/veilles/new", data=create_form("mauvais"))
    assert r.status_code == 403
    assert store.list_all() == []


def test_create_edit_pause_flow(client):
    csrf = login(client)
    r = client.post("/veilles/new", data=create_form(csrf), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/veilles/veille-cyber?ok=created"
    v = store.load("veille-cyber")
    assert v.schedule == "30 7 * * 2" and v.pdf.accent == "#123456"
    assert store.read_recipients("veille-cyber") == ["a@example.org", "b@example.org"]
    page = client.get("/veilles/veille-cyber?ok=created").text
    assert "Veille créée." in page and "chaque mardi à 7h30" in page

    # édition : passage en quotidien + nouveau destinataire
    data = create_form(csrf, freq_kind="daily", time="06:00", recipients="c@example.org")
    r = client.post("/veilles/veille-cyber/edit", data=data, follow_redirects=False)
    assert r.status_code == 303
    v = store.load("veille-cyber")
    assert v.schedule == "0 6 * * *" and hermes.get_job(v.job_id)["schedule"]["expr"] == "0 6 * * *"

    # pause via HTMX (fragment de ligne)
    r = client.post("/veilles/veille-cyber/pause",
                    headers={"X-CSRF-Token": csrf, "HX-Request": "true", "HX-Target": "row-veille-cyber"})
    assert r.status_code == 200 and "Reprendre" in r.text and 'id="row-veille-cyber"' in r.text
    # lancer une veille en pause : erreur lisible dans la ligne
    r = client.post("/veilles/veille-cyber/run",
                    headers={"X-CSRF-Token": csrf, "HX-Request": "true", "HX-Target": "row-veille-cyber"})
    assert r.status_code == 400 and "reprendre avant de la lancer" in r.text
    # reprise via formulaire classique
    r = client.post("/veilles/veille-cyber/resume", data={"csrf": csrf}, follow_redirects=False)
    assert r.headers["location"] == "/veilles/veille-cyber?ok=resumed"
    assert [e["actor"] for e in service.read_audit()][0] == "ui"


def test_create_validation_errors_keep_values(client):
    csrf = login(client)
    r = client.post("/veilles/new", data=create_form(csrf, recipients="pas-une-adresse"))
    assert r.status_code == 400 and "adresse(s) invalide(s)" in r.text and "Veille Cyber" in r.text
    r = client.post("/veilles/new", data=create_form(csrf, freq_kind="cron", cron_expr="* * * * *"))
    assert r.status_code == 400 and "fréquence trop élevée" in r.text


def test_schedule_fragment(client):
    login(client)
    r = client.get("/fragments/schedule", params={"freq_kind": "monthly", "time": "06:00", "day": "1"})
    assert "le 1er de chaque mois à 6h" in r.text
    r = client.get("/fragments/schedule", params={"freq_kind": "cron", "cron_expr": "nope"})
    assert "invalide" in r.text


def test_brief_preview_escapes_html(client):
    csrf = login(client)
    r = client.post("/fragments/brief", data={"brief": "# Titre\n\n<script>alert(1)</script>"},
                    headers={"X-CSRF-Token": csrf})
    assert "<h1>Titre</h1>" in r.text and "<script>" not in r.text


def test_reports_and_traversal(client):
    csrf = login(client)
    client.post("/veilles/new", data=create_form(csrf))
    rdir = store.veille_dir("veille-cyber") / "rapports"
    (rdir / "2026-10-05-veille.pdf").write_bytes(b"%PDF-1.7 test")
    r = client.get("/veilles/veille-cyber/reports/2026-10-05-veille.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert client.get("/veilles/veille-cyber/reports/..%2Fveille.json").status_code == 404
    assert client.get("/veilles/..%2F..%2Fcron/reports/2026-10-05-veille.pdf").status_code in (404, 400)
    assert client.get("/veilles/inconnue").status_code == 404


def test_run_and_log(client):
    csrf = login(client)
    client.post("/veilles/new", data=create_form(csrf))
    r = client.post("/veilles/veille-cyber/run", data={"csrf": csrf}, follow_redirects=False)
    assert r.headers["location"] == "/veilles/veille-cyber?ok=run"
    log = store.list_run_logs("veille-cyber")[0]
    import time
    for _ in range(50):
        if "Triggered" in client.get(f"/veilles/veille-cyber/runs/{log}").text:
            break
        time.sleep(0.1)
    assert "Triggered job" in client.get(f"/veilles/veille-cyber/runs/{log}").text


def test_archive(client):
    csrf = login(client)
    client.post("/veilles/new", data=create_form(csrf))
    r = client.post("/veilles/veille-cyber/archive", data={"csrf": csrf}, follow_redirects=False)
    assert r.headers["location"] == "/?ok=archived"
    assert store.list_all() == []
    assert "Aucune veille" in client.get("/?ok=archived").text


def test_health_page(client, monkeypatch):
    import veille_ui.app as app_mod

    monkeypatch.setattr(app_mod, "brevo_account", lambda: {"email": "me@x.fr", "plans": []})
    csrf = login(client)
    client.post("/veilles/new", data=create_form(csrf))
    page = client.get("/health").text
    assert "Journal d'audit" in page and "create" in page and "me@x.fr" in page


def test_unconfigured_ui_refuses_login(env, monkeypatch):
    monkeypatch.delenv("VEILLE_UI_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("VEILLE_UI_SECRET", raising=False)
    from veille_ui.app import create_app

    c = TestClient(create_app(), base_url="http://localhost")
    assert "pas configurée" in c.get("/login").text
    assert c.post("/login", data={"password": "x"}).status_code == 401


def test_costs_section_and_peak_warning(client, env):
    from test_costs import make_db

    csrf = login(client)
    client.post("/veilles/new", data=create_form(csrf, freq_kind="weekly", dow="1", time="08:00"))
    page = client.get("/veilles/veille-cyber").text
    assert "Aucune exécution mesurée" in page
    assert "heure pleine DeepSeek" in page                      # lundi 8h Paris = 06h UTC
    make_db(env, store.load("veille-cyber").job_id)
    page = client.get("/veilles/veille-cyber").text
    assert "Coût des exécutions" in page and "pleine ×2" in page and "creuse" in page
    r = client.get("/fragments/schedule", params={"freq_kind": "weekly", "dow": "1", "time": "06:30"})
    assert "Heure pleine" not in r.text
    r = client.get("/fragments/schedule", params={"freq_kind": "weekly", "dow": "1", "time": "08:00"})
    assert "Heure pleine DeepSeek" in r.text


def test_health_shows_deepseek_balance_and_costs(client, monkeypatch):
    import veille_ui.app as app_mod

    monkeypatch.setattr(app_mod, "brevo_account", lambda: {"error": "hors ligne"})
    monkeypatch.setattr(app_mod.deepseek, "balance", lambda: {"available": True, "balances": [
        {"currency": "USD", "total": "12.50", "granted": "0.00", "topped_up": "12.50"}]})
    csrf = login(client)
    client.post("/veilles/new", data=create_form(csrf))
    page = client.get("/health").text
    assert "DeepSeek" in page and "12.50 USD" in page and "Total" in page


def test_report_history_listed_first_and_counted(client, env):
    """Les rapports passés restent visibles après une mise à jour : liste complète, en tête."""
    csrf = login(client)
    client.post("/veilles/new", data=create_form(csrf))
    rdir = store.veille_dir("veille-cyber") / "rapports"
    days = ["2026-09-21", "2026-09-28", "2026-10-05"]
    for day in days:
        (rdir / f"{day}-veille.pdf").write_bytes(b"%PDF" + b"x" * 2048)
        (rdir / f"{day}-veille.md").write_text("# r\n")
    page = client.get("/veilles/veille-cyber").text
    assert 'id="rapports"' in page and "Rapports <span class=\"muted small\">(3)</span>" in page
    assert page.index('id="rapports"') < page.index("Coût des exécutions")
    positions = [page.index(f"{day}-veille.pdf") for day in reversed(days)]
    assert positions == sorted(positions)  # du plus récent au plus ancien
    assert 'data-label="PDF"' in page  # présentation en cartes sur mobile
    dashboard = client.get("/").text
    assert 'href="/veilles/veille-cyber#rapports">3 rapports</a>' in dashboard
    # rapports horodatés (v0.3.2) : deux le même jour, heure affichée, téléchargeables
    for stamp in ("2026-10-05-09h05", "2026-10-05-14h30"):
        (rdir / f"{stamp}-veille.pdf").write_bytes(b"%PDF" + b"x" * 2048)
    page = client.get("/veilles/veille-cyber").text
    assert "2026-10-05 14:30" in page and page.index("2026-10-05-14h30-veille.pdf") < page.index("2026-10-05-09h05")
    assert client.get("/veilles/veille-cyber/reports/2026-10-05-14h30-veille.pdf").status_code == 200
