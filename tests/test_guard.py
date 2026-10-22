"""Non-régression : incident du 21-22/10/2026 sur la veille « composants-nomenclature-2 » (v0.7.1).

Tir de contrôle de 111 min, statut « ok », code de sortie 0, mais ni rapport ni e-mail : le garde-fou
avait refusé tous les appels dont l'agent avait besoin, sans dire quoi faire, et l'exécution a continué.
Désormais : refus actionnables, scan exécutable tel que le prompt le donne, arrêt au 3e refus et échec
explicite de toute exécution qui se termine sans avoir publié son rapport.
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_components import BOM, finalize, tools  # noqa: F401 - fixtures partagées
from test_prompt_injection import load_plugin

from veille_core import cli, components, guard, hermes, render, service, store

SLUG = "composants-nomenclature-2"
DATE = "date +%Y-%m-%d-%Hh%M"


@pytest.fixture(autouse=True)
def no_tracked_runs(monkeypatch):
    """Aucune exécution suivie d'un test à l'autre."""
    monkeypatch.setattr(guard, "_runs", {}, raising=False)


@pytest.fixture
def incident(tools):  # noqa: F811
    return service.create_veille(name="Composants nomenclature 2", slug=SLUG, subject="BOM",
                                 schedule_expr=service.COMPONENTS_SCHEDULE, recipients=["achats@example.org"],
                                 brief=store.brief_templates()["composants"], kind="composants",
                                 components=components.parse_components(BOM), batch_size=2)


@pytest.fixture
def run(incident):
    return guard.session(task_id=f"cron:{incident.job_id}:exec1")


def scan(**extra) -> dict:
    return {"command": f"veillectl components scan {SLUG}", **extra}


# --------------------------------------------------------------------------
# 1. Paramètre supplémentaire : refusé, avec la liste de ceux qui sont permis
# --------------------------------------------------------------------------
@pytest.mark.parametrize("extra", [{"background": True}, {"notify": True}, {"pty": True},
                                   {"background": True, "notify": True}])
def test_extra_terminal_parameter_is_refused_with_the_allowed_ones(run, extra):
    reason = guard.check("terminal", scan(timeout=guard.COMMAND_TIMEOUT, **extra), run)
    assert reason.startswith(f"paramètre(s) {', '.join(sorted(extra))} non autorisé(s) pour terminal : ")
    assert "params permis : command, timeout (<= 360 s), workdir ; relance sans les autres" in reason
    assert "premier plan" in reason  # pourquoi : pas d'arrière-plan pour une veille


def test_extra_file_parameter_lists_the_allowed_ones(run, incident):
    reason = guard.check("read_file", {"path": f"{incident.dir}/config.md", "encoding": "latin-1"}, run)
    assert "params permis : limit, offset, path ; relance sans les autres" in reason


# --------------------------------------------------------------------------
# 2. Scan : timeout = COMMAND_TIMEOUT accepté ; au-delà, en deçà ou absent, refus qui donne l'appel exact
#    (0.7.2 : 360 s au lieu de 1 200, sous le délai d'outil de Hermes ; plafonds : tests/test_scan_timeout.py)
# --------------------------------------------------------------------------
def test_scan_runs_with_the_documented_timeout(run):
    assert guard.check("terminal", scan(timeout=360), run) is None
    assert guard.check("terminal", scan(timeout="360", workdir=str(store.veille_dir(SLUG))), run) is None
    reason = guard.check("terminal", scan(timeout=1200), run)
    assert reason.startswith("durée maximale 360 s") and "relance avec un timeout <= 360" in reason


@pytest.mark.parametrize("args", [scan(), scan(timeout=60), scan(timeout=0)])
def test_scan_without_its_timeout_is_refused(run, args):
    """Sans timeout, Hermes arrête la commande au bout de 180 s, avant la fin de sa tranche."""
    reason = guard.check("terminal", args, run)
    assert reason.startswith("timeout 360 obligatoire pour veillectl components scan : ")
    assert f'{{"command": "veillectl components scan {SLUG}", "timeout": 360}}' in reason


# --------------------------------------------------------------------------
# 3. Dossier de la veille : lecture refusée (choix C), refus explicite et documenté
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tool", ["read_file", "search_files"])
@pytest.mark.parametrize("name", ["", "veille.json", "components.json", "notices.json", "recipients.txt"])
def test_veille_folder_is_not_readable_and_the_refusal_says_what_is(run, incident, tool, name):
    reason = guard.check(tool, {"path": str(incident.dir / name) if name else str(incident.dir)}, run)
    assert reason.startswith(f"fichier hors de la veille {SLUG} : ")
    assert f"{incident.dir}/config.md" in reason and f"{incident.dir}/job-prompt.md" in reason
    assert f"{incident.dir}/rapports/" in reason and "fichier précis" in reason


def test_veille_folder_refusal_is_documented_in_the_prompt(incident):
    prompt = render.render(incident)
    assert "OUTILS AUTORISÉS" in prompt and "fichier précis" in prompt


def test_write_refusal_lists_the_output_files(run, incident):
    reason = guard.check("write_file", {"path": f"{incident.dir}/components.json", "content": "{}"}, run)
    assert reason.startswith(f"fichier hors de la veille {SLUG} : ") and "D-pcn.json" in reason


# --------------------------------------------------------------------------
# 4. Fin d'exécution sans rapport publié : échec explicite
# --------------------------------------------------------------------------
def test_run_without_report_fails(run, incident):
    old = incident.dir / "rapports" / "2026-10-12-23h15-veille.pdf"  # rapport d'une exécution précédente
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"%PDF")
    os.utime(old, (time.time() - 3600,) * 2)
    assert guard.enforce("terminal", {"command": DATE}, run, "tour-1") is None
    reason = guard.finish("tour-1", run)
    assert reason == "aucun rapport publié par cette exécution (ni PDF, ni e-mail)"
    last = service.read_audit(1)[0]
    assert (last["actor"], last["action"], last["slug"], last["detail"]) == ("veille-guard", "échec", SLUG, reason)
    assert guard.finish("tour-1", None) is None  # une seule fois par exécution


def test_run_with_report_succeeds(run, incident, tools):  # noqa: F811
    assert guard.enforce("terminal", {"command": DATE}, run, "tour-1") is None
    finalize(incident, [])  # PDF et e-mail de l'exécution
    assert guard.finish("tour-1", run) is None
    assert [a["action"] for a in service.read_audit()].count("échec") == 0


def test_run_without_any_tool_call_fails(run):
    assert "aucun rapport" in guard.finish("tour-sans-appel", run)


def test_unguarded_or_unknown_runs_are_never_failed(incident):
    assert guard.finish("tour-1", None) is None  # session interactive
    assert guard.finish("tour-1", guard.session(cron=True)) is None  # job planifié inconnu : rien à publier
    assert guard.finish("", guard.session(task_id=f"cron:{incident.job_id}:x")) is None  # tour inconnu de Hermes


def test_plugin_declares_the_failure_to_hermes(run, incident, monkeypatch):
    """Hermes v0.22 : une réponse finale dont la 1re ligne est [CRON_FAILURE] met l'exécution en échec
    (last_status « error », last_error = la suite)."""
    plugin = load_plugin(monkeypatch)
    ids = {"task_id": f"cron:{incident.job_id}:exec1", "session_id": "20261021_231500_9f3a", "turn_id": "tour-1"}
    assert plugin._on_pre_tool_call(tool_name="terminal", args={"command": DATE}, **ids) is None
    out = plugin._on_transform_llm_output(response_text="Terminé.", session_id=ids["session_id"],
                                          turn_id="tour-1")
    assert out.split("\n")[0] == "[CRON_FAILURE]"
    assert out.split("\n")[1] == "veille-guard : aucun rapport publié par cette exécution (ni PDF, ni e-mail)"
    assert out.endswith("Réponse de l'agent :\n" + guard.wrap("Terminé."))  # texte exposé au web : non fiable
    # rapport publié : réponse inchangée ; session interactive : jamais touchée
    plugin._on_pre_tool_call(tool_name="terminal", args={"command": DATE}, **{**ids, "turn_id": "tour-2"})
    finalize(incident, [])
    assert plugin._on_transform_llm_output(response_text="OK", turn_id="tour-2") is None
    assert plugin._on_transform_llm_output(response_text="OK", session_id="s", turn_id="t") is None


def test_plugin_stops_the_run_and_bounds_the_agent_text(incident, monkeypatch):
    plugin = load_plugin(monkeypatch)
    ids = {"task_id": f"cron:{incident.job_id}:exec1", "session_id": "20261021_231500_9f3a", "turn_id": "tour-1"}
    verdicts = [plugin._on_pre_tool_call(tool_name="process_manage", args={"action": "list"}, **ids) for _ in range(3)]
    assert guard.STOPPED not in verdicts[1]["message"] and guard.STOPPED in verdicts[2]["message"]
    assert guard.STOPPED in plugin._on_pre_tool_call(tool_name="web_search", args={"query": "x"}, **ids)["message"]
    out = plugin._on_transform_llm_output(response_text="x" * 5000, turn_id="tour-1")
    assert "veille-guard : le guard a refusé 4 appels : l'analyse ne peut pas aboutir ; aucun rapport" in out
    assert "x" * 1000 in out and "x" * 1001 not in out  # last_error borné


def test_run_wait_fails_without_report(incident, capsys):
    """`make run-now` : `hermes cron run` sort en 0 même en échec ; veillectl vérifie lui-même le rapport."""
    assert cli.main(["run", SLUG, "--wait"]) == 1
    assert "aucun rapport publié" in capsys.readouterr().err


def hermes_run(publish: bool, **job):
    """Faux `hermes cron run` : publie (ou non) le rapport, puis enregistre l'exécution comme Hermes."""
    def run_sync(job_id, timeout=0):
        if publish:
            (store.veille_dir(SLUG) / "rapports").mkdir(parents=True, exist_ok=True)
            (store.veille_dir(SLUG) / "rapports" / f"{store.report_stamp()}-veille.pdf").write_bytes(b"%PDF")
        jobs = hermes.load_jobs()
        jobs[0].update(job)
        hermes.paths.jobs_file().write_text(json.dumps({"jobs": jobs}))
        return f"Ran now: {'failed' if job.get('last_status') == 'error' else 'succeeded'}."
    return run_sync


