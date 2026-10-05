#!/usr/bin/env python3
"""Chien de garde de la veille conteneurisée (dead man's switch).

Rôle : détecter les pannes SILENCIEUSES. Le conteneur délivre en `local` (aucun
chat), donc un échec de production ne se voit que par l'ABSENCE du rapport — ce
script transforme cette absence en e-mail d'alerte.

Il tourne sur l'HÔTE et de façon DÉTERMINISTE (aucun LLM) : c'est indispensable,
car les pannes les plus probables (clé API expirée, quota épuisé, conteneur
arrêté) empêcheraient un contrôleur interne de s'exécuter ou d'alerter.

Trois contrôles :
  1. le conteneur tourne ;
  2. un rapport récent et plausible existe sur le volume ;
  3. le dernier run du job s'est terminé en succès.

Politique d'alerte (e-mail, via le skill brevo-email — même chemin d'envoi que la
veille) :
  - OK -> KO : alerte immédiate ;
  - KO -> KO : rappel tous les REMIND_DAYS jours (pas de spam quotidien) ;
  - KO -> OK : e-mail de rétablissement.

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

REPORT_GLOB_SUFFIX = "-veille.pdf"
REPORT_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def report_date(path):
    """Date de référence d'un rapport : celle inscrite dans son NOM de fichier.

    On ne se fie pas à la date de modification : une copie (migration de volume,
    restauration de sauvegarde) la remet à maintenant, ce qui fausserait
    entièrement le contrôle de fraîcheur.
    """
    m = REPORT_DATE_RE.match(os.path.basename(path))
    if m:
        try:
            return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 8, 0)
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(os.path.getmtime(path))
DEFAULT_VOLUME = "/root/veille-hermes/data"
DEFAULT_CONTAINER = "veille-hermes"
DEFAULT_STATE = "/var/lib/veille-watchdog/state.json"
DEFAULT_BREVO = os.path.expanduser(
    "~/.hermes/skills/email/brevo-email/scripts/brevo_send.py"
)
ALERT_FROM_NAME = "Surveillance veille"


def run(cmd, timeout=60):
    """Exécute une commande et renvoie (code, sortie_combinée)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# Contrôles
# --------------------------------------------------------------------------
def check_container(container):
    code, out = run(["docker", "inspect", "-f", "{{.State.Running}}", container])
    if code != 0:
        return False, f"conteneur « {container} » introuvable ou démon Docker injoignable", out.strip()
    running = out.strip() == "true"
    if not running:
        return False, f"conteneur « {container} » n'est pas en cours d'exécution", out.strip()
    return True, "conteneur en cours d'exécution", ""


def check_report(volume, max_age_hours, min_size):
    reports_dir = os.path.join(volume, "veille", "rapports")
    if not os.path.isdir(reports_dir):
        return False, f"répertoire des rapports absent ({reports_dir})", ""
    candidates = [
        n
        for n in os.listdir(reports_dir)
        if n.endswith(REPORT_GLOB_SUFFIX)
    ]
    if not candidates:
        return False, "aucun rapport sur le volume", ""
    # Tri par date du NOM (immunisé contre les copies), pas par date de modification
    newest = max(candidates, key=lambda n: report_date(os.path.join(reports_dir, n)))
    path = os.path.join(reports_dir, newest)
    ref = report_date(path)
    age_h = (dt.datetime.now() - ref).total_seconds() / 3600.0
    size = os.path.getsize(path)
    detail = (
        f"{newest} — {size / 1024:.0f} Ko — daté du {ref:%d/%m/%Y} "
        f"({age_h / 24:.1f} j)"
    )
    if size < min_size:
        return False, f"le dernier rapport est suspicieusement petit ({size} octets)", detail
    if age_h > max_age_hours:
        return False, f"aucun rapport depuis {age_h / 24:.1f} jours (seuil {max_age_hours / 24:.0f} j)", detail
    return True, "rapport récent présent", detail


def check_last_run(container):
    """Contrôle souple : en cas d'ambiguïté on n'alarme pas (pas de faux positif)."""
    code, out = run(["docker", "exec", container, "hermes", "cron", "list"])
    if code != 0:
        return None, "dernier run illisible (conteneur arrêté ?)", ""
    for line in out.splitlines():
        if "Last run:" in line:
            value = line.split("Last run:", 1)[1].strip()
            ok = value.rstrip().endswith("ok")
            return ok, f"dernier run : {value}", ""
    return None, "dernier run non renseigné", ""


def check_logs(container, lines=25):
    code, out = run(["docker", "logs", "--tail", str(lines), container])
    return out.strip() if code == 0 else "(logs indisponibles)"


