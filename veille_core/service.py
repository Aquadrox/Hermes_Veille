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
from pathlib import Path
from urllib.parse import quote_plus

from . import components as comp
from . import credentials, digikey, hermes, paths, pricing, render, schedule, settings, store
from .store import PdfOptions, StoreError, Veille

LEGACY_JOB_NAME = "Veille technologique hebdomadaire"
LEGACY_SLUG = "industrie-4-0"
BOOTSTRAP_MARKER = ".bootstrapped"
PLACEHOLDER_RECIPIENT = "destinataire@example.com"
COMPONENTS_TEMPLATE = "composants"
COMPONENTS_SCHEDULE = "0 23 * * 0"  # dimanche 23h : heures creuses DeepSeek toute l'année
CHAPTER_MARK = "<!-- chapitre-composants -->"


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
        "report_count": len(reports),
        "running": running_pid(v.slug) is not None,
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
        try:
            store.save(v)
            store.write_recipients(slug, recipients)
            store.write_brief(slug, brief)
            if kind == "composants":
                comp.save_components(slug, {"components": comp.replace_components([], components or []),
                                            "batch": []})
            _set_link(v, include_components)
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
            if recipients != store.read_recipients(slug):
                changes.append("destinataires")
            store.write_recipients(slug, recipients)
        if brief is not None:
            if brief.replace("\r\n", "\n") != store.read_brief(slug):
                changes.append("brief")
            store.write_brief(slug, brief)
        if components is not None:
            if v.kind != "composants":
                raise StoreError("seule une veille composants a une liste de composants")
            data = comp.load_components(slug)
            new_list = comp.replace_components(data["components"], components)
            if [comp.key_of(c["mpn"], c["manufacturer"]) for c in new_list] != [
                    comp.key_of(c["mpn"], c["manufacturer"]) for c in data["components"]]:
                changes.append("composants")
            data["components"] = new_list
            comp.save_components(slug, data)
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


def _start(slug: str, launch, actor: str, action: str) -> str:
    """Démarre un traitement en arrière-plan (journal dans runs/, PID suivi) ; renvoie le journal."""
    log_name = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".log"
    store.runs_dir(slug).mkdir(parents=True, exist_ok=True)
    pid = launch(store.runs_dir(slug) / log_name)
    _pid_file(slug).write_text(str(pid))
    audit(actor, action, slug, log_name)
    return log_name


def run_now(slug: str, actor: str = "cli") -> str:
    """Lance la veille en arrière-plan ; renvoie le nom du journal."""
    job_id = _check_runnable(store.load(slug))
    return _start(slug, lambda log: hermes.run_detached(job_id, log), actor, "run")


def check_statuses_now(slug: str, actor: str = "cli") -> str:
    """Vérifie tout de suite le statut Digi-Key des composants, en arrière-plan.

    Déterministe : ni LLM (aucun coût en tokens), ni e-mail, ni recherche web ; le lot de la
    prochaine exécution planifiée n'est pas modifié. Renvoie le nom du journal.
    """
    _components_veille(slug)
    if not digikey.credentials():
        raise ServiceError("clés Digi-Key absentes : les saisir dans l'interface, page Sources")
    if running_pid(slug):
        raise ServiceError("un lancement est déjà en cours")
    app_dir = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (app_dir, os.environ.get("PYTHONPATH"))))}
    cmd = [sys.executable, "-m", "veille_core", "components", "scan", slug, "--status-only"]
    return _start(slug, lambda log: hermes.spawn_detached(cmd, log, env), actor, "components-check")


def run_wait(slug: str, actor: str = "cli") -> str:
    """Lance la veille et attend la fin (cible `make run-now`)."""
    v = store.load(slug)
    job_id = _check_runnable(v)
    audit(actor, "run", slug, "synchrone")
    return hermes.run_sync(job_id)


def archive(slug: str, actor: str = "cli") -> Path:
    with _locked():
        v = store.load(slug)
        # load_jobs (et non _safe_jobs) : un job non supprimé continuerait d'envoyer des e-mails.
        if v.job_id and hermes.get_job(v.job_id, hermes.load_jobs()) is not None:
            hermes.remove(v.job_id)
        dest = paths.archive_dir() / f"{slug}-{dt.datetime.now():%Y%m%d-%H%M%S}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(v.dir), str(dest))
        for other in _linked_veilles(slug):  # plus de chapitre vers une veille archivée
            other.components = None
            store.save(other)
            render.write(other)
    audit(actor, "archive", slug, str(dest))
    return dest


