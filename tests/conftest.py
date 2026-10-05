import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Volume Hermes jetable + faux binaire `hermes` + gabarits du dépôt."""
    home = tmp_path / "data"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("VEILLES_DIR", raising=False)
    monkeypatch.setenv("VEILLE_DEFAULTS", str(REPO / "veille"))
    monkeypatch.setenv("HERMES_BIN", str(REPO / "tests" / "fake_hermes.py"))
    monkeypatch.delenv("VEILLE_RECIPIENTS", raising=False)
    return home


@pytest.fixture
def legacy(env):
    """Ancienne disposition mono-veille avec un job existant."""
    import json

    old = env / "veille"
    (old / "rapports").mkdir(parents=True)
    shutil.copy(REPO / "veille" / "templates" / "industrie-4-0.md", old / "config.md")
    (old / "destinataire.txt").write_text("boss@example.org\n")
    (old / "job-prompt.md").write_text("ancien prompt\n")
    (old / ".seeded").touch()
    (old / "rapports" / "2026-09-28-veille.pdf").write_bytes(b"%PDF" + b"x" * 30000)
    (old / "rapports" / "2026-09-28-veille.md").write_text("# rapport\n")
    (env / "cron").mkdir()
    (env / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{
        "id": "e982d99ccf9a", "name": "Veille technologique hebdomadaire",
        "prompt": "ancien prompt", "workdir": str(old),
        "schedule": {"kind": "cron", "expr": "0 8 * * 1"},
        "enabled": True, "state": "scheduled", "last_status": "ok",
        "last_run_at": "2026-09-28T08:05:00+02:00"}]}))
    return old


os.environ.setdefault("TZ", "Europe/Paris")
