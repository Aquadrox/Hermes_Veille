"""Avancement d'un lancement manuel suivi (interface, Telegram) : `runs/.progress.json` de la veille.

Écrit au démarrage du lancement puis par les commandes déterministes qu'il exécute
(`veillectl components scan | progress | finalize`) ; lu par l'interface et le MCP. Une exécution
planifiée n'écrit rien : personne ne la regarde (voir `service._tracker`).

Confort d'affichage seulement : une écriture qui échoue est ignorée, un fichier d'une autre version
ou mal formé est ignoré ; l'exécution continue.
"""
from __future__ import annotations

import datetime as dt
import json
import time

from . import store
from .components import norm_mpn

FILE = ".progress.json"
SCHEMA = 1  # format du fichier : un autre (mise à jour de l'image en cours de lancement) est ignoré
MIN_INTERVAL_S = 1.0  # boucle Digi-Key (≈ 0,6 s par référence) : une écriture par seconde au plus
STEPS = {  # étapes affichées, par type de lancement
    "analyse": (("digikey", "Statut Digi-Key"), ("web", "Recherche web"), ("finalize", "Rapport et envoi")),
    "statuts": (("digikey", "Statut Digi-Key"),),
    "alternatives": (("alternatives", "Recherche d'alternatives"),),
}
PHASES = {"notices": "enregistrement des avis", "pdf": "rapport PDF", "mail": "envoi de l'e-mail"}
CHIP_STATES = ("pending", "searching", "done")


def path(slug: str):
    return store.runs_dir(slug) / FILE


def _valid(data) -> bool:
    """Forme attendue par les écrivains et par `view` (rien d'autre n'est lu)."""
    if not isinstance(data, dict) or data.get("v") != SCHEMA \
            or not all(isinstance(data.get(k), str) for k in ("log", "kind", "step")) or data["kind"] not in STEPS:
        return False
    batch, result = data.get("batch", []), data.get("result", {})
    return (isinstance(data.get("digikey", {}), dict) and isinstance(data.get("phase", ""), str)
            and isinstance(result, dict) and all(isinstance(n, int) for n in result.values())
            and isinstance(batch, list)
            and all(isinstance(b, dict) and isinstance(b.get("mpn"), str) and isinstance(b.get("manufacturer"), str)
                    and b.get("state") in CHIP_STATES and isinstance(b.get("notices", 0), int) for b in batch))


