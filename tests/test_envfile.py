"""Sync des secrets de l'environnement vers le `.env` du volume (50-seed-veille).

Régressions : une clé présente uniquement dans l'environnement (env_file du conteneur) doit
être recopiée dans $HERMES_HOME/.env à chaque démarrage, sans jamais fuiter sa valeur. C'est
l'échec réel du tir « Industrie 4.0 » du 2026-10-25 (`blocked_config` : clé provider absente).
"""
import stat

import pytest

from veille_core import envfile


@pytest.fixture(autouse=True)
def _clean_secrets(monkeypatch):
    """Isole les tests de l'environnement de l'hôte (qui définit déjà ces clés)."""
    for key in envfile.SECRET_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_sync_writes_missing_secret_to_volume_env(env, monkeypatch):
    # La clé n'existe que dans l'environnement (cas conteneur), pas dans le .env du volume.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert not (env / ".env").exists()
    assert envfile.sync_env_to_file() == ["DEEPSEEK_API_KEY"]
    # L'environnement est ensuite privé de la clé (processus lancé par l'agent) : le fichier
    # du volume doit servir de repli, exactement comme le prévoit `envfile.get`.
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert envfile.get("DEEPSEEK_API_KEY") == "sk-test"


def test_sync_is_idempotent(env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("BREVO_API_KEY", "xkey-test")
    envfile.sync_env_to_file()
    assert envfile.sync_env_to_file() == []  # rien à réécrire
    lines = [ln for ln in (env / ".env").read_text().splitlines() if ln.startswith("DEEPSEEK_API_KEY=")]
    assert lines == ["DEEPSEEK_API_KEY=sk-test"]


def test_sync_replaces_stale_value_without_duplicate(env, monkeypatch):
    (env / ".env").write_text("DEEPSEEK_API_KEY=ancienne\nAUTRE=1\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "nouvelle")
    assert envfile.sync_env_to_file() == ["DEEPSEEK_API_KEY"]
    text = (env / ".env").read_text()
    assert text.count("DEEPSEEK_API_KEY=") == 1
    assert "DEEPSEEK_API_KEY=nouvelle" in text and "AUTRE=1" in text


def test_sync_preserves_other_lines_and_mode_600(env, monkeypatch):
    (env / ".env").write_text("AUTRE=1\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    envfile.sync_env_to_file()
    p = env / ".env"
    assert "AUTRE=1" in p.read_text()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_sync_never_returns_values(env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
    assert "sk-secret" not in repr(envfile.sync_env_to_file())


def test_sync_skips_empty_and_multiline_values(env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("BREVO_API_KEY", "a\nb")
    assert envfile.sync_env_to_file() == []
    assert not (env / ".env").exists()
