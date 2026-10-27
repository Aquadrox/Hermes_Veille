"""Client Digi-Key (urlopen simulé), passage complet et ses reprises sur incident réseau, lecture des secrets."""
import http.client
import io
import json
import re
import urllib.error

import pytest
from test_components import tools, watch  # noqa: F401 - fixtures partagées
from test_progress import manual

from veille_core import components as comp
from veille_core import digikey, envfile, progress, service


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def product(mpn="LM317T", mfr="Texas Instruments", status="Last Time Buy", url="https://www.digikey.com/p/1"):
    return {"ManufacturerProductNumber": mpn, "Manufacturer": {"Name": mfr},
            "ProductStatus": {"Status": status}, "ProductUrl": url}


def found(*products, key="ExactMatches"):
    """Réponse de la recherche par mot-clé (KeywordResponse v4)."""
    return {key: list(products), "ProductsCount": len(products)}


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://api.digikey.com/x", code, "err", headers, io.BytesIO(b"secret-body"))


class Fake:
    """Rejoue une liste de réponses (dict -> JSON, Exception -> levée) et enregistre les requêtes."""

    def __init__(self, *answers):
        self.answers, self.requests, self.sleeps = list(answers), [], []

    def urlopen(self, req, timeout):
        self.requests.append(req)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return Resp(json.dumps(a).encode())

    def client(self, **kw):
        return digikey.Client("cid", "s3cr3t", urlopen=self.urlopen, sleep=self.sleeps.append,
                              clock=lambda: 0.0, **kw)


TOKEN = {"access_token": "tok", "expires_in": 600}


def test_status_ok_and_headers():
    f = Fake(TOKEN, found(product()), found(product(mpn="AB/C-1", status="Active", url=None)))
    c = f.client()
    assert c.status("LM317T", "Texas Instruments Inc.") == {"status": "Last Time Buy",
                                                             "product_url": "https://www.digikey.com/p/1"}
    auth, search = f.requests[0], f.requests[1]
    assert auth.full_url.endswith("/v1/oauth2/token") and b"grant_type=client_credentials" in auth.data
    assert search.full_url == "https://api.digikey.com/products/v4/search/keyword" and search.get_method() == "POST"
    assert json.loads(search.data) == {"Keywords": "LM317T", "Limit": 50, "Offset": 0}  # KeywordRequest v4
    assert search.headers["Authorization"] == "Bearer tok" and search.headers["X-digikey-client-id"] == "cid"
    assert search.headers["Content-type"] == "application/json"
    assert "productdetails" not in "".join(r.full_url for r in f.requests)  # exige un Account-Id en 2-legged
    assert c.status("AB/C-1", "Analog Devices") == {"error": "fabricant différent chez Digi-Key (Texas Instruments)"}
    assert c.calls == 3 and f.sleeps == [digikey.MIN_INTERVAL] * 2  # un appel par référence, débit limité


def test_not_found_and_odd_answers():
    f = Fake(TOKEN, found(product(mpn="LM317T-DG"), key="Products"), http_error(404), {},
             {"ExactMatches": "x", "Products": None}, ["pas", "un", "objet"])
    c = f.client(min_interval=0)
    # MPN prolongé chez le même fabricant : référence proche (variante), jamais la référence elle-même
    assert c.status("LM317T", "Texas Instruments") == {"error": "référence exacte introuvable chez Digi-Key", "near": [
        {"mpn": "LM317T-DG", "manufacturer": "Texas Instruments", "status": "Last Time Buy",
         "url": "https://www.digikey.com/p/1", "kind": "variante"}]}
    for _ in range(4):  # 404, vide, champs mal typés, réponse qui n'est pas un objet
        assert c.status("LM317T", "Texas Instruments") == {"error": "introuvable chez Digi-Key"}
    assert c.calls == 6 and not f.answers


def test_ambiguous_reference_picks_the_tracked_manufacturer():
    """« 1N4148 » existe chez plusieurs fabricants : la fiche du fabricant suivi est retenue."""
    f = Fake(TOKEN, found(product("1N4148", "Vishay", status="Active"),
                          product("1N4148", "onsemi", status="Obsolete", url="https://dk/o")))
    assert f.client(min_interval=0).status("1N4148", "onsemi") == {"status": "Obsolete", "product_url": "https://dk/o"}


