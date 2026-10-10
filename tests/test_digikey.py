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
    return {"Product": {"ManufacturerProductNumber": mpn, "Manufacturer": {"Name": mfr},
                        "ProductStatus": {"Status": status}, "ProductUrl": url}}


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
    f = Fake(TOKEN, product(), product(mpn="AB/C-1", status="Active", url=None), {})
    c = f.client()
    assert c.status("LM317T", "Texas Instruments Inc.") == {"status": "Last Time Buy",
                                                             "product_url": "https://www.digikey.com/p/1"}
    auth, details = f.requests[0], f.requests[1]
    assert auth.full_url.endswith("/v1/oauth2/token") and b"grant_type=client_credentials" in auth.data
    assert details.full_url.endswith("/products/v4/search/LM317T/productdetails") and details.get_method() == "GET"
    assert details.headers["Authorization"] == "Bearer tok" and details.headers["X-digikey-client-id"] == "cid"
    assert c.calls == 2  # fiche trouvée : aucune recherche de plus
    assert c.status("AB/C-1", "Analog Devices")["error"].startswith("fabricant différent")
    assert f.requests[2].full_url.endswith("/search/AB%2FC-1/productdetails")  # le « / » ne change pas de route
    assert c.calls == 4 and f.sleeps == [digikey.MIN_INTERVAL] * 3  # débit limité, recherche comprise


def test_mismatch_and_not_found():
    # chaque refus de la fiche est suivi d'une recherche par mot-clé, ici sans résultat : motif de la fiche gardé
    f = Fake(TOKEN, product(mpn="LM317T-DG"), {}, product(mfr="STMicroelectronics"), {"Products": []},
             http_error(404), http_error(404), {}, {"ExactMatches": "x", "Products": None})
    c = f.client(min_interval=0)
    assert c.status("LM317T", "Texas Instruments")["error"].startswith("référence différente")
    assert c.status("LM317T", "Texas Instruments")["error"].startswith("fabricant différent")
    assert c.status("LM317T", "Texas Instruments") == {"error": "introuvable chez Digi-Key"}
    assert c.status("LM317T", "Texas Instruments") == {"error": "introuvable chez Digi-Key"}
    assert c.calls == 9 and not f.answers


def search_result(*products, key="ExactMatches"):
    return {key: [p["Product"] for p in products], "ProductsCount": len(products)}


def test_ambiguous_reference_found_by_keyword_search():
    """« 1N4148 » existe chez plusieurs fabricants : la fiche répond 400 sans identifiant de fabricant."""
    f = Fake(TOKEN, http_error(400), search_result(product("1N4148", "Vishay", status="Active"),
                                                   product("1N4148", "onsemi", status="Obsolete", url="https://dk/o")))
    c = f.client(min_interval=0)
    assert c.status("1N4148", "onsemi") == {"status": "Obsolete", "product_url": "https://dk/o"}
    search = f.requests[-1]
    assert search.full_url == "https://api.digikey.com/products/v4/search/keyword" and search.get_method() == "POST"
    assert json.loads(search.data) == {"Keywords": "1N4148", "Limit": 50, "Offset": 0}
    assert search.headers["Content-type"] == "application/json" and search.headers["Authorization"] == "Bearer tok"


def test_punctuation_and_exact_reference_preferred():
    # Nexperia : « 2N7002215 » saisi, « 2N7002,215 » chez Digi-Key (fabricant « Nexperia USA Inc. »)
    f = Fake(TOKEN, http_error(404), search_result(product("2N7002,215", "Nexperia USA Inc.", status="Active"),
                                                   key="Products"))
    assert f.client(min_interval=0).status("2N7002215", "Nexperia")["status"] == "Active"
    # deux fiches à la ponctuation près : celle au MPN identique l'emporte, même placée après
    f = Fake(TOKEN, http_error(404), search_result(product("BAV99,215", "Nexperia", status="Obsolete"),
                                                   product("BAV99215", "Nexperia", status="Active")))
    assert f.client(min_interval=0).status("BAV99215", "Nexperia")["status"] == "Active"
    # le « + » (sans plomb) n'est pas une ponctuation : jamais confondu avec la version au plomb
    f = Fake(TOKEN, http_error(404), search_result(product("MAX232CPE", "Analog Devices", status="Obsolete")))
    assert f.client(min_interval=0).status("MAX232CPE+", "Analog Devices") == {"error": "introuvable chez Digi-Key"}


def test_malformed_product_fields_never_crash():
    p = {"Product": {"ManufacturerProductNumber": "LM317T", "Manufacturer": "Texas Instruments",
                     "ProductStatus": ["Active"]}}
    f = Fake(TOKEN, p, ["pas", "un", "objet"])
    assert f.client(min_interval=0).status("LM317T", "Texas Instruments")["error"].startswith("fabricant différent")


def test_end_of_life_flag_and_unsafe_url():
    p = product(status="", url="javascript:alert(1)")
    p["Product"]["EndOfLife"] = True
    assert Fake(TOKEN, p).client().status("LM317T", "Texas Instruments") == {"status": "Obsolete",
                                                                             "product_url": None}


def test_expired_token_renewed_once():
    f = Fake(TOKEN, http_error(401), {"access_token": "tok2"}, product())
    assert f.client(min_interval=0).status("LM317T", "Texas Instruments")["status"] == "Last Time Buy"
    assert f.requests[-1].headers["Authorization"] == "Bearer tok2"


def test_rate_limited_waits_retry_after():
    f = Fake(TOKEN, http_error(429, {"Retry-After": "7"}), product())
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
    ((TOKEN, http_error(404), http_error(400)), "HTTP 400"),  # recherche mal formée : erreur, pas « introuvable »
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
