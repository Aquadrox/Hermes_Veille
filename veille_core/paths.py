"""Emplacements sur le volume (surchargés par variables d'environnement pour les tests)."""
from __future__ import annotations

import os
from pathlib import Path


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def veilles_dir() -> Path:
    return Path(os.environ.get("VEILLES_DIR", str(hermes_home() / "veilles")))


def archive_dir() -> Path:
    return veilles_dir() / ".archive"


def imports_dir() -> Path:
    """Nomenclatures à importer (déposées pour Telegram / CLI) et analyses en cours de l'interface."""
    return hermes_home() / "imports"


def legacy_dir() -> Path:
    """Ancienne disposition mono-veille (avant la v0.3)."""
    return hermes_home() / "veille"


def defaults_dir() -> Path:
    """Gabarits livrés avec l'image (brief, template du prompt)."""
    return Path(os.environ.get("VEILLE_DEFAULTS", "/opt/hermes/veille-defaults"))


def jobs_file() -> Path:
    return hermes_home() / "cron" / "jobs.json"


def hermes_bin() -> str:
    return os.environ.get("HERMES_BIN", "/opt/hermes/.venv/bin/hermes")


def audit_log() -> Path:
    return veilles_dir() / ".audit.log"


def database() -> Path:
    """Base SQLite : connaissance partagée des composants et carnet d'adresses (voir db.py)."""
    return veilles_dir() / ".veille.db"
