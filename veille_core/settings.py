"""Réglages d'exploitation saisis dans l'interface : adresse(s) des alertes du chien de garde.

Stockage : `veilles/.settings.json`, écriture atomique, mode 0644. Ce ne sont pas des secrets :
le fichier est INCLUS dans les sauvegardes. Le chien de garde de l'hôte (scripts/watchdog.py,
bibliothèque standard seule) le lit directement : même format, même filtre, même priorité
(vérifié par tests/test_settings.py).

Destinataires des alertes, par priorité : interface > `ALERT_EMAIL` du `.env` > expéditeur
Brevo. L'interface fait foi ; le `.env` ne sert plus que de repli (installations existantes).
"""
from __future__ import annotations

import json

from . import envfile, paths, store

MAX_ALERT_EMAILS = 10


class SettingsError(store.StoreError):
    pass


def path():
    return paths.veilles_dir() / ".settings.json"


def _stored() -> dict:
    return store.read_json_dict(path(), SettingsError, "fichier des paramètres")


def _saved(value) -> list[str]:
    """Adresses enregistrées ; une valeur malformée (fichier édité à la main) est ignorée."""
    return [a for a in value if isinstance(a, str) and "@" in a] if isinstance(value, list) else []


def _split(value: str | None) -> list[str]:
    return [a.strip() for a in (value or "").split(",") if a.strip()]


def alert_status() -> dict:
    """Adresses enregistrées, adresses EFFECTIVES et leur origine ("ui", "env", "sender" ou None)."""
    try:
        stored, error = _stored(), None
    except SettingsError as exc:  # affiché ; le .env prend le relais, comme dans le chien de garde
        stored, error = {}, str(exc)
    saved = _saved(stored.get("alert_emails"))
    env_value = _split(envfile.get("ALERT_EMAIL"))
    sender = _split(envfile.get("BREVO_SENDER_EMAIL"))
    origin = "ui" if saved else "env" if env_value else "sender" if sender else None
    return {"saved": saved, "effective": saved or env_value or sender, "origin": origin,
            "env_ignored": bool(saved and env_value and env_value != saved), "error": error,
            "updated_at": stored.get("updated_at")}


def save_alert_emails(text: str) -> list[str] | None:
    """Enregistre les adresses (texte libre ; vide = retour au `.env`) ; None si inchangées.

    À appeler sous le verrou des veilles (service.save_alert_emails).
    """
    addresses = store.parse_recipients(text)
    if len(addresses) > MAX_ALERT_EMAILS:
        raise SettingsError(f"{MAX_ALERT_EMAILS} adresses d'alerte au plus")
    try:
        data = _stored()
    except SettingsError:
        data = {}  # illisible : remplacé (rien d'exploitable à conserver)
    else:
        if data.get("alert_emails", []) == addresses:
            return None
    data.update(alert_emails=addresses, updated_at=store.now_iso())
    store.atomic_write(path(), json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    return addresses
