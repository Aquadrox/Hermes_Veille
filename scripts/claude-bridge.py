#!/usr/bin/env python3
"""claude-bridge — pont entre le conteneur des veilles et Claude Code installé sur l'HÔTE.

Le conteneur ne peut pas lancer le binaire `claude` de l'hôte (ni ses identifiants, qui restent ici) : il
dépose une recherche sur une socket Unix placée dans le dossier de données (vu du conteneur :
/opt/data/claude-bridge/bridge.sock) et vient en chercher le résultat. Le pont lance pour chaque
recherche `claude -p` avec les SEULS outils WebSearch et WebFetch, dans un dossier vide, sans réglages
utilisateur (ni hooks, ni plugins, ni serveurs MCP) ; Claude n'a donc accès ni aux fichiers ni au shell
de l'hôte, même si une page lue tente de le détourner.

Protocole (HTTP sur la socket, JSON) :
  GET  /health              état, file d'attente, recherches du jour
  POST /jobs                {"id", "prompt", "schema"} → 202 ; un id déjà connu renvoie son état
  GET  /jobs/<id>           {"state": queued|running|done|error, "result": <sortie JSON de claude>, "error"}

Recherches asynchrones : une recherche dure plusieurs minutes, plus qu'un appel d'outil de Hermes ;
le conteneur interroge l'état par tranches. Les résultats sont conservés sur disque (STATE_DIR,
RETENTION_DAYS) : un redémarrage du pont ne perd que les recherches en cours, déclarées en échec.

Bornes (variables d'environnement, voir deploy/systemd/veille-claude-bridge.service) : un seul `claude`
à la fois par défaut, un plafond de recherches par jour, une durée maximale par recherche.
Bibliothèque standard uniquement : ce script tourne avec le python3 de l'hôte.
"""
from __future__ import annotations

import argparse
import datetime as dt
import grp
import http.server
import json
import os
import pwd
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

