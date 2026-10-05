"""Application FastAPI : routes de l'interface. Toute la logique passe par veille_core.service."""
from __future__ import annotations

import datetime as dt
import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from starlette.middleware.trustedhost import TrustedHostMiddleware

from veille_core import hermes, paths, schedule, service, store, version
from veille_core.store import StoreError

from . import auth

HERE = Path(__file__).resolve().parent
ACTOR = "ui"
USER_ERRORS = (StoreError, schedule.ScheduleError, service.ServiceError, hermes.HermesError)

# Messages affichés après redirection : codes en liste blanche (aucun texte réfléchi).
FLASH = {
    "created": "Veille créée.",
    "updated": "Veille enregistrée.",
    "paused": "Veille mise en pause.",
    "resumed": "Veille reprise.",
    "run": "Lancement démarré : le journal s'affiche ci-dessous.",
    "archived": "Veille archivée.",
    "mail": "E-mail de test envoyé.",
}
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
        value = value.astimezone()
    days = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
    return f"{days[value.weekday()]} {value:%d/%m/%Y %H:%M}"


def fmt_size(n) -> str:
    return f"{(n or 0) / 1024:.0f} Ko"


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


templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["dt"] = fmt_dt
templates.env.filters["size"] = fmt_size
templates.env.globals["status_class"] = status_class
templates.env.globals["DAYS"] = schedule.DAYS
templates.env.globals["VERSION"] = version()


# --------------------------------------------------------------------------
# Formulaire veille <-> champs
# --------------------------------------------------------------------------
def form_defaults(v: store.Veille | None = None, template: str = "vierge") -> dict:
    if v is None:
        briefs = store.brief_templates()
        return {"name": "", "slug": "", "subject": "", "recipients": "",
                "brief": briefs.get(template, ""), "freq": {"kind": "weekly", "dow": 1, "hour": 8, "minute": 0},
                "cron_expr": "", "pdf_title": "", "pdf_header": "", "pdf_footer": "Document interne",
                "pdf_audience": "direction & ingénierie", "pdf_accent": "#0f766e", "model": ""}
    freq = schedule.from_cron(v.schedule)
    return {"name": v.name, "slug": v.slug, "subject": v.subject,
            "recipients": "\n".join(store.read_recipients(v.slug)), "brief": store.read_brief(v.slug),
            "freq": freq, "cron_expr": v.schedule if freq["kind"] == "cron" else "",
            "pdf_title": v.pdf.title, "pdf_header": v.pdf.header, "pdf_footer": v.pdf.footer,
            "pdf_audience": v.pdf.audience, "pdf_accent": v.pdf.accent, "model": v.model or ""}


def freq_from_form(form) -> dict:
    kind = form.get("freq_kind", "weekly")
    if kind == "cron":
        return {"kind": "cron", "expr": form.get("cron_expr", "")}
    hh, _, mm = (form.get("time") or "08:00").partition(":")
    return {"kind": kind, "hour": hh or 8, "minute": mm or 0,
            "dow": form.get("dow", 1), "day": form.get("day", 1)}


def values_from_form(form) -> dict:
    freq = freq_from_form(form)
    return {"name": form.get("name", "").strip(), "slug": form.get("slug", "").strip(),
            "subject": form.get("subject", "").strip(), "recipients": form.get("recipients", ""),
            "brief": form.get("brief", ""), "freq": freq, "cron_expr": form.get("cron_expr", ""),
            "pdf_title": form.get("pdf_title", ""), "pdf_header": form.get("pdf_header", ""),
            "pdf_footer": form.get("pdf_footer", ""), "pdf_audience": form.get("pdf_audience", ""),
            "pdf_accent": form.get("pdf_accent", "#0f766e"), "model": form.get("model", "").strip()}


def pdf_from_values(values: dict) -> dict:
    return {k[4:]: values[k] for k in ("pdf_title", "pdf_header", "pdf_footer", "pdf_audience", "pdf_accent")}


