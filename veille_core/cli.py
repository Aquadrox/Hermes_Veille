"""veillectl — gestion des veilles en ligne de commande (dans le conteneur).

  veillectl list
  veillectl show <slug>
  veillectl create --name N --subject S --schedule "0 8 * * 1" --recipients a@x,b@y [--brief-file F | --template vierge]
  veillectl edit <slug> [--name N] [--subject S] [--schedule CRON] [--recipients ...] [--brief-file F]
  veillectl pause|resume|archive|test-email <slug>
  veillectl run <slug> [--wait]
  veillectl costs [slug] [--days N]
  veillectl reconcile | migrate | bootstrap
  veillectl report publish <slug> <rapport.md>                      (dernière étape du job : PDF + e-mail)

Recherche déléguée à Claude Code sur l'hôte (pont : scripts/install-claude-bridge.sh) :
  veillectl edit <slug> --claude-research on|off                    (option de la veille, désactivée par défaut)
  veillectl research claude <slug>                                  (étape RECHERCHE du job, option activée)
  veillectl research ask <slug> [--question Q] [--wait]             (à la demande, option activée ou non)
  veillectl research list|show <slug> [AAAAMMJJ-HHMMSS]             (recherches conservées pour analyse)
  veillectl research status                                         (état du pont)

Veille composants (PCN / fins de vie) :
  veillectl create --kind composants --name N --schedule "0 23 * * 0" --recipients a@x [--components-file bom.csv]
  veillectl edit <slug> [--components-file F] [--include-components <slug-composants>|""] [--batch-size N]
  veillectl components list|notices <slug> [--type EOL]
  veillectl components add <slug> (--file bom.csv | "MPN;Fabricant" …)
  veillectl components remove <slug> MPN …
  veillectl components import <slug> bom.xlsx [--dry-run] [--replace] [--mpn COL] [--manufacturer COL] …
  veillectl components scan <slug>                                  (étape 1 du job)
  veillectl components scan <slug> --status-only                    (statut Digi-Key seul, à la demande)
  veillectl components alternatives <slug> [MPN …]                  (alternatives Digi-Key : ces MPN, sinon fins de vie)
  veillectl components progress <slug> MPN …                        (étape 3 du job, lancement suivi)
  veillectl components finalize <slug> <avis.json>                  (étape 5 du job)
  veillectl components reset <slug>                                 (recherche remise à zéro, copie archivée)
  veillectl components chapter <slug> --for <veille> --append <rapport.md>

Base des composants (connaissance partagée entre veilles, reconstruite à chaque démarrage) :
  veillectl catalog rebuild | stats
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import bom_import, catalog, claude_research, components, db, envfile, hermes, schedule, service, store, version


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
        print(f"  rapport      : {rep['date']} {rep['time'] or '     '}  {rep.get('pdf', '(pas de PDF)')}")
    return 0


def _components_file(path: str | None) -> list[dict] | None:
    """Fichier de composants : texte « MPN ; Fabricant » (inchangé) ou Excel (colonnes
    reconnues automatiquement ; une ligne invalide est une erreur, comme pour le texte)."""
    if not path:
        return None
    if Path(path).suffix.lower() not in (".xlsx", ".xlsm"):
        return components.parse_components(Path(path).read_text(encoding="utf-8"))
    p = bom_import.propose_options(bom_import.read_path(Path(path)))
    if p["error"] or p["rejected"]:
        raise components.ComponentsError(p["error"] or "lignes invalides : " + ", ".join(
            f"{r['line']} ({r['reason']})" for r in p["rejected"][:10]) + " — `veillectl components import` "
            "permet de les ignorer et de choisir les colonnes")
    return p["components"]


def _print_proposal(p: dict) -> None:
    print(f"Feuille : {p['sheet'] or '—'}   ligne d'en-tête : "
          f"{p['header_row'] + 1 if p['header_row'] is not None else 'aucune'}   lignes de données : {p['rows']}")
    for f in bom_import.FIELDS:
        m = p["mapping"][f]
        col = (f"{bom_import.column_name(m['col'])} « {p['columns'][m['col']]} »" if m["col"] is not None
               else "(aucune)")
        print(f"  {bom_import.LABELS[f]:<26} → {col}  [{m['confidence'] or 'non trouvée'}]")
    if p["error"]:
        print(f"✗ {p['error']}")
        return
    print(f"{len(p['components'])} composant(s) valide(s), {len(p['rejected'])} ligne(s) ignorée(s)")
    for r in p["rejected"][:10]:
        print(f"  ligne {r['line']} : {r['reason']}")
    for c in p["preview"]:
        print(f"  {c['mpn']:<28} {c['manufacturer']}")


def _import(args) -> int:
    if not args.items:
        raise store.StoreError("chemin du fichier à importer manquant")
    columns = {f: v for f, v in (("mpn", args.mpn), ("manufacturer", args.manufacturer)) if v is not None}
    p = bom_import.propose_options(bom_import.read_path(Path(args.items[0]), args.sheet), args.header_row,
                                   columns, args.default_manufacturer or "")
    _print_proposal(p)
    if p["error"]:
        return 1
    if args.dry_run:
        print("(simulation : rien n'est enregistré)")
        return 0
    st = service.import_components(args.slug, p["components"], "replace" if args.replace else "add")
    print(f"✓ import : {st['added']} ajouté(s), {st['existing']} déjà présent(s), {st['removed']} retiré(s) — "
          f"{st['total']} composant(s) suivis{_known(st)}")
    return 0


def _known(st: dict) -> str:
    """Reprise depuis la base des composants, à la suite du compte rendu d'un ajout."""
    return f" ; base : {st['known']} déjà connu(s), {st['notices_known']} avis repris" if st.get("known") else ""


