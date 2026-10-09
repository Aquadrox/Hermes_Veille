import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("watchdog", REPO / "scripts" / "watchdog.py")
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


def volume(tmp_path, *, last_status="ok", report_days_ago=2, paused=False, max_age=192, size=30000, kind=None):
    vol = tmp_path / "data"
    d = vol / "veilles" / "demo"
    (d / "rapports").mkdir(parents=True)
    (d / "veille.json").write_text(json.dumps({
        "slug": "demo", "name": "Démo", "job_id": "j1", "max_age_hours": max_age,
        "created_at": "2026-01-01T00:00:00+01:00", **({"kind": kind} if kind else {})}))
    if report_days_ago is not None:
        date = (dt.datetime.now() - dt.timedelta(days=report_days_ago)).strftime("%Y-%m-%d")
        (d / "rapports" / f"{date}-veille.pdf").write_bytes(b"x" * size)
    (vol / "cron").mkdir()
    job = {"id": "j1", "name": "Démo [demo]", "last_status": last_status,
           "enabled": not paused, "state": "paused" if paused else "scheduled"}
    (vol / "cron" / "jobs.json").write_text(json.dumps({"jobs": [job]}))
    return vol


def run(vol, tmp_path, *extra):
    return watchdog.main(["--volume", str(vol), "--skip-container", "--to", "ops@example.org",
                          "--state-file", str(tmp_path / "state.json"), "--env-file", "/nonexistent",
                          *extra])


def test_healthy(tmp_path, capsys):
    assert run(volume(tmp_path), tmp_path, "--dry-run") == 0
    assert "tout va bien" in capsys.readouterr().out


@pytest.mark.parametrize("kwargs,needle", [
    ({"report_days_ago": 10}, "aucun rapport depuis"),
    ({"last_status": "error"}, "dernier run en échec"),
    ({"size": 100}, "suspicieusement petit"),
    ({"report_days_ago": 3, "max_age": 48}, "aucun rapport depuis"),
])
def test_problems(tmp_path, capsys, kwargs, needle):
    assert run(volume(tmp_path, **kwargs), tmp_path, "--dry-run") == 1
    out = capsys.readouterr().out
    assert needle in out and "[dry-run] e-mail NON envoyé" in out


def test_components_report_size_threshold(tmp_path, capsys):
    # rapport composants sans avis : court (≈ 19 Ko), mais plausible ; une veille thématique non
    assert run(volume(tmp_path, size=15000, kind="composants"), tmp_path, "--dry-run") == 0
    assert run(volume(tmp_path / "b", size=15000), tmp_path, "--dry-run") == 1
    assert run(volume(tmp_path / "c", size=2000, kind="composants"), tmp_path, "--dry-run") == 1
    assert "suspicieusement petit" in capsys.readouterr().out


def test_paused_veille_ignored(tmp_path):
    assert run(volume(tmp_path, report_days_ago=30, last_status="error", paused=True), tmp_path, "--dry-run") == 0


def test_missing_job(tmp_path, capsys):
    vol = volume(tmp_path)
    (vol / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}))
    assert run(vol, tmp_path, "--dry-run") == 1
    assert "job cron absent" in capsys.readouterr().out


def test_legacy_layout(tmp_path):
    vol = tmp_path / "data"
    (vol / "veille" / "rapports").mkdir(parents=True)
    date = dt.datetime.now().strftime("%Y-%m-%d")
    (vol / "veille" / "rapports" / f"{date}-veille.pdf").write_bytes(b"x" * 30000)
    (vol / "cron").mkdir()
    (vol / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
        {"id": "x", "name": "Veille technologique hebdomadaire", "last_status": "ok"}]}))
    assert run(vol, tmp_path, "--dry-run") == 0


def test_decide_policy():
    assert watchdog.decide({}, ["a"], 1000, 3) == "alert"
    st = {"problem_keys": ["a"], "last_alert_ts": 1000}
    assert watchdog.decide(st, ["a"], 2000, 3) is None
    assert watchdog.decide(st, ["a"], 1000 + 3 * 86400, 3) == "remind"
    assert watchdog.decide(st, ["a", "b"], 2000, 3) == "alert"
    assert watchdog.decide(st, [], 2000, 3) == "recovered"
    assert watchdog.decide({"status": "ko"}, [], 2000, 3) == "recovered"  # état de l'ancienne version
    assert watchdog.decide({"status": "ok"}, [], 2000, 3) is None


def test_state_written_and_no_repeat(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(watchdog, "send_mail", lambda a, s, b, d: sent.append(s) or True)
    vol = volume(tmp_path, report_days_ago=10)
    assert run(vol, tmp_path) == 1
    assert run(vol, tmp_path) == 1
    assert len(sent) == 1 and sent[0].startswith("[ALERTE]")
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["problem_keys"] == ["demo:report"]