# --------------------------------------------------------------------------
# Santé
# --------------------------------------------------------------------------
def brevo_account() -> dict:
    env = {}
    try:
        for line in (paths.hermes_home() / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    key = os.environ.get("BREVO_API_KEY") or env.get("BREVO_API_KEY")
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
        response.headers.setdefault("Cache-Control", "no-store")
        return response

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

    # -------------------------------------------------------------- création / édition
    @app.get("/veilles/new", response_class=HTMLResponse)
    def new_form(request: Request, template: str = "vierge"):
        templates_ = list(store.brief_templates())
        return render(request, "veille_form.html", values=form_defaults(template=template),
                      creating=True, templates_=templates_, template=template)

    @app.post("/veilles/new")
    async def create(request: Request):
        form = await request.form()
        values = values_from_form(form)
        try:
            v = service.create_veille(
                name=values["name"], slug=values["slug"] or None, subject=values["subject"],
                schedule_expr=schedule.to_cron(values["freq"]),
                recipients=store.parse_recipients(values["recipients"]), brief=values["brief"],
                pdf=pdf_from_values(values), model=values["model"] or None, actor=ACTOR)
        except USER_ERRORS as exc:
            return render(request, "veille_form.html", 400, values=values, creating=True,
                          templates_=list(store.brief_templates()), error=str(exc))
        return RedirectResponse(f"/veilles/{v.slug}?ok=created", status_code=303)

    def detail_ctx(slug: str) -> dict:
        v = store.load(slug)
        row = service.describe(v)
        logs = store.list_run_logs(slug)
        runs = hermes.runs(v.job_id, 10) if v.job_id and row["status"]["exists"] else []
        return {"row": row, "v": v, "reports": store.list_reports(slug), "logs": logs, "runs": runs,
                "next_runs": schedule.next_runs(v.schedule, 3)}

    @app.get("/veilles/{slug}", response_class=HTMLResponse)
    def detail(request: Request, slug: str):
        try:
            ctx = detail_ctx(slug)
        except StoreError:
            return render(request, "error.html", 404, error="Veille introuvable.")
        return render(request, "veille_detail.html", **ctx)

    @app.get("/veilles/{slug}/edit", response_class=HTMLResponse)
    def edit_form(request: Request, slug: str):
        try:
            v = store.load(slug)
        except StoreError:
            return render(request, "error.html", 404, error="Veille introuvable.")
        return render(request, "veille_form.html", values=form_defaults(v), creating=False, v=v)

    @app.post("/veilles/{slug}/edit")
    async def update(request: Request, slug: str):
        form = await request.form()
        values = values_from_form(form)
        try:
            v = store.load(slug)
            service.update_veille(
                slug, name=values["name"], subject=values["subject"],
                schedule_expr=schedule.to_cron(values["freq"]),
                recipients=store.parse_recipients(values["recipients"]), brief=values["brief"],
                pdf=pdf_from_values(values), model=values["model"], actor=ACTOR)
        except USER_ERRORS as exc:
            values["slug"] = slug
            return render(request, "veille_form.html", 400, values=values, creating=False,
                          v=store.load(slug), error=str(exc))
        return RedirectResponse(f"/veilles/{v.slug}?ok=updated", status_code=303)

    # -------------------------------------------------------------- fragments HTMX
    @app.get("/fragments/schedule", response_class=HTMLResponse)
    def schedule_preview(request: Request):
        try:
            expr = schedule.to_cron(freq_from_form(request.query_params))
            ctx = {"expr": expr, "human": schedule.describe(expr), "runs": schedule.next_runs(expr, 3)}
        except schedule.ScheduleError as exc:
            ctx = {"error": str(exc)}
        return render(request, "partials/schedule_preview.html", **ctx)

    @app.post("/fragments/brief", response_class=HTMLResponse)
    async def brief_preview(request: Request):
        form = await request.form()
        return HTMLResponse('<div class="md">' + _md.render(str(form.get("brief", ""))) + "</div>")

    @app.get("/veilles/{slug}/runs/{name}", response_class=HTMLResponse)
    def run_log(request: Request, slug: str, name: str):
        try:
            path = store.run_log_path(slug, name)
        except StoreError:
            return PlainTextResponse("journal introuvable", status_code=404)
        text = path.read_text(encoding="utf-8", errors="replace")[-20000:]
        latest = (store.list_run_logs(slug, 1) or [None])[0]
        running = latest == name and service.running_pid(slug) is not None
        return render(request, "partials/run_log.html", slug=slug, name=name, text=text, running=running)

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

    # -------------------------------------------------------------- santé
    @app.get("/health", response_class=HTMLResponse)
    def health(request: Request):
        return render(request, "health.html", rows=service.overview(), brevo=brevo_account(),
                      audit=service.read_audit(40))

    # -------------------------------------------------------------- actions
    # Déclarée APRÈS /veilles/{slug}/edit : sinon « edit » serait pris pour une action.
    @app.post("/veilles/{slug}/{action}")
    def action(request: Request, slug: str, action: str):
        ops = {"pause": (service.pause, "paused"), "resume": (service.resume, "resumed"),
               "run": (service.run_now, "run"), "test-email": (service.send_test_email, "mail"),
               "archive": (service.archive, "archived")}
        if action not in ops:
            return PlainTextResponse("action inconnue", status_code=404)
        func, code = ops[action]
        htmx_row = request.headers.get("HX-Target", "").startswith("row-")
        try:
            store.check_slug(slug)
            func(slug, actor=ACTOR)
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
