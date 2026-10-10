"""Application FastAPI : routes de l'interface. Toute la logique passe par veille_core.service."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from veille_core import (
    bom_import,
    components,
    credentials,
    deepseek,
    envfile,
    hermes,
    pricing,
    schedule,
    service,
    store,
    version,
)
from veille_core.store import StoreError

from . import auth

HERE = Path(__file__).resolve().parent
ACTOR = "ui"
ANCHOR_RE = re.compile(r"c-[0-9a-f]{10}")  # identifiant d'un composant (components.anchor), fullmatch
COMP_ROWS_SHOWN = 200  # liste des composants : lignes affichées avant « tout afficher » (mobile)
USER_ERRORS = (StoreError, schedule.ScheduleError, service.ServiceError, hermes.HermesError)

# Messages affichés après redirection : codes en liste blanche (aucun texte réfléchi).
FLASH = {
    "created": "Veille créée.",
    "created-known": "Veille créée. Avis déjà connus d'autres veilles repris de la base des composants : "
                     "ils partiront avec son premier e-mail.",
    "updated": "Veille enregistrée.",
    "updated-known": "Veille enregistrée. Avis déjà connus d'autres veilles repris de la base des composants "
                     "pour les nouvelles références : ils partiront avec son prochain e-mail.",
    "paused": "Veille mise en pause.",
    "resumed": "Veille reprise.",
    "run": "Lancement démarré : son suivi s'affiche ci-dessous.",
    "checking": "Vérification des statuts Digi-Key démarrée : l'avancement s'affiche ci-dessous.",
    "reset": "Recherche réinitialisée (copie archivée) : «\u00a0Lancer l'analyse\u00a0» repart de zéro.",
    "archived": "Veille archivée.",
    "mail": "E-mail de test envoyé.",
    "creds-saved": "Clés enregistrées. Utilise « Tester la connexion » pour les vérifier.",
    "creds-unchanged": "Aucune modification : les champs laissés vides conservent leur valeur.",
    "creds-deleted": "Clés supprimées.",
    "settings-saved": "Paramètres enregistrés. Utilise « E-mail de test » pour vérifier l'adresse.",
    "settings-unchanged": "Aucune modification.",
    "alert-mail": "E-mail de test envoyé aux adresses d'alerte.",
    "contact-forgotten": "Adresse retirée du carnet (elle reste destinataire des veilles qui l'utilisent).",
}
# Fichiers statiques : URL versionnées par empreinte (?v=STATIC_V), donc mises en cache longtemps.
STATIC_CACHE = "public, max-age=31536000, immutable"
# Déjà compressés : PDF et classeurs Excel (format zip) ne passent pas par gzip.
GZIP_EXCLUDED = (*DEFAULT_EXCLUDED_CONTENT_TYPES, "application/pdf",
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

_md = MarkdownIt("commonmark", {"html": False}).enable("table")


# --------------------------------------------------------------------------
# Présentation
# --------------------------------------------------------------------------
def fmt_dt(value) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo:
        value = value.astimezone(schedule.ZONE)
    days = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
    return f"{days[value.weekday()]} {value:%d/%m/%Y %H:%M}"


def fmt_size(n) -> str:
    return f"{(n or 0) / 1024:.0f} Ko"


def fmt_usd(value) -> str:
    if value is None:
        return "—"
    return f"{value:.4f} $" if value < 0.01 else f"{value:.2f} $"


def fmt_int(value) -> str:
    return f"{int(value or 0):,}".replace(",", "\u202f")


def status_class(row: dict) -> str:
    st = row["status"]
    if not st["exists"]:
        return "bad"
    if row["running"]:
        return "run"
    if st["paused"]:
        return "paused"
    if st.get("last_status") not in (None, "", "ok"):
        return "bad"
    return "ok"


def static_version() -> str:
    """Empreinte du contenu de static/ : change dès qu'un fichier change (cache navigateur renouvelé)."""
    h = hashlib.sha256()
    for f in sorted((HERE / "static").rglob("*")):
        if f.is_file():
            h.update(f.relative_to(HERE).as_posix().encode() + b"\0" + f.read_bytes())
    return h.hexdigest()[:12]


templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["dt"] = fmt_dt
templates.env.filters["size"] = fmt_size
templates.env.filters["usd"] = fmt_usd
templates.env.filters["num"] = fmt_int
templates.env.globals["status_class"] = status_class
templates.env.globals["DAYS"] = schedule.DAYS
templates.env.globals["NOTICE_TYPES"] = components.TYPES
templates.env.globals["WEB_STATUSES"] = components.WEB_STATUSES
templates.env.globals["web_status_label"] = components.web_status_label
templates.env.globals["CRITICAL"] = components.CRITICAL
templates.env.globals["badge_class"] = components.badge_class
templates.env.globals["COMP_ROWS_SHOWN"] = COMP_ROWS_SHOWN
templates.env.globals["FIELDS"] = bom_import.FIELDS
templates.env.globals["LABELS"] = bom_import.LABELS
templates.env.globals["column_name"] = bom_import.column_name
templates.env.globals["VERSION"] = version()
templates.env.globals["STATIC_V"] = static_version()


# --------------------------------------------------------------------------
# Formulaire veille <-> champs
# --------------------------------------------------------------------------
def form_defaults(v: store.Veille | None = None, template: str = "vierge", kind: str = "veille") -> dict:
    if v is None:
        briefs = store.brief_templates()
        composants = kind == "composants"
        return {"name": "", "slug": "", "subject": "", "recipients": "", "kind": kind,
                "brief": briefs.get(service.COMPONENTS_TEMPLATE if composants else template, ""),
                "freq": schedule.from_cron(service.COMPONENTS_SCHEDULE) if composants
                else {"kind": "weekly", "dow": 1, "hour": 8, "minute": 0},
                "cron_expr": "", "pdf_title": "", "pdf_header": "", "pdf_footer": "Document interne",
                "pdf_audience": "achats, méthodes & ingénierie" if composants else "direction & ingénierie",
                "pdf_accent": "#0f766e", "model": "", "components": "", "include_components": "",
                "batch_size": store.Veille.batch_size}
    freq = schedule.from_cron(v.schedule)
    comps = components.load_components(v.slug)["components"] if v.kind == "composants" else []
    return {"name": v.name, "slug": v.slug, "subject": v.subject, "kind": v.kind,
            "recipients": "\n".join(store.read_recipients(v.slug)), "brief": store.read_brief(v.slug),
            "freq": freq, "cron_expr": v.schedule if freq["kind"] == "cron" else "",
            "pdf_title": v.pdf.title, "pdf_header": v.pdf.header, "pdf_footer": v.pdf.footer,
            "pdf_audience": v.pdf.audience, "pdf_accent": v.pdf.accent, "model": v.model or "",
            "components": components.format_components(comps), "include_components": v.components or "",
            "batch_size": v.batch_size}


def freq_from_form(form) -> dict:
    kind = form.get("freq_kind", "weekly")
    if kind == "cron":
        return {"kind": "cron", "expr": form.get("cron_expr", "")}
    hh, _, mm = (form.get("time") or "08:00").partition(":")
    return {"kind": kind, "hour": hh or 8, "minute": mm or 0,
            "dow": form.get("dow", 1), "day": form.get("day", 1)}