def test_run_wait_reports_hermes_error(incident, monkeypatch):
    reason = "veille-guard : le guard a refusé 3 appels : l'analyse ne peut pas aboutir ; rapport x publié"
    monkeypatch.setattr(hermes, "run_sync", hermes_run(False, last_run_at="2026-10-22T00:51:29+02:00",
                                                       last_status="error", last_error=reason))
    with pytest.raises(service.ServiceError, match="aucun rapport publié .* — veille-guard : le guard a refusé 3"):
        service.run_wait(SLUG)
    # rapport publié, mais exécution arrêtée par le garde-fou : échec aussi (statut de Hermes)
    monkeypatch.setattr(hermes, "run_sync", hermes_run(True, last_run_at="2026-10-22T01:30:00+02:00",
                                                       last_status="error", last_error=reason))
    with pytest.raises(service.ServiceError, match="^échec : veille-guard : le guard a refusé 3"):
        service.run_wait(SLUG)


def test_run_wait_ignores_the_status_of_a_previous_run(incident, monkeypatch):
    """`hermes cron run` qui n'a rien exécuté : l'erreur affichée n'est pas celle d'une exécution précédente."""
    monkeypatch.setattr(hermes, "run_sync", hermes_run(False, last_run_at="2026-10-15T00:00:00+02:00",
                                                       last_status="error", last_error="ancienne erreur"))
    with pytest.raises(service.ServiceError, match="ancienne erreur"):
        service.run_wait(SLUG)  # 1re fois : nouvelle date, erreur de cette exécution
    with pytest.raises(service.ServiceError) as exc:
        service.run_wait(SLUG)  # même date : rien de nouveau, seul le rapport manquant compte
    assert "ancienne erreur" not in str(exc.value) and guard.NO_REPORT in str(exc.value)