def brevo_script() -> str:
    return os.environ.get(
        "BREVO_SCRIPT", str(paths.hermes_home() / "skills/email/brevo-email/scripts/brevo_send.py"))


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
                    budget_s: float = 900, plan_batch: bool = True) -> dict:
    """Statut Digi-Key de tous les composants, puis lot de recherche web du jour (enregistré).

    `plan_batch=False` (vérification à la demande) : le lot web en cours n'est pas touché, pour ne
    pas fausser une exécution planifiée qui serait entre ses étapes « scan » et « finalize ».

    Les appels réseau (plusieurs minutes pour 300 composants) se font HORS verrou ; les résultats
    sont ensuite appliqués par clé à la liste relue sous verrou (éditions concurrentes préservées).
    """
    _components_veille(slug)
    day = comp.today()
    creds = digikey.credentials()
    info: dict = {"configured": bool(creds or client), "checked": 0, "errors": 0}
    results: dict[str, dict] = {}
    if creds or client:
        client = client or digikey.Client(*creds)
        start = time.monotonic()
        todo = sorted(comp.load_components(slug)["components"],
                      key=lambda c: (c.get("dk") or {}).get("checked_at") or "")
        for c in todo:
            if ((c.get("dk") or {}).get("checked_at") or "") == day:
                continue  # déjà vérifié aujourd'hui (relance du même jour)
            if client.calls >= digikey.MAX_CALLS or time.monotonic() - start > budget_s:
                info["stopped"] = "budget de temps ou d'appels atteint : suite au prochain passage"
                break
            try:
                results[comp.key_of(c["mpn"], c["manufacturer"])] = client.status(c["mpn"], c["manufacturer"])
            except digikey.DigiKeyError as exc:
                info["error"] = str(exc)
                break
    with _locked():
        v = _components_veille(slug)
        data, notices = comp.load_components(slug), comp.load_notices(slug)
        new = []
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
                if ntype:
                    url = r["product_url"] or comp.normalize_url(
                        "https://www.digikey.com/en/products/result?keywords=" + quote_plus(c["mpn"]))
                    new += comp.merge(notices, [{
                        "key": key, "mpn": c["mpn"], "manufacturer": c["manufacturer"], "type": ntype,
                        "title": f"Statut Digi-Key : {r['status']}", "number": "", "published": "",
                        "ltb_date": "", "eol_date": "", "scope": "référence", "summary": "",
                        "sources": [{"url": url, "label": "Digi-Key"}], "origin": "digikey"}], day)
            else:
                info["errors"] += 1
            c["dk"] = dk
        lot = comp.batch(data["components"], v.batch_size) if data["components"] and plan_batch else []
        if plan_batch:
            data["batch"] = [comp.key_of(c["mpn"], c["manufacturer"]) for c in lot]
        comp.save_components(slug, data)
        if new:
            comp.save_notices(slug, notices)
    known: dict[str, list[str]] = {}
    for n in notices:
        known.setdefault(n["key"], []).append(n.get("number") or n["title"])
    audit(actor, "components-scan", slug,
          f"Digi-Key {info['checked']} vérifié(s), {len(new)} avis ; lot web {len(lot)}")
    return {"slug": slug, "date": day, "digikey": info, "new_digikey_notices": len(new),
            "batch": [{"mpn": c["mpn"], "manufacturer": c["manufacturer"], "ref": c.get("ref", ""),
                       "since": comp.search_since(c), "digikey_status": (c.get("dk") or {}).get("status"),
                       "known": known.get(comp.key_of(c["mpn"], c["manufacturer"]), [])[:20]}
                      for c in lot]}


def _edit_components(slug: str, edit, actor: str, action: str) -> int:
    """Modifie la liste SOUS le verrou (relecture comprise : aucune édition concurrente perdue) ;
    renvoie la différence de taille. Enregistrée dès qu'une entrée change (pas seulement le nombre)."""
    def entries(lst):
        return [(c["mpn"], c["manufacturer"], c.get("ref", "")) for c in lst]
    with _locked():
        data = comp.load_components(_components_veille(slug).slug)
        before = data["components"]
        data["components"] = comp.replace_components(before, edit(before))
        delta = abs(len(data["components"]) - len(before))
        if entries(data["components"]) != entries(before):
            comp.save_components(slug, data)
    audit(actor, action, slug, f"{delta} composant(s)")
    return delta


