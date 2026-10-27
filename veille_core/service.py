"""Opérations métier sur les veilles — seule API destinée à la CLI et à l'interface.

Règle de cohérence : le job Hermes fait foi pour l'ÉTAT (actif / en pause, prochain
et dernier run) ; `veille.json` porte le reste et de quoi recréer le job. Chaque
opération met à jour les deux, puis re-rend le prompt.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote_plus

from . import (
    catalog,
    contacts,
    credentials,
    db,
    digikey,
    guard,
    hermes,
    paths,
    pricing,
    progress,
    render,
    schedule,
    settings,
    store,
)
from . import components as comp
from .store import PdfOptions, StoreError, Veille

LEGACY_JOB_NAME = "Veille technologique hebdomadaire"
LEGACY_SLUG = "industrie-4-0"
BOOTSTRAP_MARKER = ".bootstrapped"
PLACEHOLDER_RECIPIENT = store.PLACEHOLDER_RECIPIENT
COMPONENTS_TEMPLATE = "composants"
COMPONENTS_SCHEDULE = "0 23 * * 0"  # dimanche 23h : heures creuses DeepSeek toute l'année
CHAPTER_MARK = "<!-- chapitre-composants -->"
SCAN_BUDGET_S = 900  # passage Digi-Key, toutes tranches confondues : la suite au prochain passage
# Tranche d'un appel `veillectl components scan` (D1, voir components_scan) : elle, l'appel Digi-Key en cours à son
# terme (jeton expiré : 2 authentifications et 2 requêtes) et l'enregistrement tiennent dans guard.COMMAND_TIMEOUT.
SCAN_SLICE_S = guard.COMMAND_TIMEOUT - 4 * digikey.HTTP_TIMEOUT - 60  # 220 s
SCAN_BUDGET_STOP = "budget de temps ou d'appels atteint : suite au prochain passage"
SCAN_RESUME_S = 3600  # passage inachevé repris s'il a avancé depuis moins d'une heure (tranches d'un même tir)
# Appels Digi-Key des recherches d'alternatives d'une série (un à trois par recherche, voir digikey.Client.similar) :
# avec digikey.MAX_CALLS, sous le quota journalier.
ALT_MAX_CALLS = 100


class ServiceError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Outils
# --------------------------------------------------------------------------
@contextlib.contextmanager
def _locked():
    """Sérialise les modifications (CLI, interface et seed peuvent coexister)."""
    root = paths.veilles_dir()
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


audit = store.audit


def read_audit(limit: int = 50) -> list[dict]:
    try:
        lines = paths.audit_log().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in reversed(lines[-limit:]):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def _best_effort(what: str, fn, *args, slug: str = "", trace: bool = True):
    """Appel à la base SQLite (index des composants, carnet d'adresses) : un échec est tracé, jamais bloquant.
    Les fichiers de la veille font foi, et l'index se reconstruit au démarrage (`rebuild_catalog`).
    `trace=False` : lecture pour l'affichage d'une page, qui signale elle-même la panne (journal non inondé)."""
    try:
        return fn(*args)
    except db.ERRORS as exc:
        if trace:
            audit("system", "db-error", slug, f"{what} : {type(exc).__name__}: {exc}"[:300])
        return None


def job_name(v: Veille) -> str:
    return f"{v.name} [{v.slug}]"


def _apply_schedule(v: Veille, expr: str) -> None:
    schedule.validate_cron(expr)
    v.schedule = expr
    v.period = schedule.period_of(expr)
    v.max_age_hours = schedule.max_age_hours(expr)


def _pdf(data: dict | None, base: PdfOptions | None = None) -> PdfOptions:
    pdf = base or PdfOptions()
    for key, value in (data or {}).items():
        if key in PdfOptions.__dataclass_fields__ and value is not None:
            setattr(pdf, key, str(value).strip())
    if pdf.accent and not (pdf.accent.startswith("#") and len(pdf.accent) in (4, 7)):
        raise StoreError("couleur d'accent invalide (format #rrggbb)")
    return pdf


def _create_job(v: Veille, paused: bool = False) -> str:
    return hermes.create(name=job_name(v), schedule=v.schedule, prompt=render.short_prompt(v),
                         workdir=str(v.dir), model=v.model, provider=v.provider, paused=paused)


# --------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------
def overview() -> list[dict]:
    jobs = _safe_jobs()
    rows = []
    for v in store.list_all():
        rows.append(describe(v, jobs))
    return rows


def describe(v: Veille, jobs: list[dict] | None = None) -> dict:
    jobs = _safe_jobs() if jobs is None else jobs
    status = hermes.job_status(hermes.get_job(v.job_id, jobs))
    reports = store.list_reports(v.slug)
    running = running_pid(v.slug) is not None
    return {
        "veille": v,
        "status": status,
        "schedule_human": schedule.describe(v.schedule),
        "recipients": store.read_recipients(v.slug),
        "last_report": reports[0] if reports else None,
        "report_count": len(reports),
        "running": running,
        "progress": run_progress(v.slug, running=True) if running else None,
        "components": _components_summary(v),
    }


def _components_summary(v: Veille) -> dict | None:
    if v.kind != "composants":
        return None
    linked = [x.slug for x in _linked_veilles(v.slug)]
    try:
        return {**comp.summary(comp.load_components(v.slug)["components"], comp.load_notices(v.slug)),
                "linked": linked}
    except comp.ComponentsError as exc:
        return {"error": str(exc), "linked": linked}


def _safe_jobs() -> list[dict]:
    try:
        return hermes.load_jobs()
    except hermes.HermesError:
        return []


# --------------------------------------------------------------------------
# Consommation et coût
# --------------------------------------------------------------------------
def _run_row(run: dict, table: dict) -> dict:
    total = {c: sum(m[c] for m in run["models"]) for c in hermes._TOKEN_COLS}
    cost, peak, unknown = 0.0, False, []
    for m in run["models"]:
        usd, peak = pricing.run_cost(m["model"], input_tokens=m["input_tokens"],
                                     cache_read_tokens=m["cache_read_tokens"],
                                     output_tokens=m["output_tokens"],
                                     started_at=run["started_at"], table=table)
        if usd is None:
            unknown.append(m["model"])
            usd = m["hermes_estimate_usd"] or 0.0  # repli : estimation de Hermes
        cost += usd
    web_usd = pricing.web_cost(run["web_requests"], run["started_at"], table)
    cost += web_usd
    started = dt.datetime.fromtimestamp(run["started_at"], schedule.ZONE) if run["started_at"] else None
    ended = run["ended_at"]
    return {
        "session_id": run["session_id"],
        "started_at": started,
        "duration_min": round((ended - run["started_at"]) / 60, 1) if ended and run["started_at"] else None,
        "models": sorted({m["model"] for m in run["models"] if m["model"]}),
        "api_calls": sum(m["api_calls"] for m in run["models"]),
        **total,
        "peak": peak,
        "web_requests": run["web_requests"],
        "web_usd": round(web_usd, 4),
        "cost_usd": round(cost, 4),
        "hermes_estimate_usd": round(sum(m["hermes_estimate_usd"] for m in run["models"]), 4),
        "unpriced_models": unknown,
    }


def costs(slug: str, days: int = 30, limit: int = 50) -> dict:
    """Exécutions mesurées d'une veille (tokens, durée, heures pleines, coût) et totaux."""
    v = store.load(slug)
    table = pricing.load()
    runs = [_run_row(r, table) for r in hermes.run_usage(v.job_id, limit)]
    now = dt.datetime.now(schedule.ZONE)
    since = now - dt.timedelta(days=days)
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    recent = [r for r in runs if r["started_at"] and r["started_at"] >= since]
    this_month = [r for r in runs if r["started_at"] and r["started_at"] >= month]
    return {
        "slug": slug,
        "runs": runs,
        "days": days,
        "total_days_usd": round(sum(r["cost_usd"] for r in recent), 4),
        "runs_days": len(recent),
        "total_month_usd": round(sum(r["cost_usd"] for r in this_month), 4),
        "runs_month": len(this_month),
        "average_run_usd": round(sum(r["cost_usd"] for r in runs) / len(runs), 4) if runs else None,
        "pricing_updated": table.get("updated"),
        "currency": table.get("currency", "USD"),
    }


def costs_overview(days: int = 30) -> dict:
    rows = []
    for v in store.list_all():
        c = costs(v.slug, days)
        rows.append({"slug": v.slug, "name": v.name, "total_month_usd": c["total_month_usd"],
                     "runs_month": c["runs_month"], "total_days_usd": c["total_days_usd"],
                     "average_run_usd": c["average_run_usd"]})
    return {"days": days, "veilles": rows,
            "total_month_usd": round(sum(r["total_month_usd"] for r in rows), 4),
            "total_days_usd": round(sum(r["total_days_usd"] for r in rows), 4)}


# --------------------------------------------------------------------------
# Création / modification
# --------------------------------------------------------------------------
def create_veille(*, name: str, subject: str, schedule_expr: str, recipients: list[str],
                  brief: str, pdf: dict | None = None, slug: str | None = None,
                  model: str | None = None, provider: str | None = None,
                  paused: bool = False, kind: str = "veille", components: list[dict] | None = None,
                  include_components: str | None = None, batch_size: int | None = None,
                  actor: str = "cli") -> Veille:
    name = (name or "").strip()
    if not name:
        raise StoreError("le nom est obligatoire")
    slug = store.check_slug(slug or store.slugify(name))
    if kind not in store.KINDS:
        raise StoreError(f"type de veille inconnu : {kind!r}")
    if not recipients:
        raise StoreError("au moins un destinataire est requis")
    with _locked():
        if store.veille_dir(slug).exists():
            raise StoreError(f"une veille « {slug} » existe déjà")
        v = Veille(slug=slug, name=name, subject=(subject or "").strip(), kind=kind,
                   model=model or None, provider=provider or None, pdf=_pdf(pdf))
        _apply_schedule(v, schedule_expr)
        if batch_size is not None:
            v.batch_size = _batch_size(batch_size)
        reprise: dict = {}
        try:
            store.save(v)
            store.write_recipients(slug, recipients)
            store.write_brief(slug, brief)
            if kind == "composants":
                data = {"components": [], "batch": []}
                comp.save_components(slug, data)
                reprise = _store_components(slug, data, components or [])
            _set_link(v, include_components)
            render.write(v)
            v.job_id = _create_job(v, paused=paused)
            store.save(v)
        except Exception:
            shutil.rmtree(v.dir, ignore_errors=True)
            if reprise.get("changed"):  # contribution indexée d'une veille qui n'existe pas
                _best_effort("index des composants", catalog.sync, slug, [], [], slug=slug)
            raise
        _best_effort("carnet d'adresses", contacts.remember, recipients, slug=slug)
    audit(actor, "create", slug, f"{schedule_expr}; {len(recipients)} destinataire(s)" + _reprise_note(reprise))
    return v


def update_veille(slug: str, *, name: str | None = None, subject: str | None = None,
                  schedule_expr: str | None = None, recipients: list[str] | None = None,
                  brief: str | None = None, pdf: dict | None = None,
                  model: str | None = None, provider: str | None = None,
                  components: list[dict] | None = None, include_components: str | None = None,
                  batch_size: int | None = None, actor: str = "cli") -> Veille:
    with _locked():
        v = store.load(slug)
        # Avant toute écriture : jobs.json illisible ≠ job absent (échouer plutôt que doublonner).
        jobs = hermes.load_jobs()
        changes = []
        if name is not None and name.strip() and name.strip() != v.name:
            v.name = name.strip()
            changes.append("nom")
        if subject is not None and subject.strip() != v.subject:
            v.subject = subject.strip()
            changes.append("sujet")
        new_schedule = None
        if schedule_expr is not None:
            expr = " ".join(schedule_expr.split())
            if expr != v.schedule:
                _apply_schedule(v, expr)
                new_schedule = expr
                changes.append("fréquence")
        if pdf is not None:
            v.pdf = _pdf(pdf, v.pdf)
        if model is not None and (model or None) != v.model:
            v.model = model or None
            changes.append("modèle")
        if provider is not None and (provider or None) != v.provider:
            v.provider = provider or None
            if "modèle" not in changes:
                changes.append("modèle")
        if recipients is not None:
            old = store.read_recipients(slug)
            if recipients != old:
                changes.append("destinataires")
            store.write_recipients(slug, recipients)
            known = {a.lower() for a in old}
            added = [a for a in recipients if a.lower() not in known]
            if added:  # adresses nouvelles seulement : une adresse « oubliée » du carnet n'y revient pas seule
                _best_effort("carnet d'adresses", contacts.remember, added, slug=slug)
        if brief is not None:
            if brief.replace("\r\n", "\n") != store.read_brief(slug):
                changes.append("brief")
            store.write_brief(slug, brief)
        if components is not None:
            if v.kind != "composants":
                raise StoreError("seule une veille composants a une liste de composants")
            reprise = _store_components(slug, comp.load_components(slug), components)
            if reprise.get("keys_changed"):
                changes.append("composants" + _reprise_note(reprise))
        if batch_size is not None and _batch_size(batch_size) != v.batch_size:
            v.batch_size = _batch_size(batch_size)
            changes.append("taille du lot")
        if include_components is not None and _set_link(v, include_components):
            changes.append("chapitre composants")
        store.save(v)
        render.write(v)
        job = hermes.get_job(v.job_id, jobs)
        if job is None:
            v.job_id = _create_job(v)
            store.save(v)
        else:
            hermes.edit(v.job_id, name=job_name(v), schedule=new_schedule,
                        prompt=render.short_prompt(v), workdir=str(v.dir),
                        model=v.model if "modèle" in changes else None,
                        provider=v.provider if "modèle" in changes else None)
    audit(actor, "update", slug, ", ".join(changes) or "aucun changement")
    return v


# --------------------------------------------------------------------------
# Cycle de vie
# --------------------------------------------------------------------------
def _job_id_or_fail(v: Veille) -> str:
    if not v.job_id or hermes.get_job(v.job_id, _safe_jobs()) is None:
        raise ServiceError("job Hermes absent — lancer `veillectl reconcile`")
    return v.job_id


def pause(slug: str, actor: str = "cli") -> None:
    v = store.load(slug)
    hermes.pause(_job_id_or_fail(v))
    audit(actor, "pause", slug)


def resume(slug: str, actor: str = "cli") -> None:
    v = store.load(slug)
    if PLACEHOLDER_RECIPIENT in store.read_recipients(slug):
        raise ServiceError("renseigner de vrais destinataires avant de reprendre la veille")
    hermes.resume(_job_id_or_fail(v))
    audit(actor, "resume", slug)


def _pid_file(slug: str) -> Path:
    return store.runs_dir(slug) / ".running"


def running_pid(slug: str) -> int | None:
    try:
        pid = int(_pid_file(slug).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    # Processus zombie (fini mais pas encore récolté) : considéré terminé.
    try:
        with open(f"/proc/{pid}/stat") as fh:
            if fh.read().split(")")[-1].split()[0] == "Z":
                return None
    except OSError:
        pass
    return pid


def _current_state(slug: str) -> dict | None:
    """Avancement du DERNIER lancement manuel ; None : absent, illisible, ou d'un lancement plus ancien."""
    state = progress.read(slug)
    latest = store.list_run_logs(slug, 1)
    return state if state and latest and state["log"] == latest[0] else None


def run_progress(slug: str, log: str | None = None, running: bool | None = None) -> dict | None:
    """Avancement du dernier lancement manuel (vue commune interface / MCP) ; None s'il n'est pas suivi.
    `log` : seulement si c'est ce journal-là ; `running` : déjà connu de l'appelant."""
    state = _current_state(slug)
    if state is None or log not in (None, state["log"]):
        return None
    return progress.view(state, running_pid(slug) is not None if running is None else running)


def _tracker(slug: str, kind: str) -> progress.Tracker | None:
    """Suivi du lancement manuel EN COURS de ce type (interface, Telegram), sinon None : une exécution
    planifiée ou une commande tapée à la main n'écrit rien, personne ne la regarde ; un lancement terminé
    (étape « done ») non plus."""
    state = _current_state(slug)
    if state is None or state["kind"] != kind or state["step"] == "done" or running_pid(slug) is None:
        return None
    return progress.Tracker(slug, state)


def _check_runnable(v: Veille) -> str:
    job_id = _job_id_or_fail(v)
    if hermes.is_paused(hermes.get_job(job_id)):
        # `hermes cron run` réactiverait le job : on ne le fait pas implicitement.
        raise ServiceError("veille en pause : la reprendre avant de la lancer")
    if PLACEHOLDER_RECIPIENT in store.read_recipients(v.slug):
        raise ServiceError("renseigner de vrais destinataires avant de lancer la veille")
    if running_pid(v.slug):
        raise ServiceError("un lancement est déjà en cours")
    return job_id


def _start(slug: str, launch, actor: str, action: str, track: str | None = None) -> str:
    """Démarre un traitement en arrière-plan (journal dans runs/, PID suivi) ; renvoie le journal.
    `track` : type de lancement dont l'avancement est suivi (progress.STEPS), écrit avant le départ."""
    log_name = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".log"
    store.runs_dir(slug).mkdir(parents=True, exist_ok=True)
    if track:
        progress.start(slug, log_name, track)
    pid = launch(store.runs_dir(slug) / log_name)
    _pid_file(slug).write_text(str(pid))
    audit(actor, action, slug, log_name)
    return log_name


def run_now(slug: str, actor: str = "cli") -> str:
    """Lance la veille en arrière-plan ; renvoie le nom du journal."""
    v = store.load(slug)
    job_id = _check_runnable(v)
    return _start(slug, lambda log: hermes.run_detached(job_id, log), actor, "run",
                  "analyse" if v.kind == "composants" else None)


def _require_digikey() -> tuple[str, str]:
    pair = digikey.credentials()
    if not pair:
        raise ServiceError("clés Digi-Key absentes : les saisir dans l'interface, page Sources")
    return pair


def _spawn_components(slug: str, args: list[str], actor: str, action: str, track: str) -> str:
    """`veillectl components <args>` en arrière-plan (Digi-Key seul : ni LLM, ni e-mail), un lancement à la fois ;
    renvoie le nom du journal."""
    _components_veille(slug)
    _require_digikey()
    if running_pid(slug):
        raise ServiceError("un lancement est déjà en cours")
    app_dir = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (app_dir, os.environ.get("PYTHONPATH"))))}
    cmd = [sys.executable, "-m", "veille_core", "components", *args]
    return _start(slug, lambda log: hermes.spawn_detached(cmd, log, env), actor, action, track)


