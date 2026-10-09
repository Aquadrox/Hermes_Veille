"""Clés d'API des sources de données (distributeurs), saisissables dans l'interface.

Stockage : `veilles/.credentials.json`, mode 0600, écriture atomique, EXCLU des sauvegardes
(comme le `.env`). Après une restauration, les clés sont à ressaisir.

Une clé définie dans le `.env` est prioritaire et n'est pas modifiable depuis l'interface :
une seule source de vérité, affichée comme telle. Aucune valeur n'est jamais renvoyée à
l'interface ni au MCP : seulement un aperçu masqué et l'origine (.env ou interface).
"""
from __future__ import annotations

import json
import re

from . import envfile, paths, store

# Sources connues : ajouter un distributeur = ajouter une entrée (et son client).
PROVIDERS = {
    "digikey": {
        "name": "Digi-Key",
        "portal": "https://developer.digikey.com",
        "help": ("Créer une application « Production » abonnée à l'API Product Information v4 "
                 "(OAuth 2-legged), puis copier son Client ID et son Client Secret."),
        "fields": {"DIGIKEY_CLIENT_ID": ("Client ID", False), "DIGIKEY_CLIENT_SECRET": ("Client Secret", True)},
    },
}
VALUE_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{8,256}$")


class CredentialsError(store.StoreError):
    pass


def path():
    return paths.veilles_dir() / ".credentials.json"


def _stored() -> dict:
    return store.read_json_dict(path(), CredentialsError, "fichier des clés")


def _stored_or_empty() -> dict:
    """Pour écrire : un fichier illisible est remplacé (ses valeurs sont de toute façon perdues)."""
    try:
        return _stored()
    except CredentialsError:
        return {"corrupted": True}


def get(key: str) -> str | None:
    """Valeur d'une clé : `.env` (environnement ou fichier du volume), sinon interface."""
    value = envfile.get(key)
    if value:
        return value
    try:
        return (_stored().get("values") or {}).get(key) or None
    except CredentialsError:
        return None


def _provider(provider: str) -> dict:
    if provider not in PROVIDERS:
        raise CredentialsError(f"source inconnue : {provider}")
    return PROVIDERS[provider]


def mask(value: str | None) -> str:
    if not value:
        return ""
    return "••••" + value[-4:] if len(value) >= 12 else "••••"


def status(provider: str) -> dict:
    """État affichable : origine et aperçu masqué de chaque champ, jamais la valeur."""
    p = _provider(provider)
    try:
        stored, error = _stored(), None
    except CredentialsError as exc:  # affiché, sans bloquer la page ni les clés du .env
        stored, error = {}, str(exc)
    values = stored.get("values") or {}
    fields = {}
    for key, (label, secret) in p["fields"].items():
        env_value = envfile.get(key)
        origin = "env" if env_value else ("ui" if values.get(key) else None)
        fields[key] = {"label": label, "secret": secret, "origin": origin, "masked": mask(env_value or values.get(key))}
    origins = {f["origin"] for f in fields.values()}
    return {"id": provider, **{k: p[k] for k in ("name", "portal", "help")}, "fields": fields,
            "configured": None not in origins, "env": "env" in origins, "error": error,
            "updated_at": (stored.get("updated_at") or {}).get(provider)}


def save(provider: str, values: dict[str, str]) -> list[str]:
    """Enregistre les champs fournis (vide = inchangé) ; renvoie les NOMS des champs modifiés.

    À appeler sous le verrou des veilles (service.save_credentials).
    """
    p = _provider(provider)
    data = _stored_or_empty()
    corrupted = data.pop("corrupted", False)
    current = data.setdefault("values", {})
    changed = []
    for key in p["fields"]:
        value = (values.get(key) or "").strip()
        if not value:
            continue
        if envfile.get(key):
            raise CredentialsError(f"{key} est défini dans le .env du serveur : le modifier là-bas")
        if not VALUE_RE.match(value):
            raise CredentialsError(f"{p['fields'][key][0]} : format invalide (8 à 256 caractères, sans espace)")
        if current.get(key) != value:
            current[key] = value
            changed.append(key)
    if changed or corrupted:
        data.setdefault("updated_at", {})[provider] = store.now_iso()
        store.atomic_write(path(), json.dumps(data, indent=1) + "\n", mode=0o600)
    return changed


def delete(provider: str) -> bool:
    p = _provider(provider)
    data = _stored_or_empty()
    corrupted = data.pop("corrupted", False)
    values = data.get("values") or {}
    removed = [k for k in p["fields"] if values.pop(k, None) is not None]
    if removed or corrupted:
        (data.get("updated_at") or {}).pop(provider, None)
        store.atomic_write(path(), json.dumps(data, indent=1) + "\n", mode=0o600)
    return bool(removed or corrupted)
