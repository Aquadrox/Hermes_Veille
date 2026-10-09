#!/usr/bin/env python3
"""Chien de garde des veilles conteneurisées (dead man's switch).

Rôle : détecter les pannes SILENCIEUSES. Le conteneur délivre en `local` (aucun
chat), donc un échec de production ne se voit que par l'ABSENCE du rapport — ce
script transforme cette absence en e-mail d'alerte à l'administrateur.

Il tourne sur l'HÔTE, de façon DÉTERMINISTE (aucun LLM) et avec la seule
bibliothèque standard : les pannes les plus probables (clé API expirée, quota
épuisé, conteneur arrêté) empêcheraient un contrôleur interne d'alerter. Il ne
dépend pas non plus d'une installation Hermes sur l'hôte : il lit le volume et
envoie avec le skill brevo-email DU DÉPÔT et le `.env` du dépôt.

Contrôles :
  1. le conteneur tourne ;
  2. pour chaque veille ACTIVE (`data/veilles/*/veille.json`, les veilles en pause
     sont ignorées) :
     - son job existe dans `data/cron/jobs.json` ;
     - son dernier run ne s'est pas terminé en erreur ;
     - un rapport récent et plausible existe — « récent » selon SA fréquence
       (`max_age_hours`, calculé à l'enregistrement de la veille).

Destinataires : page « Paramètres » de l'interface (`veilles/.settings.json` du volume),
sinon ALERT_EMAIL du `.env`, sinon l'expéditeur Brevo — même priorité que veille_core.settings.

Politique d'alerte (un seul e-mail récapitulatif) :
  - nouveau problème : alerte immédiate ;
  - mêmes problèmes : rappel tous les REMIND_DAYS jours (pas de spam quotidien) ;
  - plus aucun problème : e-mail de rétablissement.

Codes de sortie : 0 = tout va bien, 1 = problème détecté, 2 = erreur d'exécution.

Exemples :
  watchdog.py                      # contrôle + alerte si besoin
  watchdog.py --dry-run            # n'envoie rien, n'écrit pas l'état
  watchdog.py --test-notify        # envoie un e-mail de test et sort
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_VOLUME = os.environ.get("VEILLE_VOLUME", os.path.join(REPO, "data"))
DEFAULT_ENV = os.path.join(REPO, ".env")
DEFAULT_BREVO = os.path.join(REPO, "skills", "email", "brevo-email", "scripts", "brevo_send.py")
DEFAULT_CONTAINER = "veille-hermes"
DEFAULT_STATE = "/var/lib/veille-watchdog/state.json"
DEFAULT_MAX_AGE_HOURS = 192
SETTINGS_FILE = os.path.join("veilles", ".settings.json")  # écrit par l'interface (veille_core.settings)
# Rapport d'une veille composants : produit sans LLM, court quand rien n'a changé (≈ 19 Ko).
COMPONENTS_MIN_SIZE = 8000
ALERT_FROM_NAME = "Surveillance veille"
REPORT_SUFFIX = "-veille.pdf"
REPORT_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:-(\d{2})h(\d{2}))?")  # heure optionnelle (v0.3.2)
OK_STATUSES = {None, "", "ok", "success", "running", "skipped"}


def run(cmd, timeout=60):
    """Exécute une commande et renvoie (code, sortie_combinée)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"{type(exc).__name__}: {exc}"


def load_env(path):
    env = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                env[k.strip()] = v
    except OSError:
        pass
    return env


def alert_recipients(volume, env):
    """(destinataires séparés par des virgules | None, origine) : interface > ALERT_EMAIL > expéditeur."""
    try:
        with open(os.path.join(volume, SETTINGS_FILE), encoding="utf-8") as fh:
            saved = json.load(fh).get("alert_emails")
    except FileNotFoundError:
        saved = None
    except (OSError, ValueError, AttributeError) as exc:  # illisible : le .env prend le relais
        print(f"avertissement : paramètres illisibles ({type(exc).__name__}) — repli sur le .env", file=sys.stderr)
        saved = None
    saved = [a for a in saved if isinstance(a, str) and "@" in a] if isinstance(saved, list) else []
    if saved:
        return ",".join(saved), "interface"
    for key, origin in (("ALERT_EMAIL", ".env"), ("BREVO_SENDER_EMAIL", "expéditeur Brevo")):
        if env.get(key):
            return env[key], origin
    return None, None


