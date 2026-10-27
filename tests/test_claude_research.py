"""Recherche déléguée à Claude Code (option `claude_research`) : pont de l'hôte (scripts/claude-bridge.py) avec
un faux binaire `claude`, client du conteneur (claude_research.py), garde-fou, prompt, CLI, MCP et interface."""
import importlib.util
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from veille_core import claude_research, guard, render, service, store
from veille_core import cli as veillectl
from veille_core.store import StoreError

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("claude_bridge", REPO / "scripts" / "claude-bridge.py")
bridge_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_mod)

ANSWER = {
    "items": [
        {"theme": "OT", "title": "Faille PLC", "summary": "Un correctif.", "url": "https://ex.org/a",
         "date": "2026-10-20", "source": "ex.org", "confidence": "haute", "verified": True},
        {"theme": "OT", "title": "Sans lien", "summary": "x", "url": "accueil", "date": "2026-10-21",
         "source": "y"},
    ],
    "coverage": [{"theme": "OT", "status": "couvert"}],
    "notes": "RAS",
}

REPORT = {"report_md": "# Veille Cyber\n\n::: {.card}\nFaille PLC corrigée [src](https://ex.org/a).\n:::\n\n"
                        + "Analyse détaillée. " * 20 + "\n\n<div class=\"chart-slot\"></div>\n",
          "charts": {"charts": [{"title": "Failles", "source": "ex.org", "bars": [{"label": "PLC", "value": 3}]}]},
          "mail": {"period": "Semaine 44 · 26–30 octobre 2026", "summary": "Une faille PLC corrigée."}}

