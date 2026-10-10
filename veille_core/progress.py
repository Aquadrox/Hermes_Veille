"""Avancement d'un lancement manuel suivi (interface, Telegram) : `runs/.progress.json` de la veille.

Écrit au démarrage du lancement puis par les commandes déterministes qu'il exécute
(`veillectl components scan | progress | finalize`) ; lu par l'interface et le MCP. Une exécution
planifiée n'écrit rien : personne ne la regarde (voir `service._tracker`).

Confort d'affichage seulement : une écriture qui échoue est ignorée, l'exécution continue.
"""
from __future__ import annotations

import datetime as dt
import json
import time

from . import store
from .components import norm_mpn

FILE = ".progress.json"
MIN_INTERVAL_S = 1.0  # boucle Digi-Key (≈ 0,6 s par référence) : une écriture par seconde au plus
STEPS = {  # étapes affichées, par type de lancement
    "analyse": (("digikey", "Statut Digi-Key"), ("web", "Recherche web"), ("finalize", "Rapport et envoi")),
    "statuts": (("digikey", "Statut Digi-Key"),),
}
PHASES = {"notices": "enregistrement des avis", "pdf": "rapport PDF", "mail": "envoi de l'e-mail"}


def path(slug: str):
    return store.runs_dir(slug) / FILE


def read(slug: str) -> dict | None:
    try:
        data = json.loads(path(slug).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class Tracker:
    """État d'un lancement, gardé en mémoire et écrit d'un bloc : un lecteur ne voit jamais un JSON partiel.

    Un seul écrivain à la fois : les étapes d'un lancement se suivent (scan, signaux de l'agent, finalize).
    """

    def __init__(self, slug: str, state: dict):
        self.slug, self.state, self._last = slug, state, 0.0

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
    Tracker(slug, {"log": log, "kind": kind, "started_at": store.now_iso(), "step": "start"}).update()


def _minutes(since: str | None, until: str | None = None) -> int | None:
    try:
        a = dt.datetime.fromisoformat(since)
        b = dt.datetime.fromisoformat(until) if until else dt.datetime.now(a.tzinfo)
    except (TypeError, ValueError):
        return None
    return max(0, int((b - a).total_seconds() // 60))


def _step_detail(state: dict, step: str) -> tuple[str, int | None, int | None, str]:
    """(détail, valeur, maximum, composant en cours) de l'étape `step`."""
    if step == "digikey":
        dk = state.get("digikey") or {}
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
    if finished:
        summary = "Terminé"
        if "new" in result:
            summary += (f" : {result['new']} nouvel(s) avis"
                        + (f", dont {result['critical']} fin(s) de vie / dernière(s) commande(s)"
                           if result.get("critical") else ""))
        elif state.get("digikey"):
            summary += f" : {_step_detail(state, 'digikey')[0]}"
    elif current < 0:
        summary = "Démarrage…" if running else "Interrompu au démarrage"
    else:
        label = f"étape {current + 1}/{len(ids)} — {steps_def[current][1]}"
        summary = (f"Interrompu à l'{label}" if interrupted else f"É{label[1:]}") + (f" : {detail}" if detail else "")
        if doing and running:
            summary += f" · en cours : {doing}"
    return {
        "log": state.get("log"), "kind": state.get("kind"), "step": step, "steps": steps,
        "running": running, "finished": finished, "interrupted": interrupted,
        "value": value, "max": maximum or None, "current": doing if running else "",
        "batch": batch, "result": result,
        "minutes": _minutes(state.get("started_at"), None if running else state.get("updated_at")),
        "summary": summary,
    }
