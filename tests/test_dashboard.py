"""Accueil : indicateurs (service.dashboard), compte à rebours, mini-graphe de coût, navigation, CSP."""
import datetime as dt
import json
import re
import sqlite3
import time

import pytest
from test_costs import SCHEMA
from test_ui import client, login  # noqa: F401 - fixtures partagées

from veille_core import components as comp
from veille_core import schedule, service, store
from veille_ui.app import fmt_until, spark

UTC = dt.UTC
NOW = dt.datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------- présentation
@pytest.mark.parametrize("value,expected", [
    ("2026-10-28T12:00:00Z", NOW),
    ("2026-10-28T13:00:00+01:00", NOW),
    ("2026-10-28T13:00:00", NOW),  # heure naïve : Paris (UTC+1 après le 25/10/2026)
    (NOW, NOW),
    ("pas une date", None),
    ("", None),
    (None, None),
    (42, None),
])
def test_parse_when(value, expected):
    assert schedule.parse_when(value) == expected


@pytest.mark.parametrize("delta,expected", [
    (dt.timedelta(hours=4, minutes=12), "dans 4 h 12"),
    (dt.timedelta(hours=1, minutes=5), "dans 1 h 05"),
    (dt.timedelta(hours=3), "dans 3 h"),
    (dt.timedelta(days=2, hours=5, minutes=40), "dans 2 j 5 h"),
    (dt.timedelta(days=2, minutes=3), "dans 2 j"),
    (dt.timedelta(minutes=12, seconds=30), "dans 12 min"),
    (dt.timedelta(seconds=30), "imminente"),
    (dt.timedelta(hours=-1), "imminente"),  # dépassée : Hermes ne l'a pas encore lancée
])
def test_fmt_until(delta, expected):
    assert fmt_until((NOW + delta).isoformat(), now=NOW) == expected


def test_fmt_until_unknown():
    assert fmt_until(None, now=NOW) == "—" and fmt_until("illisible", now=NOW) == "—"


def test_spark_bars_proportional_and_inside_the_box():
    assert spark([]) == []
    bars = spark([{"cost_usd": c} for c in (1.0, 0.5, 0.0)])
    assert [b["h"] for b in bars] == [28, 14, 2]  # la plus chère pleine hauteur ; 2 px au moins
    assert all(b["y"] + b["h"] == 28 for b in bars)  # alignées en bas
    assert [b["x"] for b in bars] == sorted(b["x"] for b in bars)
    assert [b["h"] for b in spark([{"cost_usd": 0}] * 3)] == [2, 2, 2]  # coûts nuls : pas de division par zéro
    for n in (1, 12, 100, 120):  # quel que soit le nombre d'exécutions, rien ne déborde du SVG (120 px)
        bars = spark([{"cost_usd": 1}] * n)
        assert len(bars) == n and max(b["x"] + b["w"] for b in bars) <= 120 and min(b["w"] for b in bars) >= 1


# ---------------------------------------------------------------- service
def _veille(name, **kw):
    return service.create_veille(name=name, subject="s", schedule_expr="30 6 * * 1",
                                 recipients=["a@example.org"], brief="# Brief\n", **kw)


def _jobs(env, **fields_by_job):
    path = env / "cron" / "jobs.json"
    jobs = json.loads(path.read_text())["jobs"]
    for job in jobs:
        job.update(fields_by_job.get(job["id"], {}))
    path.write_text(json.dumps({"jobs": jobs}))


def _reports(slug, *days):
    rdir = store.veille_dir(slug) / "rapports"
    rdir.mkdir(parents=True, exist_ok=True)
    for day in days:
        (rdir / f"{day}-veille.pdf").write_bytes(b"%PDF" + b"x" * 2048)