def test_punctuation_and_exact_reference_preferred():
    # Nexperia : « 2N7002215 » saisi, « 2N7002,215 » chez Digi-Key (fabricant « Nexperia USA Inc. »)
    f = Fake(TOKEN, found(product("2N7002,215", "Nexperia USA Inc.", status="Active"), key="Products"))
    assert f.client(min_interval=0).status("2N7002215", "Nexperia")["status"] == "Active"
    # deux fiches à la ponctuation près : celle au MPN identique l'emporte, même placée après
    f = Fake(TOKEN, found(product("BAV99,215", "Nexperia", status="Obsolete"), product("BAV99215", "Nexperia",
                                                                                      status="Active")))
    assert f.client(min_interval=0).status("BAV99215", "Nexperia")["status"] == "Active"


@pytest.mark.parametrize("tracked,other", [
    ("MAX232CPE+", "MAX232CPE"),   # « + » sans plomb : jamais confondu avec la version au plomb
    ("SMBJ6.0A", "SMBJ60A"),       # point décimal : 6 V ≠ 60 V
    ("1.5KE6.8A", "1.5KE68A"),
])
def test_significant_characters_never_confused(tracked, other):
    f = Fake(TOKEN, found(product(other, "Littelfuse Inc.", status="Obsolete"), key="Products"))
    assert f.client(min_interval=0).status(tracked, "Littelfuse") == {"error": "introuvable chez Digi-Key"}


def test_malformed_product_fields_never_crash_nor_leak_objects():
    p = {"ManufacturerProductNumber": "LM317T", "Manufacturer": {"Name": "Texas Instruments"},
         "ProductStatus": {"Status": {"x": 1}}, "ProductUrl": ["javascript:x"], "EndOfLife": True}
    assert Fake(TOKEN, found(p)).client().status("LM317T", "Texas Instruments") == {"status": "Obsolete",
                                                                                   "product_url": None}
    p["Manufacturer"] = "Texas Instruments"  # pas un objet : fabricant non confirmé
    assert Fake(TOKEN, found(p)).client().status("LM317T", "Texas Instruments") == {"error": "introuvable chez Digi-Key"}


def test_end_of_life_flag_and_unsafe_url():
    p = product(status="", url="javascript:alert(1)")
    p["EndOfLife"] = True
    assert Fake(TOKEN, found(p)).client().status("LM317T", "Texas Instruments") == {"status": "Obsolete",
                                                                                   "product_url": None}


def test_expired_token_renewed_once():
    f = Fake(TOKEN, http_error(401), {"access_token": "tok2"}, found(product()))
    assert f.client(min_interval=0).status("LM317T", "Texas Instruments")["status"] == "Last Time Buy"
    assert f.requests[-1].headers["Authorization"] == "Bearer tok2" and f.requests[-1].get_method() == "POST"
    assert json.loads(f.requests[-1].data)["Keywords"] == "LM317T"  # corps renvoyé à l'identique


def test_rate_limited_is_a_transient_error_with_retry_after():
    """429 : plus de nouvel essai dans le client, c'est le passage qui reprend (voir test_scan_rate_limited…)."""
    f = Fake(TOKEN, http_error(429, {"Retry-After": "7"}))
    with pytest.raises(digikey.TransientError, match=r"HTTP 429 \(recherche de « LM317T »\)") as exc:
        f.client(min_interval=0).status("LM317T", "Texas Instruments")
    assert exc.value.retry_after == 7 and len(f.requests) == 2 and f.sleeps == []
    for headers in (None, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}):  # absent ou date : 10 s
        with pytest.raises(digikey.TransientError) as exc:
            Fake(TOKEN, http_error(429, headers)).client().status("LM317T", "Texas Instruments")
        assert exc.value.retry_after == digikey.RETRY_AFTER == 10


@pytest.mark.parametrize("answers,match", [
    ((http_error(401),), "authentification Digi-Key refusée"),
    (({},), "jeton absent"),
    ((urllib.error.URLError("dns"),), "injoignable"),
    ((http_error(503),), "authentification Digi-Key indisponible : HTTP 503"),
    ((TOKEN, http_error(500)), "HTTP 500"),
    ((TOKEN, http_error(400)), r"HTTP 400 \(recherche de « LM317T »\)"),  # refusée : erreur nommée, pas « introuvable »
    ((TOKEN, OSError("reset")), "injoignable"),
    ((TOKEN, ConnectionResetError(104, "Connection reset by peer")), r"injoignable \(ConnectionResetError\)"),
])
def test_errors_never_leak_secrets(answers, match):
    with pytest.raises(digikey.DigiKeyError, match=match) as exc:
        Fake(*answers).client().status("LM317T", "Texas Instruments")
    assert "s3cr3t" not in str(exc.value) and "secret-body" not in str(exc.value)


