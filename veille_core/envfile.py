"""Secrets : variable d'environnement, sinon le `.env` du volume (`/opt/data/.env`).

Le conteneur reçoit le `.env` du dépôt en variables, mais un processus lancé par l'agent
(terminal Hermes) peut en être privé : le fichier du volume sert alors de repli.
"""
from __future__ import annotations

import os

from . import paths, store

# Clés que le conteneur reçoit en variables (env_file du dépôt) et qu'il faut AUSSI écrire
# dans le `.env` du volume : Hermes lit les clés de provider dans ce fichier, et un processus
# lancé par l'agent (terminal Hermes) peut être privé de l'environnement (voir `get`).
SECRET_KEYS = ("DEEPSEEK_API_KEY", "BREVO_API_KEY", "BREVO_SENDER_EMAIL", "BREVO_SENDER_NAME")


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


def sync_env_to_file(keys: tuple[str, ...] = SECRET_KEYS) -> list[str]:
    """Recopie les clés de `keys` de l'environnement vers `$HERMES_HOME/.env` (idempotent).

    Renvoie les NOMS des clés écrites, jamais les valeurs. Appelée à chaque démarrage
    (50-seed-veille) pour qu'aucun déploiement ni redémarrage ne perde les secrets.
    """
    env_path = paths.hermes_home() / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    current = {}
    for line in lines:
        name, sep, raw = line.strip().partition("=")
        if sep and name and not name.startswith("#"):
            current[name] = raw
    written = []
    for key in keys:
        value = os.environ.get(key)
        if not value or "\n" in value or "\r" in value:
            continue  # vide ou multi-ligne : jamais écrit (protège le fichier)
        if current.get(key) == value:
            continue  # déjà à jour : aucune réécriture (démarrage sans effet de bord)
        lines = [ln for ln in lines
                 if not (not ln.strip().startswith("#") and ln.strip().partition("=")[0] == key)]
        lines.append(f"{key}={value}")
        current[key] = value
        written.append(key)
    if written:
        store.atomic_write(env_path, "\n".join(lines) + "\n", mode=0o600)
    return written
