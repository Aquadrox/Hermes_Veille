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
from pathlib import Path

from . import hermes, paths, pricing, render, schedule, store
from .store import PdfOptions, StoreError, Veille

LEGACY_JOB_NAME = "Veille technologique hebdomadaire"
LEGACY_SLUG = "industrie-4-0"
BOOTSTRAP_MARKER = ".bootstrapped"
PLACEHOLDER_RECIPIENT = "destinataire@example.com"


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


def audit(actor: str, action: str, slug: str = "", detail: str = "") -> None:
    line = json.dumps({"at": store.now_iso(), "actor": actor, "action": action,
                       "slug": slug, "detail": detail}, ensure_ascii=False)
    try:
        paths.audit_log().parent.mkdir(parents=True, exist_ok=True)
        with open(paths.audit_log(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


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
    return {
        "veille": v,
        "status": status,
        "schedule_human": schedule.describe(v.schedule),
        "recipients": store.read_recipients(v.slug),
        "last_report": reports[0] if reports else None,
        "running": running_pid(v.slug) is not None,
    }


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
                  paused: bool = False, actor: str = "cli") -> Veille:
    name = (name or "").strip()
    if not name:
        raise StoreError("le nom est obligatoire")
    slug = store.check_slug(slug or store.slugify(name))
    if not recipients:
        raise StoreError("au moins un destinataire est requis")
    with _locked():
        if store.veille_dir(slug).exists():
            raise StoreError(f"une veille « {slug} » existe déjà")
        v = Veille(slug=slug, name=name, subject=(subject or "").strip(),
                   model=model or None, provider=provider or None, pdf=_pdf(pdf))
        _apply_schedule(v, schedule_expr)
        try:
            store.save(v)
            store.write_recipients(slug, recipients)
            store.write_brief(slug, brief)
            render.write(v)
            v.job_id = _create_job(v, paused=paused)
            store.save(v)
        except Exception:
            shutil.rmtree(v.dir, ignore_errors=True)
            raise
    audit(actor, "create", slug, f"{schedule_expr}; {len(recipients)} destinataire(s)")
    return v


def update_veille(slug: str, *, name: str | None = None, subject: str | None = None,
                  schedule_expr: str | None = None, recipients: list[str] | None = None,
                  brief: str | None = None, pdf: dict | None = None,
                  model: str | None = None, provider: str | None = None,
                  actor: str = "cli") -> Veille:
    with _locked():
        v = store.load(slug)
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
            if recipients != store.read_recipients(slug):
                changes.append("destinataires")
            store.write_recipients(slug, recipients)
        if brief is not None:
            if brief.replace("\r\n", "\n") != store.read_brief(slug):
                changes.append("brief")
            store.write_brief(slug, brief)
        store.save(v)
        render.write(v)
        job = hermes.get_job(v.job_id, _safe_jobs())
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


def run_now(slug: str, actor: str = "cli") -> str:
    """Lance la veille en arrière-plan ; renvoie le nom du journal."""
    v = store.load(slug)
    job_id = _check_runnable(v)
    log_name = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".log"
    pid = hermes.run_detached(job_id, store.runs_dir(slug) / log_name)
    _pid_file(slug).write_text(str(pid))
    audit(actor, "run", slug, log_name)
    return log_name


def run_wait(slug: str, actor: str = "cli") -> str:
    """Lance la veille et attend la fin (cible `make run-now`)."""
    v = store.load(slug)
    job_id = _check_runnable(v)
    audit(actor, "run", slug, "synchrone")
    return hermes.run_sync(job_id)


def archive(slug: str, actor: str = "cli") -> Path:
    with _locked():
        v = store.load(slug)
        if v.job_id and hermes.get_job(v.job_id, _safe_jobs()) is not None:
            hermes.remove(v.job_id)
        dest = paths.archive_dir() / f"{slug}-{dt.datetime.now():%Y%m%d-%H%M%S}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(v.dir), str(dest))
    audit(actor, "archive", slug, str(dest))
    return dest


def brevo_script() -> str:
    return os.environ.get(
        "BREVO_SCRIPT", str(paths.hermes_home() / "skills/email/brevo-email/scripts/brevo_send.py"))


def send_test_email(slug: str, actor: str = "cli") -> str:
    v = store.load(slug)
    recipients_file = v.dir / "recipients.txt"
    cmd = [sys.executable, brevo_script(), "--bcc-file", str(recipients_file),
           "--subject", f"[TEST] {v.name} — canal d'envoi",
           "--text", (f"Ceci est un e-mail de test de la veille « {v.name} ».\n\n"
                      f"Vous le recevez car vous figurez parmi ses destinataires "
                      f"({schedule.describe(v.schedule)}).\n\n— Veille automatique."),
           "--env-file", str(paths.hermes_home() / ".env")]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"envoi impossible : {exc}") from exc
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    audit(actor, "test-email", slug, "ok" if p.returncode == 0 else "échec")
    if p.returncode != 0:
        raise ServiceError(out or "échec de l'envoi")
    return out


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
        job = hermes.find_by_name(LEGACY_JOB_NAME, _safe_jobs())
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
        jobs = _safe_jobs()
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
    if (job.get("prompt") != render.short_prompt(v) or job.get("workdir") != str(v.dir)
            or job.get("name") != job_name(v)):
        hermes.edit(v.job_id, name=job_name(v), prompt=render.short_prompt(v), workdir=str(v.dir))
        actions.append("job réaligné")
    return f"{v.slug}: " + ("→ " + ", ".join(actions) if actions else "ok")
