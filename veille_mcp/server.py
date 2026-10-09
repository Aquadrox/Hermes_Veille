"""Outils MCP : lecture (readOnlyHint) et modification des veilles.

Les outils de lecture sont annotés `readOnlyHint` : avec `trust: untrusted` côté Hermes,
eux seuls s'exécutent sans approbation ; toute modification demande l'accord de
l'utilisateur (boutons d'approbation dans Telegram).
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from veille_core import bom_import, components, deepseek, hermes, paths, pricing, schedule, service, store, version
from veille_core.store import StoreError

USER_ERRORS = (StoreError, schedule.ScheduleError, service.ServiceError, hermes.HermesError)
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
SEND = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
DESTROY = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

Frequency = Literal["daily", "weekdays", "weekly", "monthly", "cron"]
Kind = Literal["veille", "composants"]
ImportMode = Literal["add", "replace"]
NoticeType = Literal["EOL", "PDN", "OBSOLETE", "LTB", "NRND", "PCN", "AUTRE"]
DAY_NAMES = {
    "dimanche": 0, "lundi": 1, "mardi": 2, "mercredi": 3, "jeudi": 4, "vendredi": 5, "samedi": 6,
    "sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5, "saturday": 6,
}
MAX_TEXT = 20_000

INSTRUCTIONS = """\
Gestion des veilles technologiques automatisées (rapports PDF envoyés par e-mail).

- Chaque veille a un identifiant `slug` : commence par `list_veilles` pour le trouver.
- Une veille = un sujet, un brief de mission (le prompt éditable du rapport), une fréquence,
  des destinataires (envoi en copie cachée) et des rapports datés.
- `check_component_statuses` (veille composants) vérifie le statut Digi-Key sans LLM ni e-mail.
- `run_veille_now` et `send_test_email` envoient de VRAIS e-mails aux destinataires :
  confirme avec l'utilisateur avant de les appeler.
- Pour modifier une partie du brief, préfère `edit_brief` (remplacement ciblé) à
  `update_brief` (remplacement complet).