def _components(args) -> int:
    slug = args.slug
    if args.action == "list":
        for c in components.load_components(service._components_veille(slug).slug)["components"]:
            dk = c.get("dk") or {}
            print(f"{c['mpn']:<28} {c['manufacturer']:<24} "
                  f"Digi-Key {dk.get('status') or dk.get('error') or '—':<22} web {c.get('web_checked_at') or 'jamais'}")
    elif args.action == "notices":
        for n in service.notices(slug, args.type):
            print(f"{n['found_at']}  {n['type']:<8} {n['mpn']:<24} {n['title'][:60]:<60} {n['sources'][0]['url']}")
    elif args.action == "add":
        items = _components_file(args.file) or []
        items += components.parse_components("\n".join(args.items))
        st = service.import_components(slug, items, "add")
        print(f"✓ {st['added']} composant(s) ajouté(s){_known(st)}")
    elif args.action == "remove":
        print(f"✓ {service.remove_components(slug, args.items)} composant(s) retiré(s)")
    elif args.action == "import":
        return _import(args)
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
    elif args.action == "alternatives":
        r = service.components_alternatives(slug, args.items or None, actor="cli", track=True)
        print(f"✓ alternatives : {service.alternatives_note(r)}")
        for s in r["skipped"]:
            print(f"  {s['mpn']} : {s['reason']}")
    elif args.action == "progress":
        # Toujours un succès : un affichage ne doit jamais faire échouer le job.
        noted = service.components_progress(slug, args.items)
        print("✓ avancement noté" if noted else "(aucun lancement suivi : rien à noter)")
    elif args.action == "finalize":
        if not args.items:
            raise store.StoreError("chemin du fichier JSON des avis manquant")
        print(service.components_finalize(slug, Path(args.items[0])))
    elif args.action == "reset":
        print(f"✓ recherche réinitialisée (avis et statuts effacés) ; copie dans {service.reset_components(slug)}")
    elif args.action == "chapter":
        if not (args.for_slug and args.append):
            raise store.StoreError("--for <veille> et --append <rapport.md> sont requis")
        result = service.components_chapter(slug, args.for_slug, Path(args.append))
        if result is None:
            print(f"avertissement : veille composants « {slug} » introuvable — chapitre ignoré")
        else:
            prefix, xlsx = result
            print("✓ chapitre composants ajouté")
            if prefix:
                print(f"PREFIXE_SUJET={prefix}")
            if xlsx:  # sinon (échec tracé dans l'audit) : e-mail envoyé sans la liste Excel
                print(f"PIECE_JOINTE={xlsx}")
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
              f"sortie {r['output_tokens']:>8}  web {r['web_requests']:>4} req.  "
              f"{'PLEINE' if r['peak'] else 'creuse'}  {_usd(r['cost_usd'])}"
              f"  (estimation Hermes {_usd(r['hermes_estimate_usd'])})")
    print(f"mois en cours : {_usd(c['total_month_usd'])} ({c['runs_month']} run(s)) — "
          f"{days} derniers jours : {_usd(c['total_days_usd'])} — tarifs du {c['pricing_updated']}")
    return 0