def _sessions(env, job_id, *starts):
    db = sqlite3.connect(env / "state.db")
    db.executescript(SCHEMA.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
    for start in starts:
        sid = f"cron_{job_id}_{dt.datetime.fromtimestamp(start, UTC):%Y%m%d_%H%M%S}"
        db.execute("INSERT INTO sessions (id, source, model, started_at, ended_at, input_tokens, output_tokens)"
                   " VALUES (?, 'cron', 'deepseek-v4-pro', ?, ?, 1000000, 0)", (sid, start, start + 60))
    db.commit()
    db.close()


@pytest.fixture
def three(env):
    """A active, B en pause, C composants (2 avis dont 1 fin de vie) ; prochaines exécutions et rapports."""
    a, b = _veille("Veille A"), _veille("Veille B", paused=True)
    c = service.create_veille(name="Composants C", subject="BOM", schedule_expr=service.COMPONENTS_SCHEDULE,
                              recipients=["a@example.org"], brief="# b\n", kind="composants",
                              components=comp.parse_components("LM317T;Texas Instruments\nNE555P;Texas Instruments"))
    notices = [{"mpn": m, "manufacturer": "Texas Instruments", "type": t, "title": t, "url": "https://ti.com/pcn",
                "found_at": "2026-10-20T10:00:00+02:00", "date": "2026-10-15"} for m, t in (("LM317T", "EOL"), ("NE555P", "PCN"))]
    for n in notices:
        n["key"] = comp.key_of(n["mpn"], n["manufacturer"])
    comp.save_notices(c.slug, notices)
    # Ordre lexicographique trompeur : C (« 14:00Z ») précède A (« 14:30+01:00 ») en texte, mais A (13:30Z) est la plus proche.
    _jobs(env, **{a.job_id: {"next_run_at": "2026-10-28T14:30:00+01:00"},
                  b.job_id: {"next_run_at": "2026-10-28T08:00:00Z"},  # en pause : ignorée
                  c.job_id: {"next_run_at": "2026-10-28T14:00:00Z"}})
    _reports(a.slug, "2026-10-01", "2026-10-08", "2026-10-15", "2026-10-22")
    _reports(c.slug, "2026-10-05", "2026-10-12-23h05", "2026-10-26")
    return a, b, c


def test_dashboard_indicators(three):
    a, b, c = three
    d = service.dashboard()
    assert [x["veille"].slug for x in d["cards"]] == [x.slug for x in store.list_all()]
    assert d["next"]["card"]["veille"].slug == a.slug  # comparaison des instants, pas des textes
    assert d["next"]["at"] == dt.datetime(2026, 10, 28, 13, 30, tzinfo=UTC)
    assert d["critical"] == 1 and d["has_components"] is True
    assert [(r["veille"].slug, r["stamp"]) for r in d["reports"]] == [
        (c.slug, "2026-10-26"), (a.slug, "2026-10-22"), (a.slug, "2026-10-15"),
        (c.slug, "2026-10-12-23h05"), (a.slug, "2026-10-08")]  # toutes veilles confondues, plafonnées à 5
    assert d["month_usd"] == 0 and all(x["spark"] == [] for x in d["cards"])  # aucune exécution mesurée


def test_dashboard_costs_month_and_spark(env, three):
    a, _, _ = three
    old = dt.datetime(2020, 1, 6, 4, 0, tzinfo=UTC).timestamp()  # lundi, heure creuse, hors du mois
    _sessions(env, a.job_id, old, time.time() - 5)
    d = service.dashboard()
    card = next(x for x in d["cards"] if x["veille"].slug == a.slug)
    assert [r["started_at"].year for r in card["spark"]] == [2020, dt.datetime.now().year]  # plus ancienne d'abord
    assert card["month_usd"] == pytest.approx(card["spark"][-1]["cost_usd"]) and card["month_usd"] > 0
    assert d["month_usd"] == pytest.approx(card["month_usd"])
    assert service.card(a) == card  # carte seule (htmx) : même contenu qu'à l'accueil


def test_dashboard_without_veilles_or_schedule(env):
    assert service.dashboard() == {"cards": [], "next": None, "month_usd": 0, "critical": 0,
                                   "has_components": False, "reports": []}
    _veille("En pause", paused=True)
    d = service.dashboard()
    assert d["next"] is None and d["has_components"] is False


def test_costs_overview_unchanged_by_refactor(three, monkeypatch):
    """La table des tarifs est lue une fois pour toutes les veilles (et non une fois par veille)."""
    from veille_core import pricing
    calls = []
    real = pricing.load
    monkeypatch.setattr(pricing, "load", lambda: calls.append(1) or real())
    ov = service.costs_overview()
    assert len(ov["veilles"]) == 3 and len(calls) == 1
    calls.clear()
    service.dashboard()
    assert len(calls) == 1


# ---------------------------------------------------------------- interface
def test_dashboard_page(client, three):  # noqa: F811
    a, b, c = three
    login(client)
    page = client.get("/").text
    assert 'class="hero"' in page and "Prochaine exécution" in page and f'href="/veilles/{a.slug}">Veille A</a>' in page
    assert re.search(r"<dt>Actives</dt><dd>2<small> / 3</small></dd>", page)
    assert re.search(r'<div class="alert"><dt>Fin de vie</dt><dd><a href="/composants">1</a></dd></div>', page)
    for v in three:
        assert f'<article id="row-{v.slug}" class="vcard' in page
    assert 'class="vcard paused"' in page and "Reprendre" in page
    assert page.count('<li>') >= 5 and f'/veilles/{c.slug}/reports/2026-10-26-veille.pdf' in page
    assert 'href="/" aria-current="page"' in page


def test_dashboard_card_shows_cost_and_keeps_it_after_htmx_action(client, env, three):  # noqa: F811
    a, _, _ = three
    _sessions(env, a.job_id, time.time() - 5)
    csrf = login(client)
    card = client.get("/").text.split(f'id="row-{a.slug}"')[1].split("</article>")[0]
    assert '<a class="spark" href="/veilles/veille-a#couts">' in card and "<rect " in card and "ce mois" in card
    r = client.post(f"/veilles/{a.slug}/pause", headers={"X-CSRF-Token": csrf, "HX-Request": "true",
                                                         "HX-Target": f"row-{a.slug}"})
    assert r.status_code == 200 and "Reprendre" in r.text and 'class="spark"' in r.text  # même carte qu'au chargement
    assert 'id="couts"' in client.get(f"/veilles/{a.slug}").text  # ancre du mini-graphe


def test_dashboard_empty_and_all_paused(client, env):  # noqa: F811
    login(client)
    page = client.get("/").text
    assert "Aucune veille" in page and 'class="kpis"' not in page and 'class="cards"' not in page
    _veille("En pause", paused=True)
    page = client.get("/").text
    assert "Rien de planifié" in page and "Toutes les veilles sont en pause." in page
    assert "<dt>Fin de vie</dt>" not in page  # pas de veille composants
    (env / "cron" / "jobs.json").write_text("illisible")  # jobs de Hermes illisibles : jamais « tout en pause »
    page = client.get("/").text
    assert "Rien de planifié" in page and "en pause." not in page and 'href="/health">Santé</a>' in page


@pytest.mark.parametrize("path,current", [
    ("/", 'href="/" aria-current="page"'),
    ("/veilles/veille-a", 'href="/" aria-current="page"'),  # une veille relève de l'accueil
    ("/composants", 'href="/composants" aria-current="page"'),
    ("/veilles/new", 'href="/veilles/new" aria-current="page"'),
    ("/settings", 'href="/settings" aria-current="page"'),
    ("/sources", 'href="/sources" aria-current="page"'),
])
def test_navigation_marks_current_page(client, three, path, current):  # noqa: F811
    login(client)
    page = client.get(path).text
    nav = page[page.index('<nav class="tabs"'):page.index("</nav>")]
    assert current in nav and nav.count('aria-current="page"') == 1  # sur le seul lien de la page (pas sur « Plus »)
    assert ('<summary class="active">' in nav) == (path in ("/settings", "/sources"))
    assert 'action="/logout"' in nav  # déconnexion dans le menu « Plus »


def test_login_page_has_no_navigation(client):  # noqa: F811
    page = client.get("/login").text
    assert '<nav class="tabs"' not in page and 'class="nav"' not in page and "viewport-fit=cover" in page


@pytest.mark.parametrize("path", ["/", "/veilles/veille-a", "/composants", "/veilles/new", "/settings", "/sources",
                                  "/health"])
def test_pages_respect_csp(client, three, path, monkeypatch):  # noqa: F811
    """CSP sans 'unsafe-inline' : ni style ni script en ligne, ni gestionnaire d'événement dans le HTML."""
    import veille_ui.app as app_mod
    monkeypatch.setattr(app_mod, "brevo_account", lambda: {"error": "hors ligne"})
    login(client)
    r = client.get(path)
    assert r.status_code == 200
    assert not re.search(r"\sstyle=", r.text)
    assert not re.search(r"<script(?![^>]*\ssrc=)", r.text)
    assert not re.search(r"\son[a-z]+=", r.text)