def check_statuses_now(slug: str, actor: str = "cli") -> str:
    """Vérifie tout de suite le statut Digi-Key des composants, en arrière-plan.

    Déterministe : ni LLM (aucun coût en tokens), ni e-mail, ni recherche web ; le lot de la
    prochaine exécution planifiée n'est pas modifié. Renvoie le nom du journal.
    """
    return _spawn_components(slug, ["scan", slug, "--status-only"], actor, "components-check", "statuts")


def alternatives_now(slug: str, mpns: list[str] | None = None, actor: str = "cli") -> str:
    """Cherche tout de suite, en arrière-plan, les alternatives des références `mpns`, sinon des composants en fin
    de vie qui l'attendent (`components.alternatives_due`, comme après un passage Digi-Key). Renvoie le journal.

    Seuls les MPN suivis passent en argument (tels qu'enregistrés : `components.MPN_RE`, jamais une option)."""
    picked = []
    if mpns:
        chosen, _ = comp.select(comp.load_components(_components_veille(slug).slug)["components"], mpns)
        picked = list(dict.fromkeys(c["mpn"] for c in chosen))
        if not picked:
            raise ServiceError("aucune de ces références n'est suivie par cette veille")
    return _spawn_components(slug, ["alternatives", slug, *picked], actor, "components-alternatives", "alternatives")