def _on_off(value: str | None) -> bool | None:
    return None if value is None else value == "on"


def _research(args) -> int:
    if args.action == "status":
        st = claude_research.status()
        if not st["reachable"]:
            print(f"✗ {st['error']}")
            return 1
        print(f"✓ pont Claude joignable — {st.get('today')}/{st.get('max_per_day')} recherche(s) aujourd'hui, "
              f"{st.get('running')} en cours, {st.get('queued')} en attente, {st.get('timeout_s')} s max"
              + (f", modèle {st['model']}" if st.get("model") else ""))
        return 0
    if not args.slug:
        raise store.StoreError("slug de la veille manquant")
    if args.action == "claude":  # étape du job : la sortie est lue par l'agent (claude_research.agent_text)
        try:
            rec = claude_research.for_job(args.slug)
        except claude_research.BridgeError as exc:
            print(f"ÉCHEC de la recherche Claude : {exc}. Fais la recherche toi-même avec web_search / web_extract, "
                  "selon les thèmes du brief.")
            return 1
        print(claude_research.agent_text(rec))
        return 1 if rec.get("state") == "error" else 0
    if args.action == "ask":
        rec = service.research_start(args.slug, args.question, origin="cli")
        print(f"✓ recherche Claude {rec['stamp']} déposée auprès du pont")
        if not args.wait:
            print(f"  résultat : veillectl research show {args.slug} {rec['stamp']}")
            return 0
        rec = service.research_wait(args.slug, rec, claude_research.REUSE_S)
    elif args.action == "list":
        for r in claude_research.list_research(args.slug):
            cost = "—" if r["cost_usd"] is None else f"{r['cost_usd']:.2f} $"
            print(f"{r['stamp']}  {r['origin']:<4} {r['state']:<8} {r['items']:>3} info.  {cost:>8}  "
                  f"{r['question'] or ''}{r['error'] or ''}"[:200])
        return 0
    else:
        rec = claude_research.current(args.slug, args.stamp)
    print(json.dumps({k: v for k, v in rec.items() if k != "prompt"}, ensure_ascii=False, indent=1))
    return 1 if rec.get("state") == "error" else 0


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
    p_create.add_argument("--components-file", help="composants : « MPN ; Fabricant » par ligne")
    p_create.add_argument("--include-components", help="veille : slug de la veille composants en chapitre")
    p_create.add_argument("--batch-size", type=int)
    p_create.add_argument("--claude-research", choices=("on", "off"), default="off",
                          help="recherche déléguée à Claude Code sur l'hôte")

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
    p_edit.add_argument("--claude-research", choices=("on", "off"), help="recherche déléguée à Claude Code sur l'hôte")

    p_comp = sub.add_parser("components", help="veille composants : PCN et fins de vie")
    p_comp.add_argument("action", choices=("list", "notices", "add", "remove", "import", "scan", "progress",
                                           "finalize", "chapter", "reset", "alternatives"))
    p_comp.add_argument("slug")
    p_comp.add_argument("items", nargs="*", help="add : lignes « MPN;Fabricant » ; remove, progress, alternatives : "
                                                  "MPN ; finalize : fichier JSON")
    p_comp.add_argument("--file", help="add : fichier « MPN ; Fabricant » (collage tableur accepté)")
    p_comp.add_argument("--type", choices=tuple(components.TYPES), help="notices : filtre par type")
    p_comp.add_argument("--for", dest="for_slug", help="chapter : veille qui reçoit le chapitre")
    p_comp.add_argument("--append", help="chapter : rapport Markdown du jour de cette veille")
    imp = p_comp.add_argument_group("import (fichier CSV ou Excel)")
    imp.add_argument("--sheet", help="feuille Excel (défaut : la première non vide)")
    imp.add_argument("--header-row", help="ligne d'en-tête (défaut : détectée ; 0 = aucune)")
    imp.add_argument("--mpn", help="colonne du MPN : libellé, lettre (A…) ou numéro")
    imp.add_argument("--manufacturer", help="colonne du fabricant")
    imp.add_argument("--ref", help=argparse.SUPPRESS)  # colonne de la réf. interne (avant 0.5.0) : acceptée, ignorée
    imp.add_argument("--default-manufacturer", help="fabricant si la colonne ou la cellule est vide")
    imp.add_argument("--replace", action="store_true", help="remplacer la liste (défaut : ajouter)")
    imp.add_argument("--dry-run", action="store_true", help="afficher la proposition sans rien enregistrer")
    p_comp.add_argument("--status-only", action="store_true",
                        help="scan : statut Digi-Key seulement (le lot de la prochaine exécution est conservé)")

    p_report = sub.add_parser("report", help="veille thématique : publication du rapport du jour")
    p_report.add_argument("action", choices=("publish",))
    p_report.add_argument("slug")
    p_report.add_argument("md", help="rapports/AAAA-MM-JJ-HHhMM-veille.md (à côté : -charts.json, -mail.json)")

    p_res = sub.add_parser("research", help="recherche déléguée à Claude Code sur l'hôte")
    p_res.add_argument("action", choices=("claude", "ask", "list", "show", "status"))
    p_res.add_argument("slug", nargs="?")
    p_res.add_argument("stamp", nargs="?", help="show : recherche AAAAMMJJ-HHMMSS (défaut : la plus récente)")
    p_res.add_argument("--question", help="ask : question précise, dans le périmètre du brief")
    p_res.add_argument("--wait", action="store_true", help="ask : attendre le résultat")

    p_costs = sub.add_parser("costs", help="consommation de tokens et coût des exécutions")
    p_costs.add_argument("slug", nargs="?")
    p_costs.add_argument("--days", type=int, default=30)

    p_cat = sub.add_parser("catalog", help="base des composants partagée entre veilles")
    p_cat.add_argument("action", choices=("rebuild", "stats"))

    sub.add_parser("reconcile")
    sub.add_parser("migrate")
    sub.add_parser("bootstrap")
    sub.add_parser("env-sync", help="recopie les secrets de l'environnement vers le .env du volume")
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
        if args.cmd == "research":
            return _research(args)
        if args.cmd == "catalog":
            st = service.rebuild_catalog() if args.action == "rebuild" else catalog.stats()
            if "database" in st:
                print(st["database"])
            print(f"✓ base des composants : {st['parts']} référence(s), {st['notices']} avis, "
                  f"{st['sources']} veille(s) (actives et archivées)")
            for line in st.get("skipped", []):
                print(f"  ignorée — {line}")
            return 0
        if args.cmd == "create":
            if args.kind == "composants" and not args.brief_file:
                args.template = service.COMPONENTS_TEMPLATE
            v = service.create_veille(
                name=args.name, slug=args.slug, subject=args.subject,
                schedule_expr=args.schedule, recipients=store.parse_recipients(args.recipients),
                brief=_brief_from(args), model=args.model, paused=args.paused, kind=args.kind,
                components=_components_file(args.components_file),
                include_components=args.include_components, batch_size=args.batch_size,
                claude_research=args.claude_research == "on")
            print(f"✓ veille créée : {v.slug} (job {v.job_id}) — {schedule.describe(v.schedule)}")
        elif args.cmd == "edit":
            v = service.update_veille(
                args.slug, name=args.name, subject=args.subject, schedule_expr=args.schedule,
                recipients=store.parse_recipients(args.recipients) if args.recipients else None,
                brief=_brief_from(args), model=args.model,
                components=_components_file(args.components_file),
                include_components=args.include_components, batch_size=args.batch_size,
                claude_research=_on_off(args.claude_research))
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
        elif args.cmd == "report":
            print(service.report_publish(args.slug, Path(args.md)))
        elif args.cmd == "migrate":
            slug = service.migrate_legacy()
            print(f"✓ migration effectuée -> {slug}" if slug else "rien à migrer")
        elif args.cmd == "bootstrap":
            slug = service.bootstrap_default()
            print(f"✓ veille par défaut créée : {slug}" if slug else "rien à amorcer")
        elif args.cmd == "env-sync":
            written = envfile.sync_env_to_file()
            print("✓ secrets synchronisés : " + (", ".join(written) if written else "aucun (environnement vide)"))
        elif args.cmd == "reconcile":
            for line in service.reconcile_all():
                print(line)
    except (store.StoreError, schedule.ScheduleError, service.ServiceError, hermes.HermesError, *db.ERRORS) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
