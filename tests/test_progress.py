"""Avancement d'un lancement manuel suivi (veille composants) : écrit par le scan Digi-Key, les signaux de
l'agent et finalize ; lu par l'interface (page de la veille, tableau de bord) et le MCP. Rien n'est écrit
pour une exécution planifiée."""
import json
import os

import pytest
from test_components import FakeClient, cand, finalize, tools, ui, watch  # noqa: F401 - fixtures partagées

from veille_core import cli, progress, render, service, store
from veille_core import components as comp


def manual(slug, kind="analyse", pid=None):
    """Lancement manuel tel que l'interface le démarre (journal créé, comme spawn_detached) ; PID vivant
    (ce processus) par défaut."""
    def launch(log):
        log.touch()
        return pid or os.getpid()
    return service._start(slug, launch, "test", "run", kind)


def state(slug):
    return progress.read(slug)


@pytest.fixture
def every_write(monkeypatch):
    """Toutes les écritures, sans attendre MIN_INTERVAL_S entre deux références Digi-Key."""
    monkeypatch.setattr(progress, "MIN_INTERVAL_S", 0)


# --------------------------------------------------------------------------
# Écriture : lancement suivi seulement
# --------------------------------------------------------------------------
def test_scan_publishes_digikey_progress_then_batch(watch, every_write):  # noqa: F811
    log = manual(watch.slug)
    assert state(watch.slug)["step"] == "start" and state(watch.slug)["log"] == log
    seen = []
    out = service.components_scan(watch.slug, client=FakeClient(
        {}, on_call=lambda mpn: seen.append(dict(state(watch.slug)["digikey"]))))
    assert seen == [{"done": 0, "total": 3, "current": "LM317T"}, {"done": 1, "total": 3, "current": "STM32F103C8T6"},
                    {"done": 2, "total": 3, "current": "NE555P"}]
    st = state(watch.slug)
    assert st["step"] == "web" and st["digikey"] == {"done": 3, "total": 3}
    assert st["batch"] == [{"mpn": "LM317T", "manufacturer": "Texas Instruments", "state": "pending"},
                           {"mpn": "STM32F103C8T6", "manufacturer": "STMicroelectronics", "state": "pending"}]
    assert out["track_progress"] is True  # l'agent signalera ses groupes de recherche


def test_scheduled_run_writes_nothing(watch):  # noqa: F811
    out = service.components_scan(watch.slug, client=FakeClient({}))
    assert out["track_progress"] is False and state(watch.slug) is None
    assert service.components_progress(watch.slug, ["LM317T"]) is False
    finalize(watch, [])
    assert state(watch.slug) is None and service.run_progress(watch.slug) is None


def test_finished_manual_run_is_not_tracked_anymore(watch):  # noqa: F811
    """Exécution planifiée après un lancement manuel terminé (PID mort) : l'ancien état reste intact."""
    manual(watch.slug, pid=999_999)
    before = state(watch.slug)
    assert service.components_scan(watch.slug, client=FakeClient({}))["track_progress"] is False
    finalize(watch, [])
    assert state(watch.slug) == before


def test_agent_signals_then_finalize(watch, tools):  # noqa: F811
    manual(watch.slug)
    service.components_scan(watch.slug)  # sans clés Digi-Key : étape ignorée
    assert state(watch.slug)["digikey"] == {"skipped": True}
    assert service.components_progress(watch.slug, ["lm317t"]) is True  # casse ignorée
    assert [b["state"] for b in state(watch.slug)["batch"]] == ["searching", "pending"]
    service.components_progress(watch.slug, ["STM32F103C8T6", "INCONNU", "<script>"])
    st = state(watch.slug)
    assert [b["state"] for b in st["batch"]] == ["done", "searching"]
    assert "INCONNU" not in json.dumps(st) and "<script>" not in json.dumps(st)  # rien n'est recopié
    finalize(watch, [cand(["LM317T"])])
    st = state(watch.slug)
    assert st["step"] == "done" and st["phase"] == "mail"
    assert [(b["state"], b["notices"]) for b in st["batch"]] == [("done", 1), ("done", 0)]
    assert st["result"] == {"new": 1, "critical": 1, "rejected": 0}
    view = service.run_progress(watch.slug, running=False)
    assert view["finished"] and not view["interrupted"]
    assert view["summary"] == "Terminé : 1 nouvel(s) avis, dont 1 fin(s) de vie / dernière(s) commande(s)"