def run_wait(slug: str, actor: str = "cli") -> str:
    """Lance la veille et attend la fin (cible `make run-now`) ; ServiceError si elle n'a publié aucun rapport
    ou si Hermes l'a enregistrée en échec (garde-fou compris).

    `hermes cron run` sort en 0 même quand l'exécution échoue : rapport produit et statut sont vérifiés ici.
    """
    v = store.load(slug)
    job_id = _check_runnable(v)
    audit(actor, "run", slug, "synchrone")
    before = hermes.job_status(hermes.get_job(job_id, _safe_jobs())).get("last_run_at")
    started = time.time()
    out = hermes.run_sync(job_id)
    job = hermes.job_status(hermes.get_job(job_id, _safe_jobs()))
    # Statut de CETTE exécution seulement : sans nouvelle date, il serait celui de la précédente.
    failed = job.get("last_run_at") != before and job.get("last_status") not in (None, "", "ok")
    error = (job.get("last_error") or f"statut {job['last_status']}") if failed else None
    report = store.published_since(slug, started)
    if report is None or error:
        raise ServiceError("échec : " + " — ".join(filter(None, (None if report else guard.NO_REPORT, error)))
                           + f"\n{out}")
    return out


def archive(slug: str, actor: str = "cli") -> Path:
    with _locked():
        v = store.load(slug)
        # load_jobs (et non _safe_jobs) : un job non supprimé continuerait d'envoyer des e-mails.
        if v.job_id and hermes.get_job(v.job_id, hermes.load_jobs()) is not None:
            hermes.remove(v.job_id)
        dest = paths.archive_dir() / f"{slug}-{dt.datetime.now():%Y%m%d-%H%M%S}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(v.dir), str(dest))
        if v.kind == "composants":  # sa connaissance reste dans la base, sous le nom de l'archive
            _best_effort("index des composants", catalog.archive, slug, dest.name, slug=slug)
        for other in _linked_veilles(slug):  # plus de chapitre vers une veille archivée
            other.components = None
            store.save(other)
            render.write(other)
    audit(actor, "archive", slug, str(dest))
    return dest


def brevo_script() -> str:
    return os.environ.get(
        "BREVO_SCRIPT", str(paths.bundled_skills_dir() / "email/brevo-email/scripts/brevo_send.py"))


def _brevo(args: list[str], timeout: int = 120) -> str:
    """Envoi par le skill brevo-email ; renvoie sa sortie ou lève ServiceError."""
    cmd = [sys.executable, brevo_script(), *args, "--env-file", str(paths.hermes_home() / ".env")]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"envoi impossible : {exc}") from exc
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode != 0:
        raise ServiceError(out or "échec de l'envoi")
    return out


def send_test_email(slug: str, actor: str = "cli") -> str:
    v = store.load(slug)
    try:
        out = _brevo(["--bcc-file", str(v.dir / "recipients.txt"),
                      "--subject", f"[TEST] {v.name} — canal d'envoi",
                      "--text", (f"Ceci est un e-mail de test de la veille « {v.name} ».\n\n"
                                 f"Vous le recevez car vous figurez parmi ses destinataires "
                                 f"({schedule.describe(v.schedule)}).\n\n— Veille automatique.")],
                     timeout=60)
    except ServiceError:
        audit(actor, "test-email", slug, "échec")
        raise
    audit(actor, "test-email", slug, "ok")
    return out