def values_from_form(form) -> dict:
    freq = freq_from_form(form)
    kind = form.get("kind", "veille")
    return {"name": form.get("name", "").strip(), "slug": form.get("slug", "").strip(),
            "subject": form.get("subject", "").strip(), "recipients": form.get("recipients", ""),
            "kind": kind if kind in store.KINDS else "veille",
            "brief": form.get("brief", ""), "freq": freq, "cron_expr": form.get("cron_expr", ""),
            "pdf_title": form.get("pdf_title", ""), "pdf_header": form.get("pdf_header", ""),
            "pdf_footer": form.get("pdf_footer", ""), "pdf_audience": form.get("pdf_audience", ""),
            "pdf_accent": form.get("pdf_accent", "#0f766e"), "model": form.get("model", "").strip(),
            "components": form.get("components", ""), "include_components": form.get("include_components", ""),
            "batch_size": form.get("batch_size", "") or store.Veille.batch_size}


def kind_fields(values: dict) -> dict:
    """Champs propres au type de veille, pour service.create_veille / update_veille."""
    if values["kind"] == "composants":
        return {"components": components.parse_components(values["components"]),
                "batch_size": values["batch_size"]}
    return {"include_components": values["include_components"]}


def schedule_ctx(freq: dict) -> dict:
    """Aperçu d'une fréquence (expression, libellé, prochaines exécutions) ou son erreur."""
    try:
        expr = schedule.to_cron(freq)
        runs = schedule.next_runs(expr, 3)
        return {"expr": expr, "human": schedule.describe(expr), "runs": runs, "peak": pricing.is_peak(runs[0])}
    except schedule.ScheduleError as exc:
        return {"error": str(exc)}


def component_watches() -> list[store.Veille]:
    return [x for x in store.list_all() if x.kind == "composants"]


def pdf_from_values(values: dict) -> dict:
    return {k[4:]: values[k] for k in ("pdf_title", "pdf_header", "pdf_footer", "pdf_audience", "pdf_accent")}