def import_components(slug: str, items: list[dict], mode: str = "add", actor: str = "cli") -> dict:
    """Import d'une liste (fichier analysé par bom_import) : ajout ou remplacement, sous verrou.
    Renvoie {"added", "existing", "removed", "total"} ; l'historique des avis est conservé."""
    stats: dict = {}

    def edit(current):
        new, st = comp.combine(current, items, mode)
        stats.update(st)
        return new
    _edit_components(slug, edit, actor, f"components-import-{mode}")
    return {**stats, "total": len(comp.load_components(slug)["components"])}


def add_components(slug: str, items: list[dict], actor: str = "cli") -> int:
    """Ajoute des références (doublons ignorés) ; renvoie le nombre réellement ajouté."""
    return import_components(slug, items, "add", actor)["added"]


def remove_components(slug: str, mpns: list[str], actor: str = "cli") -> int:
    """Retire les références de ces MPN (tous fabricants) ; l'historique des avis est conservé."""
    drop = {comp.norm_mpn(m) for m in mpns}
    return _edit_components(slug, lambda current: [c for c in current if comp.norm_mpn(c["mpn"]) not in drop],
                            actor, "components-remove")


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
    return comp.to_xlsx(comp.group(comp.load_notices(slug), comp.load_components(slug)["components"],
                                   include_quiet=True))


def _write_xlsx(rdir: Path, slug: str, data: dict, notices: list[dict], stamp: str) -> Path:
    """Instantané Excel de la veille composants `slug`, joint à l'e-mail : écrit à côté du rapport
    (`rdir`), hors verrou (données déjà lues)."""
    path = rdir / comp.xlsx_name(slug, stamp)
    store.atomic_write(path, comp.to_xlsx(comp.group(notices, data["components"], include_quiet=True)))
    return path


def _build_pdf(v: Veille, md: Path, pdf: Path, subtitle: str, meta: str) -> None:
    script = os.environ.get(
        "BUILD_REPORT", str(paths.hermes_home() / "skills/productivity/styled-pdf-report/scripts/build_report.py"))
    title = v.pdf.title or v.name
    cmd = [sys.executable, script, "--md", str(md), "--out", str(pdf), "--title", title,
           "--subtitle", subtitle, "--meta", meta, "--header", v.pdf.header or title,
           "--footer", v.pdf.footer, "--accent", v.pdf.accent, "--toc-depth", "1"]
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
        data, notices = comp.load_components(slug), comp.load_notices(slug)
        valid, rejected = comp.validate_candidates(raw, data["components"])
        new = comp.merge(notices, valid, day)
        lot = set(data["batch"])
        for c in data["components"]:
            if comp.key_of(c["mpn"], c["manufacturer"]) in lot:
                c["web_checked_at"] = day
        data["batch"] = []
        comp.save_components(slug, data)
        comp.save_notices(slug, notices)
        linked = [x.slug for x in _linked_veilles(slug)]
    audit(actor, "components-finalize", slug, f"{len(new)} nouvel(s) avis, {len(rejected)} rejet(s)")

    report = comp.pending(notices, "report", {day})
    view = comp.digest(data["components"], notices, report)
    body = comp.to_markdown(view, heading="#")
    searched = [c["mpn"] for c in data["components"] if comp.key_of(c["mpn"], c["manufacturer"]) in lot]
    body += (f"\n*Recherche web de cette exécution ({len(searched)}) : "
             f"{', '.join(comp.md_escape(m) for m in searched) or 'aucune'}.*\n")
    if rejected:
        body += f"\n*{len(rejected)} entrée(s) de la recherche web rejetée(s) (source ou référence invalide).*\n"
    md, pdf = (store.veille_dir(slug) / "rapports" / f"{stamp}-veille.{ext}" for ext in ("md", "pdf"))
    store.atomic_write(md, body)
    _build_pdf(v, md, pdf, subtitle=f"Avis PCN et fins de vie — {stamp[:10]} {stamp[11:]}",
               meta=f"Composants suivis : {view['components']} | Recherche web du jour : {len(lot)}")
    _mark(slug, {n["id"] for n in report}, "report", day)
    lines = [f"OK: veille composants — {len(new)} nouvel(s) avis "
             f"(dont {sum(1 for n in new if n['type'] in comp.CRITICAL)} fin(s) de vie / dernière(s) commande(s)), "
             f"{len(rejected)} rejet(s), PDF {pdf}"]
    lines += [f"  rejet {r}" for r in rejected[:20]]
    mail = comp.pending(notices, "mail")
    if linked:
        # Livrés par le chapitre de la veille : le canal e-mail reste à jour en cas de détachement.
        _mark(slug, {n["id"] for n in mail}, "mail", f"via {linked[0]}")
        lines.append(f"e-mail : inclus dans la veille {', '.join(linked)} (chapitre composants)")
        return "\n".join(lines)
    view = comp.digest(data["components"], notices, mail)
    xlsx = _write_xlsx(pdf.parent, slug, data, notices, stamp)
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as fh:
        fh.write(comp.to_html_email(view, f"{v.name} — {day}", "Détail et historique : interface web des veilles."))
    try:
        out = _brevo(["--bcc-file", str(v.dir / "recipients.txt"),
                      "--subject", f"{comp.subject_prefix(view)}{v.name} — {day}",
                      "--html-file", fh.name, "--text", comp.to_text_email(view),
                      "--attach", str(pdf), "--attach", str(xlsx), "--verify"])
    finally:
        os.unlink(fh.name)
    _mark(slug, {n["id"] for n in mail}, "mail", day)
    audit(actor, "components-mail", slug, f"{len(mail)} avis")
    return "\n".join([*lines, out])


