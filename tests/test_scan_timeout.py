"""Non-régression : incident du 22/10/2026 sur la veille « composants-nomenclature-2 » (v0.7.1).

Tir de 27 min en échec explicite, mais ni rapport ni e-mail : `veillectl components scan` dure ≈ 17 min pour
386 références, alors que Hermes v0.22.2 abandonne TOUT appel d'outil au bout de 420 s (agent/tool_executor.py,
`_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S` : « timed out after 420.0s », sans sortie). Le garde-fou ne connaissait que le
plafond du premier plan (TERMINAL_MAX_FOREGROUND_TIMEOUT = 1 200 s) et exigeait `timeout: 1200`.

Désormais (décisions D1 à D6 : veille_core/guard.py, service.components_scan, CHANGELOG 0.7.2) : un seul plafond,
guard.COMMAND_TIMEOUT, sous ceux de Hermes ; le scan avance par tranches reprenables, chacune sous ce plafond ;
un passage partiel est publié comme tel ; le contrat du prompt est exactement celui du garde-fou.
"""
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_components import tools  # noqa: F401 - fixtures partagées
from test_guard import hermes_run

from veille_core import components as comp
from veille_core import digikey, guard, hermes, render, service, store

REPO = Path(__file__).resolve().parent.parent
SLUG = "composants-nomenclature-2"
STAMP = "2026-10-22-07h00"
# Hermes v0.22.2 (FROM du Dockerfile), valeurs par défaut de l'image :
HERMES_TOOL_DEADLINE = 420  # agent/tool_executor.py : délai de tout appel d'outil, au-delà abandonné sans sortie
HERMES_FOREGROUND_MAX = 600  # tools/terminal_tool.py : au-delà, commande passée en arrière-plan
# Pire cas d'un appel en cours à la fin d'une tranche (jeton expiré : 2 authentifications et 2 requêtes),
# puis enregistrement de la liste et de la base des composants (même marge qu'en 0.7.1).
IN_FLIGHT = 4 * digikey.HTTP_TIMEOUT + 60
PER_REF = 2.6  # s par référence chez Digi-Key le 22/10 (386 références ≈ 17 min)


class Clock:
    """Horloge simulée : seul le temps passé chez Digi-Key (requêtes et attentes) avance."""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class SlowDigiKey:
    """Digi-Key du 22/10 : `per_ref` s par référence ; `on_call(n, mpn)` simule une panne ou un arrêt."""

    def __init__(self, clock: Clock, on_call=None, per_ref: float = PER_REF):
        self.clock, self.on_call, self.per_ref, self.calls = clock, on_call, per_ref, 0

    def status(self, mpn, manufacturer):
        self.calls += 1
        self.clock.t += self.per_ref
        if self.on_call:
            self.on_call(self.calls, mpn)
        return {"status": "Active", "product_url": None}

    def pause(self, seconds):
        self.clock.t += seconds


@pytest.fixture(autouse=True)
def image_0_7_1(monkeypatch):
    """Environnement de l'image du 22/10 (0.7.1) ; depuis 0.7.2, le garde-fou ne lit plus cette variable (D3)."""
    monkeypatch.setenv("TERMINAL_MAX_FOREGROUND_TIMEOUT", "1200")
    monkeypatch.setattr(guard, "_runs", {})


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=c, time=time.time))
    return c


def bom(n: int) -> list[dict]:
    return comp.parse_components("\n".join(f"CRCW{i:04d};Vishay {i % 7}" for i in range(n)))


def components_veille(name: str, n: int, slug: str | None = None):
    return service.create_veille(name=name, slug=slug, subject="BOM", schedule_expr=service.COMPONENTS_SCHEDULE,
                                 recipients=["achats@example.org"], brief=store.brief_templates()["composants"],
                                 kind="composants", components=bom(n), batch_size=30)


@pytest.fixture
def incident(tools):  # noqa: F811
    """La veille du 22/10 : 386 références, lot web de 30."""
    return components_veille("Composants nomenclature 2", 386, SLUG)


def run_of(v) -> guard.Session:
    return guard.session(task_id=f"cron:{v.job_id}:exec1")


