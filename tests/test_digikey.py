"""Client Digi-Key (urlopen simulé) et lecture des secrets (envfile)."""
import io
import json
import urllib.error

import pytest

from veille_core import digikey, envfile


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
    for _ in range(5):  # autre référence seulement, 404, vide, champs mal typés, réponse qui n'est pas un objet
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


def test_rate_limited_waits_retry_after():
    f = Fake(TOKEN, http_error(429, {"Retry-After": "7"}), found(product()))
    assert f.client(min_interval=0).status("LM317T", "Texas Instruments")["status"] == "Last Time Buy"
    assert 7 in f.sleeps
    f = Fake(TOKEN, http_error(429, None), http_error(429, None))
    with pytest.raises(digikey.DigiKeyError, match="HTTP 429"):
        f.client(min_interval=0).status("LM317T", "Texas Instruments")
    assert 10 in f.sleeps


@pytest.mark.parametrize("answers,match", [
    ((http_error(401),), "authentification Digi-Key refusée"),
    (({},), "jeton absent"),
    ((urllib.error.URLError("dns"),), "injoignable"),
    ((TOKEN, http_error(500)), "HTTP 500"),
    ((TOKEN, http_error(400)), r"HTTP 400 \(recherche de « LM317T »\)"),  # refusée : erreur nommée, pas « introuvable »
    ((TOKEN, OSError("reset")), "injoignable"),
])
def test_errors_never_leak_secrets(answers, match):
    with pytest.raises(digikey.DigiKeyError, match=match) as exc:
        Fake(*answers).client().status("LM317T", "Texas Instruments")
    assert "s3cr3t" not in str(exc.value) and "secret-body" not in str(exc.value)


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