# --------------------------------------------------------------------------
# E-mail
# --------------------------------------------------------------------------
def send_mail(args, subject, body, dry_run):
    if dry_run:
        print("--- [dry-run] e-mail NON envoyé ---")
        print(f"À      : {args.to_file}")
        print(f"Sujet  : {subject}")
        print(body)
        print("--- fin ---")
        return True
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(body)
        body_path = fh.name
    try:
        code, out = run(
            [
                sys.executable,
                args.brevo_script,
                "--to-file", args.to_file,
                "--subject", subject,
                "--body-file", body_path,
                "--from-name", ALERT_FROM_NAME,
            ],
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
    now = dt.datetime.now().strftime("%A %d %B %Y à %H:%M")
    lines = [
        "La surveillance de la veille a détecté un problème.",
        "",
        f"Détecté le : {now}",
        f"Problème(s) :",
    ]
    lines += [f"  - {p}" for p in problems]
    lines += ["", "Détails :"]
    lines += [f"  - {d}" for d in details if d]
    lines += [
        "",
        "Que faire ?",
        "  1. vérifier le conteneur      : docker ps -a --filter name=" + args.container,
        "  2. lire les logs              : cd /root/veille-hermes && make logs",
        f"  3. relancer le job            : docker exec {args.container} hermes cron run <id>",
        "",
        f"--- dernières lignes de log ({args.container}) ---",
        logs or "(aucun log)",
        "",
        "— Surveillance automatique de la veille. Un e-mail de rétablissement suivra",
        "  dès que la production repart. Rappel tous les "
        f"{args.remind_days} jours si le problème persiste.",
    ]
    return "\n".join(lines)


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


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Chien de garde de la veille conteneurisée")
    ap.add_argument("--volume", default=DEFAULT_VOLUME, help="volume de données du conteneur")
    ap.add_argument("--container", default=DEFAULT_CONTAINER, help="nom du conteneur")
    ap.add_argument("--max-age-hours", type=float, default=192, help="âge maximal du dernier rapport (défaut 192 h = 8 j)")
    ap.add_argument("--min-size", type=int, default=20000, help="taille minimale plausible d'un PDF (défaut 20 000 o)")
    ap.add_argument("--remind-days", type=float, default=3, help="délai entre deux rappels (défaut 3 j)")
    ap.add_argument("--state-file", default=DEFAULT_STATE)
    ap.add_argument("--to-file", default=None, help="destinataire (défaut : <volume>/veille/destinataire.txt)")
    ap.add_argument("--brevo-script", default=DEFAULT_BREVO)
    ap.add_argument("--dry-run", action="store_true", help="n'envoie rien et n'écrit pas l'état")
    ap.add_argument("--test-notify", action="store_true", help="envoie un e-mail de test puis sort")
    args = ap.parse_args()

    if args.to_file is None:
        args.to_file = os.path.join(args.volume, "veille", "destinataire.txt")

    if args.test_notify:
        ok = send_mail(
            args,
            "[TEST] Surveillance de la veille — canal d'alerte opérationnel",
            "Ceci est un test du canal d'alerte de la veille technologique.\n\n"
            "Si vous recevez ce message, la surveillance pourra vous prévenir en cas de panne.\n\n"
            "— Surveillance automatique de la veille.",
            args.dry_run,
        )
        return 0 if ok else 2

    problems, details = [], []

    ok_c, msg_c, det_c = check_container(args.container)
    details.append(msg_c + (f" ({det_c})" if det_c else ""))
    if not ok_c:
        problems.append(msg_c)

    ok_r, msg_r, det_r = check_report(args.volume, args.max_age_hours, args.min_size)
    details.append(msg_r + (f" ({det_r})" if det_r else ""))
    if not ok_r:
        problems.append(msg_r)

    ok_l, msg_l, det_l = check_last_run(args.container)
    details.append(msg_l + (f" ({det_l})" if det_l else ""))
    if ok_l is False:
        problems.append(msg_l)

    state = load_state(args.state_file)
    previous = state.get("status")
    status = "ko" if problems else "ok"
    now = dt.datetime.now().timestamp()

    print(f"statut : {status}" + (f" — {'; '.join(problems)}" if problems else " — tout va bien"))
    for d in details:
        print(f"  - {d}")

    if args.dry_run:
        if problems:
            subject = "[ALERTE] Veille technologique — " + problems[0][:70]
            if len(problems) > 1:
                subject += f" (+{len(problems) - 1})"
            send_mail(args, subject, build_body(args, problems, details, check_logs(args.container)), True)
        else:
            print("(rien à envoyer — tout va bien)")
        return 1 if problems else 0

    if status == "ko":
        last_alert = state.get("last_alert_ts", 0)
        should = previous != "ko" or (now - last_alert) >= args.remind_days * 86400
        if should:
            subject = "[ALERTE] Veille technologique — " + problems[0][:70]
            if len(problems) > 1:
                subject += f" (+{len(problems) - 1} autre{'s' if len(problems) > 2 else ''})"
            sent = send_mail(args, subject, build_body(args, problems, details, check_logs(args.container)), False)
            print(f"alerte envoyée : {sent}")
            if sent:
                state["last_alert_ts"] = now
        else:
            print("alerte déjà envoyée récemment — rappel non dû")
    elif previous == "ko":
        sent = send_mail(
            args,
            "[RÉTABLI] Veille technologique — production repartie",
            "Bonne nouvelle : la veille technologique est de nouveau opérationnelle.\n\n"
            "Détails :\n" + "\n".join(f"  - {d}" for d in details) + "\n\n"
            "— Surveillance automatique de la veille.",
            False,
        )
        print(f"e-mail de rétablissement envoyé : {sent}")

    state["status"] = status
    state["last_check_ts"] = now
    state["last_problems"] = problems
    save_state(args.state_file, state)

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