VERSION = 1
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
MAX_BODY = 256_000  # prompt + schéma : un brief tient en quelques dizaines de Ko
TOOLS = "WebSearch,WebFetch"
# Les sorties de claude au-delà sont tronquées : un résultat de recherche reste sous quelques centaines de Ko.
MAX_OUTPUT = 4_000_000


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class Bridge:
    """File des recherches : au plus `concurrency` processus `claude` à la fois, état persisté par recherche."""

    def __init__(self, state_dir: Path, claude: str, *, concurrency: int = 1, timeout: int = 1200,
                 max_per_day: int = 20, model: str = "", retention_days: int = 30):
        self.state_dir = state_dir
        self.claude = claude
        self.timeout = timeout
        self.max_per_day = max_per_day
        self.model = model
        self.retention_days = retention_days
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(concurrency)
        self.jobs: dict[str, dict] = {}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    # ---------------------------------------------------------------- persistance
    def _path(self, job_id: str) -> Path:
        return self.state_dir / f"{job_id}.json"

    def _save(self, job: dict) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".job.", dir=self.state_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(job, fh, ensure_ascii=False)
        os.replace(tmp, self._path(job["id"]))

    def _load(self) -> None:
        limit = time.time() - self.retention_days * 86400
        for path in self.state_dir.glob("*.json"):
            try:
                if path.stat().st_mtime < limit:
                    path.unlink()
                    continue
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(job, dict) or not ID_RE.fullmatch(str(job.get("id", ""))):
                continue
            if job.get("state") in ("queued", "running"):  # interrompue par l'arrêt du pont
                job.update(state="error", error="pont redémarré pendant la recherche", finished_at=now_iso())
                self._save(job)
            self.jobs[job["id"]] = job

    # ---------------------------------------------------------------- API
    def today_count(self) -> int:
        today = dt.date.today().isoformat()
        return sum(1 for j in self.jobs.values() if str(j.get("created_at", "")).startswith(today))

    def health(self) -> dict:
        with self.lock:
            states = [j["state"] for j in self.jobs.values()]
            return {"ok": True, "version": VERSION, "claude": self.claude, "model": self.model or None,
                    "running": states.count("running"), "queued": states.count("queued"),
                    "today": self.today_count(), "max_per_day": self.max_per_day, "timeout_s": self.timeout}

    def public(self, job: dict) -> dict:
        return {k: v for k, v in job.items() if k not in ("prompt", "schema")}

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return self.public(job) if job else None

    def submit(self, body: dict) -> tuple[int, dict]:
        job_id, prompt, schema = body.get("id"), body.get("prompt"), body.get("schema")
        if not isinstance(job_id, str) or not ID_RE.fullmatch(job_id):
            return 400, {"error": "id invalide (minuscules, chiffres et tirets, 80 caractères au plus)"}
        if not isinstance(prompt, str) or not prompt.strip():
            return 400, {"error": "prompt vide"}
        if schema is not None and not isinstance(schema, dict):
            return 400, {"error": "schema : objet JSON attendu"}
        with self.lock:
            if job_id in self.jobs:
                return 200, self.public(self.jobs[job_id])
            if self.today_count() >= self.max_per_day:
                return 429, {"error": f"plafond de {self.max_per_day} recherches Claude par jour atteint"}
            job = {"id": job_id, "state": "queued", "created_at": now_iso(), "prompt": prompt, "schema": schema}
            self.jobs[job_id] = job
            self._save(job)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return 202, self.public(job)

    # ---------------------------------------------------------------- exécution
    def command(self, schema: dict | None) -> list[str]:
        cmd = [self.claude, "-p", "--output-format", "json", "--tools", TOOLS, "--allowedTools", TOOLS,
               "--permission-mode", "dontAsk", "--strict-mcp-config", "--setting-sources", "project",
               "--no-session-persistence"]
        if schema:
            cmd += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def _finish(self, job: dict, **fields) -> None:
        with self.lock:
            job.update(finished_at=now_iso(), **fields)
            job.pop("prompt", None)  # conservé côté conteneur avec le résultat ; inutile ici
            job.pop("schema", None)
            self._save(job)

    def _run(self, job: dict) -> None:
        with self.slots:
            with self.lock:
                job.update(state="running", started_at=now_iso())
                self._save(job)
            # Dossier vide : aucun fichier de projet (CLAUDE.md, .claude/) ne se charge, rien à lire.
            cwd = tempfile.mkdtemp(prefix="claude-bridge-")
            started = time.monotonic()
            try:
                proc = subprocess.run(self.command(job.get("schema")), input=job["prompt"], cwd=cwd,
                                      capture_output=True, text=True, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self._finish(job, state="error", error=f"délai de {self.timeout} s dépassé")
                return
            except OSError as exc:
                self._finish(job, state="error", error=f"claude non lancé : {exc}")
                return
            finally:
                shutil.rmtree(cwd, ignore_errors=True)
            elapsed = round(time.monotonic() - started, 1)
            try:
                result = json.loads(proc.stdout[:MAX_OUTPUT])
            except ValueError:
                err = (proc.stderr or proc.stdout or "").strip()[-1000:]
                self._finish(job, state="error", elapsed_s=elapsed,
                             error=f"sortie de claude illisible (code {proc.returncode}) : {err}")
                return
            if not isinstance(result, dict) or result.get("is_error") or proc.returncode:
                detail = result.get("result") if isinstance(result, dict) else None
                self._finish(job, state="error", elapsed_s=elapsed, result=result,
                             error=f"claude en échec (code {proc.returncode}) : {str(detail or proc.stderr)[:1000]}")
                return
            self._finish(job, state="done", elapsed_s=elapsed, result=result)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "veille-claude-bridge"
    bridge: Bridge  # renseigné par serve()

    def address_string(self) -> str:  # socket Unix : pas d'adresse IP
        return "socket"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"{self.command} {self.path} — {fmt % args}\n")

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - nom imposé par http.server
        if self.path == "/health":
            return self._send(200, self.bridge.health())
        m = re.fullmatch(r"/jobs/([a-z0-9-]+)", self.path)
        job = self.bridge.get(m.group(1)) if m else None
        if job is None:
            return self._send(404, {"error": "recherche inconnue"})
        return self._send(200, job)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/jobs":
            return self._send(404, {"error": "chemin inconnu"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self._send(413, {"error": f"requête vide ou trop longue ({MAX_BODY} octets au plus)"})
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return self._send(400, {"error": "JSON invalide"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "objet JSON attendu"})
        status, payload = self.bridge.submit(body)
        return self._send(status, payload)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _owner(spec: str) -> tuple[int, int]:
    """« uid:gid » (nombres ou noms) : propriétaire de la socket, l'utilisateur hermes du conteneur."""
    user, _, group = spec.partition(":")
    uid = int(user) if user.isdigit() else pwd.getpwnam(user).pw_uid
    gid = int(group) if group.isdigit() else grp.getgrnam(group).gr_gid if group else uid
    return uid, gid


def serve(socket_path: Path, bridge: Bridge, owner: str | None = None) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    handler = type("BoundHandler", (Handler,), {"bridge": bridge})
    server = Server(str(socket_path), handler)
    if owner:
        uid, gid = _owner(owner)
        os.chown(socket_path, uid, gid)
    os.chmod(socket_path, 0o660)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    print(f"claude-bridge : écoute sur {socket_path} (claude : {bridge.claude})", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--socket", required=True, help="chemin de la socket (dossier de données de l'hôte)")
    ap.add_argument("--state-dir", default="/var/lib/veille-claude-bridge")
    ap.add_argument("--owner", help="propriétaire de la socket, uid:gid (utilisateur hermes du conteneur)")
    ap.add_argument("--claude", default=os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude")
    args = ap.parse_args(argv)
    bridge = Bridge(Path(args.state_dir), args.claude,
                    concurrency=_env_int("CLAUDE_BRIDGE_CONCURRENCY", 1),
                    timeout=_env_int("CLAUDE_BRIDGE_TIMEOUT", 1200),
                    max_per_day=_env_int("CLAUDE_BRIDGE_MAX_PER_DAY", 20),
                    model=os.environ.get("CLAUDE_BRIDGE_MODEL", "").strip(),
                    retention_days=_env_int("CLAUDE_BRIDGE_RETENTION_DAYS", 30))
    serve(Path(args.socket), bridge, args.owner)
    return 0


if __name__ == "__main__":
    sys.exit(main())