def documented_scan_call(v) -> dict:
    """Appel exact du scan, tel que le prompt de la veille le donne à l'agent."""
    m = re.search(r'\{"command": "veillectl components scan [^"]+", "timeout": \d+\}', render.render(v))
    return json.loads(m.group(0))


def agent_scan(v, clock: Clock, on_call=None, per_ref: float = PER_REF, max_calls: int = 20):
    """L'agent selon son prompt : l'appel documenté, permis par le garde-fou, relancé tel quel tant que la sortie
    dit "complete": false. Renvoie la durée de chaque appel (temps Digi-Key simulé) et la dernière sortie."""
    call = documented_scan_call(v)
    durations, out = [], {}
    for _ in range(max_calls):
        assert guard.enforce("terminal", call, run_of(v), "tour-1") is None
        start = clock.t
        out = service.components_scan(v.slug, client=SlowDigiKey(clock, on_call, per_ref))
        durations.append(clock.t - start)
        if out.get("complete") is not False:
            return durations, out
    raise AssertionError(f"scan jamais terminé en {max_calls} appels")


def checked_today(slug: str) -> set[str]:
    day = comp.today()
    return {c["mpn"] for c in comp.load_components(slug)["components"] if (c.get("dk") or {}).get("checked_at") == day}


def write_pcn(v) -> Path:
    path = v.dir / "rapports" / f"{STAMP}-pcn.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"notices": [], "statuses": []}))
    return path


def published(v, tools) -> tuple[str, str, str]:  # noqa: F811
    """(rapport tel que pandoc le rend, sans les échappements Markdown ; e-mail HTML ; e-mail texte) de la
    dernière publication de `v`."""
    mail = tools()[-1]
    md = next((v.dir / "rapports").glob("*-veille.md")).read_text()
    return re.sub(r"\\(.)", r"\1", md), mail["html"], mail["args"][mail["args"].index("--text") + 1]


# --------------------------------------------------------------------------
# 1. Reproduction de l'incident : un scan plus long que le délai d'outil de Hermes aboutit quand même
# --------------------------------------------------------------------------
def test_incident_scan_longer_than_the_tool_deadline_goes_to_the_end(incident, clock, tools):  # noqa: F811
    """22/10 : 386 références à 2,6 s ≈ 1 000 s de Digi-Key. Chaque appel du scan doit finir avant que Hermes ne
    l'abandonne (420 s) ; relancé tel quel, le passage reprend où il en était ; puis lot web, D-pcn.json, finalize,
    PDF, Excel et e-mail, sans intervention humaine (critère d'acceptation 1)."""
    durations, out = agent_scan(incident, clock)
    assert max(durations) + IN_FLIGHT <= HERMES_TOOL_DEADLINE, durations  # 0.7.1 : un seul appel de 900 s
    assert 2 <= len(durations) <= 6  # le budget du passage (900 s) borne le nombre de tranches
    assert out["complete"] is True and len(out["batch"]) == 30
    # budget du passage atteint (≈ 346 références) : dit, jamais présenté comme complet
    assert out["digikey"]["done"] == len(checked_today(SLUG)) < out["digikey"]["total"] == 386
    assert "suite au prochain passage" in out["digikey"]["stopped"]
    finalize = {"command": f"veillectl components finalize {SLUG} {write_pcn(incident)}",
                "timeout": guard.COMMAND_TIMEOUT}
    assert guard.enforce("terminal", finalize, run_of(incident), "tour-1") is None
    assert "OK: veille composants" in service.components_finalize(SLUG, write_pcn(incident))
    assert sum(a.endswith(("-veille.pdf", ".xlsx")) for a in tools()[-1]["args"]) == 2  # PDF et Excel joints
    assert guard.finish("tour-1", run_of(incident)) is None  # rapport publié : exécution en succès


