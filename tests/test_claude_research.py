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

FAKE_CLAUDE = """\
#!{python}
import json, os, sys
log = os.environ["FAKE_CLAUDE_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({{"args": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd()}}) + "\\n")
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "crash":
    print("boom", file=sys.stderr)
    sys.exit(1)
if mode == "slow":
    import time
    time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "1")))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 7,
                  "total_cost_usd": 0.42, "result": "fini",
                  "usage": {{"server_tool_use": {{"web_search_requests": 9, "web_fetch_requests": 4}}}},
                  "structured_output": json.loads(os.environ["FAKE_CLAUDE_ANSWER"])}}))
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE.format(python=sys.executable))
    path.chmod(0o755)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_ANSWER", json.dumps(ANSWER))
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


# --------------------------------------------------------------------------
# Option de la veille, prompt et garde-fou
# --------------------------------------------------------------------------
def test_option_is_off_by_default_and_prompt_unchanged(veille):
    assert veille.claude_research is False
    prompt = render.render(veille)
    assert "veillectl research claude" not in prompt
    assert render.RESEARCH_WEB in prompt


def test_option_toggles_prompt_and_guard(veille):
    s_off = guard.session(task_id=f"cron:{veille.job_id}:e")
    call = {"command": f"veillectl research claude {veille.slug}", "timeout": guard.COMMAND_TIMEOUT}
    assert "commande non autorisée" in guard.check("terminal", call, s_off)
    v = enabled(veille)
    prompt = render.render(v)
    assert f'{{"command": "veillectl research claude {v.slug}", "timeout": {guard.COMMAND_TIMEOUT}}}' in prompt
    assert "EN COURS" in prompt and "ÉCHEC" in prompt
    s_on = guard.session(task_id=f"cron:{v.job_id}:e")
    assert s_on.claude is True
    assert guard.check("terminal", call, s_on) is None
    # timeout maximal obligatoire, aucun argument de plus, pas pour une autre veille
    assert "timeout 360 obligatoire" in guard.check("terminal", {"command": call["command"]}, s_on)
    assert guard.check("terminal", {**call, "command": call["command"] + " --question x"}, s_on)
    assert guard.check("terminal", {**call, "command": "veillectl research claude autre-veille"}, s_on)
    assert guard.check("terminal", {**call, "command": f"veillectl research ask {v.slug}"}, s_on)
    audit = (v.dir.parent / ".audit.log").read_text()
    assert "recherche Claude activée" in audit
    v = service.update_veille(v.slug, claude_research=False)
    assert "veillectl research claude" not in render.render(v)


def test_option_refused_for_components_watch(env):
    with pytest.raises(StoreError, match="veilles thématiques seulement"):
        service.create_veille(name="Compo", subject="", schedule_expr="0 23 * * 0", recipients=["a@example.org"],
                              brief="# b\n", kind="composants", components=[], claude_research=True)


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
def test_end_to_end_job_research(bridge, veille, fake_claude):
    v = enabled(veille)
    assert claude_research.status()["reachable"] is True
    rec = claude_research.for_job(v.slug, seconds=20)
    assert rec["state"] == "done" and rec["origin"] == "job"
    assert rec["meta"] == {"cost_usd": 0.42, "num_turns": 7, "duration_s": rec["meta"]["duration_s"],
                           "web_search_requests": 9, "web_fetch_requests": 4}
    assert [i["url"] for i in claude_research.items(rec)] == ["https://ex.org/a"]  # URL invalide écartée
    saved = json.loads((store.research_dir(v.slug) / f"{rec['stamp']}-claude.json").read_text())
    assert saved["response"] == ANSWER and "Thèmes : OT, IT." in saved["prompt"]
    (c,) = calls(fake_claude[1])
    args = c["args"]
    assert args[args.index("--tools") + 1] == "WebSearch,WebFetch"
    assert "--strict-mcp-config" in args and args[args.index("--setting-sources") + 1] == "project"
    assert json.loads(args[args.index("--json-schema") + 1]) == claude_research.SCHEMA
    assert "Thèmes : OT, IT." in c["stdin"] and c["cwd"].startswith(tempfile.gettempdir())
    text = claude_research.agent_text(rec)
    assert text.startswith("OK: recherche Claude terminée — 1 information(s)")
    assert json.loads(text.split("\n", 1)[1])["coverage"] == ANSWER["coverage"]
    # relancée par l'agent : même recherche, aucun nouvel appel à Claude
    assert claude_research.for_job(v.slug, seconds=1)["stamp"] == rec["stamp"]
    assert len(calls(fake_claude[1])) == 1


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
    assert veillectl.main(["research", "claude", v.slug]) == 1
    out = capsys.readouterr().out
    assert out.startswith("ÉCHEC de la recherche Claude") and "web_search" in out
    rec = claude_research.load(v.slug)
    assert rec["state"] == "error" and "boom" in rec["error"]


def test_bridge_absent_falls_back(env, veille, monkeypatch, capsys):
    monkeypatch.setenv(claude_research.SOCKET_ENV, "/tmp/absent-claude-bridge.sock")
    v = enabled(veille)
    assert claude_research.status()["reachable"] is False
    assert veillectl.main(["research", "claude", v.slug]) == 1
    assert "pont Claude absent" in capsys.readouterr().out
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
    assert 'name="claude_research" checked' in client.get("/veilles/veille-ot/edit").text
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
