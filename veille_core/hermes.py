"""Adaptateur UNIQUE vers le planificateur de Hermes Agent.

- Écritures : la CLI `hermes cron` (interface publique), appelée avec une liste
  d'arguments — jamais à travers un shell.
- Lectures : `cron/jobs.json` directement (Hermes l'écrit de façon atomique).

Isoler ce module permet de suivre les évolutions de l'amont sans toucher au reste.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path

from . import paths

SKILLS = ("brevo-email", "styled-pdf-report")
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
    args = ["create", schedule, prompt, "--name", name, "--workdir", workdir, "--deliver", "local"]
    for skill in SKILLS:
        args += ["--skill", skill]
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
         model: str | None = None, provider: str | None = None) -> None:
    args = ["edit", job_id]
    for flag, value in (("--name", name), ("--schedule", schedule), ("--prompt", prompt),
                        ("--workdir", workdir), ("--model", model), ("--provider", provider)):
        if value is not None:
            args += [flag, value]
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
    """Lance `hermes cron run` en arrière-plan, sortie dans `log_path` ; renvoie le PID.

    Nouvelle session : le run survit à un redémarrage du processus appelant (UI).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        p = subprocess.Popen(
            [paths.hermes_bin(), "cron", "run", job_id],
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, env=os.environ.copy(),
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