# --------------------------------------------------------------------------
# Santé
# --------------------------------------------------------------------------
def brevo_account() -> dict:
    key = envfile.get("BREVO_API_KEY")
    if not key:
        return {"error": "BREVO_API_KEY absente"}
    req = urllib.request.Request("https://api.brevo.com/v3/account",
                                 headers={"api-key": key, "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001 - simple affichage
        return {"error": f"API Brevo injoignable ({type(exc).__name__})"}
    plans = [{"type": p.get("type"), "credits": p.get("credits"), "credits_type": p.get("creditsType")}
             for p in data.get("plan", [])]
    return {"email": data.get("email"), "plans": plans}


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
def create_app() -> FastAPI:
    settings = auth.Settings()
    app = FastAPI(title="Veilles", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        public = path in ("/login", "/healthz") or path.startswith("/static/")
        session = auth.read_session(settings, request.cookies.get(auth.COOKIE))
        request.state.session = session
        if not public and session is None:
            if request.headers.get("HX-Request"):
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            return RedirectResponse("/login", status_code=303)
        if request.method == "POST" and path != "/login":
            token = request.headers.get("X-CSRF-Token")
            if token is None and request.headers.get("content-type", "").startswith(
                    "application/x-www-form-urlencoded"):
                # body() (et non form()) : Starlette rejoue le corps mis en cache pour la route.
                fields = parse_qs((await request.body()).decode("utf-8", "replace"))
                token = (fields.get("csrf") or [None])[0]
            if not auth.csrf_ok(session or {}, token):
                return PlainTextResponse("jeton CSRF invalide — recharge la page", status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        static = path.startswith("/static/") and response.status_code == 200
        response.headers.setdefault("Cache-Control", STATIC_CACHE if static else "no-store")
        return response

    # Ajoutée après `guard` : enveloppe toute la pile (les en-têtes ci-dessus sont déjà posés).
    app.add_middleware(GZipMiddleware, minimum_size=1024, exclude_content_types=GZIP_EXCLUDED)

    def render(request: Request, template: str, status_code: int = 200, /, **ctx):
        session = request.state.session or {}
        flash = FLASH.get(request.query_params.get("ok", ""))
        return templates.TemplateResponse(request, template, {"csrf": session.get("csrf", ""), "flash": flash, **ctx},
                                          status_code=status_code)

    # -------------------------------------------------------------- auth
    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return render(request, "login.html", configured=settings.configured)

    @app.post("/login")
    async def login(request: Request):
        key = request.client.host if request.client else "?"
        if auth.throttled(key):
            return render(request, "login.html", 429, configured=True,
                          error="Trop de tentatives : réessaie dans 15 minutes.")
        form = await request.form()
        if not auth.verify_password(settings, str(form.get("password", ""))):
            auth.record_failure(key)
            service.audit(ACTOR, "login-failed", detail=key)
            return render(request, "login.html", 401, configured=settings.configured,
                          error="Mot de passe incorrect.")
        auth.reset_failures(key)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(auth.COOKIE, auth.new_session(settings), max_age=auth.SESSION_MAX_AGE,
                        httponly=True, samesite="strict", secure=settings.cookie_secure)
        service.audit(ACTOR, "login", detail=key)
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE)
        return resp

    # -------------------------------------------------------------- tableau de bord
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return render(request, "dashboard.html", rows=service.overview())

    def row_response(request: Request, slug: str, error: str | None = None):
        row = service.describe(store.load(slug))
        return render(request, "partials/row.html", 400 if error else 200, row=row, error=error)

    @app.get("/veilles/{slug}/row", response_class=HTMLResponse)
    def dashboard_row(request: Request, slug: str):
        """Ligne du tableau de bord, ré-interrogée pendant un lancement (avancement, puis état final)."""
        try:
            return row_response(request, slug)
        except StoreError:  # veille archivée entre-temps : 286 vide, htmx retire la ligne et arrête d'interroger
            return Response(status_code=286)

    # -------------------------------------------------------------- création / édition
    def form_page(request: Request, values: dict, status_code: int = 200, /, **ctx):
        briefs = [t for t in store.brief_templates() if t != service.COMPONENTS_TEMPLATE]
        # Aperçu de la fréquence rendu avec la page : pas d'aller-retour supplémentaire au chargement.
        return render(request, "veille_form.html", status_code, values=values, watches=component_watches(),
                      templates_=briefs if values["kind"] == "veille" else [],
                      preview=schedule_ctx(values["freq"]), contacts=service.address_book(), **ctx)

    def notice_count(v: store.Veille) -> int:
        """Avis d'une veille composants (0 sinon, ou fichier illisible : il n'empêche pas d'enregistrer) ; en
        hausse après un enregistrement, des avis ont été repris de la base des composants (message dédié)."""
        try:
            return len(components.load_notices(v.slug)) if v.kind == "composants" else 0
        except components.ComponentsError:
            return 0

    @app.get("/veilles/new", response_class=HTMLResponse)
    def new_form(request: Request, template: str = "vierge", kind: str = "veille"):
        kind = kind if kind in store.KINDS else "veille"
        return form_page(request, form_defaults(template=template, kind=kind), creating=True, template=template)

    @app.post("/veilles/new")
    async def create(request: Request):
        form = await request.form()
        values = values_from_form(form)
        try:
            v = service.create_veille(
                name=values["name"], slug=values["slug"] or None, subject=values["subject"],
                schedule_expr=schedule.to_cron(values["freq"]),
                recipients=store.parse_recipients(values["recipients"]), brief=values["brief"],
                pdf=pdf_from_values(values), model=values["model"] or None, kind=values["kind"],
                actor=ACTOR, **kind_fields(values))
        except USER_ERRORS as exc:
            return form_page(request, values, 400, creating=True, error=str(exc))
        return RedirectResponse(f"/veilles/{v.slug}?ok={'created-known' if notice_count(v) else 'created'}",
                                status_code=303)

    def detail_ctx(slug: str) -> dict:
        v = store.load(slug)
        row = service.describe(v)
        logs = store.list_run_logs(slug)
        nxt = schedule.next_runs(v.schedule, 3)
        return {"row": row, "v": v, "reports": store.list_reports(slug), "logs": logs,
                "next_runs": nxt, "next_peak": bool(nxt) and pricing.is_peak(nxt[0]),
                "costs": service.costs(slug), **components_ctx(v)}

    def component_rows(slug: str) -> list[dict]:
        """Tous les composants (et ceux retirés qui ont des avis), une ligne chacun."""
        return components.group(components.load_notices(slug), components.load_components(slug)["components"],
                                include_quiet=True)

    def components_ctx(v: store.Veille) -> dict:
        if v.kind != "composants":
            return {}
        rows = component_rows(v.slug)
        return {"digikey_configured": credentials.status("digikey")["configured"], "comp_rows": rows,
                "critical_count": sum(1 for r in rows if r["critical"] and r["tracked"]),
                "notice_total": sum(len(r["notices"]) for r in rows),
                "linked": [x.slug for x in service._linked_veilles(v.slug)]}

    @app.get("/veilles/{slug}", response_class=HTMLResponse)
    def detail(request: Request, slug: str):
        try:
            ctx = detail_ctx(slug)
        except components.ComponentsError as exc:  # fichier corrompu : le dire, pas « introuvable »
            return render(request, "error.html", 500, error=str(exc))
        except StoreError:
            return render(request, "error.html", 404, error="Veille introuvable.")
        return render(request, "veille_detail.html", **ctx)

    @app.get("/veilles/{slug}/history", response_class=HTMLResponse)
    def history(request: Request, slug: str):
        """Historique d'exécution Hermes, chargé après la page : `hermes cron runs` démarre toute la
        CLI Hermes (près d'une seconde), la page s'affiche sans l'attendre."""
        try:
            v = store.load(slug)
        except StoreError:
            return PlainTextResponse("veille introuvable", status_code=404)
        runs = hermes.runs(v.job_id, 10) if v.job_id and hermes.get_job(v.job_id, service._safe_jobs()) else []
        return render(request, "partials/history.html", runs=runs)

    @app.get("/veilles/{slug}/components/{anchor}", response_class=HTMLResponse)
    def component_detail(request: Request, slug: str, anchor: str):
        """Détail d'un composant, chargé au clic dans la liste (htmx)."""
        try:
            if not ANCHOR_RE.fullmatch(anchor) or store.load(slug).kind != "composants":
                raise StoreError("introuvable")
            row = next((r for r in component_rows(slug) if r["anchor"] == anchor), None)
        except components.ComponentsError as exc:
            return PlainTextResponse(str(exc), status_code=500)
        except StoreError:
            row = None
        if row is None:
            return PlainTextResponse("composant introuvable", status_code=404)
        return render(request, "partials/component_detail.html", r=row)

    @app.get("/veilles/{slug}/edit", response_class=HTMLResponse)
    def edit_form(request: Request, slug: str):
        try:
            v = store.load(slug)
            values = form_defaults(v)
        except components.ComponentsError as exc:
            return render(request, "error.html", 500, error=str(exc))
        except StoreError:
            return render(request, "error.html", 404, error="Veille introuvable.")
        return form_page(request, values, creating=False, v=v)

    @app.post("/veilles/{slug}/edit")
    async def update(request: Request, slug: str):
        form = await request.form()
        values = values_from_form(form)
        try:
            v = store.load(slug)
            values["kind"] = v.kind  # le type ne change pas après la création
            before = notice_count(v)
            service.update_veille(
                slug, name=values["name"], subject=values["subject"],
                schedule_expr=schedule.to_cron(values["freq"]),
                recipients=store.parse_recipients(values["recipients"]), brief=values["brief"],
                pdf=pdf_from_values(values), model=values["model"], actor=ACTOR, **kind_fields(values))
        except USER_ERRORS as exc:
            values["slug"] = slug
            return form_page(request, values, 400, creating=False, v=store.load(slug), error=str(exc))
        code = "updated-known" if notice_count(v) > before else "updated"
        return RedirectResponse(f"/veilles/{v.slug}?ok={code}", status_code=303)

    # -------------------------------------------------------------- fragments HTMX
    @app.get("/fragments/schedule", response_class=HTMLResponse)
    def schedule_preview(request: Request):
        return render(request, "partials/schedule_preview.html", **schedule_ctx(freq_from_form(request.query_params)))

    @app.post("/fragments/brief", response_class=HTMLResponse)
    async def brief_preview(request: Request):
        form = await request.form()
        return HTMLResponse('<div class="md">' + _md.render(str(form.get("brief", ""))) + "</div>")

    # -------------------------------------------------------------- import de nomenclature
    def session_secret(request: Request) -> str:
        return (request.state.session or {}).get("csrf", "")

    def proposal(secret: str, token: str, form) -> tuple[str, dict, dict]:
        """Réanalyse le fichier conservé avec les choix du formulaire : (nom, table, proposition).
        Synchrone (analyse d'un fichier) : appelé via run_in_threadpool."""
        form = dict(form)
        # Autre feuille ou autre ligne d'en-tête : les colonnes changent, la détection repart de zéro.
        changed = form.get("import_changed")
        if changed in ("sheet", "header"):
            for f in bom_import.FIELDS:
                form.pop(f"import_{f}", None)
            if changed == "sheet":
                form.pop("import_header", None)
        filename, data = bom_import.unstash(token, secret)
        table = bom_import.read_table(data, filename, form.get("import_sheet") or None)
        mapping = None
        if any(f"import_{f}" in form for f in bom_import.FIELDS):
            mapping = {f: form.get(f"import_{f}", "") for f in bom_import.FIELDS}
        p = bom_import.propose_options(table, form.get("import_header"), mapping,
                                       str(form.get("import_default_mfr", "")))
        return filename, table, p

    async def import_panel(request: Request, token: str, form):
        try:
            filename, table, p = await run_in_threadpool(proposal, session_secret(request), token, form)
        except components.ComponentsError as exc:
            return render(request, "partials/import.html", 400, error=str(exc))
        choices = [" | ".join(c for c in row if c)[:70] or "(vide)" for row in table["rows"][:bom_import.HEADER_SCAN]]
        return render(request, "partials/import.html", p=p, token=token, filename=filename,
                      header_choices=choices, mode=form.get("import_mode", "add"))

    @app.post("/fragments/import", response_class=HTMLResponse)
    async def import_upload(request: Request):
        # Refus AVANT de recevoir le fichier (Starlette l'écrirait d'abord en entier sur disque).
        if int(request.headers.get("content-length") or 0) > bom_import.MAX_BYTES + 65_536:
            return render(request, "partials/import.html", 400,
                          error=f"fichier trop volumineux : {bom_import.MAX_BYTES // 1_000_000} Mo au plus")
        form = await request.form(max_files=1, max_fields=20)
        upload = form.get("import_file")
        if not hasattr(upload, "read") or not upload.filename:
            return render(request, "partials/import.html", 400, error="Choisir un fichier CSV ou Excel.")
        data = await upload.read(bom_import.MAX_BYTES + 1)
        try:
            await run_in_threadpool(bom_import.read_table, data, upload.filename)  # refus immédiat si illisible
            token = bom_import.stash(data, upload.filename, session_secret(request))
        except components.ComponentsError as exc:
            return render(request, "partials/import.html", 400, error=str(exc))
        service.audit(ACTOR, "components-import-upload", detail=f"{len(data)} octets")
        return await import_panel(request, token, {})

    @app.post("/fragments/import/preview", response_class=HTMLResponse)
    async def import_preview(request: Request):
        form = await request.form()
        return await import_panel(request, str(form.get("import_token", "")), form)

    @app.post("/fragments/import/apply", response_class=HTMLResponse)
    async def import_apply(request: Request):
        form = await request.form()
        current_text = str(form.get("components", ""))
        try:
            _, _, p = await run_in_threadpool(proposal, session_secret(request), str(form.get("import_token", "")),
                                              form)
            if p["error"]:
                raise components.ComponentsError(p["error"])
            current = components.parse_components(current_text)
            merged, st = components.combine(current, p["components"], form.get("import_mode", "add"))
        except components.ComponentsError as exc:
            return render(request, "partials/components_field.html", 400, import_error=str(exc),
                          components_text=current_text)
        msg = (f"Import appliqué : {st['added']} ajouté(s), {st['existing']} déjà présent(s)"
               + (f", {st['removed']} retiré(s)" if st["removed"] else "")
               + ". Vérifie la liste puis enregistre la veille.")
        return render(request, "partials/components_field.html", components_text=components.format_components(merged),
                      import_result=msg)

    @app.get("/veilles/{slug}/runs/{name}", response_class=HTMLResponse)
    def run_log(request: Request, slug: str, name: str, poll: bool = False):
        """Journal d'un lancement, précédé de son avancement s'il est suivi. `poll` (suivi depuis la page de la
        veille) : à la fin, l'en-tête (état, boutons) part avec le journal ; journal disparu (veille archivée) :
        286, htmx arrête le suivi."""
        try:
            path = store.run_log_path(slug, name)
            text = path.read_text(encoding="utf-8", errors="replace")[-20000:]
        except (StoreError, OSError):
            return PlainTextResponse("journal introuvable", status_code=286 if poll else 404)
        latest = (store.list_run_logs(slug, 1) or [None])[0]
        running = latest == name and service.running_pid(slug) is not None
        head = {}
        if poll and not running:
            try:
                v = store.load(slug)
                head = {"v": v, "row": service.describe(v)}
            except StoreError:  # veille archivée entre-temps : le journal seul
                pass
        return render(request, "partials/run_log.html", slug=slug, name=name, text=text, running=running,
                      progress=service.run_progress(slug, name, running), **head)

    @app.get("/veilles/{slug}/components.xlsx")
    def components_export(slug: str):
        try:
            data = service.components_xlsx(slug)
        except components.ComponentsError as exc:  # fichier corrompu : le dire, pas « introuvable »
            return PlainTextResponse(str(exc), status_code=500)
        except USER_ERRORS:
            return PlainTextResponse("veille composants introuvable", status_code=404)
        name = components.xlsx_name(slug, components.today())
        return Response(data, media_type=components.XLSX_TYPE,
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # -------------------------------------------------------------- fichiers
    @app.get("/veilles/{slug}/reports/{name}")
    def report(slug: str, name: str):
        try:
            path = store.report_path(slug, name)
        except StoreError:
            return PlainTextResponse("rapport introuvable", status_code=404)
        if name.endswith(".pdf"):
            return FileResponse(path, media_type="application/pdf", filename=name,
                                content_disposition_type="inline")
        return FileResponse(path, media_type="text/plain; charset=utf-8", filename=name,
                            content_disposition_type="inline")

    # -------------------------------------------------------------- sources (clés d'API)
    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request):
        return render(request, "sources.html", sources=service.sources_status())

    @app.post("/sources/{provider}/{op}")
    async def source_action(request: Request, provider: str, op: str):
        form = await request.form()
        try:
            if op == "save":
                changed = service.save_credentials(
                    provider, {k: str(form.get(k, "")) for k in credentials.PROVIDERS.get(provider, {}).get("fields", {})},
                    actor=ACTOR)
                return RedirectResponse(f"/sources?ok={'creds-saved' if changed else 'creds-unchanged'}", 303)
            if op == "delete":
                service.delete_credentials(provider, actor=ACTOR)
                return RedirectResponse("/sources?ok=creds-deleted", 303)
            if op == "test":
                result = service.check_credentials(provider, actor=ACTOR)
                return render(request, "sources.html", sources=service.sources_status(), result=result)
        except USER_ERRORS as exc:
            return render(request, "sources.html", 400, sources=service.sources_status(), error=str(exc))
        return PlainTextResponse("action inconnue", status_code=404)

    # -------------------------------------------------------------- paramètres (interface seule)
    def settings_page(request: Request, status_code: int = 200, /, **ctx):
        return render(request, "settings.html", status_code, alert=service.alert_status(),
                      contacts=service.address_book(limit=None), **ctx)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_form(request: Request):
        return settings_page(request)

    @app.post("/settings/{op}")
    async def settings_action(request: Request, op: str):
        form = await request.form()
        try:
            if op == "save":
                # Hors de la boucle d'événements : le verrou des veilles peut être tenu quelques secondes.
                changed = await run_in_threadpool(service.save_alert_emails, str(form.get("alert_emails", "")),
                                                  actor=ACTOR)
                return RedirectResponse(f"/settings?ok={'settings-saved' if changed else 'settings-unchanged'}", 303)
            if op == "test":
                await run_in_threadpool(service.send_test_alert, actor=ACTOR)
                return RedirectResponse("/settings?ok=alert-mail", 303)
            if op == "forget":
                removed = await run_in_threadpool(service.forget_contact, str(form.get("email", "")), actor=ACTOR)
                return RedirectResponse(f"/settings?ok={'contact-forgotten' if removed else 'settings-unchanged'}#carnet",
                                        303)
        except USER_ERRORS as exc:
            typed = {"typed": str(form.get("alert_emails", ""))} if op == "save" else {}  # saisie conservée
            return settings_page(request, 400, error=str(exc), **typed)
        return PlainTextResponse("action inconnue", status_code=404)

    # -------------------------------------------------------------- santé
    @app.get("/health", response_class=HTMLResponse)
    def health(request: Request):
        # Comptes Brevo et DeepSeek interrogés en parallèle (deux appels réseau, chacun jusqu'à 10 s).
        with ThreadPoolExecutor(2) as pool:
            brevo, balance = pool.submit(brevo_account), pool.submit(deepseek.balance)
            return render(request, "health.html", rows=service.overview(), brevo=brevo.result(),
                          deepseek=balance.result(), costs=service.costs_overview(),
                          catalog=service.catalog_stats(), audit=service.read_audit(40))

    # -------------------------------------------------------------- actions
    # Déclarée APRÈS /veilles/{slug}/edit : sinon « edit » serait pris pour une action.
    @app.post("/veilles/{slug}/{action}")
    def action(request: Request, slug: str, action: str):
        ops = {"pause": (service.pause, "paused"), "resume": (service.resume, "resumed"),
               "run": (service.run_now, "run"), "test-email": (service.send_test_email, "mail"),
               "archive": (service.archive, "archived"),
               "check-status": (service.check_statuses_now, "checking"),
               "reset-search": (service.reset_components, "reset")}
        if action not in ops:
            return PlainTextResponse("action inconnue", status_code=404)
        func, code = ops[action]
        htmx_row = request.headers.get("HX-Target", "").startswith("row-")
        try:
            store.check_slug(slug)
            func(slug, actor=ACTOR)
        except components.ComponentsError as exc:  # fichier corrompu : la page de la veille ne s'afficherait pas
            return render(request, "error.html", 500, error=str(exc))
        except USER_ERRORS as exc:
            if htmx_row:
                return row_response(request, slug, str(exc))
            return render(request, "veille_detail.html", 400, error=str(exc), **detail_ctx(slug))
        if action == "archive":
            return RedirectResponse(f"/?ok={code}", status_code=303)
        if htmx_row:
            return row_response(request, slug)
        return RedirectResponse(f"/veilles/{slug}?ok={code}", status_code=303)

    return app


app = create_app()