def test_run_wait_succeeds_with_report(incident, monkeypatch):
    monkeypatch.setattr(hermes, "run_sync", hermes_run(True, last_run_at="2026-10-22T00:20:00+02:00", last_status="ok"))
    assert service.run_wait(SLUG) == "Ran now: succeeded."


# --------------------------------------------------------------------------
# 5. Refus répétés : arrêt au 3e, message clair, exécution en échec
# --------------------------------------------------------------------------
def incident_calls(v) -> list[tuple[str, dict]]:
    """Les appels refusés de la nuit du 21 au 22/10/2026 (journal d'audit), dans l'ordre."""
    r = v.dir / "rapports"
    return [
        ("terminal", scan(timeout=1200, background=True)),                       # 23:07:32
        ("terminal", scan(timeout=1800)),                                        # 23:21:46
        ("search_files", {"pattern": "*", "path": str(v.dir)}),                  # 23:21:54
        ("process_manage", {"action": "poll", "session_id": "proc_1"}),          # 23:22:14
        ("read_file", {"path": str(v.dir)}),                                     # 23:30:26
        ("terminal", {"command": f"veillectl components scan {SLUG} > {r}/.scan.log 2>&1 &"}),  # 23:45:49
    ]


def test_incident_is_stopped_at_the_third_refusal(run, incident):
    calls = incident_calls(incident)
    reasons = [guard.enforce(tool, args, run, "tour-1") for tool, args in calls]
    assert all(reasons)
    assert guard.STOPPED not in reasons[0] and guard.STOPPED not in reasons[1]
    assert reasons[2].endswith("exécution arrêtée : le guard a refusé 3 appels : l'analyse ne peut pas aboutir. "
                               "Tout nouvel appel sera refusé : termine maintenant par ta réponse finale.")
    # au-delà : même un appel permis est refusé, l'agent ne peut plus que conclure
    assert guard.enforce("web_search", {"query": "LM317T EOL"}, run, "tour-1").startswith("exécution arrêtée")
    assert guard.finish("tour-1", run) == ("le guard a refusé 7 appels : l'analyse ne peut pas aboutir ; "
                                           "aucun rapport publié par cette exécution (ni PDF, ni e-mail)")
    details = [a["detail"] for a in service.read_audit(20) if a["action"] == "refus"]
    assert len(details) == 7 and details[0] == "web_search LM317T EOL — exécution arrêtée"


