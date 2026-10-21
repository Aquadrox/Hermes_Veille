"""Adaptateur UNIQUE vers le planificateur de Hermes Agent.

- Écritures : la CLI `hermes cron` (interface publique), appelée avec une liste
  d'arguments — jamais à travers un shell.
- Lectures : `cron/jobs.json` directement (Hermes l'écrit de façon atomique), et
  `state.db` en lecture seule pour la consommation de tokens des exécutions.

Isoler ce module permet de suivre les évolutions de l'amont sans toucher au reste.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
from pathlib import Path

from . import paths

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_CREATED = re.compile(r"Created job:\s*([0-9a-zA-Z_-]+)")


class HermesError(RuntimeError):
    pass


def _clean(text: str) -> str:
    return _ANSI.sub("", text or "").strip()


def _cli(*args: str, timeout: int = 120) -> str:
    cmd = [paths.hermes_bin(), "cron", *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HermesError(f"`hermes cron {args[0]}` impossible : {exc}") from exc
    out = _clean((p.stdout or "") + "\n" + (p.stderr or ""))
    if p.returncode != 0 or "Failed to" in out:
        raise HermesError(f"`hermes cron {args[0]}` a échoué : {out[-500:]}")
    return out


# --------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------
def load_jobs(jobs_file: Path | None = None) -> list[dict]:
    path = jobs_file or paths.jobs_file()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise HermesError(f"jobs.json illisible : {exc}") from exc
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if isinstance(jobs, dict):  # carte id -> job
        jobs = [{"id": k, **v} for k, v in jobs.items() if isinstance(v, dict)]
    return [j for j in jobs if isinstance(j, dict)]


def get_job(job_id: str | None, jobs: list[dict] | None = None) -> dict | None:
    if not job_id:
        return None
    for job in jobs if jobs is not None else load_jobs():
        if job.get("id") == job_id:
            return job
    return None


def find_by_name(name: str, jobs: list[dict] | None = None) -> dict | None:
    for job in jobs if jobs is not None else load_jobs():
        if job.get("name") == name:
            return job
    return None


def state_db() -> Path:
    return paths.hermes_home() / "state.db"


_TOKEN_COLS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")


_EXTRACT_MAX_URLS = 5  # Hermes ne transmet que les 5 premières URL d'un `web_extract` (tools/web_tools.py)


def _web_requests(tool_calls: str | None, seen: set[str]) -> int:
    """Requêtes de recherche web d'un message de l'agent (`messages.tool_calls`, format OpenAI), comme Hermes
    les envoie : une par `web_search`, une par URL de `web_extract` (le moteur lit une page par requête).
    `seen` : identifiants d'appel déjà comptés dans la session (la compression du contexte en recopie la
    fin sous le même identifiant de session)."""
    try:
        calls = json.loads(tool_calls or "[]")
    except ValueError:
        return 0
    count = 0
    for call in calls if isinstance(calls, list) else []:
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(fn, dict) or fn.get("name") not in ("web_search", "web_extract"):
            continue
        call_id = call.get("id") if isinstance(call.get("id"), str) else None
        if call_id in seen:
            continue
        if call_id:
            seen.add(call_id)
        if fn["name"] == "web_search":
            count += 1
            continue
        args = fn.get("arguments")
        try:
            urls = (json.loads(args) if isinstance(args, str) else args or {}).get("urls")
        except (ValueError, AttributeError):
            urls = None  # arguments illisibles : Hermes n'envoie rien
        count += min(len(urls), _EXTRACT_MAX_URLS) if isinstance(urls, list) else 0
    return count


def run_usage(job_id: str | None, limit: int = 50, db: Path | None = None) -> list[dict]:
    """Consommation des exécutions d'un job, la plus récente d'abord.

    Chaque exécution cron est une session Hermes `cron_<job_id>_<AAAAMMJJ_HHMMSS>` dans state.db.
    Les tokens sont lus par modèle dans `session_model_usage` (boucle principale ET tâches
    auxiliaires : compression, titres…), sinon sur la ligne de session ; les requêtes de recherche
    web (`web_requests`) dans les appels d'outils de l'agent (`messages`). Lecture seule ; toute
    erreur (base absente, schéma différent) donne une liste vide.
    """
    if not job_id or not re.fullmatch(r"[0-9A-Za-z-]+", job_id):
        return []
    path = db or state_db()
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            # Intervalle sur la clé primaire (`cron_<id>_` <= id < `cron_<id>``, « ` » suit « _ ») :
            # l'index est utilisé, là où un LIKE parcourrait toute la table des sessions.
            prefix = f"cron_{job_id}_"
            sessions = conn.execute(
                "SELECT id, model, started_at, ended_at, end_reason, api_call_count, estimated_cost_usd, "
                + ", ".join(_TOKEN_COLS)
                + " FROM sessions WHERE id >= ? AND id < ? ORDER BY started_at DESC LIMIT ?",
                (prefix, prefix[:-1] + "`", int(limit)),
            ).fetchall()
            usage: dict[str, list] = {}
            web: dict[str, int] = {}
            seen: dict[str, set[str]] = {}
            if sessions:  # détail de toutes les exécutions, une requête par table
                ids = [row["id"] for row in sessions]
                marks = ",".join("?" * len(ids))
                try:
                    for m in conn.execute(
                        "SELECT session_id, model, SUM(api_call_count) AS calls, SUM(estimated_cost_usd) AS est, "
                        + ", ".join(f"SUM({c}) AS {c}" for c in _TOKEN_COLS)
                        + f" FROM session_model_usage WHERE session_id IN ({marks}) GROUP BY session_id, model",
                        ids,
                    ):
                        usage.setdefault(m["session_id"], []).append(m)
                except sqlite3.OperationalError:
                    pass
                try:  # seuls les messages de l'agent qui appellent un outil web sont décodés
                    for m in conn.execute(
                        f"SELECT session_id, tool_calls FROM messages WHERE session_id IN ({marks})"
                        " AND role = 'assistant' AND tool_calls IS NOT NULL"  # index partiel de Hermes
                        " AND (instr(tool_calls, '\"web_search\"') > 0"
                        " OR instr(tool_calls, '\"web_extract\"') > 0)", ids,
                    ):
                        sid = m["session_id"]
                        web[sid] = web.get(sid, 0) + _web_requests(m["tool_calls"], seen.setdefault(sid, set()))
                except sqlite3.OperationalError:
                    pass
            runs = []
            for row in sessions:
                per_model = usage.get(row["id"], [])
                models = [{"model": m["model"], "api_calls": m["calls"] or 0,
                           "hermes_estimate_usd": m["est"] or 0.0,
                           **{c: m[c] or 0 for c in _TOKEN_COLS}} for m in per_model]
                if not models:  # base antérieure au détail par modèle : ligne de session
                    models = [{"model": row["model"], "api_calls": row["api_call_count"] or 0,
                               "hermes_estimate_usd": row["estimated_cost_usd"] or 0.0,
                               **{c: row[c] or 0 for c in _TOKEN_COLS}}]
                runs.append({"session_id": row["id"], "started_at": row["started_at"],
                             "ended_at": row["ended_at"], "end_reason": row["end_reason"],
                             "models": models, "web_requests": web.get(row["id"], 0)})
            return runs
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def is_paused(job: dict) -> bool:
    return (
        job.get("state") == "paused"
        or bool(job.get("paused_at"))
        or job.get("enabled") is False
    )


def job_status(job: dict | None) -> dict:
    """Vue normalisée de l'état d'un job pour les appelants."""
    if not job:
        return {"exists": False, "paused": False, "state": "absent"}
    return {
        "exists": True,
        "paused": is_paused(job),
        "state": "en pause" if is_paused(job) else "actif",
        "next_run_at": None if is_paused(job) else job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "last_error": job.get("last_error"),
        "prompt": job.get("prompt"),
        "workdir": job.get("workdir"),
        "name": job.get("name"),
    }


# --------------------------------------------------------------------------
# Écriture (CLI)
# --------------------------------------------------------------------------
def create(*, name: str, schedule: str, prompt: str, workdir: str,
           model: str | None = None, provider: str | None = None, paused: bool = False) -> str:
    # Aucun skill attaché : PDF et e-mail sont produits par `veillectl`, jamais par l'agent.
    args = ["create", schedule, prompt, "--name", name, "--workdir", workdir, "--deliver", "local"]
    if model:
        args += ["--model", model]
    if provider:
        args += ["--provider", provider]
    if paused:
        args.append("--paused")
    out = _cli(*args)
    m = _CREATED.search(out)
    if m:
        return m.group(1)
    job = find_by_name(name)
    if job:
        return job["id"]
    raise HermesError(f"job créé mais identifiant introuvable dans : {out[-300:]}")


def edit(job_id: str, *, name: str | None = None, schedule: str | None = None,
         prompt: str | None = None, workdir: str | None = None,
         model: str | None = None, provider: str | None = None, clear_skills: bool = False) -> None:
    args = ["edit", job_id]
    for flag, value in (("--name", name), ("--schedule", schedule), ("--prompt", prompt),
                        ("--workdir", workdir), ("--model", model), ("--provider", provider)):
        if value is not None:
            args += [flag, value]
    if clear_skills:
        args.append("--clear-skills")
    if len(args) > 2:
        _cli(*args)


def pause(job_id: str) -> None:
    _cli("pause", job_id)


def resume(job_id: str) -> None:
    _cli("resume", job_id)


def remove(job_id: str) -> None:
    _cli("remove", job_id)


def run_sync(job_id: str, timeout: int = 3 * 3600) -> str:
    """Exécute le job jusqu'au bout (la CLI `run` est synchrone)."""
    return _cli("run", job_id, timeout=timeout)


