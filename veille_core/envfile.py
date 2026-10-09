"""Secrets : variable d'environnement, sinon le `.env` du volume (`/opt/data/.env`).

Le conteneur reçoit le `.env` du dépôt en variables, mais un processus lancé par l'agent
(terminal Hermes) peut en être privé : le fichier du volume sert alors de repli.
"""
from __future__ import annotations

import os

from . import paths


def get(key: str) -> str | None:
    value = os.environ.get(key)
    if value:
        return value
    try:
        lines = (paths.hermes_home() / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        name, sep, raw = line.strip().partition("=")
        if sep and name.strip() == key and not name.startswith("#"):
            return raw.strip().strip("'\"") or None
    return None