def report_date(path):
    """Date de référence d'un rapport : celle inscrite dans son NOM de fichier.

    On ne se fie pas à la date de modification : une copie (migration de volume,
    restauration de sauvegarde) la remet à maintenant, ce qui fausserait
    entièrement le contrôle de fraîcheur.
    """
    m = REPORT_DATE_RE.match(os.path.basename(path))
    if m:
        try:
            return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                               int(m.group(4) or 8), int(m.group(5) or 0))
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(os.path.getmtime(path))


def parse_iso(value):
    if not value:
        return None
    try:
        d = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


# --------------------------------------------------------------------------
# Lecture du volume
# --------------------------------------------------------------------------
def load_jobs(volume):
    path = os.path.join(volume, "cron", "jobs.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}, "jobs.json absent"
    except (OSError, ValueError) as exc:
        return {}, f"jobs.json illisible ({exc})"
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = [{"id": k, **v} for k, v in jobs.items() if isinstance(v, dict)]
    return {j.get("id"): j for j in jobs if isinstance(j, dict)}, None


def is_paused(job):
    return job.get("state") == "paused" or bool(job.get("paused_at")) or job.get("enabled") is False


def load_veilles(volume):
    """Veilles du volume ; repli sur l'ancienne disposition mono-veille."""
    root = os.path.join(volume, "veilles")
    out = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            meta = os.path.join(root, name, "veille.json")
            if name.startswith(".") or not os.path.isfile(meta):
                continue
            try:
                with open(meta, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                out.append({"slug": name, "name": name, "dir": os.path.join(root, name), "broken": True})
                continue
            data["dir"] = os.path.join(root, name)
            out.append(data)
        return out
    legacy = os.path.join(volume, "veille")
    if os.path.isdir(legacy):
        out.append({"slug": "veille", "name": "Veille technologique", "dir": legacy,
                    "job_name": "Veille technologique hebdomadaire",
                    "max_age_hours": DEFAULT_MAX_AGE_HOURS})
    return out


# --------------------------------------------------------------------------
# Contrôles
# --------------------------------------------------------------------------
def check_container(container):
    code, out = run(["docker", "inspect", "-f", "{{.State.Running}}", container])
    if code != 0:
        return f"conteneur « {container} » introuvable ou démon Docker injoignable"
    if out.strip() != "true":
        return f"conteneur « {container} » n'est pas en cours d'exécution"
    return None


def check_report(veille, max_age_hours, min_size, now):
    """Renvoie (problème | None, détail)."""
    reports_dir = os.path.join(veille["dir"], "rapports")
    candidates = []
    if os.path.isdir(reports_dir):
        candidates = [n for n in os.listdir(reports_dir) if n.endswith(REPORT_SUFFIX)]
    if not candidates:
        created = parse_iso(veille.get("created_at"))
        if created and (now - created).total_seconds() / 3600 < max_age_hours:
            return None, "aucun rapport encore (veille récente)"
        return "aucun rapport", ""
    newest = max(candidates, key=lambda n: report_date(os.path.join(reports_dir, n)))
    path = os.path.join(reports_dir, newest)
    ref = report_date(path)
    age_h = (now - ref).total_seconds() / 3600.0
    size = os.path.getsize(path)
    detail = f"{newest} — {size / 1024:.0f} Ko — daté du {ref:%d/%m/%Y} ({age_h / 24:.1f} j)"
    if size < min_size:
        return f"dernier rapport suspicieusement petit ({size} octets)", detail
    if age_h > max_age_hours:
        return (f"aucun rapport depuis {age_h / 24:.1f} jours "
                f"(seuil {max_age_hours / 24:.0f} j)"), detail
    return None, detail


def check_veilles(volume, min_size, now):
    """Renvoie (problèmes {clé: message}, détails [str])."""
    problems, details = {}, []
    jobs, jobs_err = load_jobs(volume)
    veilles = load_veilles(volume)
    if not veilles:
        problems["veilles"] = "aucune veille trouvée sur le volume"
        return problems, details
    if jobs_err:
        details.append(f"jobs : {jobs_err}")
    for v in veilles:
        slug, label = v["slug"], v.get("name", v["slug"])
        if v.get("broken"):
            problems[f"{slug}:meta"] = f"[{label}] veille.json illisible"
            continue
        job = jobs.get(v.get("job_id")) if v.get("job_id") else None
        if job is None and v.get("job_name"):
            job = next((j for j in jobs.values() if j.get("name") == v["job_name"]), None)
        if job is None:
            if not jobs_err:
                problems[f"{slug}:job"] = f"[{label}] job cron absent"
            details.append(f"[{label}] job introuvable")
            continue
        if is_paused(job):
            details.append(f"[{label}] en pause — non surveillée")
            continue
        status = job.get("last_status")
        if status not in OK_STATUSES:
            err = (job.get("last_error") or "")[:160]
            problems[f"{slug}:run"] = f"[{label}] dernier run en échec ({status})" + (f" : {err}" if err else "")
        max_age = float(v.get("max_age_hours") or DEFAULT_MAX_AGE_HOURS)
        vmin = min(min_size, COMPONENTS_MIN_SIZE) if v.get("kind") == "composants" else min_size
        prob, det = check_report(v, max_age, vmin, now)
        if prob:
            problems[f"{slug}:report"] = f"[{label}] {prob}"
        details.append(f"[{label}] dernier run : {job.get('last_run_at') or '—'} ({status or '—'})"
                       + (f" ; {det}" if det else ""))
    return problems, details


def check_logs(container, lines=25):
    code, out = run(["docker", "logs", "--tail", str(lines), container])
    return out.strip() if code == 0 else "(logs indisponibles)"


# --------------------------------------------------------------------------
# E-mail
# --------------------------------------------------------------------------
def send_mail(args, subject, body, dry_run):
    if dry_run:
        print("--- [dry-run] e-mail NON envoyé ---")
        print(f"À      : {args.to}")
        print(f"Sujet  : {subject}")
        print(body)
        print("--- fin ---")
        return True
    if not args.to:
        print("✗ aucun destinataire d'alerte (interface : Paramètres ; ou ALERT_EMAIL dans .env, ou --to)",
              file=sys.stderr)
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(body)
        body_path = fh.name
    try:
        code, out = run(
            [sys.executable, args.brevo_script, "--to", args.to, "--subject", subject,
             "--body-file", body_path, "--from-name", ALERT_FROM_NAME, "--env-file", args.env_file],
            timeout=120,
        )
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass
    print(out.strip())
    return code == 0


def build_body(args, problems, details, logs):
    now = dt.datetime.now().strftime("%d/%m/%Y à %H:%M")
    lines = ["La surveillance des veilles a détecté un problème.", "", f"Détecté le : {now}", "Problème(s) :"]
    lines += [f"  - {p}" for p in problems]
    lines += ["", "État :"] + [f"  - {d}" for d in details if d]
    lines += [
        "",
        "Que faire ?",
        f"  1. vérifier le conteneur : docker ps -a --filter name={args.container}",
        f"  2. lire les logs         : cd {REPO} && make logs",
        f"  3. état des veilles      : cd {REPO} && make veilles",
        f"  4. relancer une veille   : cd {REPO} && make run-now VEILLE=<slug>",
        "",
        f"--- dernières lignes de log ({args.container}) ---",
        logs or "(aucun log)",
        "",
        "— Surveillance automatique des veilles. Un e-mail de rétablissement suivra dès que",
        f"  la production repart. Rappel tous les {args.remind_days:g} jours si le problème persiste.",
    ]
    return "\n".join(lines)


def alert_subject(problems):
    subject = "[ALERTE] Veille — " + problems[0][:80]
    if len(problems) > 1:
        subject += f" (+{len(problems) - 1} autre{'s' if len(problems) > 2 else ''})"
    return subject


# --------------------------------------------------------------------------
# État (anti-spam)
# --------------------------------------------------------------------------
def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"avertissement : état non écrit ({exc})", file=sys.stderr)


def decide(state, problem_keys, now_ts, remind_days):
    """'alert' | 'remind' | 'recovered' | None, selon l'état précédent."""
    previous = set(state.get("problem_keys") or ([] if state.get("status") != "ko" else ["legacy"]))
    current = set(problem_keys)
    if current:
        if current - previous:
            return "alert"
        if now_ts - state.get("last_alert_ts", 0) >= remind_days * 86400:
            return "remind"
        return None
    return "recovered" if previous else None


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Chien de garde des veilles conteneurisées")
    ap.add_argument("--volume", default=DEFAULT_VOLUME, help=f"volume de données (défaut {DEFAULT_VOLUME})")
    ap.add_argument("--env-file", default=DEFAULT_ENV, help="fichier .env (BREVO_*, ALERT_EMAIL en repli)")
    ap.add_argument("--container", default=DEFAULT_CONTAINER, help="nom du conteneur")
    ap.add_argument("--min-size", type=int, default=20000, help="taille minimale plausible d'un PDF (octets)")
    ap.add_argument("--remind-days", type=float, default=3, help="délai entre deux rappels (jours)")
    ap.add_argument("--state-file", default=DEFAULT_STATE)
    ap.add_argument("--to", default=None,
                    help="destinataire des alertes (défaut : Paramètres de l'interface, sinon ALERT_EMAIL du .env)")
    ap.add_argument("--brevo-script", default=DEFAULT_BREVO)
    ap.add_argument("--skip-container", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--dry-run", action="store_true", help="n'envoie rien et n'écrit pas l'état")
    ap.add_argument("--test-notify", action="store_true", help="envoie un e-mail de test puis sort")
    args = ap.parse_args(argv)

    env = load_env(args.env_file)
    origin = "--to" if args.to else None
    if not args.to:
        args.to, origin = alert_recipients(args.volume, env)
    print(f"alertes → {args.to or 'AUCUN destinataire'} ({origin or 'à renseigner : Paramètres'})")

    if args.test_notify:
        ok = send_mail(
            args,
            "[TEST] Surveillance des veilles — canal d'alerte opérationnel",
            "Ceci est un test du canal d'alerte des veilles.\n\n"
            "Si vous recevez ce message, la surveillance pourra vous prévenir en cas de panne.\n\n"
            "— Surveillance automatique des veilles.",
            args.dry_run,
        )
        return 0 if ok else 2

    now = dt.datetime.now()
    problems = {}
    if not args.skip_container:
        msg = check_container(args.container)
        if msg:
            problems["container"] = msg
    if not os.path.isdir(args.volume):
        problems["volume"] = f"volume introuvable ({args.volume})"
        details = []
    else:
        vproblems, details = check_veilles(args.volume, args.min_size, now)
        problems.update(vproblems)

    msgs = list(problems.values())
    print("statut : " + (f"ko — {'; '.join(msgs)}" if msgs else "ok — tout va bien"))
    for d in details:
        print(f"  - {d}")

    state = load_state(args.state_file)
    action = decide(state, problems.keys(), now.timestamp(), args.remind_days)

    if action in ("alert", "remind"):
        logs = "" if args.skip_container else check_logs(args.container)
        sent = send_mail(args, alert_subject(msgs), build_body(args, msgs, details, logs), args.dry_run)
        print(f"alerte {'(rappel) ' if action == 'remind' else ''}envoyée : {sent}")
        if sent and not args.dry_run:
            state["last_alert_ts"] = now.timestamp()
    elif action == "recovered":
        sent = send_mail(
            args,
            "[RÉTABLI] Veille — production repartie",
            "Bonne nouvelle : les veilles sont de nouveau opérationnelles.\n\nÉtat :\n"
            + "\n".join(f"  - {d}" for d in details)
            + "\n\n— Surveillance automatique des veilles.",
            args.dry_run,
        )
        print(f"e-mail de rétablissement envoyé : {sent}")
    elif msgs:
        print("alerte déjà envoyée récemment — rappel non dû")

    if not args.dry_run:
        state.update(status="ko" if msgs else "ok", problem_keys=sorted(problems),
                     last_check_ts=now.timestamp(), last_problems=msgs)
        save_state(args.state_file, state)

    return 1 if msgs else 0


if __name__ == "__main__":
    sys.exit(main())
