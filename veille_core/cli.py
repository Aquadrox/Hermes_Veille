"""veillectl — gestion des veilles en ligne de commande (dans le conteneur).

  veillectl list
  veillectl show <slug>
  veillectl create --name N --subject S --schedule "0 8 * * 1" --recipients a@x,b@y [--brief-file F | --template vierge]
  veillectl edit <slug> [--name N] [--subject S] [--schedule CRON] [--recipients ...] [--brief-file F]
  veillectl pause|resume|archive|test-email <slug>
  veillectl run <slug> [--wait]
  veillectl reconcile | migrate | bootstrap
"""
from __future__ import annotations

import argparse
import sys

from . import hermes, schedule, service, store


def _print_overview() -> int:
    rows = service.overview()
    if not rows:
        print("aucune veille")
        return 0
    for r in rows:
        v, st = r["veille"], r["status"]
        last = r["last_report"]["date"] if r["last_report"] else "—"
        print(f"{v.slug:<24} {st['state']:<10} {r['schedule_human']:<32} "
              f"dernier rapport {last:<10}  {len(r['recipients'])} dest.  job {v.job_id or '—'}")
    return 0


def _show(slug: str) -> int:
    r = service.describe(store.load(slug))
    v, st = r["veille"], r["status"]
    print(f"{v.name} ({v.slug})")
    print(f"  sujet        : {v.subject}")
    print(f"  fréquence    : {r['schedule_human']}  [{v.schedule}]")
    print(f"  état         : {st['state']}  prochain : {st.get('next_run_at') or '—'}")
    print(f"  dernier run  : {st.get('last_run_at') or '—'}  statut : {st.get('last_status') or '—'}")
    if st.get("last_error"):
        print(f"  erreur       : {st['last_error']}")
    print(f"  destinataires: {', '.join(r['recipients']) or '—'}")
    print(f"  répertoire   : {v.dir}")
    for rep in store.list_reports(slug)[:5]:
        print(f"  rapport      : {rep['date']}  {rep.get('pdf', '(pas de PDF)')}")
    return 0


def _brief_from(args) -> str | None:
    if getattr(args, "brief_file", None):
        with open(args.brief_file, encoding="utf-8") as fh:
            return fh.read()
    if getattr(args, "template", None):
        templates = store.brief_templates()
        if args.template not in templates:
            raise store.StoreError(f"gabarit inconnu : {args.template} ({', '.join(templates)})")
        return templates[args.template]
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="veillectl", description="Gestion des veilles")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("show", "pause", "resume", "archive", "test-email"):
        sub.add_parser(name).add_argument("slug")
    p_run = sub.add_parser("run")
    p_run.add_argument("slug")
    p_run.add_argument("--wait", action="store_true", help="attendre la fin du run")

    p_create = sub.add_parser("create")
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--slug")
    p_create.add_argument("--subject", default="")
    p_create.add_argument("--schedule", required=True, help="expression cron 5 champs")
    p_create.add_argument("--recipients", required=True, help="adresses séparées par des virgules")
    p_create.add_argument("--brief-file")
    p_create.add_argument("--template", default="vierge")
    p_create.add_argument("--model")
    p_create.add_argument("--paused", action="store_true")

    p_edit = sub.add_parser("edit")
    p_edit.add_argument("slug")
    p_edit.add_argument("--name")
    p_edit.add_argument("--subject")
    p_edit.add_argument("--schedule")
    p_edit.add_argument("--recipients")
    p_edit.add_argument("--brief-file")
    p_edit.add_argument("--model")

    sub.add_parser("reconcile")
    sub.add_parser("migrate")
    sub.add_parser("bootstrap")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "list":
            return _print_overview()
        if args.cmd == "show":
            return _show(args.slug)
        if args.cmd == "create":
            v = service.create_veille(
                name=args.name, slug=args.slug, subject=args.subject,
                schedule_expr=args.schedule, recipients=store.parse_recipients(args.recipients),
                brief=_brief_from(args), model=args.model, paused=args.paused)
            print(f"✓ veille créée : {v.slug} (job {v.job_id}) — {schedule.describe(v.schedule)}")
        elif args.cmd == "edit":
            v = service.update_veille(
                args.slug, name=args.name, subject=args.subject, schedule_expr=args.schedule,
                recipients=store.parse_recipients(args.recipients) if args.recipients else None,
                brief=_brief_from(args), model=args.model)
            print(f"✓ veille modifiée : {v.slug}")
        elif args.cmd == "pause":
            service.pause(args.slug)
            print(f"✓ {args.slug} en pause")
        elif args.cmd == "resume":
            service.resume(args.slug)
            print(f"✓ {args.slug} reprise")
        elif args.cmd == "run":
            if args.wait:
                print(service.run_wait(args.slug))
            else:
                log = service.run_now(args.slug)
                print(f"✓ lancement en arrière-plan — journal : {store.runs_dir(args.slug) / log}")
        elif args.cmd == "archive":
            print(f"✓ archivée dans {service.archive(args.slug)}")
        elif args.cmd == "test-email":
            print(service.send_test_email(args.slug))
        elif args.cmd == "migrate":
            slug = service.migrate_legacy()
            print(f"✓ migration effectuée -> {slug}" if slug else "rien à migrer")
        elif args.cmd == "bootstrap":
            slug = service.bootstrap_default()
            print(f"✓ veille par défaut créée : {slug}" if slug else "rien à amorcer")
        elif args.cmd == "reconcile":
            for line in service.reconcile_all():
                print(line)
    except (store.StoreError, schedule.ScheduleError, service.ServiceError, hermes.HermesError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