def components_chapter(slug: str, for_slug: str, report_md: Path, actor: str = "job") -> tuple[str, Path] | None:
    """Ajoute le chapitre composants au rapport Markdown du jour d'une veille et écrit à côté la liste
    Excel à joindre à l'e-mail ; renvoie (préfixe d'objet « [EOL ×2] » ou vide, chemin de l'Excel),
    ou None si la veille composants n'existe plus.

    Idempotent : relancé sur le même fichier, le chapitre précédent (toujours en fin) est remplacé.
    """
    target = store.load(for_slug)
    rdir = store.veille_dir(for_slug) / "rapports"
    path = report_md.resolve()
    if path.parent != rdir.resolve() or not store.REPORT_RE.match(path.name) \
            or not path.name.endswith(".md") or not path.is_file():
        raise StoreError(f"rapport Markdown invalide pour la veille {for_slug} : {report_md} "
                         f"(attendu : {rdir}/AAAA-MM-JJ-HHhMM-veille.md, fichier existant)")
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
        data, notices = comp.load_components(slug), comp.load_notices(slug)
        # Livrés un jour où la veille n'a finalement produit aucun PDF (exécution en échec après
        # l'ajout du chapitre) : pas réellement envoyés, donc réinclus.
        failed = {d for d in comp.delivery_days(notices, channel) if not any(rdir.glob(f"{d}*-veille.pdf"))}
        new = comp.pending(notices, channel, {day} | failed)
        view = comp.digest(data["components"], notices, new)
        text = path.read_text(encoding="utf-8").split(CHAPTER_MARK)[0].rstrip()
        store.atomic_write(path, f"{text}\n\n{CHAPTER_MARK}\n\n{comp.to_markdown(view)}")
        for n in new:  # nouvelle date de livraison, y compris pour les réinclus
            n["delivered"].pop(channel, None)
        comp.mark_delivered(notices, {n["id"] for n in new}, channel, day)
        comp.save_notices(slug, notices)
    # Même horodatage que le rapport : l'Excel d'une relance remplace le précédent.
    xlsx = _write_xlsx(rdir, slug, data, notices, path.name.removesuffix("-veille.md"))
    audit(actor, "components-chapter", slug, f"{for_slug} : {len(new)} avis")
    return comp.subject_prefix(view).strip(), xlsx


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
    if (job.get("prompt") != render.short_prompt(v) or job.get("workdir") != str(v.dir)
            or job.get("name") != job_name(v)):
        hermes.edit(v.job_id, name=job_name(v), prompt=render.short_prompt(v), workdir=str(v.dir))
        actions.append("job réaligné")
    return f"{v.slug}: " + ("→ " + ", ".join(actions) if actions else "ok")