def run_detached(job_id: str, log_path: Path) -> int:
    """Lance `hermes cron run` en arrière-plan, sortie dans `log_path` ; renvoie le PID."""
    return spawn_detached([paths.hermes_bin(), "cron", "run", job_id], log_path)


def spawn_detached(cmd: list[str], log_path: Path, env: dict[str, str] | None = None) -> int:
    """Lance `cmd` (liste d'arguments, jamais de shell) en arrière-plan, sortie dans `log_path`.

    Nouvelle session : le processus survit à un redémarrage de l'appelant (UI).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        p = subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, env=env or os.environ.copy(),
        )
    # Récolte le processus à sa fin (sinon il reste zombie dans l'UI).
    threading.Thread(target=p.wait, name=f"reap-{p.pid}", daemon=True).start()
    return p.pid


def runs(job_id: str, limit: int = 10) -> list[dict]:
    """Historique d'exécution (`hermes cron runs`), analysé ligne par ligne."""
    try:
        out = _cli("runs", job_id, "--limit", str(limit), timeout=30)
    except HermesError:
        return []
    rows: list[dict] = []
    for line in out.splitlines():
        if line.startswith("    ") and rows:
            rows[-1]["error"] = line.strip()
            continue
        parts = line.split()
        if len(parts) >= 5 and parts[2].startswith("job="):
            rows.append({"id": parts[0], "status": parts[1], "source": parts[3].removeprefix("source="),
                         "at": parts[4], "error": None})
    return rows