- `archive_veille` retire la veille du planning (rien n'est effacé) ; il exige de répéter le slug.
- Heures exprimées à l'heure de Paris.
- Veilles COMPOSANTS (`kind` = composants) : surveillance des avis PCN et des fins de vie (EOL,
  PDN, obsolescence, dernière commande LTB, NRND) d'une liste de références « MPN ; Fabricant ;
  Réf. interne ». `list_notices` donne les avis détectés (fins de vie en tête, avec leurs liens
  sources) ; `list_components` l'état de chaque référence. Les FINS DE VIE sont l'information la
  plus critique : présente-les toujours en premier. `include_components` (sur une veille
  thématique) ajoute ces avis en chapitre de son e-mail.
- IMPORT d'une nomenclature (CSV ou Excel) : dépose le fichier reçu dans le dossier d'import du
  serveur (chemin donné par `preview_component_import` s'il est introuvable), appelle
  `preview_component_import`, MONTRE la correspondance proposée (colonnes MPN / Fabricant /
  Réf. interne, aperçu, lignes ignorées) et attends l'accord de l'utilisateur ou ses corrections
  avant `import_components` (mode add par défaut ; replace remplace toute la liste).
- Clés d'API des distributeurs (Digi-Key) : JAMAIS par la conversation. Elles se saisissent dans
  l'interface web, page « Sources » ; `health` indique seulement si elles sont configurées.
- Coûts : `get_costs` donne les tokens et le coût en USD de chaque exécution. DeepSeek facture
  le double en heures pleines (lun–ven 01h–04h et 06h–10h UTC) ; `preview_schedule` le signale.
"""


# --------------------------------------------------------------------------
# Outils internes
# --------------------------------------------------------------------------
def _actor() -> str:
    return os.environ.get("VEILLE_MCP_ACTOR", "mcp")


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except USER_ERRORS as exc:
        raise ToolError(str(exc)) from None


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="minutes")
    return str(value)


def _host_path(path) -> str | None:
    """Chemin du fichier côté hôte (si VEILLE_HOST_DATA est fourni), pour l'envoyer en pièce jointe."""
    host = os.environ.get("VEILLE_HOST_DATA")
    if not host:
        return None
    rel = os.path.relpath(str(path), str(paths.hermes_home()))
    return os.path.join(host, rel)


def _schedule_expr(frequency: Frequency | None, time: str | None, day_of_week: str | int | None,
                   day_of_month: int | None, cron: str | None) -> str | None:
    """Paramètres lisibles -> expression cron (None si aucune fréquence demandée)."""
    if frequency is None and cron is None:
        return None
    if cron is not None and frequency in (None, "cron"):
        return schedule.to_cron({"kind": "cron", "expr": cron})
    hh, _, mm = (time or "08:00").partition(":")
    dow = day_of_week if day_of_week is not None else 1
    if isinstance(dow, str) and not dow.isdigit():
        if dow.strip().lower() not in DAY_NAMES:
            raise schedule.ScheduleError(f"jour inconnu : {dow!r} (ex. « lundi »)")
        dow = DAY_NAMES[dow.strip().lower()]
    return schedule.to_cron({"kind": frequency, "hour": hh, "minute": mm or 0, "dow": dow,
                             "day": day_of_month if day_of_month is not None else 1})


def _summary(row: dict) -> dict:
    v, st = row["veille"], row["status"]
    return {
        "slug": v.slug,
        "name": v.name,
        "subject": v.subject,
        "state": "en cours" if row["running"] else st["state"],
        "frequency": row["schedule_human"],
        "cron": v.schedule,
        "next_run": st.get("next_run_at"),
        "last_run": st.get("last_run_at"),
        "last_status": st.get("last_status"),
        "recipients_count": len(row["recipients"]),
        "last_report": row["last_report"]["date"] if row["last_report"] else None,
        "kind": v.kind,
        **({"components": row["components"]} if row["components"] else {}),
        **({"include_components": v.components} if v.components else {}),
    }


# --------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------
def list_veilles() -> list[dict]:
    """Liste toutes les veilles avec leur état (actif / en pause / en cours), leur fréquence,
    le prochain et le dernier run, et la date du dernier rapport."""
    return [_summary(r) for r in service.overview()]


def get_veille(slug: str) -> dict:
    """Détail d'une veille : fréquence, prochaines exécutions, destinataires, dernier run et
    dernière erreur, options du PDF, derniers rapports."""
    v = _guard(store.load, slug)
    row = service.describe(v)
    st = row["status"]
    return {
        **_summary(row),
        "recipients": row["recipients"],
        "next_runs": [] if st["paused"] else [_iso(d) for d in schedule.next_runs(v.schedule, 3)],
        "last_error": st.get("last_error"),
        "job_id": v.job_id,
        "model": v.model,
        "pdf": {"title": v.pdf.title, "header": v.pdf.header, "footer": v.pdf.footer,
                "audience": v.pdf.audience, "accent": v.pdf.accent},
        "alert_after_days": round(v.max_age_hours / 24, 1),
        "reports": [r["date"] for r in store.list_reports(slug)[:5]],
    }


def get_brief(slug: str) -> str:
    """Texte complet du brief de mission (Markdown) d'une veille : périmètre, thèmes, règles,
    structure et mise en forme du rapport."""
    _guard(store.load, slug)
    return store.read_brief(slug)


def list_brief_templates() -> list[str]:
    """Modèles de brief disponibles pour créer une veille (ex. « vierge », « industrie-4-0 » ;
    « composants » : brief de recherche d'une veille composants)."""
    return sorted(store.brief_templates())


def list_reports(slug: str, limit: int = 10) -> list[dict]:
    """Rapports d'une veille, du plus récent au plus ancien (date, présence du PDF et du Markdown)."""
    _guard(store.load, slug)
    out = []
    for r in store.list_reports(slug)[: max(1, min(limit, 100))]:
        item = {"date": r["date"], "pdf": r.get("pdf"), "markdown": r.get("md"), "size_kb": round((r.get("size") or 0) / 1024)}
        if r.get("pdf"):
            item["pdf_host_path"] = _host_path(store.report_path(slug, r["pdf"]))
        out.append(item)
    return out


def read_report(slug: str, date: str | None = None, max_chars: int = MAX_TEXT) -> dict:
    """Contenu Markdown d'un rapport (le plus récent si `date` est omise, format AAAA-MM-JJ) :
    pour le résumer, en extraire des points ou répondre à une question sur la veille."""
    _guard(store.load, slug)
    reports = store.list_reports(slug)
    if date:
        reports = [r for r in reports if r["date"] == date]
    report = next((r for r in reports if r.get("md")), None)
    if report is None:
        raise ToolError("aucun rapport Markdown " + (f"daté du {date}" if date else "pour cette veille"))
    text = store.report_path(slug, report["md"]).read_text(encoding="utf-8", errors="replace")
    limit = max(1000, min(max_chars, 100_000))
    pdf = report.get("pdf")
    return {"date": report["date"], "truncated": len(text) > limit, "markdown": text[:limit],
            "pdf_host_path": _host_path(store.report_path(slug, pdf)) if pdf else None}


def get_run_log(slug: str, name: str | None = None, max_chars: int = 6000) -> dict:
    """Journal d'un lancement manuel (le plus récent si `name` est omis) et indique s'il est
    encore en cours."""
    _guard(store.load, slug)
    logs = store.list_run_logs(slug, 20)
    name = name or (logs[0] if logs else None)
    if not name:
        raise ToolError("aucun lancement manuel pour cette veille")
    path = _guard(store.run_log_path, slug, name)
    text = path.read_text(encoding="utf-8", errors="replace")
    running = bool(logs) and name == logs[0] and service.running_pid(slug) is not None
    return {"name": name, "running": running, "log": text[-max(500, min(max_chars, 50_000)):]}


def run_history(slug: str, limit: int = 10) -> list[dict]:
    """Historique d'exécution du job selon Hermes (date, statut, origine, erreur)."""
    v = _guard(store.load, slug)
    return hermes.runs(v.job_id, max(1, min(limit, 50))) if v.job_id else []


def preview_schedule(frequency: Frequency = "weekly", time: str = "08:00",
                     day_of_week: str | int | None = None, day_of_month: int | None = None,
                     cron: str | None = None) -> dict:
    """Vérifie une fréquence avant de l'appliquer : libellé, expression cron et 3 prochaines
    exécutions. `frequency` : daily | weekdays | weekly | monthly | cron ; `time` « HH:MM » ;
    `day_of_week` (weekly) « lundi »… ou 0–6 (0 = dimanche) ; `day_of_month` (monthly) 1–28 ;
    `cron` : expression 5 champs (frequency = cron)."""
    expr = _guard(_schedule_expr, frequency, time, day_of_week, day_of_month, cron)
    runs = schedule.next_runs(expr, 3)
    peak = pricing.is_peak(runs[0])
    return {"cron": expr, "frequency": schedule.describe(expr),
            "next_runs": [_iso(d) for d in runs],
            "deepseek_peak": peak,
            "note": ("heure pleine DeepSeek : tarif ×2 (préférer 6h–8h l'été / 5h–7h l'hiver, après 12h, "
                     "ou le week-end)") if peak else "heure creuse DeepSeek"}


def list_components(slug: str) -> list[dict]:
    """Références suivies par une veille composants : MPN, fabricant, réf. interne, statut
    Digi-Key, avis le plus critique, dates des dernières vérifications (Digi-Key et web)."""
    v = _guard(service._components_veille, slug)
    comps = _guard(components.load_components, v.slug)["components"]
    worst = components.worst_by_key(_guard(components.load_notices, v.slug))
    out = []
    for c in comps:
        dk, w = c.get("dk") or {}, worst.get(components.key_of(c["mpn"], c["manufacturer"]))
        out.append({"mpn": c["mpn"], "manufacturer": c["manufacturer"], "ref": c.get("ref", ""),
                    "digikey_status": dk.get("status"), "digikey_error": dk.get("error"),
                    "digikey_checked_at": dk.get("checked_at"), "web_checked_at": c.get("web_checked_at"),
                    "worst_notice": w["type"] if w else None})
    return out


def list_notices(slug: str | None = None, type: NoticeType | None = None, since: str | None = None,
                 limit: int = 50) -> list[dict]:
    """Avis PCN / fin de vie détectés, FINS DE VIE EN TÊTE puis les plus récents : type, référence,
    intitulé, numéro, dates (publication, dernière commande, fin de vie), résumé et liens sources.
    `slug` omis : toutes les veilles composants. `since` (AAAA-MM-JJ) : détectés depuis cette date."""
    slugs = [slug] if slug else [v.slug for v in store.list_all() if v.kind == "composants"]
    rows = []
    for s in slugs:
        rows += [{**n, "veille": s} for n in _guard(service.notices, s, type, since)]
    rows = components.for_display(rows)[: max(1, min(limit, 500))]
    return [{"veille": n["veille"], "type": n["type"], "type_label": components.TYPES[n["type"]][0],
             "critical": n["type"] in components.CRITICAL, "mpn": n["mpn"], "manufacturer": n["manufacturer"],
             "title": n["title"], "number": n.get("number"), "scope": n.get("scope"),
             "published": n.get("published"), "ltb_date": n.get("ltb_date"), "eol_date": n.get("eol_date"),
             "summary": n.get("summary"), "sources": n["sources"], "origin": n.get("origin"),
             "found_at": n["found_at"]} for n in rows]


def _import_proposal(filename: str, sheet: str | None, header_row: int | None, mpn_column: str | None,
                     manufacturer_column: str | None, ref_column: str | None,
                     default_manufacturer: str | None) -> dict:
    try:
        path = bom_import.import_path(filename)
    except StoreError as exc:
        where = _host_path(paths.imports_dir()) or str(paths.imports_dir())
        raise ToolError(f"{exc} — dossier d'import sur le serveur : {where}") from None
    columns = {f: v for f, v in (("mpn", mpn_column), ("manufacturer", manufacturer_column),
                                 ("ref", ref_column)) if v is not None}
    return _guard(lambda: bom_import.propose_options(bom_import.read_path(path, sheet), header_row, columns,
                                                     default_manufacturer or ""))


def preview_component_import(filename: str, sheet: str | None = None, header_row: int | None = None,
                             mpn_column: str | None = None, manufacturer_column: str | None = None,
                             ref_column: str | None = None, default_manufacturer: str | None = None) -> dict:
    """Analyse une nomenclature (CSV ou Excel) déposée dans le dossier d'import et PROPOSE la
    correspondance des colonnes (MPN, fabricant, réf. interne), reconnue par règles, sans rien
    enregistrer : feuilles, en-têtes, colonnes retenues et leur confiance, aperçu, lignes ignorées.
    `header_row` : 0 = pas d'en-tête, n = ligne n (omis = détection) ; colonnes : libellé, lettre ou
    numéro, pour corriger la proposition."""
    p = _import_proposal(filename, sheet, header_row, mpn_column, manufacturer_column, ref_column,
                         default_manufacturer)
    return {"filename": filename, "sheets": p["sheets"], "sheet": p["sheet"],
            "header_row": p["header_row"] + 1 if p["header_row"] is not None else 0,
            "columns": [f"{bom_import.column_name(i)} : {c}" for i, c in enumerate(p["columns"])],
            "mapping": {f: {"column": (f"{bom_import.column_name(m['col'])} : {p['columns'][m['col']]}"
                                       if m["col"] is not None else None), "confidence": m["confidence"]}
                        for f, m in p["mapping"].items()},
            "error": p["error"], "valid": len(p["components"]), "data_rows": p["rows"],
            "preview": p["preview"], "rejected": p["rejected"][:20], "rejected_count": len(p["rejected"])}


def _cost_run(r: dict) -> dict:
    return {"date": _iso(r["started_at"]), "duration_min": r["duration_min"], "models": r["models"],
            "input_tokens": r["input_tokens"] + r["cache_read_tokens"],
            "input_cached_tokens": r["cache_read_tokens"], "output_tokens": r["output_tokens"],
            "reasoning_tokens": r["reasoning_tokens"], "api_calls": r["api_calls"],
            "peak_hours": r["peak"], "cost_usd": r["cost_usd"],
            "hermes_estimate_usd": r["hermes_estimate_usd"]}


def get_costs(slug: str | None = None, days: int = 30) -> dict:
    """Tokens consommés et coût (USD) des exécutions : détail par exécution pour une veille
    (`slug`), sinon total du mois et des `days` derniers jours pour chaque veille. Le coût tient
    compte des heures pleines DeepSeek (×2) ; `hermes_estimate_usd` est l'estimation de Hermes,
    au seul tarif creux."""
    days = max(1, min(days, 366))
    if slug is None:
        return service.costs_overview(days)
    c = _guard(service.costs, slug, days)
    return {**{k: val for k, val in c.items() if k != "runs"}, "runs": [_cost_run(r) for r in c["runs"][:30]]}


def health() -> dict:
    """État général : version, veilles en échec ou sans job, solde DeepSeek, coût du mois,
    dernières actions du journal d'audit."""
    rows = service.overview()
    problems = []
    for r in rows:
        st, v = r["status"], r["veille"]
        if not st["exists"]:
            problems.append(f"{v.slug} : job Hermes absent")
        elif not st["paused"] and st.get("last_status") not in (None, "", "ok"):
            problems.append(f"{v.slug} : dernier run {st['last_status']} — {st.get('last_error') or ''}".strip())
    return {"version": version(), "veilles": len(rows),
            "actives": sum(1 for r in rows if r["status"]["exists"] and not r["status"]["paused"]),
            "problems": problems, "deepseek_balance": deepseek.balance(),
            "sources": [{"name": s["name"], "configured": s["configured"], "from_env": s["env"]}
                        for s in service.sources_status()],
            "cost_month_usd": service.costs_overview()["total_month_usd"],
            "audit": service.read_audit(15)}


# --------------------------------------------------------------------------
# Écriture
# --------------------------------------------------------------------------
def create_veille(name: str, subject: str, recipients: list[str], frequency: Frequency | None = None,
                  time: str | None = None, day_of_week: str | int | None = None,
                  day_of_month: int | None = None, cron: str | None = None,
                  template: str = "vierge", brief: str | None = None, slug: str | None = None,
                  paused: bool = False, pdf_title: str | None = None, pdf_footer: str | None = None,
                  model: str | None = None, kind: Kind = "veille", components_list: list[str] | None = None,
                  include_components: str | None = None) -> dict:
    """Crée une veille. Le brief vient de `brief` (Markdown complet) ou du modèle `template`
    (les champs <NOM DE LA VEILLE> et <DOMAINE> du modèle « vierge » sont remplis avec `name`
    et `subject` ; complète ensuite la section « Couverture » avec `edit_brief`).
    Fréquence par défaut : chaque lundi à 8h (veille composants : dimanche 23h, heures creuses).
    `kind="composants"` : veille PCN / fins de vie ; `components_list` = lignes « MPN ; Fabricant ;
    Réf. interne » (le modèle de brief « composants » est appliqué). `include_components` : slug
    d'une veille composants dont les avis seront ajoutés en chapitre de l'e-mail de cette veille.
    `paused=True` crée la veille sans la planifier."""
    if kind == "composants" and frequency is None and cron is None and time is None and day_of_week is None:
        expr = service.COMPONENTS_SCHEDULE
    else:
        expr = _guard(_schedule_expr, frequency or (None if cron else "weekly"), time or "08:00",
                      "lundi" if day_of_week is None else day_of_week, day_of_month, cron)
    addresses = _guard(store.parse_recipients, "\n".join(recipients))
    if kind == "composants" and brief is None and template == "vierge":
        template = service.COMPONENTS_TEMPLATE
    parsed = _guard(components.parse_components, "\n".join(components_list or []))
    if brief is None:
        templates = store.brief_templates()
        if template not in templates:
            raise ToolError(f"modèle inconnu : {template} (disponibles : {', '.join(sorted(templates))})")
        brief = templates[template].replace("<NOM DE LA VEILLE>", name).replace("<DOMAINE>", subject or name)
    pdf = {k: val for k, val in (("title", pdf_title), ("footer", pdf_footer)) if val}
    v = _guard(service.create_veille, name=name, subject=subject, schedule_expr=expr,
               recipients=addresses, brief=brief, pdf=pdf, slug=slug, model=model,
               paused=paused, kind=kind, components=parsed if kind == "composants" else None,
               include_components=include_components, actor=_actor())
    return get_veille(v.slug)


def update_veille(slug: str, name: str | None = None, subject: str | None = None,
                  frequency: Frequency | None = None, time: str | None = None,
                  day_of_week: str | int | None = None, day_of_month: int | None = None,
                  cron: str | None = None, pdf_title: str | None = None, pdf_header: str | None = None,
                  pdf_footer: str | None = None, pdf_audience: str | None = None,
                  model: str | None = None, include_components: str | None = None,
                  batch_size: int | None = None) -> dict:
    """Modifie une veille : seuls les champs fournis changent. Pour la fréquence, fournir
    `frequency` (et `time`, `day_of_week` ou `day_of_month` selon le cas) ou `cron`.
    `include_components` : slug d'une veille composants à inclure en chapitre ("" pour retirer).
    `batch_size` (veille composants) : nombre de recherches web par exécution."""
    _guard(store.load, slug)
    expr = _guard(_schedule_expr, frequency, time, day_of_week, day_of_month, cron)
    pdf = {k: val for k, val in (("title", pdf_title), ("header", pdf_header), ("footer", pdf_footer),
                                 ("audience", pdf_audience)) if val is not None}
    _guard(service.update_veille, slug, name=name, subject=subject, schedule_expr=expr,
           pdf=pdf or None, model=model, include_components=include_components,
           batch_size=batch_size, actor=_actor())
    return get_veille(slug)


def set_recipients(slug: str, recipients: list[str]) -> dict:
    """Remplace la liste complète des destinataires (envoi en copie cachée)."""
    addresses = _guard(store.parse_recipients, "\n".join(recipients))
    _guard(service.update_veille, slug, recipients=addresses, actor=_actor())
    return {"slug": slug, "recipients": store.read_recipients(slug)}


def add_recipients(slug: str, recipients: list[str]) -> dict:
    """Ajoute des destinataires (les doublons sont ignorés)."""
    current = _guard(store.read_recipients, slug)
    _guard(store.load, slug)
    addresses = _guard(store.parse_recipients, "\n".join(current + list(recipients)))
    _guard(service.update_veille, slug, recipients=addresses, actor=_actor())
    return {"slug": slug, "recipients": store.read_recipients(slug)}


def remove_recipients(slug: str, recipients: list[str]) -> dict:
    """Retire des destinataires. Au moins un destinataire doit rester."""
    _guard(store.load, slug)
    drop = {a.strip().lower() for a in recipients}
    current = store.read_recipients(slug)
    unknown = [a for a in recipients if a.strip().lower() not in {c.lower() for c in current}]
    if unknown:
        raise ToolError("adresse(s) absente(s) de la veille : " + ", ".join(unknown))
    _guard(service.update_veille, slug, recipients=[a for a in current if a.lower() not in drop],
           actor=_actor())
    return {"slug": slug, "recipients": store.read_recipients(slug)}


def update_brief(slug: str, brief: str) -> dict:
    """Remplace TOUT le brief de mission par `brief` (Markdown). Pour une modification
    ponctuelle, utiliser plutôt `edit_brief`."""
    _guard(service.update_veille, slug, brief=brief, actor=_actor())
    return {"slug": slug, "chars": len(store.read_brief(slug))}


def edit_brief(slug: str, find: str, replace: str) -> dict:
    """Modification ciblée du brief : remplace le passage exact `find` (qui doit apparaître
    une seule fois) par `replace`. Utiliser `get_brief` pour copier le passage exact."""
    _guard(store.load, slug)
    text = store.read_brief(slug)
    count = text.count(find) if find else 0
    if count != 1:
        raise ToolError(f"le passage à remplacer doit apparaître exactement une fois (trouvé {count} fois)")
    _guard(service.update_veille, slug, brief=text.replace(find, replace, 1), actor=_actor())
    return {"slug": slug, "chars": len(store.read_brief(slug))}


def add_components(slug: str, components_list: list[str]) -> dict:
    """Ajoute des références à une veille composants : lignes « MPN ; Fabricant ; Réf. interne »
    (réf. facultative). Les doublons sont ignorés."""
    items = _guard(components.parse_components, "\n".join(components_list))
    added = _guard(service.add_components, slug, items, actor=_actor())
    return {"slug": slug, "added": added, "total": len(components.load_components(slug)["components"])}


def remove_components(slug: str, mpns: list[str]) -> dict:
    """Retire des références (par MPN) d'une veille composants ; l'historique des avis est conservé."""
    removed = _guard(service.remove_components, slug, mpns, actor=_actor())
    return {"slug": slug, "removed": removed, "total": len(components.load_components(slug)["components"])}


def import_components(slug: str, filename: str, mode: ImportMode = "add", sheet: str | None = None,
                      header_row: int | None = None, mpn_column: str | None = None,
                      manufacturer_column: str | None = None, ref_column: str | None = None,
                      default_manufacturer: str | None = None) -> dict:
    """Importe dans une veille composants la nomenclature du dossier d'import, avec la
    correspondance validée par l'utilisateur (mêmes options que `preview_component_import`, à
    appeler d'abord). `mode` : add (ajout, doublons ignorés) ou replace (la liste devient celle du
    fichier ; l'historique des avis est conservé). Les lignes invalides sont ignorées."""
    p = _import_proposal(filename, sheet, header_row, mpn_column, manufacturer_column, ref_column,
                         default_manufacturer)
    if p["error"]:
        raise ToolError(p["error"])
    st = _guard(service.import_components, slug, p["components"], mode, actor=_actor())
    return {"slug": slug, **st, "ignored_rows": len(p["rejected"])}


def pause_veille(slug: str) -> dict:
    """Met une veille en pause : elle n'est plus exécutée ni surveillée jusqu'à sa reprise."""
    _guard(service.pause, slug, actor=_actor())
    return get_veille(slug)


def resume_veille(slug: str) -> dict:
    """Reprend une veille en pause : elle repart selon sa fréquence."""
    _guard(service.resume, slug, actor=_actor())
    return get_veille(slug)


def run_veille_now(slug: str) -> dict:
    """Lance la veille immédiatement, en arrière-plan : recherche, rapport PDF et ENVOI aux
    destinataires (quelques minutes). Veille composants : analyse complète (statut Digi-Key,
    recherche web, rapport) ; si elle est rattachée à une veille (`include_components`), AUCUN
    e-mail : ses nouveaux avis partiront dans le prochain e-mail de cette veille. Suivre
    l'avancement avec `get_run_log`."""
    log = _guard(service.run_now, slug, actor=_actor())
    return {"slug": slug, "started": True, "log": log,
            "hint": "appeler get_run_log dans quelques minutes pour suivre l'exécution"}


def check_component_statuses(slug: str) -> dict:
    """Veille composants : vérifie TOUT DE SUITE le statut de cycle de vie Digi-Key de toutes les
    références (Active, Last Time Buy, NRND, Obsolete…), en arrière-plan, sans LLM, sans coût en
    tokens et sans e-mail. Utile après un ajout de composants. Les fins de vie détectées
    apparaissent ensuite dans `list_notices` ; suivre l'avancement avec `get_run_log`."""
    log = _guard(service.check_statuses_now, slug, actor=_actor())
    return {"slug": slug, "started": True, "log": log,
            "hint": "quelques minutes pour 300 références ; puis list_notices"}


def send_test_email(slug: str) -> dict:
    """Envoie un court e-mail de test à tous les destinataires de la veille (vérifie le canal)."""
    out = _guard(service.send_test_email, slug, actor=_actor())
    return {"slug": slug, "sent": True, "output": out[-1000:]}


def archive_veille(slug: str, confirm_slug: str) -> dict:
    """Archive une veille : supprime son job planifié et déplace ses fichiers dans les
    archives (rien n'est effacé). `confirm_slug` doit répéter exactement `slug`."""
    if confirm_slug != slug:
        raise ToolError("confirmation invalide : `confirm_slug` doit être identique à `slug`")
    dest = _guard(service.archive, slug, actor=_actor())
    return {"slug": slug, "archived_to": str(dest)}


READ_TOOLS = [list_veilles, get_veille, get_brief, list_brief_templates, list_reports, read_report,
              get_run_log, run_history, preview_schedule, get_costs, health, list_components, list_notices,
              preview_component_import]
WRITE_TOOLS = [(create_veille, WRITE), (update_veille, WRITE), (set_recipients, WRITE),
               (add_recipients, WRITE), (remove_recipients, WRITE), (update_brief, WRITE),
               (edit_brief, WRITE), (add_components, WRITE), (remove_components, WRITE),
               (import_components, DESTROY),  # mode replace : la liste existante est remplacée
               (pause_veille, WRITE), (resume_veille, WRITE),
               (check_component_statuses, WRITE), (run_veille_now, SEND), (send_test_email, SEND),
               (archive_veille, DESTROY)]


def build_server(readonly: bool = False) -> MCPServer:
    server: MCPServer[Any] = MCPServer(
        name="veilles", title="Veilles technologiques", version=version(), log_level="WARNING",
        instructions=INSTRUCTIONS + ("\nMode LECTURE SEULE : aucune modification possible.\n" if readonly else ""),
    )
    for fn in READ_TOOLS:
        server.add_tool(fn, annotations=READ)
    if not readonly:
        for fn, annotations in WRITE_TOOLS:
            server.add_tool(fn, annotations=annotations)
    return server
