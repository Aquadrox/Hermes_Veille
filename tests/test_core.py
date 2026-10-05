import json

import pytest

from veille_core import hermes, render, schedule, service, store

BRIEF = "# Brief\n\nSujet de test.\n"


def make(name="Veille Test", **kw):
    args = dict(name=name, subject="sujet", schedule_expr="0 7 * * *",
                recipients=["a@example.org", "b@example.org"], brief=BRIEF)
    args.update(kw)
    return service.create_veille(**args)


# ---------------------------------------------------------------- schedule
@pytest.mark.parametrize("preset,expr", [
    ({"kind": "daily", "hour": 7, "minute": 30}, "30 7 * * *"),
    ({"kind": "weekdays", "hour": 9}, "0 9 * * 1-5"),
    ({"kind": "weekly", "dow": 1, "hour": 8}, "0 8 * * 1"),
    ({"kind": "monthly", "day": 1, "hour": 6}, "0 6 1 * *"),
])
def test_presets_roundtrip(preset, expr):
    assert schedule.to_cron(preset) == expr
    back = schedule.from_cron(expr)
    assert schedule.to_cron(back) == expr


def test_schedule_rejects_bad_and_too_frequent():
    with pytest.raises(schedule.ScheduleError):
        schedule.to_cron({"kind": "cron", "expr": "nope"})
    with pytest.raises(schedule.ScheduleError):
        schedule.to_cron({"kind": "cron", "expr": "*/5 * * * *"})
    with pytest.raises(schedule.ScheduleError):
        schedule.to_cron({"kind": "daily", "hour": 24})


def test_max_age_follows_frequency():
    assert schedule.max_age_hours("0 8 * * 1") == 192
    assert schedule.max_age_hours("0 8 * * *") == 48
    assert schedule.describe("0 8 * * 1") == "chaque lundi à 8h"


# ---------------------------------------------------------------- store
def test_recipients_parsing():
    assert store.parse_recipients("a@x.fr, b@y.com\n# c@z.fr\nA@x.fr") == ["a@x.fr", "b@y.com"]
    with pytest.raises(store.StoreError):
        store.parse_recipients("pas-une-adresse")


@pytest.mark.parametrize("slug", ["../etc", "A", "-x", "x-", "a/b", "é"])
def test_bad_slugs(env, slug):
    with pytest.raises(store.StoreError):
        store.check_slug(slug)


def test_report_names_whitelisted(env):
    v = make()
    with pytest.raises(store.StoreError):
        store.report_path(v.slug, "../veille.json")
    with pytest.raises(store.StoreError):
        store.run_log_path(v.slug, "../../.env")


# ---------------------------------------------------------------- service
def test_create_writes_files_and_job(env):
    v = make()
    assert v.slug == "veille-test"
    d = env / "veilles" / "veille-test"
    assert json.loads((d / "veille.json").read_text())["job_id"] == v.job_id
    assert (d / "recipients.txt").read_text() == "a@example.org\nb@example.org\n"
    prompt = (d / "job-prompt.md").read_text()
    assert "--bcc-file " + str(d) + "/recipients.txt" in prompt
    assert "les dernières 24 heures" in prompt
    assert "${" not in prompt
    job = hermes.get_job(v.job_id)
    assert job["prompt"] == render.short_prompt(v)
    assert job["workdir"] == str(d)
    assert job["schedule"]["expr"] == "0 7 * * *"
    assert v.max_age_hours == 48


def test_create_duplicate_and_rollback(env, monkeypatch):
    make()
    with pytest.raises(store.StoreError):
        make()
    monkeypatch.setenv("FAKE_HERMES_FAIL", "1")
    with pytest.raises(hermes.HermesError):
        make(name="Autre")
    assert not (env / "veilles" / "autre").exists()


def test_update_changes_schedule_and_prompt(env):
    v = make()
    service.update_veille(v.slug, name="Nouveau nom", schedule_expr="0 9 * * 1",
                          recipients=["c@example.org"])
    v2 = store.load(v.slug)
    job = hermes.get_job(v2.job_id)
    assert job["schedule"]["expr"] == "0 9 * * 1"
    assert job["name"] == "Nouveau nom [veille-test]"
    assert v2.period == "week" and v2.max_age_hours == 192
    assert store.read_recipients(v.slug) == ["c@example.org"]
    assert "chaque lundi à 9h" in (v2.dir / "job-prompt.md").read_text()