@pytest.mark.parametrize("fault", [
    ConnectionResetError(104, "Connection reset by peer"), ConnectionAbortedError(), ConnectionRefusedError(),
    urllib.error.URLError(ConnectionResetError()), urllib.error.URLError("dns"), TimeoutError("timed out"),  # socket.timeout
    http.client.IncompleteRead(b"{"), http.client.RemoteDisconnected(),
    http_error(500), http_error(502), http_error(503), http_error(504),
])
@pytest.mark.parametrize("during", ["jeton", "recherche"])
def test_transient_errors(fault, during):
    """Panne passagère, à l'obtention du jeton comme à la recherche : TransientError, sans nouvel essai dans le
    client (le passage reprend). Toujours une DigiKeyError : le contrat de status() est inchangé."""
    f = Fake(fault) if during == "jeton" else Fake(TOKEN, fault)
    with pytest.raises(digikey.TransientError) as exc:
        f.client(min_interval=0).status("LM317T", "Texas Instruments")
    assert isinstance(exc.value, digikey.DigiKeyError) and exc.value.retry_after is None
    assert not f.answers and f.sleeps == []  # une seule tentative, aucune attente
    assert "s3cr3t" not in str(exc.value) and "secret-body" not in str(exc.value)


@pytest.mark.parametrize("answers", [
    (http_error(400),), (http_error(401),), (http_error(403),), (OSError("disque"),),  # jeton refusé ou illisible
    (TOKEN, http_error(400)), (TOKEN, http_error(403)),
    (TOKEN, http_error(401), TOKEN, http_error(401)),                    # 401 après renouvellement du jeton
    (TOKEN, OSError("disque")), (TOKEN, ValueError("JSON")),
])
def test_permanent_errors_are_not_transient(answers):
    f = Fake(*answers)
    with pytest.raises(digikey.DigiKeyError) as exc:
        f.client(min_interval=0).status("LM317T", "Texas Instruments")
    assert not isinstance(exc.value, digikey.TransientError) and not f.answers


def test_retry_delay_progressive_with_jitter_and_ceiling(monkeypatch):
    t = digikey.TransientError("x")
    for factor in (0.8, 1.2):
        monkeypatch.setattr(digikey.random, "uniform", lambda a, b, k=factor: k)
        assert [digikey.retry_delay(n, t) for n in range(1, 10)] == pytest.approx(
            [min(base * factor, 60) for base in (5, 10, 20, 30, 45, 60, 60, 60, 60)])
    monkeypatch.undo()
    delays = [digikey.retry_delay(n, t) for n in range(1, 10) for _ in range(50)]
    assert min(delays) >= 4 and max(delays) <= digikey.BACKOFF[-1] == 60
    assert digikey.retry_delay(1, digikey.TransientError("429", retry_after=7)) == 7  # Retry-After, sans aléa
    assert digikey.retry_delay(1, digikey.TransientError("429", retry_after=600)) == 60


def test_credentials_and_envfile(env, monkeypatch):
    monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
    monkeypatch.delenv("DIGIKEY_CLIENT_SECRET", raising=False)
    assert digikey.credentials() is None
    (env / ".env").write_text("# DIGIKEY_CLIENT_ID=commentaire\nDIGIKEY_CLIENT_ID='cid'\n"
                              "DIGIKEY_CLIENT_SECRET=\"sec\"\nVIDE=\n")
    assert digikey.credentials() == ("cid", "sec")
    assert envfile.get("VIDE") is None and envfile.get("ABSENTE") is None
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "depuis-env")
    assert digikey.credentials() == ("depuis-env", "sec")
    (env / ".env").unlink()
    assert envfile.get("DIGIKEY_CLIENT_SECRET") is None