def test_stopped_run_fails_even_with_a_report(run, incident, tools):  # noqa: F811
    """Choix de l'utilisateur : un arrêt par le guard n'est jamais silencieux ; le message dit le rapport."""
    for _ in range(guard.MAX_REFUSALS):
        guard.enforce("process_manage", {"action": "list"}, run, "tour-1")
    finalize(incident, [])
    reason = guard.finish("tour-1", run)
    assert reason.startswith("le guard a refusé 3 appels") and re.search(r"rapport .+-veille\.pdf publié", reason)


def test_refusals_are_counted_per_run_and_concurrently(run, incident):
    for _ in range(guard.MAX_REFUSALS - 1):
        guard.enforce("process_manage", {}, run, "tour-1")
    assert guard.enforce("web_search", {"query": "x"}, run, "tour-2") is None  # autre exécution : non arrêtée
    # Appels d'outils parallèles d'une même exécution : compte exact (le GIL rend la course rare, pas impossible :
    # le verrou protège création, éviction et décision d'arrêt).
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda _: guard.enforce("process_manage", {}, run, "tour-3"), range(200)))
    assert guard._runs["tour-3"].refusals == 200 and guard._runs["tour-1"].refusals == 2


def test_stop_keeps_a_short_audit_motive(run):
    for _ in range(guard.MAX_REFUSALS - 1):
        guard.enforce("process_manage", {}, run, "tour-1")
    assert guard.enforce("terminal", {"command": "   "}, run, "tour-1").startswith("commande vide\n" + guard.STOPPED)
    assert service.read_audit(1)[0]["detail"] == "terminal — commande vide"


