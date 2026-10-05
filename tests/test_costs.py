"""Consommation et coût : tarifs (heures pleines), lecture de state.db, service, CLI, interface."""
import datetime as dt
import sqlite3

import pytest

from veille_core import hermes, pricing, service, store

UTC = dt.UTC
PARIS_SUMMER = dt.timezone(dt.timedelta(hours=2))
PARIS_WINTER = dt.timezone(dt.timedelta(hours=1))

# Sous-ensemble du schéma de Hermes (hermes_state_common.py, v2026.9.24).
SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, started_at REAL NOT NULL, ended_at REAL,
    end_reason TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0, estimated_cost_usd REAL, api_call_count INTEGER DEFAULT 0);
CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL, model TEXT NOT NULL, billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '', billing_mode TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '', api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0, estimated_cost_usd REAL NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0, cost_status TEXT, cost_source TEXT,
    first_seen REAL, last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task));
"""


# ---------------------------------------------------------------- tarifs
@pytest.mark.parametrize("when,expected", [
    (dt.datetime(2026, 10, 5, 8, 0, tzinfo=PARIS_SUMMER), True),    # lundi 8h Paris = 06h UTC
    (dt.datetime(2026, 10, 5, 6, 30, tzinfo=PARIS_SUMMER), False),  # lundi 6h30 été = 04h30 UTC
    (dt.datetime(2026, 11, 2, 6, 30, tzinfo=PARIS_WINTER), False),  # lundi 6h30 hiver = 05h30 UTC
    (dt.datetime(2026, 11, 2, 7, 0, tzinfo=PARIS_WINTER), True),    # lundi 7h hiver = 06h00 UTC (borne)
    (dt.datetime(2026, 10, 5, 3, 59, tzinfo=UTC), True),
    (dt.datetime(2026, 10, 5, 4, 0, tzinfo=UTC), False),            # fin de plage exclue
    (dt.datetime(2026, 10, 5, 10, 0, tzinfo=UTC), False),
    (dt.datetime(2026, 10, 4, 8, 0, tzinfo=UTC), False),            # dimanche : jamais plein
])
def test_is_peak(env, when, expected):
    assert pricing.is_peak(when) is expected


def test_run_cost_matches_hand_computation(env):
    off = dt.datetime(2026, 10, 5, 4, 30, tzinfo=UTC)
    usd, peak = pricing.run_cost("deepseek-v4-pro", input_tokens=1_000_000, cache_read_tokens=2_000_000,
                                 output_tokens=500_000, started_at=off)
    assert not peak and usd == pytest.approx(0.66 + 2 * 0.022 + 0.5 * 1.98)
    usd_peak, peak = pricing.run_cost("deepseek-v4-pro", input_tokens=1_000_000, cache_read_tokens=2_000_000,
                                      output_tokens=500_000, started_at=off.replace(hour=6))
    assert peak and usd_peak == pytest.approx(2 * usd)
    assert pricing.run_cost("deepseek/deepseek-v4-pro", output_tokens=1_000_000)[0] == pytest.approx(1.98)
    assert pricing.run_cost("modele-inconnu", output_tokens=1)[0] is None


def test_pricing_override_in_volume(env):
    (env / "veilles").mkdir()
    (env / "veilles" / "pricing.json").write_text(
        '{"peak": {}, "models": {"deepseek-v4-pro": {"input_cache_hit": 0, "input_cache_miss": 1, "output": 0}}}')
    assert pricing.run_cost("deepseek-v4-pro", input_tokens=1_000_000)[0] == 1


# ---------------------------------------------------------------- state.db
def make_db(home, job_id):
    db = sqlite3.connect(home / "state.db")
    db.executescript(SCHEMA)
    peak = dt.datetime(2026, 9, 28, 6, 5, tzinfo=UTC).timestamp()   # lundi, heure pleine
    off = dt.datetime(2026, 10, 5, 4, 35, tzinfo=UTC).timestamp()   # lundi, heure creuse
    for sid, start in ((f"cron_{job_id}_20260928_080500", peak), (f"cron_{job_id}_20261005_063500", off)):
        db.execute("INSERT INTO sessions (id, source, model, started_at, ended_at, input_tokens, output_tokens,"
                   " cache_read_tokens, estimated_cost_usd, api_call_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (sid, "cron", "deepseek-v4-pro", start, start + 14 * 60, 400_000, 60_000, 1_600_000, 0.42, 38))
        db.execute("INSERT INTO session_model_usage (session_id, model, task, api_call_count, input_tokens,"
                   " output_tokens, cache_read_tokens, estimated_cost_usd) VALUES (?,?,?,?,?,?,?,?)",
                   (sid, "deepseek-v4-pro", "", 36, 400_000, 60_000, 1_600_000, 0.42))
        db.execute("INSERT INTO session_model_usage (session_id, model, task, api_call_count, input_tokens,"
                   " output_tokens, cache_read_tokens, estimated_cost_usd) VALUES (?,?,?,?,?,?,?,?)",
                   (sid, "deepseek-v4-flash", "compression", 2, 100_000, 10_000, 0, 0.02))
    # Session hors cron et cron d'un autre job : ignorées.
    db.execute("INSERT INTO sessions (id, source, started_at, input_tokens) VALUES ('tg_123', 'telegram', ?, 9)", (off,))
    db.execute("INSERT INTO sessions (id, source, started_at, input_tokens) VALUES ('cron_autre_20261005_063500', 'cron', ?, 9)", (off,))
    db.commit()
    db.close()


@pytest.fixture
def veille(env):
    return service.create_veille(name="Veille Coût", subject="s", schedule_expr="30 6 * * 1",
                                 recipients=["a@example.org"], brief="# Brief\n")


def test_run_usage_reads_cron_sessions_with_aux_models(env, veille):
    make_db(env, veille.job_id)
    runs = hermes.run_usage(veille.job_id)
    assert [r["session_id"][-15:] for r in runs] == ["20261005_063500", "20260928_080500"]
    models = {m["model"]: m for m in runs[0]["models"]}
    assert set(models) == {"deepseek-v4-pro", "deepseek-v4-flash"}
    assert models["deepseek-v4-pro"]["cache_read_tokens"] == 1_600_000


def test_run_usage_is_robust(env, veille):
    assert hermes.run_usage(veille.job_id) == []                     # base absente
    (env / "state.db").write_text("pas une base sqlite")
    assert hermes.run_usage(veille.job_id) == []                     # base illisible
    assert hermes.run_usage("x'; DROP TABLE sessions; --") == []     # identifiant refusé


def test_costs_service_doubles_peak_runs(env, veille):
    make_db(env, veille.job_id)
    c = service.costs(veille.slug, days=3650)
    off_run, peak_run = c["runs"]
    expected_off = (400_000 * 0.66 + 1_600_000 * 0.022 + 60_000 * 1.98
                    + 100_000 * 0.15 + 10_000 * 0.60) / 1e6
    assert not off_run["peak"] and off_run["cost_usd"] == pytest.approx(expected_off, abs=1e-4)
    assert peak_run["peak"] and peak_run["cost_usd"] == pytest.approx(2 * expected_off, abs=1e-4)
    assert off_run["duration_min"] == 14.0 and off_run["api_calls"] == 38
    assert off_run["hermes_estimate_usd"] == pytest.approx(0.44)
    assert c["total_days_usd"] == pytest.approx(3 * expected_off, abs=1e-3)
    ov = service.costs_overview(days=3650)
    assert ov["veilles"][0]["slug"] == veille.slug and ov["total_days_usd"] == pytest.approx(c["total_days_usd"])


def test_cli_costs(env, veille, capsys):
    from veille_core.cli import main

    assert main(["costs", veille.slug]) == 0
    assert "aucune exécution mesurée" in capsys.readouterr().out
    make_db(env, veille.job_id)
    assert main(["costs", veille.slug, "--days", "3650"]) == 0
    out = capsys.readouterr().out
    assert "PLEINE" in out and "creuse" in out and "estimation Hermes" in out
    assert main(["costs"]) == 0 and "TOTAL" in capsys.readouterr().out


def test_bootstrap_default_is_off_peak(env):
    service.bootstrap_default()
    v = store.load("industrie-4-0")
    assert v.schedule == "30 6 * * 1"
    from veille_core import schedule
    assert not pricing.is_peak(schedule.next_runs(v.schedule, 1)[0])


def test_deepseek_balance_without_key(env, monkeypatch):
    from veille_core import deepseek

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert deepseek.balance() == {"error": "DEEPSEEK_API_KEY absente"}


def test_deepseek_balance_parsing(env, monkeypatch):
    import io
    import json

    from veille_core import deepseek

    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    payload = {"is_available": True, "balance_infos": [
        {"currency": "USD", "total_balance": "12.50", "granted_balance": "0.00", "topped_up_balance": "12.50"}]}
    seen = {}

    def fake_urlopen(req, timeout):
        seen["auth"] = req.headers["Authorization"]
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(deepseek.urllib.request, "urlopen", fake_urlopen)
    b = deepseek.balance()
    assert seen["auth"] == "Bearer k"
    assert b == {"available": True, "balances": [{"currency": "USD", "total": "12.50", "granted": "0.00",
                                                   "topped_up": "12.50"}]}