def test_finalize_phases_and_mail_failure_shows_where_it_stopped(watch, tools, monkeypatch):  # noqa: F811
    manual(watch.slug)
    service.components_scan(watch.slug)
    phases = []
    real = progress.Tracker.update

    def spy(self, throttle=False, **changes):
        real(self, throttle, **changes)
        phases.append((self.state["step"], self.state.get("phase")))
    monkeypatch.setattr(progress.Tracker, "update", spy)
    monkeypatch.setenv("FAKE_BREVO_FAIL", "1")
    with pytest.raises(service.ServiceError):
        finalize(watch, [])
    assert phases == [("finalize", "notices"), ("finalize", "pdf"), ("finalize", "mail")]
    view = service.run_progress(watch.slug, running=False)  # le lancement s'est arrêté là
    assert view["interrupted"] and [s["state"] for s in view["steps"]] == ["done", "done", "failed"]
    assert view["summary"] == "Interrompu à l'étape 3/3 — Rapport et envoi : envoi de l'e-mail"


def test_finalize_linked_veille_ends_without_mail_phase(watch, tools):  # noqa: F811
    service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                          recipients=["a@example.org"], brief="# b\n", include_components=watch.slug)
    manual(watch.slug)
    service.components_scan(watch.slug)
    finalize(watch, [])
    st = state(watch.slug)
    assert st["step"] == "done" and st["phase"] == "pdf" and tools() == []


def test_status_check_progress(watch, monkeypatch, every_write):  # noqa: F811
    from veille_core import hermes
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(hermes, "spawn_detached", lambda cmd, log, env=None: (log.touch(), os.getpid())[1])
    service.check_statuses_now(watch.slug, actor="test")
    assert state(watch.slug)["kind"] == "statuts"
    service.components_scan(watch.slug, client=FakeClient({}), plan_batch=False)
    st = state(watch.slug)
    assert st["step"] == "done" and st["digikey"] == {"done": 3, "total": 3} and "batch" not in st
    assert service.run_progress(watch.slug, running=False)["summary"] == "Terminé : 3 / 3 référence(s)"


def test_status_only_scan_does_not_touch_a_running_analysis(watch):  # noqa: F811
    manual(watch.slug)
    service.components_scan(watch.slug, client=FakeClient({}), plan_batch=False)  # tapée à la main
    assert state(watch.slug)["step"] == "start"


def test_digikey_writes_are_throttled(watch, monkeypatch):  # noqa: F811
    """Boucle Digi-Key (une référence ≈ 0,6 s) : une écriture par seconde au plus, plus les changements d'étape."""
    service.add_components(watch.slug, comp.parse_components("\n".join(f"R{i};Fab" for i in range(300))))
    manual(watch.slug)
    writes = []
    real = store.atomic_write
    monkeypatch.setattr(store, "atomic_write", lambda path, data, mode=0o644: (writes.append(path),
                                                                               real(path, data, mode)))
    service.components_scan(watch.slug, client=FakeClient({}))  # 303 références, instantanées
    tracked = [p for p in writes if p.name == progress.FILE]
    assert 2 <= len(tracked) <= 3  # première référence, (une seconde écoulée), lot du jour


def test_progress_write_failure_never_breaks_the_run(watch, tools, monkeypatch):  # noqa: F811
    from types import SimpleNamespace
    manual(watch.slug)

    def broken(*a, **k):
        raise OSError("disque plein")
    # Seules les écritures de l'avancement échouent (composants, avis et rapport s'écrivent).
    monkeypatch.setattr(progress, "store", SimpleNamespace(atomic_write=broken, now_iso=store.now_iso,
                                                           runs_dir=store.runs_dir))
    assert service.components_scan(watch.slug, client=FakeClient({}))["track_progress"] is True
    assert service.components_progress(watch.slug, ["LM317T"]) is True
    assert "OK: veille composants" in finalize(watch, [cand(["LM317T"])])
    assert state(watch.slug)["step"] == "start"  # rien n'a pu être écrit, rien n'a échoué


def test_corrupted_progress_file_is_ignored(watch):  # noqa: F811
    manual(watch.slug)
    progress.path(watch.slug).write_text("{pas du json")
    assert service.run_progress(watch.slug) is None
    assert service.components_scan(watch.slug, client=FakeClient({}))["track_progress"] is False