# --------------------------------------------------------------------------
# Veille composants (PCN / fins de vie)
# --------------------------------------------------------------------------
def _batch_size(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise StoreError(f"taille de lot invalide : {value!r}") from None
    if not 1 <= n <= 500:
        raise StoreError("taille de lot hors limites (1–500)")
    return n


def _components_veille(slug: str) -> Veille:
    v = store.load(slug)
    if v.kind != "composants":
        raise ServiceError(f"« {slug} » n'est pas une veille composants")
    return v


def _linked_veilles(slug: str) -> list[Veille]:
    return [x for x in store.list_all() if x.components == slug]


def _set_link(v: Veille, target: str | None) -> bool:
    """Rattache (ou détache, cible vide) le chapitre composants d'une veille ; True si changé.

    Au rattachement, les avis déjà connus sont marqués livrés à cette veille (jour « rattachement ») :
    le premier chapitre ne contient que les nouveautés, le rappel des fins de vie couvre le reste.
    """
    target = (target or "").strip() or None
    if target == v.components:
        return False
    if target:
        if v.kind != "veille":
            raise StoreError("seule une veille thématique peut inclure un chapitre composants")
        _components_veille(target)
        notices = comp.load_notices(target)
        comp.mark_delivered(notices, {n["id"] for n in notices}, comp.chapter_channel(v.slug),
                            f"rattachement {comp.today()}")
        comp.save_notices(target, notices)
    v.components = target
    return True


def components_scan(slug: str, actor: str = "job", client: digikey.Client | None = None,
                    budget_s: float | None = None, plan_batch: bool = True) -> dict:
    """Statut Digi-Key de tous les composants, puis lot de recherche web du jour (enregistré).

    D1 (incident du 22/10/2026) — passage PAR TRANCHES reprenables. Un passage complet (≈ 17 min pour 386
    références) dépasse le délai que Hermes accorde à un appel d'outil (420 s) : chaque appel de l'agent traite au
    plus SCAN_SLICE_S de Digi-Key, ENREGISTRE ce qu'il a lu, puis rend `"complete": false` ; relancé tel quel, il
    reprend aux références non vérifiées aujourd'hui. Le passage garde son budget (SCAN_BUDGET_S, MAX_CALLS, pannes
    tolérées) et ses compteurs, cumulés sur ses tranches (`scan` de components.json) ; le lot web n'est tiré qu'à sa
    fin, avec les mêmes règles qu'avant ; entre deux tranches, aucun lot (un finalize anticipé ne marquerait pas
    recherché un lot ancien). Rejetés : l'arrière-plan avec `process_manage` (registre de processus commun à toutes les
    sessions de Hermes : une porte de plus pour une page piégée), relever les plafonds de Hermes (délai commun à
    toutes ses sessions, commande opaque et sans reprise ; voir guard.COMMAND_TIMEOUT), un script lancé par Hermes
    avant l'agent (sa sortie, dont des titres d'avis venus du web, entrerait dans le prompt sans le marquage « non
    fiable » des résultats d'outils). Un passage inachevé est repris s'il a avancé depuis moins de SCAN_RESUME_S,
    même à cheval sur minuit (références vérifiées depuis son début sautées) ; une tranche arrêtée net (SIGTERM) ne
    perd que ses propres lectures.

    `plan_batch=False` (vérification à la demande, processus détaché, sans délai d'outil) : un seul appel pour tout
    le passage ; ni le lot web en cours ni le passage du job ne sont touchés, pour ne pas fausser une exécution
    planifiée qui serait entre ses étapes « scan » et « finalize ».

    Les appels réseau se font HORS verrou ; les résultats sont ensuite appliqués par clé à la liste relue sous
    verrou (éditions concurrentes préservées).

    Lancement manuel suivi : avancement publié (référence en cours, puis lot) ; `track_progress` dit
    à l'agent de signaler ses groupes de recherche web (`components_progress`).

    Panne passagère de Digi-Key (réseau, HTTP 5xx ou 429) : le passage attend (`digikey.retry_delay`) puis
    reprend à la référence interrompue, sans réinterroger les précédentes (à la tranche suivante si l'attente
    la dépasse). Au plus `digikey.MAX_ATTEMPTS` pannes par passage, toutes références confondues ; le budget de
    temps et MAX_CALLS restent prioritaires. Après abandon, les références non interrogées gardent leur date de
    vérification : elles partent au lot web, et le rapport dit le passage partiel (`components.scan_coverage`).
    """
    _components_veille(slug)
    track = _tracker(slug, "analyse" if plan_batch else "statuts")
    day = comp.today()
    budget = SCAN_BUDGET_S if budget_s is None else budget_s
    creds = digikey.credentials()
    data = comp.load_components(slug)
    last = data.get("scan") or {}
    # Passage du job : l'inachevé qui vient d'avancer (même tir), sinon un nouveau (budget neuf) ; vérification à la
    # demande : toujours un passage à part, jamais enregistré.
    resume = plan_batch and not last.get("complete") and time.time() - last.get("at", 0) < SCAN_RESUME_S
    run = last if resume else {"date": day, "seconds": 0.0, "calls": 0, "incidents": 0, "checked": 0, "errors": 0,
                               "notices": 0}
    info: dict = {"configured": bool(creds or client), "checked": run["checked"], "errors": run["errors"]}
    results: dict[str, dict] = {}
    complete = True  # passage fini à l'issue de cet appel ; sinon tranche écoulée, suite à l'appel suivant
    total = len(data["components"])
    dk_progress: dict = {"skipped": True}
    if creds or client:
        client = client or digikey.Client(*creds)
        start = time.monotonic()
        # Sauf celles vérifiées depuis le début du passage (tranches précédentes, relance du même jour).
        todo = [c for c in sorted(data["components"], key=lambda c: (c.get("dk") or {}).get("checked_at") or "")
                if ((c.get("dk") or {}).get("checked_at") or "") < run["date"]]
        done_before = total - len(todo)
        left = budget - run["seconds"]  # budget restant du passage
        slice_s = min(SCAN_SLICE_S, left) if plan_batch else left
        calls_left = digikey.MAX_CALLS - run["calls"]
        i = 0
        failures = run["incidents"]  # pannes passagères du passage, toutes références et tranches confondues
        if failures:
            info["incidents"] = failures
        while i < len(todo):
            c = todo[i]
            elapsed = time.monotonic() - start
            if client.calls >= calls_left or elapsed > left:
                info["stopped"] = SCAN_BUDGET_STOP
                break
            if elapsed > slice_s:
                complete = False
                break
            if track:
                track.update(throttle=True, step="digikey",
                             digikey=_dk_progress(info, done_before + i, total, current=c["mpn"]))
            try:
                results[comp.key_of(c["mpn"], c["manufacturer"])] = client.status(c["mpn"], c["manufacturer"])
            except digikey.TransientError as exc:
                failures += 1
                info["incidents"] = failures
                wait = digikey.retry_delay(failures, exc)
                resume = time.monotonic() - start + wait
                attempt = f"tentative {failures}/{digikey.MAX_ATTEMPTS} : {exc}"
                if failures >= digikey.MAX_ATTEMPTS:
                    info["error"] = f"{exc} : abandon après {failures} tentatives, reprise au prochain passage"
                elif client.calls >= calls_left or resume > left:  # prioritaires
                    info["stopped"] = SCAN_BUDGET_STOP
                    info["error"] = (f"{exc} : abandon après {failures} tentative(s), budget de temps ou d'appels "
                                     "atteint, reprise au prochain passage")
                elif resume > slice_s:
                    complete = False  # l'attente déborderait la tranche : la tranche suivante reprend ici
                attempt += (" ; abandon, reprise au prochain passage" if "error" in info
                            else f" ; reprise dans {wait:.0f} s" if complete else " ; reprise à la tranche suivante")
                print(f"Digi-Key — {attempt}", file=sys.stderr, flush=True)  # journal du lancement (stdout : le JSON)
                if "error" in info or not complete:
                    break
                if track:
                    track.update(step="digikey", digikey={"done": done_before + i, "total": total,
                                                          "current": c["mpn"], "note": attempt[:200]})
                client.pause(wait)
                continue  # même référence : les précédentes ne sont pas réinterrogées
            except digikey.DigiKeyError as exc:
                info["error"] = str(exc)
                break
            i += 1
        run.update(seconds=run["seconds"] + time.monotonic() - start, calls=run["calls"] + client.calls,
                   incidents=failures)
        dk_progress = _dk_progress(info, done_before + len(results), total)
    with _locked():
        v = _components_veille(slug)
        data, notices, specs = comp.load_components(slug), comp.load_notices(slug), comp.load_specs(slug)
        new, specs_before = [], dict(specs)
        for c in data["components"]:
            key = comp.key_of(c["mpn"], c["manufacturer"])
            r = results.get(key)
            if r is None:
                continue
            prev = c.get("dk") or {}
            dk = {**prev, "checked_at": day, "error": r.get("error")}
            if "status" in r:
                info["checked"] += 1
                ntype = digikey.STATUS_TYPES.get(r["status"].lower())
                if r["status"] != prev.get("status") and (prev.get("status") or ntype):
                    dk["changed_at"] = day
                dk.update(status=r["status"], product_url=r["product_url"])
                if "category" in r or "params" in r:  # fiche (specs.json), hors de components.json
                    specs[key] = {k: r[k] for k in ("category", "params") if k in r}
                c.pop("web_status", None)  # Digi-Key fait foi, revérifié à chaque passage
                if ntype:
                    url = r["product_url"] or comp.normalize_url(
                        "https://www.digikey.com/en/products/result?keywords=" + quote_plus(c["mpn"]))
                    new += comp.merge(notices, [comp.status_notice(
                        c, ntype, f"Statut Digi-Key : {r['status']}", url, "Digi-Key", "digikey")], day)
            else:
                info["errors"] += 1
                if not prev.get("error"):  # Digi-Key ne la trouve plus : recherche web prioritaire, une fois
                    dk["changed_at"] = day
            c["dk"] = dk
        # Couverture du passage enregistrée telle quelle (D4) : une vérification ultérieure ne la change pas.
        run.update(at=time.time(), configured=info["configured"], checked=info["checked"], errors=info["errors"],
                   notices=run["notices"] + len(new), total=len(data["components"]),
                   verified=comp.checked_since(data["components"], run["date"]))
        lot = []
        if plan_batch:
            if complete:
                lot = comp.batch(data["components"], v.batch_size) if data["components"] else []
                run.update(complete=True, note=_dk_note(info))
            data["batch"] = [comp.key_of(c["mpn"], c["manufacturer"]) for c in lot]
            data["scan"] = run
        comp.save_components(slug, data)
        if new:
            comp.save_notices(slug, notices)
        keys = {comp.key_of(c["mpn"], c["manufacturer"]) for c in data["components"]}
        specs = {k: s for k, s in specs.items() if k in keys}  # références retirées : fiches oubliées
        if specs != specs_before:
            comp.save_specs(slug, specs)
        _index(slug, data["components"], notices)
        if info["configured"]:
            info.update(done=run["verified"], total=run["total"])
    if info["configured"] and complete and not (info.get("error") or info.get("stopped")):
        # Alternatives des composants en fin de vie, dans le temps restant de la tranche (la dernière, D1) ou du
        # budget : celles qui restent attendent le passage suivant ou une recherche à la demande.
        def alt_progress(c, i, n):
            track.update(throttle=True, step="digikey", digikey={**dk_progress, "current": f"{c['mpn']} (alternatives)"})
        info["alternatives"] = _alternatives(slug, client, comp.alternatives_due(data["components"], specs, notices, day),
                                             specs, day, start + (slice_s if plan_batch else left),
                                             alt_progress if track else None,
                                             max_calls=min(ALT_MAX_CALLS, digikey.MAX_CALLS - run["calls"]))
    note = _dk_note(info)
    alt = info.get("alternatives") or {}
    audit(actor, "components-scan", slug, f"Digi-Key {info['checked']} vérifié(s), {run['notices']} avis ; "
          + (f"lot web {len(lot)}" if complete else f"tranche : {info['done']}/{info['total']} vérifié(s)")
          + (f" ; alternatives : {alternatives_note(alt)}" if alt.get("searched") or alt.get("error") else "")
          + (f" ; Digi-Key : {note[:200]}" if note else ""))
    if not complete:
        if track:
            track.update(step="digikey", digikey=dk_progress)
        # Sortie différente à chaque tranche (avancement, budget) : Hermes ne la prend pas pour une boucle.
        return {"slug": slug, "date": day, "complete": False, "digikey": info,
                "next": (f"passage Digi-Key en cours : {info['done']}/{info['total']} références vérifiées, "
                         f"{run['seconds']:.0f} s sur {budget:.0f} s de budget. Relance exactement la même commande "
                         "pour continuer.")}
    if track:
        if plan_batch:
            track.update(step="web", digikey=dk_progress, batch=[
                {"mpn": c["mpn"], "manufacturer": c["manufacturer"], "state": "pending"} for c in lot])
        else:
            track.update(step="done", digikey=dk_progress)
    known: dict[str, list[str]] = {}
    for n in notices:
        known.setdefault(n["key"], []).append(n.get("number") or n["title"])
    return {"slug": slug, "date": day, "complete": True, "track_progress": track is not None and plan_batch,
            "digikey": info, "new_digikey_notices": run["notices"],
            "batch": [{"mpn": c["mpn"], "manufacturer": c["manufacturer"],
                       "since": comp.search_since(c), "digikey_status": (c.get("dk") or {}).get("status"),
                       # non nul : Digi-Key ne la trouve pas, l'agent établit lui-même son statut (« statuses »)
                       "digikey_error": (c.get("dk") or {}).get("error"), **_fallback(c),
                       "known": known.get(comp.key_of(c["mpn"], c["manufacturer"]), [])[:20]}
                      for c in lot]}


def _alternatives(slug: str, client: digikey.Client, targets: list[dict], specs: dict[str, dict], day: str,
                  deadline: float | None = None, progress=None, max_calls: int = ALT_MAX_CALLS) -> dict:
    """Recherche paramétrique Digi-Key des alternatives de `targets` (réseau, HORS verrou), puis enregistrement dans
    `dk.alternatives` de chacune, par clé, dans la liste relue SOUS verrou (éditions concurrentes préservées).

    Aucune recherche ne commence après `max_calls` appels Digi-Key (une en fait un à trois) ni après `deadline`
    (time.monotonic). Une requête refusée
    (`digikey.RequestRefused`, HTTP 400) est enregistrée comme résultat daté, affiché, et la série continue :
    une référence aux filtres refusés ne bloque pas les autres. Toute autre erreur (accès, quota, panne) arrête la
    série : ce qui reste attend la suivante. Renvoie {"searched", "found" (avec au moins une alternative),
    "pending"[, "error"]}."""
    results, info, calls = {}, {"searched": 0, "found": 0}, client.calls
    for i, c in enumerate(targets):
        if client.calls - calls >= max_calls or (deadline is not None and time.monotonic() > deadline):
            break
        if progress:
            progress(c, i, len(targets))
        key = comp.key_of(c["mpn"], c["manufacturer"])
        refused = ""
        try:
            products = client.similar(comp.leaf_category(specs.get(key)), *comp.search_filters(c, specs.get(key)))
        except digikey.RequestRefused as exc:
            products, refused = [], str(exc)[:300]
            info["error"] = refused
        except digikey.DigiKeyError as exc:
            info["error"] = str(exc)
            break
        rec = comp.alternatives_record(c, specs.get(key), products, day, refused)
        results[key] = rec
        info["searched"] += 1
        info["found"] += bool(rec["items"])
    info["pending"] = len(targets) - info["searched"]
    if results:
        with _locked():
            data = comp.load_components(slug)
            for c in data["components"]:
                rec = results.get(comp.key_of(c["mpn"], c["manufacturer"]))
                if rec is not None and c.get("dk"):
                    c["dk"]["alternatives"] = rec
            comp.save_components(slug, data)
            _index(slug, data["components"], comp.load_notices(slug))
    return info


def alternatives_note(info: dict) -> str:
    text = f"{info['searched']} recherchée(s), {info['found']} avec alternatives"
    if info.get("pending"):
        text += f", {info['pending']} en attente"
    return text + (f" ; {info['error']}" if info.get("error") else "")


def components_alternatives(slug: str, mpns: list[str] | None = None, actor: str = "cli",
                            client: digikey.Client | None = None, key: str | None = None, track: bool = False) -> dict:
    """Recherche d'alternatives à la demande (interface, MCP, CLI), synchrone : la référence `key`, ou les
    références `mpns` (tous leurs fabricants), même actives ou déjà recherchées ; sinon les composants en fin de
    vie qui l'attendent (`components.alternatives_due`). `track` : lancement détaché suivi (CLI), seul écrivain de
    son avancement. Renvoie le compte rendu de `_alternatives`, plus `targets` (MPN recherchés) et `skipped`
    ([{"mpn", "reason"}] : non suivie, sans fiche Digi-Key, famille non concernée, aucune critique)."""
    _components_veille(slug)
    pair = None if client else _require_digikey()
    day = comp.today()
    data, specs = comp.load_components(slug), comp.load_specs(slug)
    skipped = []
    if key:
        chosen = [c for c in data["components"] if comp.key_of(c["mpn"], c["manufacturer"]) == key]
    elif mpns:
        chosen, unknown = comp.select(data["components"], mpns)
        skipped += [{"mpn": m, "reason": "référence non suivie par cette veille"} for m in unknown]
    else:
        chosen = comp.alternatives_due(data["components"], specs, comp.load_notices(slug), day)
    targets = []
    for c in chosen:
        reason = comp.searchable(c, specs.get(comp.key_of(c["mpn"], c["manufacturer"])))
        if reason:
            skipped.append({"mpn": c["mpn"], "reason": reason})
        else:
            targets.append(c)
    track = _tracker(slug, "alternatives") if track else None

    def progress(c, i, n):
        track.update(throttle=True, step="alternatives", alternatives={"done": i, "total": n, "current": c["mpn"]})
    info = _alternatives(slug, client or digikey.Client(*pair), targets, specs, day,
                         progress=progress if track else None)
    if track:
        track.update(step="done", result={"found": info["found"]},
                     alternatives={"done": info["searched"], "total": len(targets),
                                   **({"note": info["error"][:200]} if info.get("error") else {})})
    audit(actor, "components-alternatives", slug, alternatives_note(info)[:300])
    return {**info, "targets": [c["mpn"] for c in targets], "skipped": skipped}


def set_critical_params(slug: str, key: str, wanted: list | None, actor: str = "cli") -> dict:
    """Caractéristiques critiques de la référence `key` (identifiants ou libellés de sa fiche Digi-Key, voir
    `components.critical_ids`) ; None : retour aux défauts de sa famille. Sous verrou. Les alternatives déjà
    trouvées restent affichées, signalées si elles l'ont été avec d'autres critères (`components.alternatives_stale`)
    jusqu'à la recherche suivante. Renvoie la fiche (`components.sheet`)."""
    with _locked():
        data = comp.load_components(_components_veille(slug).slug)
        c = next((c for c in data["components"] if comp.key_of(c["mpn"], c["manufacturer"]) == key), None)
        if c is None:
            raise ServiceError("composant introuvable dans cette veille")
        spec = comp.load_specs(slug).get(key)
        if wanted is None:
            c.pop("critical_params", None)
        else:
            c["critical_params"] = comp.critical_ids(c, spec, wanted)
        comp.save_components(slug, data)
    chosen = ", ".join(p["name"] for p in comp.criteria(c, spec))[:200] or "aucune"
    audit(actor, "components-critical", slug, f"{c['mpn']} : {chosen if wanted is not None else 'défauts de la famille'}")
    return comp.sheet(c, spec)


def _dk_note(info: dict) -> str:
    """Arrêt du passage Digi-Key (erreur, abandon, budget) et pannes surmontées par reprise : affichés, jamais
    pris pour un succès ni passés sous silence. Vide pour un passage sans incident."""
    parts = [info.get("error") or info.get("stopped")]
    if info.get("incidents") and not info.get("error"):  # l'erreur d'un abandon compte déjà les tentatives
        parts.append(f"{info['incidents']} incident(s) réseau, passage repris après attente")
    return " ; ".join(p for p in parts if p)


def _dk_progress(info: dict, done: int, total: int, **extra) -> dict:
    note = _dk_note(info)
    return {"done": done, "total": total, **extra, **({"note": note[:200]} if note else {})}


def _fallback(c: dict) -> dict:
    """Repli d'une référence inconnue de Digi-Key, pour l'agent : sa recherche alldatasheet (dont le « Direct
    Link » mène au fabricant), et ce qu'un passage précédent en a établi (statut web, site du fabricant)."""
    if not (c.get("dk") or {}).get("error"):
        return {}
    web = c.get("web_status") or {}
    return {"alldatasheet": comp.alldatasheet_url(c["mpn"]), "web_status": web.get("status"),
            "maker": web.get("maker")}


def components_progress(slug: str, mpns: list[str]) -> bool:
    """Signal de l'agent pendant la recherche web d'un lancement manuel suivi : ces MPN sont en cours de
    recherche, ceux du groupe précédent sont faits. Hors lancement suivi : rien, et jamais d'erreur
    (le job ne doit pas échouer pour un affichage). Renvoie True si l'avancement a été noté."""
    track = _tracker(slug, "analyse")
    if track is None:
        return False
    track.searching(mpns)
    return True


def _index(slug: str, components: list[dict], notices: list[dict]) -> None:
    """État enregistré d'une veille composants -> sa contribution à la base des composants."""
    _best_effort("index des composants", catalog.sync, slug, components, notices, slug=slug)


def _reprise_note(reprise: dict) -> str:
    return (f" ; {reprise['known']} référence(s) déjà connue(s), {reprise['notices']} avis repris"
            if reprise.get("known") else "")


def _adopt(slug: str, components: list[dict], notices: list[dict], keys: set[str]) -> dict:
    """Reprend de la base ce que les AUTRES veilles savent des références `keys`, nouvelles dans `slug` :
    statuts Digi-Key et web et date de la dernière recherche web (si la référence n'en a pas encore), avis
    (date de détection d'origine). Modifie `components` et `notices` en place ; renvoie {"known", "notices"}
    (références reconnues, avis repris). Les avis repris ne sont livrés sur aucun canal : ils partent dans le
    prochain rapport et le prochain e-mail de la veille."""
    known = (_best_effort("reprise depuis la base", catalog.knowledge, keys, slug, slug=slug) or {}) if keys else {}
    adopted = []
    for c in components:
        k = known.get(comp.key_of(c["mpn"], c["manufacturer"]))
        if not k:
            continue
        for field in ("dk", "web_status", "web_checked_at"):
            if k.get(field) and not c.get(field):
                c[field] = k[field]
        if (c.get("dk") or {}).get("status"):  # Digi-Key la connaît : il fait foi (comme au scan), pas de statut web
            c.pop("web_status", None)
        adopted += [{**n, "mpn": c["mpn"], "manufacturer": c["manufacturer"]} for n in k["notices"]]
    return {"known": len(known), "notices": len(comp.merge(notices, adopted))}


def _store_components(slug: str, data: dict, wanted: list[dict]) -> dict:
    """La liste de la veille devient `wanted` (appelé SOUS le verrou, `data` relu sous ce verrou) : état des
    références conservées gardé, reprise de la base pour les nouvelles, enregistrement et indexation si une
    entrée change (pas seulement le nombre). Renvoie la reprise, "changed" et "keys_changed" (références
    ajoutées, retirées ou réordonnées, pas seulement la graphie d'un MPN ou d'un fabricant).

    `notices.json` illisible : la liste s'enregistre quand même (comme avant la base), sans reprise ni index."""
    def entries(lst):
        return [(c["mpn"], c["manufacturer"]) for c in lst]
    before = data["components"]
    after = comp.replace_components(before, wanted)
    if entries(after) == entries(before):
        return {"changed": False}
    had = [comp.key_of(c["mpn"], c["manufacturer"]) for c in before]
    keys = [comp.key_of(c["mpn"], c["manufacturer"]) for c in after]
    try:
        notices = comp.load_notices(slug)
    except comp.ComponentsError:
        notices = None
    reprise = (_adopt(slug, after, notices, set(keys) - set(had)) if notices is not None
               else {"known": 0, "notices": 0})
    data["components"] = after
    comp.save_components(slug, data)
    if reprise["notices"]:
        comp.save_notices(slug, notices)
    if notices is not None:
        _index(slug, after, notices)
    return {**reprise, "changed": True, "keys_changed": keys != had}


def _edit_components(slug: str, edit, actor: str, action: str) -> tuple[int, dict]:
    """Modifie la liste SOUS le verrou (relecture comprise : aucune édition concurrente perdue) ;
    renvoie (différence de taille, reprise depuis la base)."""
    with _locked():
        data = comp.load_components(_components_veille(slug).slug)
        size = len(data["components"])
        reprise = _store_components(slug, data, edit(data["components"]))
        delta = abs(len(data["components"]) - size)
    audit(actor, action, slug, f"{delta} composant(s)" + _reprise_note(reprise))
    return delta, reprise


def import_components(slug: str, items: list[dict], mode: str = "add", actor: str = "cli") -> dict:
    """Import d'une liste (fichier analysé par bom_import) : ajout ou remplacement, sous verrou.
    Renvoie {"added", "existing", "removed", "total", "known", "notices_known"} : les deux derniers comptent
    les références déjà connues de la base des composants et les avis repris. L'historique des avis est
    conservé."""
    stats: dict = {}

    def edit(current):
        new, st = comp.combine(current, items, mode)
        stats.update(st)
        return new
    _, reprise = _edit_components(slug, edit, actor, f"components-import-{mode}")
    return {**stats, "total": len(comp.load_components(slug)["components"]),
            "known": reprise.get("known", 0), "notices_known": reprise.get("notices", 0)}


def remove_components(slug: str, mpns: list[str], actor: str = "cli") -> int:
    """Retire les références de ces MPN (tous fabricants) ; l'historique des avis est conservé."""
    drop = {comp.norm_mpn(m) for m in mpns}
    return _edit_components(slug, lambda current: [c for c in current if comp.norm_mpn(c["mpn"]) not in drop],
                            actor, "components-remove")[0]


def reset_components(slug: str, actor: str = "cli") -> Path:
    """Remet la recherche à zéro : avis effacés, statuts (Digi-Key, web), fiches, alternatives et dates de recherche
    oubliés ; la prochaine analyse revérifie tout et re-signale comme nouveaux les avis retrouvés. La liste des
    composants, leurs caractéristiques critiques, les rapports et les destinataires restent. Renvoie la copie préalable (archives : rien n'est perdu).

    Refusée pendant un lancement manuel (suivi). Une exécution planifiée n'est pas détectable : remise à zéro
    entre son scan et son finalize, les avis qu'elle trouve sont enregistrés et son lot sera recherché à nouveau.
    """
    with _locked():
        v = _components_veille(slug)
        if running_pid(slug):
            raise ServiceError("un lancement est en cours : attendre sa fin avant de réinitialiser")
        data = comp.load_components(slug)
        comp.load_notices(slug)  # illisible : erreur levée ici, rien n'est modifié
        paths.archive_dir().mkdir(parents=True, exist_ok=True)
        # Nom unique (deux remises à zéro dans la même seconde ne s'écrasent pas) : jamais une copie déjà vidée.
        dest = Path(tempfile.mkdtemp(prefix=f"{slug}-reinit-{dt.datetime.now():%Y%m%d-%H%M%S}-",
                                     dir=paths.archive_dir()))
        dest.chmod(0o755)  # comme les autres dossiers du volume (mkdtemp : 0700)
        for name in ("components.json", "notices.json", "specs.json"):
            if (v.dir / name).is_file():
                shutil.copy2(v.dir / name, dest / name)
        data = {"components": comp.reset_state(data["components"]), "batch": []}
        comp.save_components(slug, data)
        comp.save_notices(slug, [])
        comp.save_specs(slug, {})
        _index(slug, data["components"], [])  # ce qu'elle avait apporté à la base disparaît aussi
    audit(actor, "components-reset", slug, str(dest))
    return dest


def notices(slug: str, ntype: str | None = None, since: str | None = None) -> list[dict]:
    _components_veille(slug)
    if ntype and ntype not in comp.TYPES:
        raise StoreError(f"type d'avis inconnu : {ntype} ({', '.join(comp.TYPES)})")
    return comp.for_display(comp.load_notices(slug), ntype, since)


def sources_status() -> list[dict]:
    """État des sources de données (jamais les valeurs des clés)."""
    return [credentials.status(p) for p in credentials.PROVIDERS]


def save_credentials(provider: str, values: dict[str, str], actor: str = "ui") -> list[str]:
    with _locked():
        changed = credentials.save(provider, values)
    if changed:  # noms des champs seulement, jamais les valeurs
        audit(actor, "credentials-save", detail=f"{provider} : {', '.join(changed)}")
    return changed


def delete_credentials(provider: str, actor: str = "ui") -> bool:
    with _locked():
        removed = credentials.delete(provider)
    if removed:
        audit(actor, "credentials-delete", detail=provider)
    return removed


def check_credentials(provider: str, actor: str = "ui") -> str:
    """Teste la connexion à une source avec les clés configurées ; renvoie un message, ou lève."""
    credentials.status(provider)  # source connue
    pair = digikey.credentials()
    if not pair:
        raise ServiceError("identifiants Digi-Key incomplets : renseigner le Client ID et le Client Secret")
    try:
        digikey.Client(*pair).check()
    except digikey.DigiKeyError as exc:
        audit(actor, "credentials-test", detail=f"{provider} : échec")
        raise ServiceError(str(exc)) from None
    audit(actor, "credentials-test", detail=f"{provider} : ok")
    return "Connexion à Digi-Key réussie."


def rebuild_catalog(actor: str = "cli") -> dict:
    """Base contrôlée (mise de côté et recréée si corrompue) puis index des composants reconstruit depuis les
    fichiers de toutes les veilles, actives et archivées. Exécuté à chaque démarrage (hook de seed)."""
    with _locked():
        state = db.ensure()
        result = catalog.rebuild()
    if state != "base ok" or result["skipped"]:  # le démarrage ordinaire n'encombre pas le journal
        audit(actor, "catalog-rebuild", detail=f"{state} ; ignorées : {'; '.join(result['skipped']) or 'aucune'}"[:500])
    return {**result, "database": state}


def catalog_stats() -> dict | None:
    """Contenu de la base des composants (page Santé) ; None si elle est indisponible."""
    return _best_effort("base des composants", catalog.stats, trace=False)


def _catalog_state(key: str | None) -> tuple[dict, list[dict], list[dict]] | None:
    """(connaissance par clé, références, avis dédoublonnés) de la base des composants, toutes veilles
    confondues : pour chaque référence, ses statuts les plus récents ; pour chaque avis, sa première date de
    détection. None si la base est indisponible (lecture d'affichage : la page le signale)."""
    known = _best_effort("base des composants", catalog.browse, key, trace=False)
    if known is None:
        return None
    parts = [{"mpn": k["mpn"], "manufacturer": k["manufacturer"], "dk": k.get("dk"),
              "web_status": k.get("web_status"), "web_checked_at": k.get("web_checked_at")} for k in known.values()]
    found = sorted((n for k in known.values() for n in k["notices"]), key=lambda n: str(n.get("found_at") or ""))
    return known, parts, comp.merge([], found)


def catalog_rows(key: str | None = None) -> list[dict] | None:
    """Base des composants (page « Composants ») : une ligne par référence déjà analysée, comme la liste d'une
    veille (`components.group`), avec les veilles qui l'ont analysée (`watches` : slug d'une veille active ou
    dossier d'archive) ; `key` : cette seule référence. None si la base est indisponible."""
    state = _catalog_state(key)
    if state is None:
        return None
    known, parts, notices = state
    rows = comp.group(notices, parts, include_quiet=True)
    for r in rows:
        r["watches"] = sorted(known.get(r["key"], {}).get("sources", ()))
    return rows


def catalog_xlsx() -> bytes | None:
    """Base des composants au format Excel (classeur de l'export d'une veille, plus la colonne « Veilles », nommées
    comme dans l'interface) ; None si elle est indisponible."""
    state = _catalog_state(None)
    if state is None:
        return None
    known, parts, notices = state
    active = {v.slug for v in store.list_all()}
    return comp.to_xlsx(parts, notices, {key: [s if s in active else catalog.archived_label(s)
                                               for s in sorted(k["sources"])] for key, k in known.items()})


def address_book(limit: int | None = contacts.MAX_SHOWN) -> list[str]:
    """Adresses du carnet (`limit` None : toutes), proposées à la saisie des destinataires ; vide si la base
    est indisponible."""
    return _best_effort("carnet d'adresses", contacts.all_addresses, limit, trace=False) or []


def forget_contact(address: str, actor: str = "ui") -> bool:
    """Retire une adresse du carnet (elle reste destinataire des veilles qui l'utilisent)."""
    try:
        removed = contacts.forget(address)
    except db.ERRORS as exc:
        raise ServiceError(f"carnet d'adresses indisponible ({type(exc).__name__})") from None
    if removed:  # jamais l'adresse elle-même dans le journal
        audit(actor, "contacts-forget", detail="1 adresse")
    return removed


def alert_status() -> dict:
    """Adresses d'alerte du chien de garde : enregistrées, effectives et leur origine."""
    return settings.alert_status()


def save_alert_emails(text: str, actor: str = "ui") -> bool:
    """Adresses des alertes du chien de garde (interface seule : jamais le MCP)."""
    with _locked():
        saved = settings.save_alert_emails(text)
    if saved is not None:  # nombre seulement, jamais les adresses
        audit(actor, "settings-alert-emails", detail=f"{len(saved)} adresse(s)")
    return saved is not None


def send_test_alert(actor: str = "ui") -> str:
    """E-mail de test aux adresses d'alerte EFFECTIVES (celles qu'utilisera le chien de garde)."""
    to = settings.alert_status()["effective"]
    if not to:
        raise ServiceError("aucune adresse d'alerte : en renseigner une ci-dessus")
    try:
        out = _brevo(["--to", ",".join(to), "--from-name", "Surveillance veille",
                      "--subject", "[TEST] Surveillance des veilles — canal d'alerte",
                      "--text", ("Ceci est un test du canal d'alerte des veilles, envoyé depuis l'interface.\n\n"
                                 "Le chien de garde de l'hôte écrira à cette adresse en cas de panne.\n\n"
                                 "— Surveillance automatique des veilles.")],
                     timeout=60)
    except ServiceError:
        audit(actor, "test-alert", detail="échec")
        raise
    audit(actor, "test-alert", detail="ok")
    return out


def _mark(slug: str, ids: set[str], channel: str, day: str) -> None:
    with _locked():
        notices = comp.load_notices(slug)
        comp.mark_delivered(notices, ids, channel, day)
        comp.save_notices(slug, notices)


def components_xlsx(slug: str) -> bytes:
    """Liste complète des composants au format Excel (export à la demande)."""
    _components_veille(slug)
    return comp.to_xlsx(comp.load_components(slug)["components"], comp.load_notices(slug))


def _write_xlsx(rdir: Path, slug: str, data: dict, notices: list[dict], stamp: str, actor: str) -> Path | None:
    """Liste Excel de la veille composants `slug` à joindre à l'e-mail, écrite à côté du rapport (`rdir`) ;
    seul le dernier export de cette veille y est gardé (volume des sauvegardes borné).

    Pièce jointe de confort : un échec est tracé et renvoie None, l'e-mail part sans elle.
    """
    path = rdir / comp.xlsx_name(slug, stamp)
    try:
        store.atomic_write(path, comp.to_xlsx(data["components"], notices))
        old = comp.xlsx_re(slug)
        for p in rdir.iterdir():
            if p != path and old.fullmatch(p.name):
                p.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - l'e-mail (avis, PDF) passe avant la pièce jointe
        audit(actor, "components-xlsx", slug, f"échec : {type(exc).__name__}: {exc}"[:300])
        return None
    return path


def _build_pdf(v: Veille, md: Path, pdf: Path, subtitle: str, meta: str,
               data: Path | None = None, toc_depth: int = 1) -> None:
    script = os.environ.get(
        "BUILD_REPORT", str(paths.bundled_skills_dir() / "productivity/styled-pdf-report/scripts/build_report.py"))
    title = v.pdf.title or v.name
    cmd = [sys.executable, script, "--md", str(md), "--out", str(pdf), "--title", title,
           "--subtitle", subtitle, "--meta", meta, "--header", v.pdf.header or title,
           "--footer", v.pdf.footer, "--accent", v.pdf.accent, "--toc-depth", str(toc_depth),
           *(["--data", str(data)] if data else [])]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"PDF non généré : {exc}") from exc
    if p.returncode != 0 or not pdf.is_file():
        raise ServiceError("PDF non généré : " + ((p.stdout or "") + (p.stderr or "")).strip()[-500:])