def test_pause_blocks_run_then_resume(env):
    v = make()
    service.pause(v.slug)
    assert hermes.is_paused(hermes.get_job(v.job_id))
    with pytest.raises(service.ServiceError):
        service.run_now(v.slug)
    service.resume(v.slug)
    assert not hermes.is_paused(hermes.get_job(v.job_id))


def test_run_now_detached_writes_log(env):
    v = make()
    log = service.run_now(v.slug)
    path = store.runs_dir(v.slug) / log
    import time
    for _ in range(50):
        if path.exists() and "Triggered" in path.read_text():
            break
        time.sleep(0.1)
    assert "Triggered job" in path.read_text()


def test_archive_removes_job(env):
    v = make()
    dest = service.archive(v.slug)
    assert dest.is_dir() and not v.dir.exists()
    assert hermes.get_job(v.job_id) is None
    assert service.overview() == []


def test_reconcile_recreates_missing_job_and_adopts_schedule(env):
    v = make()
    hermes.remove(v.job_id)
    out = service.reconcile_all()
    assert "job recréé" in out[0]
    v2 = store.load(v.slug)
    assert v2.job_id != v.job_id and hermes.get_job(v2.job_id)
    hermes.edit(v2.job_id, schedule="0 6 * * 1")
    service.reconcile_all()
    assert store.load(v.slug).schedule == "0 6 * * 1"
    assert service.reconcile_all() == ["veille-test: ok"]


def test_audit_log(env):
    v = make()
    service.pause(v.slug, actor="ui")
    actions = [e["action"] for e in service.read_audit()]
    assert actions[:2] == ["pause", "create"]


# ---------------------------------------------------------------- migration / amorçage
def test_migrate_legacy_keeps_job_and_reports(env, legacy):
    assert service.migrate_legacy() == "industrie-4-0"
    d = env / "veilles" / "industrie-4-0"
    assert (d / "rapports" / "2026-09-28-veille.pdf").is_file()
    assert store.read_recipients("industrie-4-0") == ["boss@example.org"]
    v = store.load("industrie-4-0")
    assert v.job_id == "e982d99ccf9a"
    job = hermes.get_job("e982d99ccf9a")
    assert job["workdir"] == str(d) and "job-prompt.md" in job["prompt"]
    assert "Kindara — document interne" in (d / "job-prompt.md").read_text()
    assert legacy.is_symlink() and (legacy / "rapports" / "2026-09-28-veille.pdf").exists()
    assert service.migrate_legacy() is None  # idempotent
    assert service.bootstrap_default() is None  # une veille existe déjà


def test_migrate_rolls_back_on_hermes_failure(env, legacy, monkeypatch):
    monkeypatch.setenv("FAKE_HERMES_FAIL", "1")
    with pytest.raises(hermes.HermesError):
        service.migrate_legacy()
    assert (legacy / "destinataire.txt").is_file() and not legacy.is_symlink()
    assert not (env / "veilles" / "industrie-4-0").exists()


def test_bootstrap_without_recipients_is_paused(env):
    assert service.bootstrap_default() == "industrie-4-0"
    v = store.load("industrie-4-0")
    assert hermes.is_paused(hermes.get_job(v.job_id))
    with pytest.raises(service.ServiceError):
        service.resume("industrie-4-0")
    assert service.bootstrap_default() is None


def test_bootstrap_with_recipients_is_active(env, monkeypatch):
    monkeypatch.setenv("VEILLE_RECIPIENTS", "x@example.org,y@example.org")
    service.bootstrap_default()
    v = store.load("industrie-4-0")
    assert not hermes.is_paused(hermes.get_job(v.job_id))
    assert store.read_recipients("industrie-4-0") == ["x@example.org", "y@example.org"]


def test_cli_list_and_create(env, capsys):
    from veille_core.cli import main

    assert main(["create", "--name", "Cyber", "--schedule", "0 8 * * 1-5",
                 "--recipients", "a@example.org", "--template", "vierge"]) == 0
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "cyber" in out and "du lundi au vendredi à 8h" in out
    assert main(["show", "inconnue"]) == 1