def test_cli_progress_is_always_a_success(watch, capsys):  # noqa: F811
    assert cli.main(["components", "progress", watch.slug, "LM317T"]) == 0
    assert "aucun lancement suivi" in capsys.readouterr().out
    manual(watch.slug)
    service.components_scan(watch.slug, client=FakeClient({}))
    assert cli.main(["components", "progress", watch.slug, "LM317T", "STM32F103C8T6"]) == 0
    assert "✓ avancement noté" in capsys.readouterr().out
    assert [b["state"] for b in state(watch.slug)["batch"]] == ["searching", "searching"]


def test_prompt_asks_for_signals_only_when_tracked(watch):  # noqa: F811
    prompt = render.render(watch)
    assert f"veillectl components progress {watch.slug} <MPN>" in prompt
    assert '"track_progress": true' in prompt and "ne l'exécute JAMAIS" in prompt


# --------------------------------------------------------------------------
# Vue commune
# --------------------------------------------------------------------------
def test_view_steps_and_summaries():
    base = {"log": "20261010-100000.log", "kind": "analyse", "started_at": "2026-10-10T10:00:00+02:00",
            "updated_at": "2026-10-10T10:07:30+02:00"}
    v = progress.view({**base, "step": "start"}, running=True)
    assert v["summary"] == "Démarrage…" and [s["state"] for s in v["steps"]] == ["todo"] * 3
    v = progress.view({**base, "step": "digikey", "digikey": {"done": 12, "total": 300, "current": "LM317T"}}, True)
    assert v["summary"] == "Étape 1/3 — Statut Digi-Key : 12 / 300 référence(s) · en cours : LM317T"
    assert (v["value"], v["max"]) == (12, 300) and [s["state"] for s in v["steps"]] == ["current", "todo", "todo"]
    web = {**base, "step": "web", "batch": [{"mpn": "A1", "manufacturer": "X", "state": "done"},
                                            {"mpn": "B2", "manufacturer": "X", "state": "searching"},
                                            {"mpn": "C3", "manufacturer": "Y", "state": "searching"},
                                            {"mpn": "D4", "manufacturer": "Y", "state": "pending"}]}
    v = progress.view(web, True)
    assert v["summary"] == "Étape 2/3 — Recherche web : 1 / 4 composant(s) · en cours : B2, C3"
    assert v["current"] == "B2, C3"
    v = progress.view(web, running=False)
    assert v["interrupted"] and v["summary"] == "Interrompu à l'étape 2/3 — Recherche web : 1 / 4 composant(s)"
    assert [s["state"] for s in v["steps"]] == ["done", "failed", "todo"] and v["minutes"] == 7
    assert progress.view({**base, "step": "start"}, running=False)["summary"] == "Interrompu au démarrage"
    dk_off = progress.view({**base, "kind": "statuts", "step": "done", "digikey": {"skipped": True}}, False)
    assert dk_off["summary"] == "Terminé : clés absentes, étape ignorée" and dk_off["max"] is None
    assert progress.view({**base, "step": "digikey", "digikey": {"done": 0, "total": 0}}, True)["max"] is None


def test_run_progress_only_for_the_latest_log(watch):  # noqa: F811
    old = manual(watch.slug)
    assert service.run_progress(watch.slug, old)["log"] == old
    newer = "29991231-235959.log"  # lancement suivant (veille thématique, ou non suivi)
    (store.runs_dir(watch.slug) / newer).write_text("")
    assert service.run_progress(watch.slug) is None and service.run_progress(watch.slug, old) is None


# --------------------------------------------------------------------------
# Interface : page de la veille et tableau de bord
# --------------------------------------------------------------------------
def tracked_run(veille, monkeypatch, pid=4242):
    """Lancement suivi en pleine recherche web, PID simulé par l'interface."""
    log = manual(veille.slug)
    service.components_scan(veille.slug, client=FakeClient({}))
    service.components_progress(veille.slug, ["LM317T"])
    running = {"pid": pid}
    monkeypatch.setattr(service, "running_pid", lambda slug: running["pid"])
    return log, running


