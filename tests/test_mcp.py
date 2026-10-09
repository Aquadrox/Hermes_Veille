"""Serveur MCP : outils testés à travers le VRAI protocole (client MCP en mémoire), plus un
test stdio de bout en bout (sous-processus `python -m veille_mcp`, comme Hermes le lance)."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import Client, StdioServerParameters  # noqa: E402

from veille_core import hermes, service, store  # noqa: E402
from veille_mcp.server import build_server  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def call(server, tool, args=None):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(tool, args or {})
    return asyncio.run(go())


def data(result):
    assert not result.is_error, result.content[0].text
    sc = result.structured_content
    if sc is not None:
        return sc.get("result", sc)
    return json.loads(result.content[0].text) if result.content[0].text[:1] in "[{" else result.content[0].text


def tools(server):
    async def go():
        async with Client(server) as c:
            return {t.name: t for t in (await c.list_tools()).tools}
    return asyncio.run(go())


@pytest.fixture
def srv(env):
    return build_server()


@pytest.fixture
def demo(env):
    return service.create_veille(name="Veille Cyber", subject="cybersécurité OT", schedule_expr="0 8 * * 1",
                                 recipients=["a@example.org"], brief="# Brief\n\nCouverture : <A COMPLÉTER>\n")


def test_annotations_split_read_and_write(srv):
    t = tools(srv)
    assert len(t) == 30
    reads = {n for n, x in t.items() if x.annotations.read_only_hint}
    assert reads == {"list_veilles", "get_veille", "get_brief", "list_brief_templates", "list_reports",
                     "read_report", "get_run_log", "run_history", "preview_schedule", "get_costs", "health",
                     "list_components", "list_notices", "preview_component_import"}
    assert t["archive_veille"].annotations.destructive_hint is True
    assert t["run_veille_now"].annotations.open_world_hint is True


def test_readonly_mode_hides_write_tools(env):
    t = tools(build_server(readonly=True))
    assert len(t) == 14 and all(x.annotations.read_only_hint for x in t.values())


def test_list_and_get(srv, demo):
    rows = data(call(srv, "list_veilles"))
    assert rows[0]["slug"] == "veille-cyber" and rows[0]["state"] == "actif"
    v = data(call(srv, "get_veille", {"slug": "veille-cyber"}))
    assert v["recipients"] == ["a@example.org"] and len(v["next_runs"]) == 3
    assert v["frequency"] == "chaque lundi à 8h"


def test_unknown_slug_is_a_clean_tool_error(srv):
    r = call(srv, "get_veille", {"slug": "inconnue"})
    assert r.is_error and "veille inconnue" in r.content[0].text


def test_create_from_template_fills_placeholders(srv, env):
    v = data(call(srv, "create_veille", {
        "name": "Veille Hydrogène", "subject": "hydrogène industriel", "recipients": ["x@example.org"],
        "frequency": "weekly", "time": "07:30", "day_of_week": "mardi"}))
    assert v["slug"] == "veille-hydrogene" and v["cron"] == "30 7 * * 2"
    brief = data(call(srv, "get_brief", {"slug": "veille-hydrogene"}))
    assert "Veille Hydrogène" in brief and "<DOMAINE>" not in brief
    assert hermes.get_job(store.load("veille-hydrogene").job_id)["schedule"]["expr"] == "30 7 * * 2"
    assert service.read_audit(1)[0]["actor"] == "mcp"


def test_create_rejects_bad_input(srv):
    r = call(srv, "create_veille", {"name": "X", "subject": "y", "recipients": ["pas-une-adresse"]})
    assert r.is_error and "invalide" in r.content[0].text
    r = call(srv, "create_veille", {"name": "X", "subject": "y", "recipients": ["a@b.fr"], "day_of_week": "funday"})
    assert r.is_error and "jour inconnu" in r.content[0].text


def test_update_schedule_and_recipients(srv, demo):
    v = data(call(srv, "update_veille", {"slug": "veille-cyber", "frequency": "monthly", "day_of_month": 1,
                                          "time": "06:00"}))
    assert v["cron"] == "0 6 1 * *" and v["frequency"] == "le 1er de chaque mois à 6h"
    r = data(call(srv, "add_recipients", {"slug": "veille-cyber", "recipients": ["b@example.org", "A@example.org"]}))
    assert r["recipients"] == ["a@example.org", "b@example.org"]
    r = data(call(srv, "remove_recipients", {"slug": "veille-cyber", "recipients": ["a@example.org"]}))
    assert r["recipients"] == ["b@example.org"]
    r = call(srv, "remove_recipients", {"slug": "veille-cyber", "recipients": ["b@example.org"]})
    assert r.is_error and "au moins un destinataire" in r.content[0].text
    r = data(call(srv, "set_recipients", {"slug": "veille-cyber", "recipients": ["c@example.org"]}))
    assert r["recipients"] == ["c@example.org"]


def test_edit_brief_requires_unique_match(srv, demo):
    data(call(srv, "edit_brief", {"slug": "veille-cyber", "find": "<A COMPLÉTER>", "replace": "Siemens, Schneider"}))
    assert "Siemens, Schneider" in store.read_brief("veille-cyber")
    r = call(srv, "edit_brief", {"slug": "veille-cyber", "find": "absent", "replace": "x"})
    assert r.is_error and "trouvé 0 fois" in r.content[0].text


def test_pause_resume_and_run(srv, demo):
    assert data(call(srv, "pause_veille", {"slug": "veille-cyber"}))["state"] == "en pause"
    r = call(srv, "run_veille_now", {"slug": "veille-cyber"})
    assert r.is_error and "en pause" in r.content[0].text
    assert data(call(srv, "resume_veille", {"slug": "veille-cyber"}))["state"] == "actif"
    started = data(call(srv, "run_veille_now", {"slug": "veille-cyber"}))
    assert started["started"] is True
    import time
    for _ in range(50):
        log = data(call(srv, "get_run_log", {"slug": "veille-cyber"}))
        if "Triggered" in log["log"]:
            break
        time.sleep(0.1)
    assert "Triggered job" in log["log"]


def test_reports_and_host_paths(srv, demo, monkeypatch):
    monkeypatch.setenv("VEILLE_HOST_DATA", "/root/veille-hermes/data")
    rdir = store.veille_dir("veille-cyber") / "rapports"
    (rdir / "2026-10-05-veille.md").write_text("# Synthèse\n\nTrois messages clés.\n")
    (rdir / "2026-10-05-veille.pdf").write_bytes(b"%PDF")
    reports = data(call(srv, "list_reports", {"slug": "veille-cyber"}))
    assert reports[0]["pdf_host_path"] == "/root/veille-hermes/data/veilles/veille-cyber/rapports/2026-10-05-veille.pdf"
    rep = data(call(srv, "read_report", {"slug": "veille-cyber"}))
    assert rep["date"] == "2026-10-05" and "Trois messages clés" in rep["markdown"]
    r = call(srv, "read_report", {"slug": "veille-cyber", "date": "2020-01-01"})
    assert r.is_error
    (rdir / "2026-10-05-17h40-veille.md").write_text("# Relance du soir\n")  # 2e rapport du même jour
    reports = data(call(srv, "list_reports", {"slug": "veille-cyber"}))
    assert [(r["date"], r["time"]) for r in reports] == [("2026-10-05", "17:40"), ("2026-10-05", "")]
    rep = data(call(srv, "read_report", {"slug": "veille-cyber", "date": "2026-10-05"}))
    assert rep["time"] == "17:40" and "Relance du soir" in rep["markdown"]


def test_preview_schedule_and_health(srv, demo):
    p = data(call(srv, "preview_schedule", {"frequency": "weekdays", "time": "09:15"}))
    assert p["cron"] == "15 9 * * 1-5" and len(p["next_runs"]) == 3
    assert call(srv, "preview_schedule", {"frequency": "cron", "cron": "* * * * *"}).is_error
    h = data(call(srv, "health"))
    assert h["veilles"] == 1 and h["actives"] == 1 and h["problems"] == []


def test_archive_requires_confirmation(srv, demo):
    r = call(srv, "archive_veille", {"slug": "veille-cyber", "confirm_slug": "oui"})
    assert r.is_error and store.exists("veille-cyber")
    data(call(srv, "archive_veille", {"slug": "veille-cyber", "confirm_slug": "veille-cyber"}))
    assert not store.exists("veille-cyber")


def test_stdio_end_to_end(env, demo):
    """Comme Hermes : sous-processus stdio, handshake, appel d'outil, erreur d'outil sans casser le flux."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "veille_mcp"],
                                   env={**os.environ, "PYTHONPATH": str(REPO)}, cwd=str(REPO))

    async def go():
        async with Client(params) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            bad = await c.call_tool("get_veille", {"slug": "inconnue"})
            ok = await c.call_tool("list_veilles", {})
            return names, bad, ok

    names, bad, ok = asyncio.run(go())
    assert "list_veilles" in names and bad.is_error
    assert data(ok)[0]["slug"] == "veille-cyber"


