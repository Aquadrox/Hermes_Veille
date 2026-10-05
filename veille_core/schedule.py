"""Fréquences : préréglages lisibles <-> expressions cron, aperçu et seuil de fraîcheur.

Les préréglages servent à l'interface (formulaire sans syntaxe cron) ; Hermes ne
reçoit jamais que l'expression cron 5 champs, interprétée dans son fuseau configuré.
"""
from __future__ import annotations

import datetime as dt
import re

from croniter import croniter

DAYS = ["dimanche", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi"]
KINDS = ("daily", "weekdays", "weekly", "monthly", "cron")
GRACE_HOURS = 24

# Période couverte par un rapport, dans le vocabulaire du prompt.
PERIODS = {
    "day": "les dernières 24 heures",
    "week": "la semaine écoulée (lundi → vendredi, identifiée par son numéro ISO)",
    "month": "le mois civil écoulé",
    "custom": "la période écoulée depuis le rapport précédent (date du plus récent fichier de rapports/)",
}

_CRON_FIELD = re.compile(r"^[A-Za-z0-9*/,\-]+$")


class ScheduleError(ValueError):
    pass


def _int(value, lo, hi, label):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ScheduleError(f"{label} invalide : {value!r}") from None
    if not lo <= n <= hi:
        raise ScheduleError(f"{label} hors limites ({lo}–{hi}) : {n}")
    return n


def to_cron(preset: dict) -> str:
    """Préréglage -> expression cron (valide)."""
    kind = preset.get("kind")
    if kind == "cron":
        expr = " ".join(str(preset.get("expr", "")).split())
        validate_cron(expr)
        return expr
    if kind not in KINDS:
        raise ScheduleError(f"type de fréquence inconnu : {kind!r}")
    hour = _int(preset.get("hour", 8), 0, 23, "heure")
    minute = _int(preset.get("minute", 0), 0, 59, "minute")
    if kind == "daily":
        return f"{minute} {hour} * * *"
    if kind == "weekdays":
        return f"{minute} {hour} * * 1-5"
    if kind == "weekly":
        dow = _int(preset.get("dow", 1), 0, 6, "jour de la semaine")
        return f"{minute} {hour} * * {dow}"
    day = _int(preset.get("day", 1), 1, 28, "jour du mois")
    return f"{minute} {hour} {day} * *"


def from_cron(expr: str) -> dict:
    """Expression cron -> préréglage reconnu (sinon {'kind': 'cron'})."""
    parts = expr.split()
    if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[3] == "*":
        minute, hour, dom, _, dow = int(parts[0]), int(parts[1]), parts[2], parts[3], parts[4]
        base = {"hour": hour, "minute": minute}
        if dom == "*" and dow == "*":
            return {"kind": "daily", **base}
        if dom == "*" and dow == "1-5":
            return {"kind": "weekdays", **base}
        if dom == "*" and dow.isdigit() and 0 <= int(dow) <= 6:
            return {"kind": "weekly", "dow": int(dow), **base}
        if dow == "*" and dom.isdigit() and 1 <= int(dom) <= 28:
            return {"kind": "monthly", "day": int(dom), **base}
    return {"kind": "cron", "expr": expr}


def validate_cron(expr: str) -> None:
    parts = expr.split()
    if len(parts) != 5 or not all(_CRON_FIELD.match(p) for p in parts):
        raise ScheduleError(f"expression cron invalide (5 champs attendus) : {expr!r}")
    if not croniter.is_valid(expr):
        raise ScheduleError(f"expression cron invalide : {expr!r}")
    if min_gap_hours(expr) < 1:
        raise ScheduleError("fréquence trop élevée : au moins une heure entre deux rapports")


def next_runs(expr: str, n: int = 3, start: dt.datetime | None = None) -> list[dt.datetime]:
    it = croniter(expr, start or dt.datetime.now())
    return [it.get_next(dt.datetime) for _ in range(n)]


def _gaps_hours(expr: str, samples: int = 40) -> list[float]:
    # Départ fixe : le résultat ne dépend pas du moment de l'appel.
    runs = next_runs(expr, samples, dt.datetime(2026, 1, 1))
    return [(b - a).total_seconds() / 3600 for a, b in zip(runs, runs[1:], strict=False)]


def min_gap_hours(expr: str) -> float:
    return min(_gaps_hours(expr, 60))


def max_gap_hours(expr: str) -> float:
    return max(_gaps_hours(expr))


def max_age_hours(expr: str) -> float:
    """Âge maximal acceptable du dernier rapport (watchdog) : plus grand écart + marge."""
    return round(max_gap_hours(expr) + GRACE_HOURS, 1)


def period_of(expr: str) -> str:
    kind = from_cron(expr)["kind"]
    return {"daily": "day", "weekdays": "day", "weekly": "week", "monthly": "month"}.get(kind, "custom")


def describe(expr: str) -> str:
    """Libellé français d'une expression cron."""
    p = from_cron(expr)
    if p["kind"] == "cron":
        return f"cron « {expr} »"
    at = f"{p['hour']}h{p['minute']:02d}" if p["minute"] else f"{p['hour']}h"
    if p["kind"] == "daily":
        return f"tous les jours à {at}"
    if p["kind"] == "weekdays":
        return f"du lundi au vendredi à {at}"
    if p["kind"] == "weekly":
        return f"chaque {DAYS[p['dow']]} à {at}"
    day = "1er" if p["day"] == 1 else p["day"]
    return f"le {day} de chaque mois à {at}"


def job_expr(job: dict) -> str | None:
    """Expression cron d'un job Hermes (champ `schedule` structuré ou texte)."""
    sched = job.get("schedule")
    if isinstance(sched, dict):
        if sched.get("kind") == "cron":
            return sched.get("expr")
        return None
    if isinstance(sched, str) and len(sched.split()) == 5:
        return sched
    return None
