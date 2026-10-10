"""Avancement d'un lancement manuel suivi (veille composants) : écrit par le scan Digi-Key, les signaux de
l'agent et finalize ; lu par l'interface (page de la veille, tableau de bord) et le MCP. Rien n'est écrit
pour une exécution planifiée."""
import json
import os
import re
from types import SimpleNamespace

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
    service.import_components(watch.slug, comp.parse_components("\n".join(f"R{i};Fab" for i in range(7))))
    manual(watch.slug)
    clock = iter(1000 + 0.6 * i for i in range(1, 100))  # 0,6 s entre deux références
    monkeypatch.setattr(progress, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    writes = []
    real = store.atomic_write
    monkeypatch.setattr(store, "atomic_write", lambda path, data, mode=0o644: (writes.append(path),
                                                                               real(path, data, mode)))
    service.components_scan(watch.slug, client=FakeClient({}))  # 10 références
    # références 1, 3, 5, 7, 9 (une seconde écoulée depuis la précédente écriture), puis le lot du jour
    assert sum(1 for p in writes if p.name == progress.FILE) == 6


def test_progress_write_failure_never_breaks_the_run(watch, tools, monkeypatch):  # noqa: F811
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


@pytest.mark.parametrize("content", [
    "{pas du json", "[]",
    '{"v": 0, "log": "LOG", "kind": "analyse", "step": "web"}',  # autre version de l'image
    '{"v": 1, "log": "LOG", "kind": "analyse", "step": "web", "batch": "x"}',
    '{"v": 1, "log": "LOG", "kind": "analyse", "step": "web", "batch": [{"mpn": "LM317T", "state": "x"}]}',
    '{"v": 1, "log": "LOG", "kind": ["x"], "step": "web"}',
    '{"v": 1, "log": "LOG", "kind": "analyse", "step": "done", "result": {"new": "1"}}',
])
def test_malformed_or_other_version_state_is_ignored(watch, ui, tools, content, capsys):  # noqa: F811
    """Fichier mal formé ou d'une autre version : ignoré partout, jamais une erreur (pages, job, CLI)."""
    log = manual(watch.slug)
    progress.path(watch.slug).write_text(content.replace("LOG", log))
    assert service.run_progress(watch.slug) is None and service.describe(watch)["progress"] is None
    assert service.components_scan(watch.slug, client=FakeClient({}))["track_progress"] is False
    assert cli.main(["components", "progress", watch.slug, "LM317T"]) == 0
    assert "aucun lancement suivi" in capsys.readouterr().out
    assert "OK: veille composants" in finalize(watch, [])
    client, _ = ui
    assert client.get("/").status_code == 200
    assert client.get(f"/veilles/{watch.slug}/runs/{log}", params={"poll": 1}).status_code == 200


def test_digikey_stop_is_never_shown_as_a_success(watch, monkeypatch):  # noqa: F811
    from veille_core import digikey
    manual(watch.slug, kind="statuts")
    service.components_scan(watch.slug, client=FakeClient({"LM317T": digikey.DigiKeyError("HTTP 401")}),
                            plan_batch=False)
    view = service.run_progress(watch.slug, running=False)
    assert view["finished"] and view["warning"] == "HTTP 401" and view["summary"] == "Terminé : 0 / 3 référence(s)"
    manual(watch.slug)  # analyse : budget de temps dépassé, la suite continue avec un avertissement
    service.components_scan(watch.slug, client=FakeClient({}), budget_s=-1)
    view = service.run_progress(watch.slug, running=True)
    assert view["step"] == "web" and "budget de temps" in view["warning"]


def test_finalize_rerun_keeps_what_the_first_pass_found(watch, tools, monkeypatch, tmp_path):  # noqa: F811
    manual(watch.slug)
    service.components_scan(watch.slug)
    service.components_progress(watch.slug, ["STM32F103C8T6"])
    bad = watch.dir / "rapports" / "bad.json"
    bad.write_text("{pas du json")
    with pytest.raises(comp.ComponentsError):  # l'agent corrige le JSON et relance
        service.components_finalize(watch.slug, bad)
    st = state(watch.slug)
    assert st["step"] == "finalize" and [b["state"] for b in st["batch"]] == ["done", "done"]
    good, broken = os.environ["BUILD_REPORT"], tmp_path / "broken_pdf.py"
    broken.write_text("import sys; sys.exit(1)\n")
    monkeypatch.setenv("BUILD_REPORT", str(broken))
    with pytest.raises(service.ServiceError, match="PDF"):  # puis le PDF échoue : il relance encore
        finalize(watch, [cand(["LM317T"])])
    monkeypatch.setenv("BUILD_REPORT", good)
    finalize(watch, [cand(["LM317T"])])  # avis déjà enregistré au passage précédent
    st = state(watch.slug)
    assert st["step"] == "done" and st["result"] == {"new": 1, "critical": 1, "rejected": 0}
    assert [b["notices"] for b in st["batch"]] == [1, 0]


def test_finished_run_takes_no_more_writes(watch, tools):  # noqa: F811
    """Entre la fin de finalize et la fin du processus (réponse de l'agent), une commande tapée à la main
    ou une exécution planifiée n'écrit plus rien."""
    manual(watch.slug)
    service.components_scan(watch.slug)
    finalize(watch, [])
    before = state(watch.slug)
    assert service.components_scan(watch.slug)["track_progress"] is False
    assert service.components_progress(watch.slug, ["LM317T"]) is False and state(watch.slug) == before


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
    assert [s["state"] for s in v["steps"]] == ["done", "failed", "todo"]
    assert v["minutes"] is None  # fin réelle inconnue : pas de durée trompeuse
    assert progress.view({**base, "step": "done"}, running=False)["minutes"] == 7
    single = progress.view({**base, "kind": "statuts", "step": "digikey",
                            "digikey": {"done": 1, "total": 3, "current": "NE555P"}}, True)
    assert single["summary"] == "Statut Digi-Key : 1 / 3 référence(s) · en cours : NE555P"  # pas d'« étape 1/1 »
    assert progress.view({**base, "step": "start"}, running=False)["summary"] == "Interrompu au démarrage"
    dk_off = progress.view({**base, "kind": "statuts", "step": "done", "digikey": {"skipped": True}}, False)
    assert dk_off["summary"] == "Terminé : clés absentes, étape ignorée" and dk_off["max"] is None
    assert progress.view({**base, "step": "digikey", "digikey": {"done": 0, "total": 0}}, True)["max"] is None


def test_run_progress_only_for_the_latest_log(watch):  # noqa: F811
    old = manual(watch.slug)
    assert service.run_progress(watch.slug, old) is not None
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
    steps = re.findall(r'<li class="(\w+)">([^<]+)</li>', r[r.index('class="steps"'):])[:3]
    assert steps == [("done", "Statut Digi-Key"), ("current", "Recherche web"), ("todo", "Rapport et envoi")]
    assert re.search(r'<progress value="0" max="2"[^>]*aria-label="Avancement"', r)
    assert 'class="searching" title="Texas Instruments — recherche en cours"' in r
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
    assert "hx-trigger" not in r.split(">")[0] and "Étape 2/3" not in r
    assert re.search(r'class="badge ok">\s*actif\s*</span>', r)
    service.archive(watch.slug)
    r = client.get(f"/veilles/{watch.slug}/row")
    assert r.status_code == 286 and r.text == ""  # htmx retire la ligne et arrête d'interroger


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
