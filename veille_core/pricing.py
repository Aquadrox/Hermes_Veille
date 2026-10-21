"""Tarifs du modèle et coût d'une exécution, heures pleines comprises.

Les tarifs sont ceux de `pricing.json`, au tarif « heures creuses », avec les plages d'heures
pleines (tarif ×2 chez DeepSeek). Fichier lu dans le volume
(`/opt/data/veilles/pricing.json`, modifiable sans reconstruire l'image), section par section : une
section absente du volume (copie antérieure à son ajout) vient de celui de l'image.

Correspondance des tokens (normalisation de Hermes, `agent/usage_pricing.py`) :
  input_tokens      = entrée hors cache (cache miss)
  cache_read_tokens = entrée lue en cache (cache hit)
  output_tokens     = sortie, raisonnement COMPRIS (ne pas ajouter reasoning_tokens)

La recherche web DeepSeek (Search & Fetch) se facture à la requête, hors tokens : section `web`, pour
les exécutions démarrées quand ce moteur était en vigueur (historique `.web-backend`, tenu par l'amorçage).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import paths, schedule

FILENAME = "pricing.json"
WEB_HISTORY = ".web-backend"  # « <AAAA-MM-JJTHH:MM:SSZ> <moteur> », une ligne par changement (50-seed-veille)
_UTC = dt.UTC


def _candidates() -> list[Path]:
    return [paths.veilles_dir() / FILENAME, paths.defaults_dir() / FILENAME]


def load() -> dict:
    table: dict = {}
    for path in reversed(_candidates()):  # image, puis volume : le volume l'emporte section par section
        try:
            table.update(json.loads(path.read_text(encoding="utf-8")))
        except (FileNotFoundError, ValueError):
            continue
    return table or {"models": {}, "peak": {}}


def _as_utc(when: dt.datetime | float | int) -> dt.datetime:
    if isinstance(when, (int, float)):
        return dt.datetime.fromtimestamp(when, tz=_UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=schedule.ZONE)  # heure « murale » de Paris
    return when.astimezone(_UTC)


def is_peak(when: dt.datetime | float | int, table: dict | None = None) -> bool:
    """Vrai si `when` (timestamp, datetime aware ou heure de Paris naïve) tombe en heures pleines."""
    peak = (table or load()).get("peak") or {}
    t = _as_utc(when)
    if t.weekday() not in peak.get("weekdays", []):
        return False
    hour = t.hour + t.minute / 60
    return any(start <= hour < end for start, end in peak.get("utc_hours", []))


def model_rates(model: str | None, table: dict | None = None) -> dict | None:
    """Tarifs heures creuses d'un modèle (nom exact, ou suffixe après « fournisseur/ »)."""
    models = (table or load()).get("models") or {}
    if not model:
        return None
    name = model.strip().lower()
    return models.get(name) or models.get(name.rsplit("/", 1)[-1])


def run_cost(model: str | None, *, input_tokens: int = 0, cache_read_tokens: int = 0,
             output_tokens: int = 0, started_at: dt.datetime | float | int | None = None,
             table: dict | None = None) -> tuple[float | None, bool]:
    """Coût en USD d'une exécution (au tarif de son heure de début) et indicateur heures pleines.

    Renvoie (None, peak) si le modèle n'a pas de tarif connu.
    """
    table = table or load()
    peak = started_at is not None and is_peak(started_at, table)
    rates = model_rates(model, table)
    if rates is None:
        return None, peak
    factor = float((table.get("peak") or {}).get("multiplier", 1)) if peak else 1.0
    usd = (input_tokens * rates.get("input_cache_miss", 0)
           + cache_read_tokens * rates.get("input_cache_hit", 0)
           + output_tokens * rates.get("output", 0)) / 1_000_000 * factor
    return round(usd, 6), peak


def web_backend_at(when: dt.datetime | float | int) -> str | None:
    """Moteur de recherche web en vigueur à `when` (historique écrit par l'amorçage) ; None avant la
    première ligne : moteur gratuit de Hermes des versions antérieures à la 0.7.0."""
    stamp, backend = _as_utc(when).strftime("%Y-%m-%dT%H:%M:%SZ"), None
    try:
        lines = (paths.veilles_dir() / WEB_HISTORY).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:  # lignes ajoutées dans l'ordre chronologique
        since, _, name = line.strip().partition(" ")
        if since and since <= stamp:
            backend = name.strip() or None
    return backend


def web_cost(requests: int, started_at: dt.datetime | float | int | None, table: dict | None = None) -> float:
    """Coût en USD des requêtes de recherche web DeepSeek (Search & Fetch) d'une exécution, si ce moteur
    était en vigueur à son début (tarif unique, sans heures pleines).

    Majorant : DeepSeek ne facture ni les échecs ni les résultats vides, et Hermes sert les recherches
    répétées depuis son cache.
    """
    if not requests or started_at is None or web_backend_at(started_at) != "deepseek":
        return 0.0
    web = (table or load()).get("web") or {}
    return round(requests * float(web.get("usd_per_request", 0)), 6)