def test_every_scan_call_fits_under_hermes_limits(incident):
    """D3 : le timeout que le prompt donne au scan, et que le garde-fou exige, est sous les deux plafonds de Hermes ;
    une tranche, son dernier appel et l'enregistrement tiennent dans ce timeout."""
    call = documented_scan_call(incident)
    assert call["timeout"] == guard.COMMAND_TIMEOUT < HERMES_TOOL_DEADLINE <= HERMES_FOREGROUND_MAX
    assert service.SCAN_SLICE_S + IN_FLIGHT <= guard.COMMAND_TIMEOUT
    assert guard.check("terminal", call, run_of(incident)) is None
    for timeout in (HERMES_TOOL_DEADLINE, 1200):  # 0.7.1 exigeait 1 200 s : Hermes abandonnait à 420
        assert guard.check("terminal", {**call, "timeout": timeout}, run_of(incident)).startswith("durée maximale")
    # plus de second plafond dans l'image : celui de Hermes par défaut (600 s) suffit
    assert "TERMINAL_MAX_FOREGROUND_TIMEOUT" not in (REPO / "Dockerfile").read_text()


def test_killed_slice_loses_only_its_own_work(incident, clock):
    """Une tranche arrêtée en cours (délai mal réglé, SIGTERM) n'enregistre rien ; l'appel suivant reprend les
    références non enregistrées, sans réinterroger celles des tranches déjà enregistrées."""
    service.components_scan(SLUG, client=SlowDigiKey(clock))
    saved = checked_today(SLUG)
    assert 0 < len(saved) < 386

    def killed(n, mpn):
        if n == 10:
            raise SystemExit(143)
    with pytest.raises(SystemExit):
        service.components_scan(SLUG, client=SlowDigiKey(clock, killed))
    assert checked_today(SLUG) == saved
    seen: list[str] = []
    _, out = agent_scan(incident, clock, on_call=lambda n, mpn: seen.append(mpn))
    assert out["complete"] is True and seen and not set(seen) & saved and len(set(seen)) == len(seen)


def test_pass_budget_and_calls_are_shared_by_all_slices(tools, clock, monkeypatch):  # noqa: F811
    """5 000 références : le passage, toutes tranches confondues, garde son budget de 900 s et de MAX_CALLS appels
    (quota journalier de Digi-Key) ; le lendemain, les références qu'il n'a pas atteintes passent d'abord."""
    v = components_veille("Grande liste", comp.MAX_COMPONENTS)
    calls: list[int] = []
    durations, out = agent_scan(v, clock, on_call=lambda n, mpn: calls.append(n), per_ref=0.5)
    assert len(calls) == digikey.MAX_CALLS and sum(durations) <= service.SCAN_BUDGET_S
    assert out["complete"] is True and out["digikey"]["done"] == digikey.MAX_CALLS
    assert "suite au prochain passage" in out["digikey"]["stopped"]
    first_pass = checked_today(v.slug)
    tomorrow = (service.dt.date.fromisoformat(comp.today()) + service.dt.timedelta(days=1)).isoformat()
    monkeypatch.setattr(comp, "today", lambda: tomorrow)
    seen: list[str] = []
    out = service.components_scan(v.slug, client=SlowDigiKey(clock, lambda n, mpn: seen.append(mpn)))
    assert out["complete"] is False and seen and not set(seen) & first_pass  # nouveau passage, budget neuf


@pytest.fixture
def no_jitter(monkeypatch):
    """Attentes de reprise exactes (5, 10, 20, 30, 45, puis 60 s) : tranches déterministes."""
    monkeypatch.setattr(digikey.random, "uniform", lambda a, b: 1.0)