def read(slug: str) -> dict | None:
    try:
        data = json.loads(path(slug).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if _valid(data) else None


class Tracker:
    """État d'un lancement, gardé en mémoire et écrit d'un bloc : un lecteur ne voit jamais un JSON partiel.

    Un seul écrivain à la fois : les étapes d'un lancement se suivent (scan, signaux de l'agent, finalize).
    """

    def __init__(self, slug: str, state: dict):
        self.slug, self.state, self._last = slug, state, float("-inf")  # première écriture : immédiate

    def update(self, throttle: bool = False, **changes) -> None:
        self.state.update(changes)
        now = time.monotonic()
        if throttle and now - self._last < MIN_INTERVAL_S:
            return
        self._last = now
        self.state["updated_at"] = store.now_iso()
        try:
            store.atomic_write(path(self.slug), json.dumps(self.state, ensure_ascii=False))
        except OSError:
            pass

    def searching(self, mpns: list[str]) -> None:
        """Signal de l'agent avant un groupe de recherches : le groupe précédent est fait, celui-ci en cours.
        Seules les références du lot changent d'état : un MPN inconnu est ignoré, rien n'est recopié."""
        wanted = {norm_mpn(m) for m in mpns}
        for b in self.state.get("batch", []):
            if b["state"] == "searching":
                b["state"] = "done"
            if norm_mpn(b["mpn"]) in wanted:
                b["state"] = "searching"
        self.update()


def start(slug: str, log: str, kind: str) -> None:
    Tracker(slug, {"v": SCHEMA, "log": log, "kind": kind, "started_at": store.now_iso(), "step": "start"}).update()


def _minutes(since: str | None, until: str | None = None) -> int | None:
    try:
        a = dt.datetime.fromisoformat(since)
        b = dt.datetime.fromisoformat(until) if until else dt.datetime.now(a.tzinfo)
    except (TypeError, ValueError):
        return None
    return max(0, int((b - a).total_seconds() // 60))


def _step_detail(state: dict, step: str) -> tuple[str, int | None, int | None, str]:
    """(détail, valeur, maximum, composant en cours) de l'étape `step`."""
    if step in ("digikey", "alternatives"):
        dk = state.get(step) or {}
        if dk.get("skipped"):
            return "clés absentes, étape ignorée", None, None, ""
        done, total = dk.get("done", 0), dk.get("total", 0)
        return f"{done} / {total} référence(s)", done, total, dk.get("current") or ""
    if step == "web":
        batch = state.get("batch") or []
        done = sum(1 for b in batch if b["state"] == "done")
        current = ", ".join(b["mpn"] for b in batch if b["state"] == "searching")
        return f"{done} / {len(batch)} composant(s)", done, len(batch), current
    if step == "finalize":
        return PHASES.get(state.get("phase"), ""), None, None, ""
    return "", None, None, ""


def view(state: dict, running: bool) -> dict:
    """Vue commune (page de la veille, tableau de bord, MCP) : étapes, compteur, composant en cours, synthèse."""
    steps_def = STEPS.get(state.get("kind"), ())
    ids = [s for s, _ in steps_def]
    step = state.get("step")
    finished = step == "done"
    current = len(ids) if finished else ids.index(step) if step in ids else -1
    interrupted = not running and not finished
    steps = [{"id": s, "label": label,
              "state": "done" if i < current else ("failed" if interrupted else "current") if i == current else "todo"}
             for i, (s, label) in enumerate(steps_def)]
    detail, value, maximum, doing = _step_detail(state, step)
    batch = state.get("batch") or []
    if not running:  # recherche interrompue : pas faite
        batch = [{**b, "state": "pending"} if b["state"] == "searching" else b for b in batch]
    result = state.get("result") or {}
    # Passage Digi-Key arrêté (erreur de l'API, budget de temps) : signalé, jamais affiché comme un succès.
    warning = (state.get("digikey") or state.get("alternatives") or {}).get("note") or ""
    if finished:
        summary = "Terminé"
        if "new" in result:
            summary += (f" : {result['new']} nouvel(s) avis"
                        + (f", dont {result['critical']} fin(s) de vie / dernière(s) commande(s)"
                           if result.get("critical") else ""))
        elif ids and state.get(ids[0]):  # passage Digi-Key, recherche d'alternatives
            summary += f" : {_step_detail(state, ids[0])[0]}"
            if "found" in result:
                summary += f", {result['found']} avec alternatives"
    elif current < 0:
        summary = "Démarrage…" if running else "Interrompu au démarrage"
    else:
        name = steps_def[current][1]
        if len(ids) == 1:  # pas de « étape 1/1 »
            summary = f"Interrompu — {name}" if interrupted else name
        elif interrupted:
            summary = f"Interrompu à l'étape {current + 1}/{len(ids)} — {name}"
        else:
            summary = f"Étape {current + 1}/{len(ids)} — {name}"
        if detail:
            summary += f" : {detail}"
        if doing and running:
            summary += f" · en cours : {doing}"
    # Durée réelle : jusqu'à maintenant (en cours) ou jusqu'à la fin ; inconnue si interrompu.
    minutes = None if interrupted else _minutes(state.get("started_at"), state.get("updated_at") if finished else None)
    return {
        "step": step, "steps": steps, "running": running, "finished": finished, "interrupted": interrupted,
        "value": value, "max": maximum or None, "current": doing if running else "",
        "batch": batch, "result": result, "warning": warning, "minutes": minutes, "summary": summary,
    }
