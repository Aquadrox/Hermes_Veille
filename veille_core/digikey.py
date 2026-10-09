"""Statut de cycle de vie des références chez Digi-Key (API Product Information v4).

OAuth « 2-legged » (identifiants client, sans intervention humaine) : DIGIKEY_CLIENT_ID et
DIGIKEY_CLIENT_SECRET, lus par `envfile`. Le jeton reste en mémoire le temps d'un passage.
Les avis PCN de Digi-Key exigent l'OAuth « 3-legged » (connexion interactive) : non utilisés.

Bibliothèque standard uniquement ; débit limité (l'offre gratuite plafonne à 120 requêtes
par minute et environ 1000 par jour). Les secrets ne figurent jamais dans les erreurs.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import components, envfile

API = "https://api.digikey.com"
TOKEN_PATH = "/v1/oauth2/token"
DETAILS_PATH = "/products/v4/search/{}/productdetails"
MIN_INTERVAL = 0.6  # secondes entre deux requêtes (< 120/min)
MAX_CALLS = 900     # par passage (quota journalier de l'offre gratuite)

# Statut Digi-Key (minuscules) -> type d'avis. Active / Preliminary : pas d'avis.
STATUS_TYPES = {
    "obsolete": "OBSOLETE",
    "last time buy": "LTB",
    "not for new designs": "NRND",
    "discontinued at digi-key": "AUTRE",
}


class DigiKeyError(RuntimeError):
    pass


def credentials() -> tuple[str, str] | None:
    cid, secret = envfile.get("DIGIKEY_CLIENT_ID"), envfile.get("DIGIKEY_CLIENT_SECRET")
    return (cid, secret) if cid and secret else None


class Client:
    def __init__(self, client_id: str, client_secret: str, *, base: str = API,
                 urlopen=urllib.request.urlopen, sleep=time.sleep, clock=time.monotonic,
                 min_interval: float = MIN_INTERVAL):
        self._id, self._secret, self._base = client_id, client_secret, base
        self._urlopen, self._sleep, self._clock = urlopen, sleep, clock
        self._interval, self._last, self._token = min_interval, None, None
        self.calls = 0

    def _send(self, req: urllib.request.Request) -> dict:
        if self._last is not None:
            wait = self._interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()
        self.calls += 1
        with self._urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")

    def _auth(self) -> str:
        body = urllib.parse.urlencode({"client_id": self._id, "client_secret": self._secret,
                                       "grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(self._base + TOKEN_PATH, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            token = self._send(req).get("access_token")
        except urllib.error.HTTPError as exc:
            raise DigiKeyError(f"authentification Digi-Key refusée (HTTP {exc.code})") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise DigiKeyError(f"API Digi-Key injoignable ({type(exc).__name__})") from None
        if not token:
            raise DigiKeyError("authentification Digi-Key : jeton absent de la réponse")
        return token

    def _details(self, mpn: str) -> dict | None:
        """Fiche produit, ou None si la référence est inconnue ou ambiguë chez Digi-Key."""
        url = self._base + DETAILS_PATH.format(urllib.parse.quote(mpn, safe=""))
        for attempt in (1, 2):
            if self._token is None:
                self._token = self._auth()
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self._token}", "X-DIGIKEY-Client-Id": self._id,
                "X-DIGIKEY-Locale-Language": "en", "Accept": "application/json"})
            try:
                return self._send(req)
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 1:
                    self._token = None  # jeton expiré : un seul renouvellement
                    continue
                if exc.code == 429 and attempt == 1:
                    retry = str((exc.headers or {}).get("Retry-After") or "")
                    self._sleep(min(int(retry), 60) if retry.isdigit() else 10)
                    continue
                if exc.code in (400, 404):
                    return None
                raise DigiKeyError(f"API Digi-Key : HTTP {exc.code}") from None
            except (urllib.error.URLError, OSError, ValueError) as exc:
                raise DigiKeyError(f"API Digi-Key injoignable ({type(exc).__name__})") from None
        raise DigiKeyError("API Digi-Key : jeton refusé deux fois")

    def status(self, mpn: str, manufacturer: str) -> dict:
        """{'status', 'product_url'} ou {'error'} (référence introuvable, fabricant différent)."""
        product = (self._details(mpn) or {}).get("Product")
        if not isinstance(product, dict):
            return {"error": "introuvable chez Digi-Key"}
        got_mpn = product.get("ManufacturerProductNumber") or ""
        got_mfr = (product.get("Manufacturer") or {}).get("Name") or ""
        if components.norm_mpn(got_mpn) != components.norm_mpn(mpn):
            return {"error": f"référence différente chez Digi-Key ({got_mpn[:64]})"}
        if not components.same_manufacturer(got_mfr, manufacturer):
            return {"error": f"fabricant différent chez Digi-Key ({got_mfr[:80]})"}
        status = str((product.get("ProductStatus") or {}).get("Status") or "").strip()
        if not status and product.get("EndOfLife"):
            status = "Obsolete"
        url = components.normalize_url(product.get("ProductUrl") or "")
        return {"status": status[:40] or "Inconnu", "product_url": url}