# --------------------------------------------------------------------------
# Passage complet (components_scan) : reprises sur incident réseau
# --------------------------------------------------------------------------
class Api(Fake):
    """Digi-Key d'un passage complet : un jeton, puis la fiche « Active » de chaque référence cherchée, sauf
    les pannes `faults` (numéro de la requête, 1 = la première -> exception levée)."""

    def __init__(self, slug, faults=None):
        super().__init__()
        self.makers = {c["mpn"]: c["manufacturer"] for c in comp.load_components(slug)["components"]}
        self.faults = faults or {}

    def urlopen(self, req, timeout):
        self.requests.append(req)
        fault = self.faults(len(self.requests)) if callable(self.faults) else self.faults.get(len(self.requests))
        if fault:
            raise fault
        if req.full_url.endswith(digikey.TOKEN_PATH):
            return Resp(json.dumps(TOKEN).encode())
        mpn = json.loads(req.data)["Keywords"]
        return Resp(json.dumps(found(product(mpn, self.makers[mpn], status="Active"))).encode())

    def searched(self):
        return [json.loads(r.data)["Keywords"] for r in self.requests if not r.full_url.endswith(digikey.TOKEN_PATH)]


def reset():
    return ConnectionResetError(104, "Connection reset by peer")


def checked(slug):
    return {c["mpn"]: (c.get("dk") or {}).get("checked_at") for c in comp.load_components(slug)["components"]}


def test_scan_resumes_after_connection_resets(watch):  # noqa: F811
    """Incident du 12/10/2026 : un seul ConnectionResetError arrêtait le passage (230 / 386, les 156 suivantes
    jamais interrogées) alors que l'API répondait quelques minutes plus tard. La phase reprend après une attente,
    à la référence interrompue."""
    api = Api(watch.slug, {2: reset(), 3: reset()})  # recherche de LM317T coupée deux fois
    out = service.components_scan(watch.slug, client=api.client(min_interval=0))
    assert out["digikey"]["checked"] == 3 and "error" not in out["digikey"]
    assert set(checked(watch.slug).values()) == {comp.today()}
    assert api.searched() == ["LM317T", "LM317T", "LM317T", "STM32F103C8T6", "NE555P"]
    assert len(api.sleeps) == 2 and 4 <= api.sleeps[0] <= 6 and 8 <= api.sleeps[1] <= 12  # 5 s puis 10 s, ± 20 %
    assert out["digikey"]["incidents"] == 2


def test_scan_gives_up_after_ten_failures(watch, capsys):  # noqa: F811
    """Panne qui dure : exactement 10 tentatives, puis abandon de la phase. Les références restent non vérifiées
    (date vide) et partent au lot web, comme avant ; l'arrêt est nommé, jamais présenté comme un succès."""
    api = Api(watch.slug, lambda n: reset())
    out = service.components_scan(watch.slug, client=api.client(min_interval=0))
    dk = out["digikey"]
    assert len(api.requests) == digikey.MAX_ATTEMPTS == 10 and len(api.sleeps) == 9  # rien après la 10e
    assert dk["checked"] == 0 and dk["incidents"] == 10
    assert dk["error"] == ("API Digi-Key injoignable (ConnectionResetError) (recherche de « LM317T ») : "
                           "abandon après 10 tentatives, reprise au prochain passage")
    assert set(checked(watch.slug).values()) == {None}  # jamais marquées vérifiées
    assert [b["mpn"] for b in out["batch"]] == ["LM317T", "STM32F103C8T6"]  # lot web du jour (batch_size 2)
    assert all(b["digikey_status"] is None and b["digikey_error"] is None for b in out["batch"])
    assert comp.load_components(watch.slug)["batch"] == [comp.key_of(b["mpn"], b["manufacturer"]) for b in out["batch"]]
    log = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("Digi-Key — ")]
    assert len(log) == 10 and log[0].startswith("Digi-Key — tentative 1/10 : API Digi-Key injoignable "
                                                "(ConnectionResetError) (recherche de « LM317T ») ; reprise dans ")
    assert log[-1].endswith("tentative 10/10 : API Digi-Key injoignable (ConnectionResetError) (recherche de "
                            "« LM317T ») ; abandon, reprise au prochain passage")
    detail = service.read_audit(1)[0]
    assert detail["action"] == "components-scan" and detail["detail"] == (
        "Digi-Key 0 vérifié(s), 0 avis ; lot web 2 ; Digi-Key : " + dk["error"])
    # le passage suivant (API revenue) reprend tout
    again = Api(watch.slug)
    assert service.components_scan(watch.slug, client=again.client(min_interval=0))["digikey"]["checked"] == 3