def test_check_flag(env):
    import subprocess

    out = subprocess.run([sys.executable, "-m", "veille_mcp", "--check", "--readonly"], cwd=REPO,
                         capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO)}, check=True)
    info = json.loads(out.stdout)
    assert info["readonly"] is True and len(info["tools"]) == 14


def test_get_costs_and_peak_preview(srv, demo, env):
    from test_costs import make_db

    assert data(call(srv, "get_costs", {"slug": "veille-cyber"}))["runs"] == []
    make_db(env, store.load("veille-cyber").job_id)
    c = data(call(srv, "get_costs", {"slug": "veille-cyber", "days": 3650}))
    assert len(c["runs"]) == 2 and c["runs"][1]["peak_hours"] is True
    assert c["runs"][0]["input_cached_tokens"] == 1_600_000
    assert c["total_days_usd"] > 0
    ov = data(call(srv, "get_costs", {"days": 3650}))
    assert ov["veilles"][0]["slug"] == "veille-cyber"
    p = data(call(srv, "preview_schedule", {"frequency": "weekly", "day_of_week": "lundi", "time": "08:00"}))
    assert p["deepseek_peak"] is True
    p = data(call(srv, "preview_schedule", {"frequency": "weekly", "day_of_week": "lundi", "time": "06:30"}))
    assert p["deepseek_peak"] is False
    h = data(call(srv, "health"))
    assert "deepseek_balance" in h and "cost_month_usd" in h
