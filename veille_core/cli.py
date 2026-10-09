"""veillectl — gestion des veilles en ligne de commande (dans le conteneur).

  veillectl list
  veillectl show <slug>
  veillectl create --name N --subject S --schedule "0 8 * * 1" --recipients a@x,b@y [--brief-file F | --template vierge]
  veillectl edit <slug> [--name N] [--subject S] [--schedule CRON] [--recipients ...] [--brief-file F]
  veillectl pause|resume|archive|test-email <slug>
  veillectl run <slug> [--wait]
  veillectl costs [slug] [--days N]
  veillectl reconcile | migrate | bootstrap

Veille composants (PCN / fins de vie) :
  veillectl create --kind composants --name N --schedule "0 23 * * 0" --recipients a@x [--components-file bom.csv]
  veillectl edit <slug> [--components-file F] [--include-components <slug-composants>|""] [--batch-size N]
  veillectl components list|notices <slug> [--type EOL]
  veillectl components add <slug> (--file bom.csv | "MPN;Fabricant;Réf" …)
  veillectl components remove <slug> MPN …
  veillectl components scan <slug>                                  (étape 1 du job)
  veillectl components scan <slug> --status-only                    (statut Digi-Key seul, à la demande)
  veillectl components finalize <slug> <avis.json>                  (étape 5 du job)
  veillectl components chapter <slug> --for <veille> --append <rapport.md>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import components, hermes, schedule, service, store, version


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
    if v.components:
        print(f"  chapitre     : veille composants {v.components}")
    if r["components"]:
        print(f"  composants   : {r['components']}")
    for rep in store.list_reports(slug)[:5]:
        print(f"  rapport      : {rep['date']}  {rep.get('pdf', '(pas de PDF)')}")
    return 0


def _components_file(path: str | None) -> list[dict] | None:
    if not path:
        return None
    return components.parse_components(Path(path).read_text(encoding="utf-8"))


def _components(args) -> int:
    slug = args.slug
    if args.action == "list":
        for c in components.load_components(service._components_veille(slug).slug)["components"]:
            dk = c.get("dk") or {}
            print(f"{c['mpn']:<28} {c['manufacturer']:<24} {c.get('ref', ''):<14} "
                  f"Digi-Key {dk.get('status') or dk.get('error') or '—':<22} web {c.get('web_checked_at') or 'jamais'}")
    elif args.action == "notices":
        for n in service.notices(slug, args.type):
            print(f"{n['found_at']}  {n['type']:<8} {n['mpn']:<24} {n['title'][:60]:<60} {n['sources'][0]['url']}")
    elif args.action == "add":
        items = _components_file(args.file) or []
        items += components.parse_components("\n".join(args.items))
        print(f"✓ {service.add_components(slug, items)} composant(s) ajouté(s)")
    elif args.action == "remove":
        print(f"✓ {service.remove_components(slug, args.items)} composant(s) retiré(s)")
    elif args.action == "scan" and args.status_only:
        dk = service.components_scan(slug, actor="cli", plan_batch=False)["digikey"]
        print(f"✓ Digi-Key : {dk['checked']} statut(s) lu(s), {dk['errors']} référence(s) non trouvée(s)")
        for key in ("stopped", "error"):
            if dk.get(key):
                print(f"  {dk[key]}")
        if not dk["configured"]:
            print("  clés Digi-Key absentes : les saisir dans l'interface, page Sources")
    elif args.action == "scan":
        print(json.dumps(service.components_scan(slug), ensure_ascii=False, indent=1))
    elif args.action == "finalize":
        if not args.items:
            raise store.StoreError("chemin du fichier JSON des avis manquant")
        print(service.components_finalize(slug, Path(args.items[0])))
    elif args.action == "chapter":
        if not (args.for_slug and args.append):
            raise store.StoreError("--for <veille> et --append <rapport.md> sont requis")
        prefix = service.components_chapter(slug, args.for_slug, Path(args.append))
        if prefix is None:
            print(f"avertissement : veille composants « {slug} » introuvable — chapitre ignoré")
        else:
            print("✓ chapitre composants ajouté")
            if prefix:
                print(f"PREFIXE_SUJET={prefix}")
    return 0


def _usd(value) -> str:
    return "—" if value is None else f"{value:.4f} $"


def _costs(slug: str | None, days: int) -> int:
    if slug is None:
        ov = service.costs_overview(days)
        for r in ov["veilles"]:
            print(f"{r['slug']:<24} mois : {_usd(r['total_month_usd']):>11} ({r['runs_month']} run(s))  "
                  f"{days} j : {_usd(r['total_days_usd']):>11}  moyenne/run : {_usd(r['average_run_usd'])}")
        print(f"{'TOTAL':<24} mois : {_usd(ov['total_month_usd']):>11}  {days} j : {_usd(ov['total_days_usd'])}")
        return 0
    c = service.costs(slug, days)
    if not c["runs"]:
        print("aucune exécution mesurée (consommation lue dans state.db de Hermes)")
        return 0
    for r in c["runs"]:
        when = r["started_at"].strftime("%Y-%m-%d %H:%M") if r["started_at"] else "?"
        dur = f"{r['duration_min']} min" if r["duration_min"] is not None else "?"
        print(f"{when}  {dur:>9}  entrée {r['input_tokens']:>9} (+{r['cache_read_tokens']} en cache)  "
              f"sortie {r['output_tokens']:>8}  {'PLEINE' if r['peak'] else 'creuse'}  {_usd(r['cost_usd'])}"
              f"  (estimation Hermes {_usd(r['hermes_estimate_usd'])})")
    print(f"mois en cours : {_usd(c['total_month_usd'])} ({c['runs_month']} run(s)) — "
          f"{days} derniers jours : {_usd(c['total_days_usd'])} — tarifs du {c['pricing_updated']}")
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
    ap.add_argument("--version", action="version", version=f"veillectl {version()}")
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
    p_create.add_argument("--kind", choices=store.KINDS, default="veille")
    p_create.add_argument("--components-file", help="composants : « MPN ; Fabricant ; Réf » par ligne")
    p_create.add_argument("--include-components", help="veille : slug de la veille composants en chapitre")
    p_create.add_argument("--batch-size", type=int)

    p_edit = sub.add_parser("edit")
    p_edit.add_argument("slug")
    p_edit.add_argument("--name")
    p_edit.add_argument("--subject")
    p_edit.add_argument("--schedule")
    p_edit.add_argument("--recipients")
    p_edit.add_argument("--brief-file")
    p_edit.add_argument("--model")
    p_edit.add_argument("--components-file")
    p_edit.add_argument("--include-components", help='slug, ou "" pour retirer le chapitre')
    p_edit.add_argument("--batch-size", type=int)

    p_comp = sub.add_parser("components", help="veille composants : PCN et fins de vie")
    p_comp.add_argument("action", choices=("list", "notices", "add", "remove", "scan", "finalize", "chapter"))
    p_comp.add_argument("slug")
    p_comp.add_argument("items", nargs="*", help="add : lignes « MPN;Fabricant;Réf » ; remove : MPN ; "
                                                  "finalize : fichier JSON")
    p_comp.add_argument("--file", help="add : fichier « MPN ; Fabricant ; Réf » (collage tableur accepté)")
    p_comp.add_argument("--type", choices=tuple(components.TYPES), help="notices : filtre par type")
    p_comp.add_argument("--for", dest="for_slug", help="chapter : veille qui reçoit le chapitre")
    p_comp.add_argument("--append", help="chapter : rapport Markdown du jour de cette veille")
    p_comp.add_argument("--status-only", action="store_true",
                        help="scan : statut Digi-Key seulement (le lot de la prochaine exécution est conservé)")

    p_costs = sub.add_parser("costs", help="consommation de tokens et coût des exécutions")
    p_costs.add_argument("slug", nargs="?")
    p_costs.add_argument("--days", type=int, default=30)

    sub.add_parser("reconcile")
    sub.add_parser("migrate")
    sub.add_parser("bootstrap")
    # Python 3.11 ne rattache pas à `items` (nargs="*") les valeurs placées APRÈS une option
    # (« components add slug --file f.csv "MPN;Fab" ») : on les y ajoute, sans rien accepter d'autre.
    args, extra = ap.parse_known_args(argv)
    if extra:
        if args.cmd != "components" or any(e.startswith("-") for e in extra):
            ap.error("arguments non reconnus : " + " ".join(extra))
        args.items += extra

    try:
        if args.cmd == "list":
            return _print_overview()
        if args.cmd == "show":
            return _show(args.slug)
        if args.cmd == "costs":
            return _costs(args.slug, args.days)
        if args.cmd == "components":
            return _components(args)
        if args.cmd == "create":
            if args.kind == "composants" and not args.brief_file:
                args.template = service.COMPONENTS_TEMPLATE
            v = service.create_veille(
                name=args.name, slug=args.slug, subject=args.subject,
                schedule_expr=args.schedule, recipients=store.parse_recipients(args.recipients),
                brief=_brief_from(args), model=args.model, paused=args.paused, kind=args.kind,
                components=_components_file(args.components_file),
                include_components=args.include_components, batch_size=args.batch_size)
            print(f"✓ veille créée : {v.slug} (job {v.job_id}) — {schedule.describe(v.schedule)}")
        elif args.cmd == "edit":
            v = service.update_veille(
                args.slug, name=args.name, subject=args.subject, schedule_expr=args.schedule,
                recipients=store.parse_recipients(args.recipients) if args.recipients else None,
                brief=_brief_from(args), model=args.model,
                components=_components_file(args.components_file),
                include_components=args.include_components, batch_size=args.batch_size)
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
    except (store.StoreError, schedule.ScheduleError, service.ServiceError, hermes.HermesError, OSError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