def test_ui_run_log_shows_progress(ui, watch, monkeypatch):  # noqa: F811
    client, _ = ui
    log, running = tracked_run(watch, monkeypatch)
    r = client.get(f"/veilles/{watch.slug}/runs/{log}", params={"poll": 1}).text
    assert "Étape 2/3 — Recherche web : 0 / 2 composant(s) · en cours : LM317T" in r
    assert '<li class="done">Statut Digi-Key</li><li class="current">Recherche web</li><li class="todo">' in r
    assert '<progress value="0" max="2">' in r and 'class="searching" title="Texas Instruments — recherche en cours"' in r
    assert "style=" not in r and "<script" not in r  # CSP : ni style ni script en ligne
    assert '<pre class="log">' not in r  # journal encore vide : l'avancement suffit
    running["pid"] = None  # fin sans finalize : on montre où le lancement s'est arrêté
    r = client.get(f"/veilles/{watch.slug}/runs/{log}", params={"poll": 1}).text
    panel = r.split('id="detail-head"')[0]
    assert 'class="progress failed"' in panel and "Interrompu à l&#39;étape 2/3" in panel
    assert "<progress" not in panel and 'class="searching"' not in panel  # recherche interrompue : pas faite


def test_ui_dashboard_row_polls_only_while_running(ui, watch, monkeypatch):  # noqa: F811
    client, _ = ui
    _, running = tracked_run(watch, monkeypatch)
    dash = client.get("/").text
    row = dash[dash.index(f'id="row-{watch.slug}"'):]
    assert f'hx-get="/veilles/{watch.slug}/row" hx-trigger="every 10s"' in row[:200]
    assert "Étape 2/3 — Recherche web : 0 / 2 composant(s) · en cours : LM317T" in row
    r = client.get(f"/veilles/{watch.slug}/row")
    assert r.status_code == 200 and r.text.strip().startswith(f'<tr id="row-{watch.slug}" hx-get=')
    running["pid"] = None
    r = client.get(f"/veilles/{watch.slug}/row").text
    assert "hx-trigger" not in r.split(">")[0] and "Étape 2/3" not in r and ">\n      actif\n" in r
    service.archive(watch.slug)
    assert client.get(f"/veilles/{watch.slug}/row").status_code == 286  # htmx arrête d'interroger


def test_ui_thematic_run_shows_no_progress(ui, monkeypatch, tools):  # noqa: F811
    client, _ = ui
    from veille_core import hermes
    v = service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                              recipients=["a@example.org"], brief="# b\n")
    monkeypatch.setattr(hermes, "spawn_detached", lambda cmd, log, env=None: (log.touch(), 4242)[1])
    log = service.run_now(v.slug)
    assert progress.read(v.slug) is None
    monkeypatch.setattr(service, "running_pid", lambda slug: 4242)
    assert 'class="progress' not in client.get(f"/veilles/{v.slug}/runs/{log}", params={"poll": 1}).text
    assert f'hx-get="/veilles/{v.slug}/row" hx-trigger="every 10s"' in client.get("/").text  # état final sans recharger


# --------------------------------------------------------------------------
# MCP (Telegram)
# --------------------------------------------------------------------------
def test_mcp_progress(watch, tools, monkeypatch):  # noqa: F811
    pytest.importorskip("mcp")
    from test_mcp import call, data

    from veille_mcp.server import build_server
    tracked_run(watch, monkeypatch)
    srv = build_server()
    out = data(call(srv, "get_run_log", {"slug": watch.slug}))
    p = out["progress"]
    assert out["running"] is True and p["step"] == "web" and p["current"] == "LM317T"
    assert p["summary"] == "Étape 2/3 — Recherche web : 0 / 2 composant(s) · en cours : LM317T"
    assert p["batch"] == {"searching": ["LM317T"], "pending": ["STM32F103C8T6"]} and "steps" not in p
    (row,) = data(call(srv, "list_veilles"))
    assert row["state"] == "en cours" and row["progress"] == p["summary"]
    assert "progress" in data(call(srv, "get_veille", {"slug": watch.slug}))
    finalize(watch, [cand(["LM317T"])])
    monkeypatch.setattr(service, "running_pid", lambda slug: None)  # lancement terminé
    p = data(call(srv, "get_run_log", {"slug": watch.slug}))["progress"]
    assert p["finished"] and p["batch"] == {"done": ["LM317T (1 avis)", "STM32F103C8T6"]}
    assert p["result"] == {"new": 1, "critical": 1, "rejected": 0}