@pytest.mark.parametrize("factor", [0.8, 1.2])
def test_scan_waits_are_progressive_and_capped(watch, monkeypatch, factor):  # noqa: F811
    """Attentes vues par le sleep injecté : 5, 10, 20, 30, 45 s puis 60 s (plafond), ± 20 % ; ≈ 6 min au total."""
    monkeypatch.setattr(digikey.random, "uniform", lambda a, b: factor)
    api = Api(watch.slug, lambda n: reset())
    service.components_scan(watch.slug, client=api.client(min_interval=0))
    assert api.sleeps == pytest.approx([min(b * factor, 60) for b in (5, 10, 20, 30, 45, 60, 60, 60, 60)])
    assert max(api.sleeps) <= digikey.BACKOFF[-1] and sum(api.sleeps) <= 6.2 * 60


def test_scan_cut_in_the_middle_of_the_list(watch, monkeypatch, capsys):  # noqa: F811
    """Cas réel : coupure au milieu de la liste (ADXL335, 3e de 6). Après reprise, les 6 sont vérifiées, chacune
    interrogée une seule fois (la référence coupée : une fois de plus) ; l'avancement suivi le montre."""
    service.remove_components(watch.slug, ["NE555P"])
    service.import_components(watch.slug, comp.parse_components(
        "ADXL335;Analog Devices\nBC547;onsemi\nTL431;Texas Instruments\nNE555P;Texas Instruments"))
    order = [c["mpn"] for c in comp.load_components(watch.slug)["components"]]
    assert order == ["LM317T", "STM32F103C8T6", "ADXL335", "BC547", "TL431", "NE555P"]
    manual(watch.slug)
    monkeypatch.setattr(progress, "MIN_INTERVAL_S", 0)
    api = Api(watch.slug, {4: reset()})  # jeton, LM317T, STM32F103C8T6, puis ADXL335 coupée
    seen = []  # avancement publié pendant l'attente

    def sleep(seconds):
        api.sleeps.append(seconds)
        seen.append(progress.read(watch.slug)["digikey"])
    client = digikey.Client("cid", "s3cr3t", urlopen=api.urlopen, sleep=sleep, clock=lambda: 0.0, min_interval=0)
    out = service.components_scan(watch.slug, client=client)
    assert out["digikey"]["checked"] == 6 and out["digikey"]["incidents"] == 1 and "error" not in out["digikey"]
    assert api.searched() == ["LM317T", "STM32F103C8T6", "ADXL335", "ADXL335", "BC547", "TL431", "NE555P"]
    assert set(checked(watch.slug).values()) == {comp.today()}
    assert len(seen) == 1 and seen[0]["done"] == 2 and seen[0]["current"] == "ADXL335"
    assert re.fullmatch(r"tentative 1/10 : API Digi-Key injoignable \(ConnectionResetError\) \(recherche de "
                        r"« ADXL335 »\) ; reprise dans [4-6] s", seen[0]["note"])
    final = progress.read(watch.slug)["digikey"]
    assert final == {"done": 6, "total": 6, "note": "1 incident(s) réseau, passage repris après attente"}
    view = service.run_progress(watch.slug, running=True)
    assert view["warning"] == final["note"]  # signalé, pas un succès muet
    assert service.read_audit(1)[0]["detail"].endswith("; Digi-Key : 1 incident(s) réseau, passage repris après attente")
    assert capsys.readouterr().err.count("Digi-Key — tentative ") == 1


def test_scan_rate_limited_respects_retry_after(watch):  # noqa: F811
    """429 : Retry-After respecté (10 s sans en-tête, 60 s au plus), et compté parmi les tentatives."""
    api = Api(watch.slug, {2: http_error(429, {"Retry-After": "7"}), 4: http_error(429, None),
                           5: http_error(429, {"Retry-After": "600"})})
    out = service.components_scan(watch.slug, client=api.client(min_interval=0))
    assert api.sleeps == [7, 10, 60] and out["digikey"]["incidents"] == 3 and out["digikey"]["checked"] == 3
    assert api.searched() == ["LM317T", "LM317T", "STM32F103C8T6", "STM32F103C8T6", "STM32F103C8T6", "NE555P"]