FAKE_CLAUDE = """\
#!{python}
import json, os, sys
log = os.environ["FAKE_CLAUDE_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({{"args": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd()}}) + "\\n")
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
stdin = open(log).read().splitlines()[-1]
if mode == "crash" or (os.environ.get("FAKE_CLAUDE_CRASH_IF") and os.environ["FAKE_CLAUDE_CRASH_IF"] in stdin):
    print("boom", file=sys.stderr)
    sys.exit(1)
if mode == "slow":
    import time
    time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "1")))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 7,
                  "total_cost_usd": 0.42, "result": "fini",
                  "usage": {{"server_tool_use": {{"web_search_requests": 0, "web_fetch_requests": 4}}}},
                  "modelUsage": {{"claude-opus-5-5": {{"webSearchRequests": 0}},
                                  "claude-haiku-5-5": {{"webSearchRequests": 9}}}},
                  "structured_output": {{**json.loads(os.environ["FAKE_CLAUDE_ANSWER"]),
                                         **(json.loads(os.environ.get("FAKE_CLAUDE_REPORT") or "{{}}")
                                            if "report_md" in " ".join(sys.argv) else {{}})}}}}))
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE.format(python=sys.executable))
    path.chmod(0o755)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_ANSWER", json.dumps(ANSWER))
    monkeypatch.setenv("FAKE_CLAUDE_REPORT", json.dumps(REPORT))
    return path, log


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def bridge(env, fake_claude, monkeypatch):
    """Pont réel sur une socket courte (limite de 108 caractères), état dans un dossier jetable."""
    short = Path(tempfile.mkdtemp(prefix="cb-", dir="/tmp"))
    sock = short / "b.sock"
    monkeypatch.setenv(claude_research.SOCKET_ENV, str(sock))
    monkeypatch.setattr(claude_research, "POLL_S", 0.05)
    b = bridge_mod.Bridge(short / "state", str(fake_claude[0]), max_per_day=3, timeout=30)
    handler = type("H", (bridge_mod.Handler,), {"bridge": b, "log_message": lambda *a: None})
    server = bridge_mod.Server(str(sock), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield b
    deadline = time.time() + 10  # travaux encore lancés (recherches lentes) : finis avant de retirer leur dossier
    while time.time() < deadline and b.health()["running"] + b.health()["queued"]:
        time.sleep(0.05)
    server.shutdown()
    server.server_close()
    shutil.rmtree(short, ignore_errors=True)


@pytest.fixture
def veille(env):
    v = service.create_veille(name="Veille Cyber", subject="cybersécurité OT", schedule_expr="0 8 * * 1",
                              recipients=["a@example.org"], brief="# Brief\n\nThèmes : OT, IT.\n")
    rapports = v.dir / "rapports"
    (rapports / "2026-10-20-08h00-veille.md").write_text("# R\n\n[src](https://deja.vu/article). Voir https://x.org/b.\n")
    return v


def enabled(v):
    return service.update_veille(v.slug, claude_research=True)


def report_file(v):
    return v.dir / "rapports" / "2026-10-27-08h00-veille.md"


# --------------------------------------------------------------------------
# Option de la veille, prompt et garde-fou
# --------------------------------------------------------------------------
def test_option_is_off_by_default_and_prompt_unchanged(veille):
    assert veille.claude_research is False
    prompt = render.render(veille)
    assert "veillectl research claude" not in prompt and "0. RECHERCHE ET RÉDACTION" not in prompt
    assert "4. RECHERCHE : web_search / web_extract" in prompt and "5. RÉDIGE" in prompt


def test_option_toggles_prompt_and_guard(veille):
    s_off = guard.session(task_id=f"cron:{veille.job_id}:e")
    call = {"command": f"veillectl research claude {veille.slug} {report_file(veille)}",
            "timeout": guard.COMMAND_TIMEOUT}
    assert "commande non autorisée" in guard.check("terminal", call, s_off)
    v = enabled(veille)
    prompt = render.render(v)
    assert (f'{{"command": "veillectl research claude {v.slug} {v.dir}/rapports/D-veille.md", '
            f'"timeout": {guard.COMMAND_TIMEOUT}}}') in prompt
    assert "0. RECHERCHE ET RÉDACTION DÉLÉGUÉES À CLAUDE" in prompt and "EN COURS" in prompt and "ÉCHEC" in prompt
    assert "passe directement à l'étape 8 (PUBLIE)" in prompt and "5. RÉDIGE" in prompt  # repli gardé
    s_on = guard.session(task_id=f"cron:{v.job_id}:e")
    assert s_on.claude is True
    assert guard.check("terminal", call, s_on) is None
    # timeout maximal obligatoire, aucun argument de plus, pas pour une autre veille
    assert "timeout 360 obligatoire" in guard.check("terminal", {"command": call["command"]}, s_on)
    assert guard.check("terminal", {**call, "command": call["command"] + " --question x"}, s_on)
    assert guard.check("terminal", {**call, "command": f"veillectl research claude {v.slug}"}, s_on)
    assert guard.check("terminal", {**call, "command": f"veillectl research claude autre-veille {report_file(v)}"},
                       s_on)
    assert guard.check("terminal", {**call, "command": f"veillectl research ask {v.slug}"}, s_on)
    audit = (v.dir.parent / ".audit.log").read_text()
    assert "recherche Claude activée" in audit
    v = service.update_veille(v.slug, claude_research=False)
    assert "veillectl research claude" not in render.render(v)


def test_model_and_effort_per_veille(bridge, veille, fake_claude, capsys):
    with pytest.raises(StoreError, match="modèle de Claude inconnu"):
        service.update_veille(veille.slug, claude_model="gpt")
    v = service.update_veille(veille.slug, claude_research=True, claude_model="Sonnet", claude_effort="medium")
    assert (v.claude_model, v.claude_effort) == ("sonnet", "medium")
    assert "modèle Claude : sonnet" in (v.dir.parent / ".audit.log").read_text()
    rec = claude_research.for_job(v.slug, seconds=20)
    args = calls(fake_claude[1])[0]["args"]
    assert args[args.index("--model") + 1] == "sonnet" and args[args.index("--effort") + 1] == "medium"
    s = claude_research.summary(rec)
    assert (s["model"], s["effort"], s["models"]) == ("sonnet", "medium", ["claude-haiku-5-5", "claude-opus-5-5"])
    assert veillectl.main(["edit", v.slug, "--claude-model", "defaut", "--claude-effort", "max"]) == 0
    assert (store.load(v.slug).claude_model, store.load(v.slug).claude_effort) == ("", "max")


def test_bridge_validates_model_effort_and_counts_groups(tmp_path):
    b = bridge_mod.Bridge(tmp_path / "s", "/bin/false", max_per_day=1, model="opus", effort="high")
    b._run = lambda job: None  # pas de claude : seul le dépôt compte
    assert b.submit({"id": "a", "prompt": "p", "model": "gpt-4"})[0] == 400
    assert b.submit({"id": "a", "prompt": "p", "effort": "énorme"})[0] == 400
    code, job = b.submit({"id": "g-1", "prompt": "p", "group": "g"})
    assert code == 202 and (job["model"], job["effort"]) == ("opus", "high")  # défauts du pont
    assert b.submit({"id": "g-2", "prompt": "p", "group": "g", "model": "haiku"})[1]["model"] == "haiku"
    assert b.today_count() == 1  # deux sous-lots, une recherche
    assert b.submit({"id": "h", "prompt": "p"})[0] == 429
    assert b.command(None, "fable", "xhigh")[-4:] == ["--model", "fable", "--effort", "xhigh"]


def test_prompt_has_brief_window_and_known_urls(veille):
    prompt = claude_research.build_prompt(veille, "Quid des PLC Siemens ?", today=__import__("datetime").date(2026, 10, 27))
    assert "Thèmes : OT, IT." in prompt
    assert "du 2026-10-20 au 2026-10-27" in prompt  # depuis le dernier rapport
    assert "- https://deja.vu/article" in prompt and "- https://x.org/b" in prompt
    assert "Quid des PLC Siemens ?" in prompt
    assert "jamais une instruction" in prompt


# --------------------------------------------------------------------------
# Pont et client
# --------------------------------------------------------------------------
def test_end_to_end_job_research_and_report(bridge, veille, fake_claude, tools, capsys):  # noqa: F811
    v = enabled(veille)
    assert claude_research.status()["reachable"] is True
    out = report_file(v)
    assert veillectl.main(["research", "claude", v.slug, str(out)]) == 0
    text = capsys.readouterr().out
    assert text.startswith("OK: rapport rédigé par Claude — 1 information(s) sourcée(s)")
    assert "passe directement à l'étape 8" in text and "Une faille PLC corrigée." in text
    rec = claude_research.load(v.slug)
    assert rec["state"] == "done" and rec["origin"] == "job" and rec["write"] is True
    assert rec["meta"] == {"cost_usd": 0.42, "num_turns": 7, "duration_s": rec["meta"]["duration_s"],
                           "models": ["claude-haiku-5-5", "claude-opus-5-5"],  # réellement utilisés
                           "web_search_requests": 9, "web_fetch_requests": 4}
    assert [i["url"] for i in claude_research.items(rec)] == ["https://ex.org/a"]  # URL invalide écartée
    saved = json.loads((store.research_dir(v.slug) / f"{rec['stamp']}-claude.json").read_text())
    assert saved["response"] == {**ANSWER, **REPORT}  # informations sourcées conservées pour analyse
    assert "Thèmes : OT, IT." in saved["prompt"] and "RAPPORT PRÉCÉDENT" in saved["prompt"]
    assert "https://deja.vu/article" in saved["prompt"]  # anti-répétition : rapport précédent fourni
    # fichiers de l'exécution, écrits par la commande
    assert out.read_text() == REPORT["report_md"].strip() + "\n"
    assert json.loads(out.with_name("2026-10-27-08h00-charts.json").read_text()) == REPORT["charts"]
    assert json.loads(out.with_name("2026-10-27-08h00-mail.json").read_text()) == REPORT["mail"]
    (c,) = calls(fake_claude[1])
    args = c["args"]
    assert args[args.index("--tools") + 1] == "WebSearch,WebFetch"
    assert "--strict-mcp-config" in args and args[args.index("--setting-sources") + 1] == "project"
    assert "--model" not in args and "--effort" not in args  # défauts du compte
    assert json.loads(args[args.index("--json-schema") + 1]) == claude_research.SCHEMA_REPORT
    assert "Thèmes : OT, IT." in c["stdin"] and c["cwd"].startswith(tempfile.gettempdir())
    # relancée par l'agent : même recherche, aucun nouvel appel à Claude
    assert claude_research.for_job(v.slug, seconds=1)["stamp"] == rec["stamp"]
    assert len(calls(fake_claude[1])) == 1
    # Hermes orchestre : publication déterministe (PDF, e-mail), puis plus aucune réécriture de ce rapport
    assert "OK: PDF généré" in service.report_publish(v.slug, out)
    assert veillectl.main(["research", "claude", v.slug, str(out)]) == 1
    assert "déjà publié" in capsys.readouterr().err


def test_report_missing_falls_back_to_agent(bridge, veille, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_REPORT", json.dumps({"report_md": "trop court", "charts": {}, "mail": {}}))
    v = enabled(veille)
    assert veillectl.main(["research", "claude", v.slug, str(report_file(v))]) == 1
    text = capsys.readouterr().out
    assert text.startswith("ÉCHEC de la rédaction par Claude") and "étapes du prompt (1 à 9)" in text
    assert not report_file(v).exists()


def test_slow_research_is_polled_by_slices(bridge, veille, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "1.5")
    v = enabled(veille)
    rec = claude_research.for_job(v.slug, seconds=0.2)
    assert rec["state"] == "pending"
    assert claude_research.agent_text(rec).startswith(claude_research.PENDING)
    deadline = time.time() + 15
    while rec["state"] == "pending" and time.time() < deadline:
        rec = claude_research.for_job(v.slug, seconds=1)
    assert rec["state"] == "done"
    assert len(claude_research.stamps(v.slug)) == 1


def test_claude_failure_tells_agent_to_search_itself(bridge, veille, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    v = enabled(veille)
    assert veillectl.main(["research", "claude", v.slug, str(report_file(v))]) == 1
    out = capsys.readouterr().out
    assert out.startswith("ÉCHEC de la rédaction par Claude") and "(1 à 9)" in out
    rec = claude_research.load(v.slug)
    assert rec["state"] == "error" and "boom" in rec["error"]


def test_bridge_absent_falls_back(env, veille, monkeypatch, capsys):
    monkeypatch.setenv(claude_research.SOCKET_ENV, "/tmp/absent-claude-bridge.sock")
    v = enabled(veille)
    assert claude_research.status()["reachable"] is False
    assert veillectl.main(["research", "claude", v.slug, str(report_file(v))]) == 1
    out = capsys.readouterr().out
    assert "pont Claude absent" in out and "(1 à 9)" in out
    assert veillectl.main(["research", "status"]) == 1


def test_job_research_requires_the_option(bridge, veille):
    with pytest.raises(StoreError, match="désactivée"):
        claude_research.for_job(veille.slug)


def test_daily_cap_and_on_demand(bridge, veille):
    # à la demande : option désactivée acceptée, question conservée
    rec = service.research_start(veille.slug, "Quid des PLC ?", origin="mcp", actor="mcp")
    assert rec["question"] == "Quid des PLC ?" and not veille.claude_research
    service.research_start(veille.slug)
    service.research_start(veille.slug)
    with pytest.raises(service.ServiceError, match="plafond de 3 recherches"):
        service.research_start(veille.slug)


def test_bridge_restart_fails_interrupted_jobs(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "x-1.json").write_text(json.dumps({"id": "x-1", "state": "running", "created_at": "2026-10-27"}))
    b = bridge_mod.Bridge(state, "/bin/false")
    assert b.get("x-1")["state"] == "error"
    assert b.submit({"id": "Bad Id", "prompt": "p"})[0] == 400
    assert b.submit({"id": "ok", "prompt": " "})[0] == 400


# --------------------------------------------------------------------------
# CLI, MCP, interface
# --------------------------------------------------------------------------
def test_cli_edit_ask_list_show(bridge, veille, capsys):
    assert veillectl.main(["edit", veille.slug, "--claude-research", "on"]) == 0
    assert store.load(veille.slug).claude_research is True
    capsys.readouterr()
    assert veillectl.main(["research", "ask", veille.slug, "--question", "PLC ?", "--wait"]) == 0
    shown = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert shown["state"] == "done" and "prompt" not in shown
    assert veillectl.main(["research", "list", veille.slug]) == 0
    assert "done" in capsys.readouterr().out
    assert veillectl.main(["research", "show", veille.slug, "20990101-000000"]) == 1


def test_mcp_tools(bridge, veille):
    pytest.importorskip("mcp")
    from test_mcp import call, data

    from veille_mcp.server import build_server

    srv = build_server()
    assert data(call(srv, "update_veille", {"slug": veille.slug, "claude_research": True}))["claude_research"] is True
    started = data(call(srv, "start_claude_research", {"slug": veille.slug, "question": "PLC ?"}))
    deadline = time.time() + 15
    while True:
        got = data(call(srv, "get_claude_research", {"slug": veille.slug, "stamp": started["stamp"]}))
        if got["state"] != "pending" or time.time() > deadline:
            break
        time.sleep(0.1)
    assert got["state"] == "done" and got["items"] == 1
    assert got["response"].startswith(guard.UNTRUSTED_OPEN) and "https://ex.org/a" in got["response"]
    assert data(call(srv, "list_claude_research", {"slug": veille.slug}))[0]["origin"] == "mcp"
    assert data(call(srv, "health"))["claude_bridge"]["reachable"] is True


def test_ui_checkbox_detail_and_json(bridge, veille, monkeypatch):
    monkeypatch.setattr(claude_research, "POLL_S", 0.05)
    from test_ui import PASSWORD, create_form, login  # noqa: F401

    from veille_ui import auth
    monkeypatch.setenv("VEILLE_UI_PASSWORD_HASH", auth.hash_password(PASSWORD))
    monkeypatch.setenv("VEILLE_UI_SECRET", "s" * 64)
    auth._failures.clear()
    from fastapi.testclient import TestClient

    from veille_ui.app import create_app
    client = TestClient(create_app(), base_url="http://localhost")
    csrf = login(client)
    form = create_form(csrf, name="Veille OT", claude_research="on")
    r = client.post("/veilles/new", data=form, follow_redirects=False)
    assert r.status_code == 303
    assert store.load("veille-ot").claude_research is True
    edit = client.get("/veilles/veille-ot/edit").text
    assert 'name="claude_research" checked' in edit and 'name="claude_model"' in edit and '"xhigh"' in edit
    r = client.post("/veilles/veille-ot/edit", data={**form, "claude_model": "haiku", "claude_effort": "low"},
                    follow_redirects=False)
    assert r.status_code == 303 and store.load("veille-ot").claude_model == "haiku"
    r = client.post("/veilles/veille-ot/edit", data={k: v for k, v in form.items() if k != "claude_research"},
                    follow_redirects=False)
    assert r.status_code == 303 and store.load("veille-ot").claude_research is False
    r = client.post("/veilles/veille-ot/research", data={"csrf": csrf}, follow_redirects=False)
    assert r.headers["location"] == "/veilles/veille-ot?ok=research"
    stamp = claude_research.stamps("veille-ot")[0]
    claude_research.wait("veille-ot", claude_research.load("veille-ot"), 10)
    page = client.get("/veilles/veille-ot").text
    assert f"/veilles/veille-ot/recherches/{stamp}.json" in page and "terminée" in page
    r = client.get(f"/veilles/veille-ot/recherches/{stamp}.json")
    assert r.headers["content-type"].startswith("application/json") and r.json()["response"] == ANSWER
    assert client.get("/veilles/veille-ot/recherches/../../x.json").status_code == 404
    assert "Pont Claude Code" in client.get("/health").text


def test_agent_text_truncates(veille):
    rec = {"stamp": "20261027-080000", "slug": veille.slug, "state": "done", "window": ["a", "b"],
           "response": {"items": [{**ANSWER["items"][0], "summary": "x" * 5000}] * 20}}
    out = claude_research.agent_text(rec)
    assert len(out) < claude_research.MAX_AGENT_CHARS + 200 and "tronqué" in out
    assert out.startswith("OK:")


# --------------------------------------------------------------------------
# Socket du pont : dossier au service, jamais un lien ni un dossier modifiable par un autre compte
# --------------------------------------------------------------------------
def test_socket_dir_must_be_private(tmp_path):
    good = tmp_path / "run"
    good.mkdir(mode=0o755)
    bridge_mod.check_dir(good)
    link = tmp_path / "lien"
    link.symlink_to(good)
    with pytest.raises(SystemExit, match="n'est pas un dossier"):
        bridge_mod.check_dir(link)
    shared = tmp_path / "partage"
    shared.mkdir()
    shared.chmod(0o775)
    with pytest.raises(SystemExit, match="modifiable que par lui"):
        bridge_mod.check_dir(shared)


def test_bridge_process_creates_socket_0660(tmp_path):
    import os
    import subprocess

    run = Path(tempfile.mkdtemp(prefix="cbp-", dir="/tmp"))
    sock = run / "b.sock"
    proc = subprocess.Popen([sys.executable, str(REPO / "scripts" / "claude-bridge.py"), "--socket", str(sock),
                             "--state-dir", str(tmp_path / "state"), "--group", str(os.getgid()),
                             "--claude", "/bin/false"], stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 10
        while not sock.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert oct(sock.stat().st_mode & 0o777) == oct(0o660)
    finally:
        proc.terminate()
        proc.wait(10)
        shutil.rmtree(run, ignore_errors=True)
    assert not sock.exists()  # retirée à l'arrêt


# --------------------------------------------------------------------------
# Veille composants : Hermes orchestre, Claude recherche et analyse le lot (sous-lots), sa réponse est D-pcn.json
# --------------------------------------------------------------------------
from test_components import cand, tools, watch  # noqa: E402, F401 - fixtures partagées

from veille_core import components as comp  # noqa: E402

PCN = {"notices": [cand(["LM317T"], manufacturer="Texas Instruments", summary="Fin de vie.",
                    impact="Plus de régulateur après 2027.", urgency="immédiate",
                    action="Passer la dernière commande avant le 2027-03-01.")],
       "statuses": [],
       "replacements": [{"mpn": "LM317T", "manufacturer": "Texas Instruments", "candidates": [
           {"mpn": "LM317HVT", "manufacturer": "Texas Instruments", "compatibility": "broche à broche",
            "notes": "tension d'entrée plus élevée", "url": "https://www.ti.com/product/LM317HV"},
           {"mpn": "SANS-PAGE", "manufacturer": "X", "compatibility": "fonctionnel", "url": ""}]}],
       "synthesis": "Une fin de vie urgente chez TI.", "notes": "RAS"}


def planned(v, keys=None):
    """Lot tiré par un scan (sans Digi-Key) : toute la liste par défaut."""
    data = comp.load_components(v.slug)
    data["batch"] = keys or [comp.key_of(c["mpn"], c["manufacturer"]) for c in data["components"]]
    comp.save_components(v.slug, data)


def out_file(v):
    return v.dir / "rapports" / "2026-10-27-23h00-pcn.json"


def test_components_option_prompt_and_guard(watch):  # noqa: F811
    out = out_file(watch)
    call = {"command": f"veillectl research claude {watch.slug} {out}", "timeout": guard.COMMAND_TIMEOUT}
    assert "commande non autorisée" in guard.check("terminal", call, guard.session(task_id=f"cron:{watch.job_id}:e"))
    assert "1 bis" not in render.render(watch)
    v = service.update_veille(watch.slug, claude_research=True)
    prompt = render.render(v)
    assert f'"veillectl research claude {v.slug} {v.dir}/rapports/D-pcn.json", "timeout": 360' in prompt
    assert "1 bis. RECHERCHE ET ANALYSE DÉLÉGUÉES À CLAUDE" in prompt and "3. RECHERCHE : pour chaque composant" in prompt
    assert prompt.index("1 bis.") < prompt.index("2. LIS le brief")
    s = guard.session(task_id=f"cron:{v.job_id}:e")
    assert guard.check("terminal", call, s) is None
    assert guard.check("terminal", {**call, "command": f"veillectl research claude {v.slug}"}, s)
    veille_md = v.dir / "rapports" / "2026-10-27-23h00-veille.md"
    assert guard.check("terminal", {**call, "command": f"veillectl research claude {v.slug} {veille_md}"}, s)
    assert f"veillectl research claude {v.slug} {out}" in guard.check("terminal", {"command": call["command"]}, s)


def test_chunks_keep_manufacturers_together():
    lot = ([{"mpn": f"T{i}", "manufacturer": "TI"} for i in range(4)]
           + [{"mpn": f"S{i}", "manufacturer": "ST"} for i in range(3)]
           + [{"mpn": f"N{i}", "manufacturer": "NXP"} for i in range(7)])
    parts = claude_research.chunks(lot, 5)
    assert [[c["mpn"][0] for c in p] for p in parts] == [list("TTTT"), list("SSS"), list("NNNNN"), list("NN")]
    assert sum(map(len, parts)) == len(lot)


def test_components_end_to_end(bridge, watch, fake_claude, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setenv("FAKE_CLAUDE_ANSWER", json.dumps(PCN))
    v = service.update_veille(watch.slug, claude_research=True)
    with pytest.raises(StoreError, match="lancer d'abord veillectl components scan"):
        claude_research.for_job(v.slug)
    planned(v)
    assert veillectl.main(["research", "claude", v.slug]) == 1
    assert "fichier de sortie manquant" in capsys.readouterr().err
    assert veillectl.main(["research", "claude", v.slug, str(v.dir / "config.md")]) == 1
    assert "fichier de sortie attendu" in capsys.readouterr().err
    out = out_file(v)
    assert veillectl.main(["research", "claude", v.slug, str(out)]) == 0
    text = capsys.readouterr().out
    assert text.startswith("OK: recherche Claude terminée — 3 composant(s) recherché(s), 1 avis (dont 1 fin(s)")
    assert "passe directement à l'étape ENREGISTRE" in text
    assert json.loads(out.read_text()) == {k: v for k, v in PCN.items() if k != "notes"}
    (c,) = calls(fake_claude[1])
    assert json.loads(c["args"][c["args"].index("--json-schema") + 1]) == claude_research.SCHEMA_PCN
    assert "LOT À RECHERCHER (3 composant(s))" in c["stdin"] and '"mpn": "STM32F103C8T6"' in c["stdin"]
    rec = claude_research.load(v.slug)
    assert claude_research.summary(rec)["kind"] == "composants" and claude_research.summary(rec)["components"] == 3
    # Hermes orchestre : finalize enregistre la réponse de Claude
    assert "OK: veille composants" in service.components_finalize(v.slug, out)
    (eol,) = [n for n in comp.load_notices(v.slug) if n["type"] == "EOL"]
    assert (eol["mpn"], eol["urgency"], eol["action"]) == ("LM317T", "immédiate", PCN["notices"][0]["action"])
    data = comp.load_components(v.slug)
    assert all(c.get("web_checked_at") for c in data["components"])
    lm = next(c for c in data["components"] if c["mpn"] == "LM317T")
    assert [a["mpn"] for a in lm["web_alternatives"]["items"]] == ["LM317HVT"]  # sans page : refusé
    md = next((v.dir / "rapports").glob("*-veille.md")).read_text()
    assert "Synthèse de la recherche" in md and "Une fin de vie urgente chez TI" in md
    assert "Analyse — urgence immédiate" in md and "Remplaçants trouvés sur le web" in md and "LM317HVT" in md


def test_failed_sublot_is_not_marked_searched(bridge, watch, fake_claude, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setenv("FAKE_CLAUDE_ANSWER", json.dumps(PCN))
    monkeypatch.setenv("FAKE_CLAUDE_CRASH_IF", "STM32F103C8T6")
    monkeypatch.setattr(claude_research, "CHUNK_SIZE", 1)
    v = service.update_veille(watch.slug, claude_research=True)
    planned(v)
    out = out_file(v)
    assert veillectl.main(["research", "claude", v.slug, str(out)]) == 0
    text = capsys.readouterr().out
    assert "2 composant(s) recherché(s)" in text and "1 composant(s) d'un sous-lot en échec" in text
    rec = claude_research.load(v.slug)
    assert len(rec["parts"]) == 3 and [p["state"] for p in rec["parts"]].count("error") == 1
    assert len(calls(fake_claude[1])) == 3 and claude_research.status()["today"] == 1  # une recherche pour le pont
    stm = comp.key_of("STM32F103C8T6", "STMicroelectronics")
    assert json.loads(out.read_text())["unsearched"] == [stm]
    service.components_finalize(v.slug, out)
    checked = {comp.key_of(c["mpn"], c["manufacturer"]): c.get("web_checked_at")
               for c in comp.load_components(v.slug)["components"]}
    assert checked[stm] is None and all(d for k, d in checked.items() if k != stm)


def test_all_sublots_failed_falls_back_to_agent(bridge, watch, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    v = service.update_veille(watch.slug, claude_research=True)
    planned(v)
    assert veillectl.main(["research", "claude", v.slug, str(out_file(v))]) == 1
    text = capsys.readouterr().out
    assert text.startswith("ÉCHEC de la recherche Claude") and "étapes 3 et 4" in text
    assert not out_file(v).exists()


def test_long_research_progress_and_abandon(bridge, watch, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "1")
    monkeypatch.setattr(claude_research, "CHUNK_SIZE", 1)
    v = service.update_veille(watch.slug, claude_research=True)
    planned(v)
    rec = claude_research.for_job(v.slug, seconds=0.1)
    assert claude_research.agent_text(rec).startswith("EN COURS : recherche Claude")
    assert "0/3 sous-lot(s) terminé(s)" in claude_research.agent_text(rec)
    monkeypatch.setattr(claude_research, "MAX_WAIT_S", 0)
    rec = claude_research.for_job(v.slug, seconds=0.1)
    assert rec["state"] in ("done", "error") and "abandon" in (rec.get("error") or "")


def test_components_on_demand_previews_next_batch(bridge, watch, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_CLAUDE_ANSWER", json.dumps(PCN))
    rec = service.research_start(watch.slug, origin="mcp", actor="mcp")  # option désactivée, aucun lot en attente
    assert len(rec["batch"]) == 2  # batch_size de la veille : le lot que tirerait le prochain passage
    assert comp.load_components(watch.slug)["batch"] == []
    done = claude_research.wait(watch.slug, rec, 10)
    assert done["state"] == "done" and claude_research.items(done)[0]["mpns"] == ["LM317T"]