def test_wait_beyond_the_slice_resumes_at_the_next_slice(incident, clock, no_jitter, capsys):
    """Panne passagère en fin de tranche : l'attente déborderait la tranche (donc le délai de Hermes) ; la tranche
    s'arrête, la suivante reprend à la même référence, la panne reste comptée pour le passage."""
    last_ok = int(service.SCAN_SLICE_S // PER_REF)  # dernière référence de la 1re tranche

    def outage(n, mpn):
        if n == last_ok:
            raise digikey.TransientError("HTTP 503")
    out = service.components_scan(SLUG, client=SlowDigiKey(clock, outage))
    assert out["complete"] is False and comp.load_components(SLUG)["scan"]["incidents"] == 1
    assert "tentative 1/10 : HTTP 503 ; reprise à la tranche suivante" in capsys.readouterr().err
    seen: list[str] = []
    durations, out = agent_scan(incident, clock, on_call=lambda n, mpn: seen.append(mpn))
    assert seen[0] == f"CRCW{last_ok - 1:04d}" and out["complete"] is True
    assert out["digikey"]["incidents"] == 1 and max(durations) + IN_FLIGHT <= HERMES_TOOL_DEADLINE


def test_digikey_outage_is_bounded_for_the_whole_pass(incident, clock, no_jitter, tools):  # noqa: F811
    """Digi-Key en panne : 10 pannes au plus par PASSAGE, toutes tranches confondues (pas 10 par tranche), puis
    abandon ; le lot web part quand même et le rapport dit le passage partiel."""
    def outage(n, mpn):
        raise digikey.TransientError("API Digi-Key injoignable (URLError)")
    durations, out = agent_scan(incident, clock, on_call=outage)
    assert len(durations) == 2 and max(durations) + IN_FLIGHT <= HERMES_TOOL_DEADLINE
    assert out["complete"] is True and len(out["batch"]) == 30 and out["digikey"]["incidents"] == digikey.MAX_ATTEMPTS
    assert "abandon après 10 tentatives" in out["digikey"]["error"] and out["digikey"]["done"] == 0
    service.components_finalize(SLUG, write_pcn(incident))
    md, _, text = published(incident, tools)
    assert "Passage Digi-Key PARTIEL du" in md and "0 référence(s) sur 386" in text and "abandon" in text


def test_manual_run_shows_the_pass_progress_between_slices(incident, clock, monkeypatch):
    """Lancement manuel suivi (interface, PWA) : entre deux tranches, l'étape reste « Statut Digi-Key » avec
    l'avancement du PASSAGE (pas de la tranche) ; le lot n'apparaît qu'à la fin."""
    from test_progress import manual

    from veille_core import progress
    monkeypatch.setattr(progress, "MIN_INTERVAL_S", 0)
    manual(SLUG)
    out = service.components_scan(SLUG, client=SlowDigiKey(clock))
    first = progress.read(SLUG)
    assert first["step"] == "digikey" and first["digikey"] == {"done": out["digikey"]["done"], "total": 386}
    service.components_scan(SLUG, client=SlowDigiKey(clock))
    assert progress.read(SLUG)["digikey"]["done"] > first["digikey"]["done"] and "batch" not in progress.read(SLUG)


def test_status_check_on_demand_stays_a_single_call(incident, clock):
    """Vérification à la demande (interface, hors agent, sans délai d'outil) : un seul appel, budget du passage,
    sans toucher au passage du job ni à son lot."""
    out = service.components_scan(SLUG, client=SlowDigiKey(clock), plan_batch=False)
    assert out["complete"] is True and out["batch"] == [] and "suite au prochain passage" in out["digikey"]["stopped"]
    assert clock.t <= service.SCAN_BUDGET_S + PER_REF and "scan" not in comp.load_components(SLUG)


# --------------------------------------------------------------------------
# 2. Scan partiel : jamais présenté comme complet (D4)
# --------------------------------------------------------------------------
def test_partial_scan_is_published_as_partial(incident, clock, tools):  # noqa: F811
    agent_scan(incident, clock)
    checked = len(checked_today(SLUG))
    service.components_finalize(SLUG, write_pcn(incident))
    for body in published(incident, tools):
        assert "Passage Digi-Key PARTIEL" in body and f"{checked} référence(s) sur 386" in body, body[:400]
        assert f"{386 - checked} autre(s)" in body and "Passage Digi-Key complet" not in body


def test_complete_scan_is_published_as_complete(incident, clock, tools, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service, "SCAN_BUDGET_S", 10_000)  # budget large : toute la liste
    agent_scan(incident, clock)
    service.components_finalize(SLUG, write_pcn(incident))
    for body in published(incident, tools):
        assert "Passage Digi-Key complet" in body and "386 référence(s) sur 386" in body and "PARTIEL" not in body


def test_finalize_before_the_end_of_the_scan_says_so(incident, clock, tools):  # noqa: F811
    """L'agent a conclu entre deux tranches : le rapport dit le passage inachevé, aucun lot n'a été produit."""
    out = service.components_scan(SLUG, client=SlowDigiKey(clock))
    assert out["complete"] is False and "Relance exactement la même commande" in out["next"] and "batch" not in out
    service.components_finalize(SLUG, write_pcn(incident))
    md, html, text = published(incident, tools)
    assert "Passage Digi-Key PARTIEL" in md and "inachevé" in md and "PARTIEL" in html and "PARTIEL" in text


def test_without_digikey_no_coverage_is_claimed(incident, tools):  # noqa: F811
    assert service.components_scan(SLUG)["complete"] is True
    service.components_finalize(SLUG, write_pcn(incident))
    for body in published(incident, tools):
        assert "Passage Digi-Key" not in body


# --------------------------------------------------------------------------
# 3. Sécurité : les refus restent des refus (D2)
# --------------------------------------------------------------------------
def test_guard_refusals_still_apply(incident, tools):  # noqa: F811
    s, r = run_of(incident), incident.dir / "rapports"
    other = service.create_veille(name="Veille Cyber", subject="OT", schedule_expr="0 8 * * 1",
                                  recipients=["a@example.org"], brief="# b\n")
    scan = {"command": f"veillectl components scan {SLUG}", "timeout": guard.COMMAND_TIMEOUT}
    t = guard.COMMAND_TIMEOUT
    for tool, args in [
        ("process_manage", {"action": "poll", "session_id": "proc_1"}),
        ("process_manage", {"action": "list"}),
        ("terminal", {**scan, "background": True}),
        ("terminal", {**scan, "notify": True}),
        ("terminal", {**scan, "pty": True}),
        ("terminal", {**scan, "persist_on_release": True}),
        ("terminal", {**scan, "background": "false"}),  # chaîne non vide : vraie pour Hermes s'il ne la convertit pas
        ("terminal", {**scan, "timeout": t + 1}),
        ("terminal", {**scan, "workdir": "/opt/data"}),
        ("terminal", {"command": f"veillectl components scan {SLUG} > {r}/{STAMP}-scan.json", "timeout": t}),
        ("terminal", {"command": f"veillectl components scan {SLUG} &", "timeout": t}),
        ("terminal", {"command": f"veillectl components scan {SLUG}; cat /opt/data/.env", "timeout": t}),
        ("terminal", {"command": f"veillectl components scan {SLUG} --status-only", "timeout": t}),
        ("terminal", {"command": f"veillectl components scan {other.slug}", "timeout": t}),
        ("terminal", {"command": f"veillectl components finalize {SLUG} {r}/../veille.json"}),
        ("terminal", {"command": f"veillectl report publish {SLUG} {r}/{STAMP}-veille.md"}),  # autre type de veille
        ("terminal", {"command": "date -s 2030-01-01"}),
        ("terminal", {"command": "date +%Y-%m-%d-%Hh%M > /tmp/d"}),
        ("terminal", {"command": f"ls {incident.dir}"}),
        ("read_file", {"path": f"{incident.dir}/components.json"}),
        ("read_file", {"path": "/opt/data/.env"}),
        ("search_files", {"pattern": "*", "path": str(incident.dir)}),
        ("write_file", {"path": f"{r}/{STAMP}-scan.json", "content": "{}"}),
        ("write_file", {"path": f"{incident.dir}/components.json", "content": "{}"}),
        ("write_file", {"path": f"{other.dir}/rapports/{STAMP}-pcn.json", "content": "{}"}),
    ]:
        assert guard.check(tool, args, s), (tool, args)
    # veille thématique : aucune commande de veille composants, même pour son propre slug
    assert guard.check("terminal", {"command": f"veillectl components scan {other.slug}", "timeout": t}, run_of(other))


def test_inert_heartbeat_is_tolerated(incident):
    """Hermes remet `heartbeat` à 0 pour une commande au premier plan (tools/terminal_tool.py) : sans effet, il n'est
    plus refusé (un modèle le remplit d'office, et 3 refus arrêtent l'exécution) ; l'arrière-plan reste refusé."""
    s = run_of(incident)
    assert guard.check("terminal", {"command": "date +%Y-%m-%d-%Hh%M", "heartbeat": 60}, s) is None
    assert guard.check("terminal", {"command": "date", "heartbeat": 60, "background": True}, s)


def test_audit_names_the_refused_parameter(incident):
    """Le 22/10, le journal disait « paramètre(s) non autorisé(s) pour terminal » sans dire lequel."""
    reason = guard.enforce("terminal", {"command": "date +%Y-%m-%d-%Hh%M", "pty": True}, run_of(incident), "t")
    assert reason.startswith("paramètre(s) pty non autorisé(s) pour terminal : params permis : command, timeout")
    assert service.read_audit(1)[0]["detail"] == ("terminal date +%Y-%m-%d-%Hh%M — "
                                                  "paramètre(s) pty non autorisé(s) pour terminal")


# --------------------------------------------------------------------------
# 5. Contrat du prompt = ce que le garde-fou permet, exactement (test croisé)
# --------------------------------------------------------------------------
def documented_calls(prompt: str, rapports: str) -> list[dict]:
    """Appels `terminal` écrits dans le prompt (OUTILS AUTORISÉS et étapes), exemples remplacés ; une commande
    `veillectl` d'une étape reçoit le timeout que le prompt donne à ses appels exacts."""
    calls = [json.loads(m) for m in re.findall(r'\{"command": "[^"]+", "timeout": \d+\}', prompt)]
    (timeout,) = {c["timeout"] for c in calls}  # un seul timeout documenté (D3)
    calls += [{"command": c} for c in re.findall(r"`((?:date|ls) [^`]+)`", prompt)]
    calls += [{"command": line.strip(), "timeout": timeout} for line in prompt.splitlines()
              if line.startswith("   veillectl ")]
    for call in calls:
        call["command"] = (call["command"].replace("<MPN> [<MPN> …]", "LM317T NE555P")
                           .replace("<MPN> …", "LM317T NE555P").replace(f"{rapports}/D-", f"{rapports}/{STAMP}-"))
    return calls


@pytest.mark.parametrize("kind", ["composants", "veille"])
def test_prompt_contract_is_exactly_what_the_guard_permits(tools, kind):  # noqa: F811
    v = service.create_veille(name=f"Veille {kind}", subject="s", schedule_expr="0 8 * * 1",
                              recipients=["a@example.org"], brief="# b\n", kind=kind,
                              components=bom(3) if kind == "composants" else None)
    s = run_of(v)
    calls = documented_calls(render.render(v), str(v.dir / "rapports"))
    for call in calls:  # tout ce qui est écrit est permis…
        assert guard.check("terminal", call, s) is None, call
    # … et tout ce qui est permis est écrit : chaque commande du garde-fou pour ce type de veille, aucune autre
    documented = {" ".join(c["command"].split()[1:3]) for c in calls if c["command"].startswith("veillectl ")}
    assert documented == {name for name, _, _ in guard.VEILLECTL[kind]}
    assert {c["command"].split()[0] for c in calls} == {"date", "ls", "veillectl"}
    for other_kind, commands in guard.VEILLECTL.items():
        for name, _, _ in commands if other_kind != kind else ():
            assert guard.check("terminal", {"command": f"veillectl {name} {v.slug}"}, s)


# --------------------------------------------------------------------------
# 4 et D6. Échec : statut d'erreur et alerte (0.7.1, tests conservés) ; la veille reste active
# --------------------------------------------------------------------------
def test_failed_run_keeps_the_veille_active(incident, monkeypatch):
    """D6 : une veille reprise pour un tir manuel reste active après un échec — en pause, le chien de garde ne la
    surveillerait plus et l'échec redeviendrait silencieux."""
    monkeypatch.setattr(hermes, "run_sync", hermes_run(False, last_run_at="2026-10-22T07:27:00+02:00",
                                                       last_status="error", last_error="veille-guard : x"))
    with pytest.raises(service.ServiceError, match="aucun rapport publié"):
        service.run_wait(SLUG)
    assert not hermes.is_paused(hermes.get_job(incident.job_id))
    assert "pause" not in [a["action"] for a in service.read_audit()]