@pytest.mark.parametrize("fault,searched,message", [
    (http_error(400), ["LM317T"], "API Digi-Key : HTTP 400 (recherche de « LM317T »)"),
    (http_error(403), ["LM317T"], "API Digi-Key : HTTP 403 (recherche de « LM317T »)"),
    (http_error(404), ["LM317T", "STM32F103C8T6", "NE555P"], None),  # introuvable : pas une panne
])
def test_scan_never_retries_permanent_errors(watch, fault, searched, message):  # noqa: F811
    api = Api(watch.slug, {2: fault})
    out = service.components_scan(watch.slug, client=api.client(min_interval=0))
    assert api.searched() == searched and api.sleeps == [] and "incidents" not in out["digikey"]
    assert out["digikey"].get("error") == message
    if message:  # erreur permanente : arrêt immédiat, comme avant ; aucune référence marquée vérifiée
        assert out["digikey"]["checked"] == 0 and set(checked(watch.slug).values()) == {None}
    else:
        assert out["digikey"]["errors"] == 1 and out["digikey"]["checked"] == 2


def test_scan_expired_token_renewed_once_then_refused(watch):  # noqa: F811
    api = Api(watch.slug, {2: http_error(401), 4: http_error(401)})  # jeton refusé, renouvelé, refusé encore
    out = service.components_scan(watch.slug, client=api.client(min_interval=0))
    assert out["digikey"]["error"] == "API Digi-Key : HTTP 401 (recherche de « LM317T »)"
    assert len(api.requests) == 4 and api.sleeps == [] and "incidents" not in out["digikey"]


def test_scan_budget_keeps_priority_over_retries(watch, monkeypatch):  # noqa: F811
    """Le budget de temps et MAX_CALLS gardent la priorité : arrêt signalé comme avant, incidents comptés."""
    api = Api(watch.slug, {2: reset()})
    out = service.components_scan(watch.slug, client=api.client(min_interval=0), budget_s=3)  # attente ≥ 4 s
    dk = out["digikey"]
    assert api.sleeps == [] and dk["checked"] == 0 and dk["incidents"] == 1
    assert dk["stopped"] == "budget de temps ou d'appels atteint : suite au prochain passage"
    assert dk["error"] == ("API Digi-Key injoignable (ConnectionResetError) (recherche de « LM317T ») : abandon après "
                           "1 tentative(s), budget de temps ou d'appels atteint, reprise au prochain passage")
    assert set(checked(watch.slug).values()) == {None} and len(out["batch"]) == 2
    monkeypatch.setattr(digikey, "MAX_CALLS", 3)  # quota d'appels atteint pendant la panne : pas d'attente inutile
    api = Api(watch.slug, lambda n: reset())
    dk = service.components_scan(watch.slug, client=api.client(min_interval=0))["digikey"]
    assert len(api.requests) == 3 and len(api.sleeps) == 2 and dk["incidents"] == 3
    assert dk["stopped"] == "budget de temps ou d'appels atteint : suite au prochain passage"
    assert dk["error"].endswith(": abandon après 3 tentative(s), budget de temps ou d'appels atteint, reprise au "
                                "prochain passage")
    monkeypatch.setattr(digikey, "MAX_CALLS", 4)  # panne surmontée, puis quota atteint : les deux sont signalés
    api = Api(watch.slug, {2: reset()})  # jeton, LM317T coupée, LM317T, STM32F103C8T6 ; NE555P pour plus tard
    dk = service.components_scan(watch.slug, client=api.client(min_interval=0))["digikey"]
    assert dk["checked"] == 2 and dk["incidents"] == 1 and "error" not in dk and "stopped" in dk
    assert checked(watch.slug)["NE555P"] is None
    assert service.read_audit(1)[0]["detail"] == (
        "Digi-Key 2 vérifié(s), 0 avis ; lot web 2 ; Digi-Key : budget de temps ou d'appels atteint : suite au "
        "prochain passage ; 1 incident(s) réseau, passage repris après attente")


def test_status_only_scan_reports_the_give_up(watch, capsys):  # noqa: F811
    """Vérification à la demande (`--status-only`) : l'abandon est affiché et l'avancement suivi le signale."""
    manual(watch.slug, kind="statuts")
    api = Api(watch.slug, lambda n: reset())
    service.components_scan(watch.slug, client=api.client(min_interval=0), plan_batch=False)
    view = service.run_progress(watch.slug, running=False)
    assert view["finished"] and view["summary"] == "Terminé : 0 / 3 référence(s)"
    assert view["warning"].endswith(": abandon après 10 tentatives, reprise au prochain passage")