def components_finalize(slug: str, candidates: Path, actor: str = "job") -> str:
    """Enregistre les avis trouvés par l'agent, produit le rapport PDF, envoie l'e-mail dédié.

    Ordre sûr : les avis sont enregistrés AVANT le PDF et l'e-mail ; un canal n'est marqué livré
    qu'après succès (un échec les ré-inclut à l'envoi suivant, rien n'est perdu).
    """
    v = _components_veille(slug)
    track = _tracker(slug, "analyse")
    if track:
        for b in track.state.get("batch", []):
            b["state"] = "done"  # recherche web terminée
        track.update(step="finalize", phase="notices")
    try:
        if candidates.stat().st_size > comp.MAX_CANDIDATES_BYTES:
            raise comp.ComponentsError("fichier des avis trop volumineux")
        raw = json.loads(candidates.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise comp.ComponentsError(f"fichier introuvable : {candidates}") from None
    except (OSError, ValueError) as exc:
        raise comp.ComponentsError(f"JSON invalide ({exc}) : corriger le fichier et relancer") from None
    stamp = store.report_stamp()
    day = stamp[:10]  # un seul instant : jour des livraisons et nom du rapport toujours d'accord
    with _locked():
        data, notices, specs = comp.load_components(slug), comp.load_notices(slug), comp.load_specs(slug)
        valid, rejected = comp.validate_candidates(raw, data["components"])
        statuses, bad = comp.validate_statuses(raw, data["components"])
        rejected += bad
        new = comp.merge(notices, valid + comp.apply_statuses(data["components"], statuses, day), day)
        lot = set(data["batch"])
        for c in data["components"]:
            if comp.key_of(c["mpn"], c["manufacturer"]) in lot:
                c["web_checked_at"] = day
        data["batch"] = []
        comp.save_components(slug, data)
        comp.save_notices(slug, notices)
        _index(slug, data["components"], notices)
        linked = [x.slug for x in _linked_veilles(slug)]
    audit(actor, "components-finalize", slug,
          f"{len(new)} nouvel(s) avis, {len(statuses)} statut(s) web, {len(rejected)} rejet(s)")
    critical = sum(1 for n in new if n["type"] in comp.CRITICAL)
    if track:  # cumulés : finalize relancé après un échec (PDF, e-mail) garde les avis du premier passage
        found = Counter(n["key"] for n in new)
        for b in track.state.get("batch", []):
            b["notices"] = b.get("notices", 0) + found[comp.key_of(b["mpn"], b["manufacturer"])]
        prev = track.state.get("result") or {}
        track.update(phase="pdf", result={"new": prev.get("new", 0) + len(new),
                                          "critical": prev.get("critical", 0) + critical,
                                          "rejected": len(rejected)})

    report = comp.pending(notices, "report", {day})
    view = comp.digest(data["components"], notices, report, data.get("scan"), specs)
    cov = view["coverage"]  # D4 : un passage Digi-Key partiel est dit partout (PDF, en-tête, e-mail, sortie)
    body = comp.to_markdown(view, heading="#")
    searched = [c["mpn"] for c in data["components"] if comp.key_of(c["mpn"], c["manufacturer"]) in lot]
    body += (f"\n*Recherche web de cette exécution ({len(searched)}) : "
             f"{', '.join(comp.md_escape(m) for m in searched) or 'aucune'}.*\n")
    if rejected:
        body += f"\n*{len(rejected)} entrée(s) de la recherche web rejetée(s) (source ou référence invalide).*\n"
    md, pdf = (store.veille_dir(slug) / "rapports" / f"{stamp}-veille.{ext}" for ext in ("md", "pdf"))
    store.atomic_write(md, body)
    meta = f"Composants suivis : {view['components']} | Recherche web du jour : {len(lot)}"
    if cov:
        meta += f" | Digi-Key : {cov['checked']}/{cov['total']}" + (" (PARTIEL)" if cov["partial"] else "")
    _build_pdf(v, md, pdf, subtitle=f"Avis PCN et fins de vie — {stamp[:10]} {stamp[11:]}", meta=meta)
    _mark(slug, {n["id"] for n in report}, "report", day)
    lines = [f"OK: veille composants — {len(new)} nouvel(s) avis "
             f"(dont {critical} fin(s) de vie / dernière(s) commande(s)), "
             f"{len(statuses)} statut(s) établi(s) sur le web, {len(rejected)} rejet(s), PDF {pdf}"]
    lines += [f"  rejet {r}" for r in rejected[:20]]
    if cov and cov["partial"]:
        lines.append(f"  {cov['text']}")
    mail = comp.pending(notices, "mail")
    if linked:
        # Livrés par le chapitre de la veille : le canal e-mail reste à jour en cas de détachement.
        _mark(slug, {n["id"] for n in mail}, "mail", f"via {linked[0]}")
        lines.append(f"e-mail : inclus dans la veille {', '.join(linked)} (chapitre composants)")
        if track:
            track.update(step="done")
        return "\n".join(lines)
    if track:
        track.update(phase="mail")
    view = comp.digest(data["components"], notices, mail, data.get("scan"), specs)
    xlsx = _write_xlsx(pdf.parent, slug, data, notices, stamp, actor)
    hint = "Liste complète des composants dans le fichier Excel joint." if xlsx else ""
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as fh:
        fh.write(comp.to_html_email(view, f"{v.name} — {day}",
                                    f"{hint} Détail et historique : interface web des veilles.".strip()))
    try:
        out = _brevo(["--bcc-file", str(v.dir / "recipients.txt"),
                      "--subject", f"{comp.subject_prefix(view)}{v.name} — {day}",
                      "--html-file", fh.name, "--text", f"{comp.to_text_email(view)}\n\n{hint}".rstrip(),
                      "--attach", str(pdf), *(["--attach", str(xlsx)] if xlsx else []), "--verify"])
    finally:
        os.unlink(fh.name)
    _mark(slug, {n["id"] for n in mail}, "mail", day)
    audit(actor, "components-mail", slug, f"{len(mail)} avis")
    if track:
        track.update(step="done")
    return "\n".join([*lines, out])


def components_chapter(slug: str, for_slug: str, report_md: Path,
                       actor: str = "job") -> tuple[str, Path | None] | None:
    """Ajoute le chapitre composants au rapport Markdown du jour d'une veille et écrit à côté la liste
    Excel à joindre à l'e-mail ; renvoie (préfixe d'objet « [EOL ×2] » ou vide, chemin de l'Excel ou
    None s'il n'a pu être produit), ou None si la veille composants n'existe plus.

    Idempotent : relancé sur le même fichier, le chapitre précédent (toujours en fin) est remplacé.
    """
    target = store.load(for_slug)
    path = _report_md(for_slug, report_md)
    rdir = path.parent
    try:
        _components_veille(slug)
    except StoreError:
        return None
    if target.components != slug:
        raise StoreError(f"la veille {for_slug} n'inclut pas la veille composants {slug}")
    # Jour de livraison = jour inscrit dans le NOM du rapport (et non l'heure de cette commande) :
    # une exécution commencée avant minuit reste rattachée à son rapport (détection des échecs).
    day, channel = path.name[:10], comp.chapter_channel(for_slug)
    with _locked():
        data, notices, specs = comp.load_components(slug), comp.load_notices(slug), comp.load_specs(slug)
        # Livrés un jour où la veille n'a finalement produit aucun PDF (exécution en échec après
        # l'ajout du chapitre) : pas réellement envoyés, donc réinclus.
        failed = {d for d in comp.delivery_days(notices, channel) if not any(rdir.glob(f"{d}*-veille.pdf"))}
        new = comp.pending(notices, channel, {day} | failed)
        view = comp.digest(data["components"], notices, new, data.get("scan"), specs)
        text = path.read_text(encoding="utf-8").split(CHAPTER_MARK)[0].rstrip()
        store.atomic_write(path, f"{text}\n\n{CHAPTER_MARK}\n\n{comp.to_markdown(view)}")
        for n in new:  # nouvelle date de livraison, y compris pour les réinclus
            n["delivered"].pop(channel, None)
        comp.mark_delivered(notices, {n["id"] for n in new}, channel, day)
        comp.save_notices(slug, notices)
    # Même horodatage que le rapport : l'Excel d'une relance remplace le précédent.
    xlsx = _write_xlsx(rdir, slug, data, notices, path.name.removesuffix("-veille.md"), actor)
    audit(actor, "components-chapter", slug, f"{for_slug} : {len(new)} avis")
    return comp.subject_prefix(view).strip(), xlsx


# --------------------------------------------------------------------------
# Publication d'une veille thématique : PDF et e-mail sans l'agent
# --------------------------------------------------------------------------
MAIL_MAX_CHARS = 50_000
MAIL_LIMITS = {"period": 100, "summary": 1500}
WORDS_PER_MINUTE = 200
PUBLISH_MIN_INTERVAL = 30 * 60  # s : ni renvoi par erreur, ni rafale d'un agent détourné
LAST_PUBLISH = ".dernier-envoi"  # hors de rapports/ : l'agent ne peut pas l'écrire


def _report_md(slug: str, report_md: Path) -> Path:
    """Rapport Markdown d'une veille : dans SON dossier rapports/, nom horodaté, fichier existant."""
    rdir = store.veille_dir(slug) / "rapports"
    path = report_md.resolve()
    if path.parent != rdir.resolve() or not store.REPORT_RE.match(path.name) \
            or not path.name.endswith(".md") or not path.is_file():
        raise StoreError(f"rapport Markdown invalide pour la veille {slug} : {report_md} "
                         f"(attendu : {rdir}/AAAA-MM-JJ-HHhMM-veille.md, fichier existant)")
    return path


def _mail_text(value, limit: int, one_line: bool) -> str:
    """Texte de l'agent pour l'objet ou le corps de l'e-mail : sans caractère de contrôle ni
    invisible, espaces normalisés, borné."""
    if not isinstance(value, str):
        return ""
    lines = [" ".join("".join(ch for ch in ln if ch.isprintable()).split()) for ln in value.splitlines()]
    text = " ".join(lines) if one_line else "\n".join(ln for ln in lines if ln)
    return text[:limit].strip()


def _mail_fields(path: Path) -> tuple[str, str]:
    """Période couverte et messages clés écrits par l'agent dans <D>-mail.json."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.loads(fh.read(MAIL_MAX_CHARS))  # lecture bornée : au-delà, JSON invalide
    except FileNotFoundError:
        raise StoreError(f"fichier introuvable : {path} (période et messages clés de l'e-mail)") from None
    except (OSError, ValueError) as exc:
        raise StoreError(f"JSON invalide ({exc}) : corriger {path.name} et relancer") from None
    raw = raw if isinstance(raw, dict) else {}
    period = _mail_text(raw.get("period"), MAIL_LIMITS["period"], one_line=True)
    if not period:
        raise StoreError(f"{path.name} : « period » manquante (ex. « Semaine 42 · 12–16 octobre 2026 »)")
    return period, _mail_text(raw.get("summary"), MAIL_LIMITS["summary"], one_line=False)


def report_publish(slug: str, report_md: Path, actor: str = "job") -> str:
    """Dernières étapes d'une veille thématique : chapitre composants, PDF, envoi aux destinataires.

    L'agent, exposé au contenu du web, n'écrit que des fichiers de son dossier rapports/ (rapport,
    graphique, période et messages clés) : destinataires, pièces jointes, mise en page et objet
    viennent de la veille, jamais de lui.
    """
    v = store.load(slug)
    if v.kind != "veille":
        raise StoreError(f"{slug} est une veille composants : elle se publie par `veillectl components finalize`")
    md = _report_md(slug, report_md)
    stem = md.name.removesuffix("-veille.md")
    period, summary = _mail_fields(md.with_name(f"{stem}-mail.json"))
    last = v.dir / LAST_PUBLISH
    try:
        age = time.time() - last.stat().st_mtime
    except FileNotFoundError:
        age = None
    if age is not None and age < PUBLISH_MIN_INTERVAL:
        raise StoreError(f"rapport déjà envoyé il y a {int(age // 60)} min ({last.read_text().strip()}) : "
                         f"pas d'autre envoi avant {PUBLISH_MIN_INTERVAL // 60} min. Ne relance pas la commande.")
    prefix, xlsx = (components_chapter(v.components, slug, md, actor) or ("", None)) if v.components else ("", None)
    pdf, charts = md.with_name(f"{stem}-veille.pdf"), md.with_name(f"{stem}-charts.json")
    minutes = max(1, round(len(md.read_text(encoding="utf-8").split()) / WORDS_PER_MINUTE))
    _build_pdf(v, md, pdf, subtitle=period,
               meta=f"Période : {period} | Temps de lecture : ~{minutes} min | Public : {v.pdf.audience}",
               data=charts if charts.is_file() else None, toc_depth=2)
    text = summary or f"Rapport de la veille « {v.name} » en pièce jointe."
    if prefix:
        text += f"\n\nComposants suivis {prefix} : détail dans le chapitre « Composants » du rapport."
    subject = f"{prefix} {v.name} — {period}".strip()
    # Marqueur posé AVANT l'envoi (un envoi interrompu après acceptation ne se double pas) ; retiré si
    # Brevo n'a rien accepté. Sans --verify : le statut de remise rallongeait l'appel au-delà du délai.
    store.atomic_write(last, f"{md.name}\n")
    try:
        # « --option=valeur » : un texte de l'agent qui commence par « - » reste une valeur.
        out = _brevo(["--bcc-file", str(v.dir / "recipients.txt"), f"--subject={subject}", f"--text={text}",
                      "--attach", str(pdf), *(["--attach", str(xlsx)] if xlsx else [])])
    except ServiceError as exc:
        if "OK: email accepted" not in str(exc):
            last.unlink(missing_ok=True)
        raise
    audit(actor, "report-publish", slug, pdf.name + (f" + {xlsx.name}" if xlsx else ""))
    return f"OK: PDF généré -> {pdf}\n{out}"


# --------------------------------------------------------------------------
# Amorçage, migration, réconciliation (exécutés au boot par le seed)
# --------------------------------------------------------------------------
def migrate_legacy(actor: str = "seed") -> str | None:
    """Ancienne disposition `/opt/data/veille/` -> `/opt/data/veilles/industrie-4-0/`.

    Conserve l'historique des rapports (anti-répétition) ET le job existant (édité,
    pas recréé : son historique d'exécution est préservé). Annule le déplacement si
    l'édition du job échoue.
    """
    legacy = paths.legacy_dir()
    target = paths.veilles_dir() / LEGACY_SLUG
    if legacy.is_symlink() or not (legacy / "config.md").is_file() or target.exists():
        return None
    with _locked():
        job = hermes.find_by_name(LEGACY_JOB_NAME, hermes.load_jobs())  # illisible : réessayé au boot suivant
        expr = schedule.job_expr(job) if job else None
        v = Veille(
            slug=LEGACY_SLUG, name="Veille Industrie 4.0",
            subject="Industrie 4.0, robotique, automatisation avancée, IA industrielle",
            job_id=job["id"] if job else None,
            pdf=PdfOptions(title="Veille technologique — Industrie 4.0, robotique & IA industrielle",
                           header="Veille technologique · Industrie 4.0",
                           footer="Kindara — document interne",
                           audience="direction industrielle & ingénierie"),
        )
        _apply_schedule(v, expr or "30 6 * * 1")

        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(legacy, target)
        moved = []
        if (target / "destinataire.txt").exists():
            os.rename(target / "destinataire.txt", target / "recipients.txt")
            moved.append(("destinataire.txt", "recipients.txt"))
        if (target / "job-prompt.md").exists():
            os.rename(target / "job-prompt.md", target / "job-prompt.legacy.md")
            moved.append(("job-prompt.md", "job-prompt.legacy.md"))
        try:
            store.save(v)
            render.write(v)
            if job:
                hermes.edit(job["id"], name=job_name(v), prompt=render.short_prompt(v),
                            workdir=str(v.dir))
            else:
                v.job_id = _create_job(v)
                store.save(v)
        except Exception:
            for old, new in moved:
                os.rename(target / new, target / old)
            for f in ("veille.json", "job-prompt.md"):
                with contextlib.suppress(FileNotFoundError):
                    (target / f).unlink()
            os.rename(target, legacy)
            raise
        with contextlib.suppress(FileNotFoundError):
            (target / ".seeded").unlink()
        # Lien relatif : l'ancien chemin reste lisible depuis l'hôte ET le conteneur.
        with contextlib.suppress(OSError):
            legacy.symlink_to(Path("veilles") / LEGACY_SLUG)
    audit(actor, "migrate", LEGACY_SLUG, f"job {v.job_id}")
    return LEGACY_SLUG


def bootstrap_default(actor: str = "seed") -> str | None:
    """Premier démarrage d'une installation neuve : crée la veille Industrie 4.0.

    Une seule fois (marqueur) : archiver toutes les veilles ne la fait pas revenir.
    Sans destinataire configuré (VEILLE_RECIPIENTS), elle est créée EN PAUSE.
    """
    marker = paths.veilles_dir() / BOOTSTRAP_MARKER
    if marker.exists():
        return None
    created = None
    if not store.list_all():
        templates = store.brief_templates()
        brief = templates.get(LEGACY_SLUG) or templates.get("vierge")
        recipients = store.parse_recipients(os.environ.get("VEILLE_RECIPIENTS", ""))
        if brief:
            create_veille(
                name="Veille Industrie 4.0", slug=LEGACY_SLUG,
                subject="Industrie 4.0, robotique, automatisation avancée, IA industrielle",
                # 6h30 Paris : heures creuses DeepSeek (04h30 UTC l'été, 05h30 l'hiver).
                schedule_expr="30 6 * * 1", recipients=recipients or [PLACEHOLDER_RECIPIENT],
                brief=brief, paused=not recipients, actor=actor,
                pdf={"title": "Veille technologique — Industrie 4.0, robotique & IA industrielle",
                     "header": "Veille technologique · Industrie 4.0",
                     "audience": "direction industrielle & ingénierie"},
            )
            created = LEGACY_SLUG
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return created


def reconcile_all(actor: str = "seed") -> list[str]:
    """Réaligne chaque veille avec son job (boot, ou après une édition manuelle)."""
    report = []
    with _locked():
        try:
            jobs = hermes.load_jobs()
        except hermes.HermesError as exc:
            # Illisible n'est pas vide : recréer les jobs détacherait l'historique d'exécution
            # et les coûts (indexés par job) et doublerait les envois. On ne touche à rien.
            return [f"ERREUR {exc} — réconciliation annulée, aucune veille modifiée"]
        for v in store.list_all():
            try:
                report.append(_reconcile(v, jobs))
            except Exception as exc:  # noqa: BLE001 - une veille cassée ne bloque pas les autres
                report.append(f"{v.slug}: ERREUR {exc}")
    for line in report:
        if "→" in line:
            audit(actor, "reconcile", line.split(":", 1)[0], line)
    return report


def _reconcile(v: Veille, jobs: list[dict]) -> str:
    job = hermes.get_job(v.job_id, jobs) or hermes.find_by_name(job_name(v), jobs)
    actions = []
    if job is None:
        render.write(v)
        v.job_id = _create_job(v)
        store.save(v)
        return f"{v.slug}: → job recréé ({v.job_id})"
    if job["id"] != v.job_id:
        v.job_id = job["id"]
        actions.append("lien job rétabli")
    # Le job fait foi pour la fréquence (édition éventuelle via `hermes cron edit`).
    expr = schedule.job_expr(job)
    if expr and expr != v.schedule:
        try:
            _apply_schedule(v, expr)
            actions.append(f"fréquence adoptée ({expr})")
        except schedule.ScheduleError:
            pass
    if actions:
        store.save(v)
    if render.write(v):
        actions.append("prompt re-rendu")
    # Avant la 0.6.0 : brevo-email et styled-pdf-report attachés (« skill » : ancien champ de Hermes).
    stale_skills = bool(job.get("skills") or job.get("skill"))
    if (job.get("prompt") != render.short_prompt(v) or job.get("workdir") != str(v.dir)
            or job.get("name") != job_name(v) or stale_skills):
        hermes.edit(v.job_id, name=job_name(v), prompt=render.short_prompt(v), workdir=str(v.dir),
                    clear_skills=stale_skills)
        actions.append("job réaligné")
    return f"{v.slug}: " + ("→ " + ", ".join(actions) if actions else "ok")