def test_run_tracking_is_bounded(run):
    for i in range(guard.MAX_TRACKED_RUNS + 10):
        guard.enforce("web_search", {"query": "x"}, run, f"tour-{i}")
    assert len(guard._runs) == guard.MAX_TRACKED_RUNS and "tour-0" not in guard._runs


def test_interactive_sessions_are_neither_counted_nor_stopped():
    for _ in range(5):
        assert guard.enforce("terminal", {"command": "cat /etc/hostname"}, None, "tour-1") is None
    assert guard._runs == {}


# --------------------------------------------------------------------------
# 6. Non-régression : les refus légitimes restent des refus
# --------------------------------------------------------------------------
def test_legitimate_refusals_still_apply(run, incident):
    r = incident.dir / "rapports"
    for tool, args in [
        ("process_manage", {"action": "kill", "session_id": "proc_1"}),
        ("terminal", {"command": f"veillectl components scan {SLUG} > {r}/.scan.log 2>&1 &", "timeout": 1200}),
        ("terminal", {"command": f"veillectl components scan {SLUG}; cat /opt/data/.env", "timeout": 1200}),
        ("terminal", {"command": "veillectl components scan autre-veille", "timeout": 1200}),
        ("terminal", {"command": f"veillectl components scan {SLUG} --status-only", "timeout": 1200}),
        ("read_file", {"path": "/opt/data/.env"}),
        ("search_files", {"pattern": "KEY", "path": "/opt/data"}),
        ("write_file", {"path": f"{incident.dir}/config.md", "content": "x"}),
        ("write_file", {"path": f"{r}/scan.json", "content": "{}"}),
        ("write_file", {"path": f"{r}/2026-10-21-23h00-veille.pdf", "content": "%PDF"}),
    ]:
        assert guard.check(tool, args, run), (tool, args)
    assert "outil « process_manage » non autorisé" in guard.check("process_manage", {}, run)
    assert guard.check("terminal", {"command": f"ls {r}/ > x"}, run).startswith("caractère non autorisé")
    assert guard.check("terminal", {"command": f"veillectl report publish {SLUG} {r}/2026-10-21-23h00-veille.md"},
                       run).startswith("commande non autorisée")  # commande d'une veille thématique


def test_plugin_end_of_run_check_fails_closed(incident, monkeypatch):
    """Erreur du garde-fou à la vérification finale : échec déclaré plutôt qu'un faux succès (exécution planifiée)."""
    plugin = load_plugin(monkeypatch)
    cron = f"cron_{incident.job_id}_20261021_230012"

    def boom(*a, **k):
        raise OSError("disque")
    monkeypatch.setattr(guard, "finish", boom)
    assert "garde-fou en erreur (OSError)" in plugin._on_transform_llm_output(response_text="x", session_id=cron)
    assert plugin._on_transform_llm_output(response_text="x", session_id="s") is None  # interactif
    monkeypatch.setattr(plugin, "guard", None)  # import de veille_core impossible
    monkeypatch.setattr(plugin, "_IMPORT_ERROR", "ModuleNotFoundError", raising=False)
    assert "indisponible" in plugin._on_transform_llm_output(response_text="x", session_id=cron)


def test_report_removed_during_the_check_is_ignored(incident):
    rapports = incident.dir / "rapports"
    rapports.mkdir(parents=True, exist_ok=True)
    (rapports / "2026-10-21-23h00-veille.pdf").symlink_to(rapports / "absent.pdf")
    assert store.published_since(SLUG, 0) is None
