"""Statut de cycle de vie des références chez Digi-Key (API Product Information v4).

OAuth « 2-legged » (identifiants client, sans intervention humaine) : DIGIKEY_CLIENT_ID et
DIGIKEY_CLIENT_SECRET, saisis dans l'interface (page Sources) ou définis dans le `.env`. Le jeton reste en mémoire le temps d'un passage.
Les avis PCN de Digi-Key exigent l'OAuth « 3-legged » (connexion interactive) : non utilisés.

Recherche d'une référence : fiche produit exacte (un appel), puis, si Digi-Key ne la reconnaît pas,
recherche par mot-clé (un appel de plus). Une référence encore introuvable part à la recherche web
de l'agent, qui établit lui-même son statut (`components.WEB_STATUSES`).

Bibliothèque standard uniquement ; débit limité (l'offre gratuite plafonne à 120 requêtes
par minute et environ 1000 par jour). Les secrets ne figurent jamais dans les erreurs.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import components
from . import credentials as creds

API = "https://api.digikey.com"
TOKEN_PATH = "/v1/oauth2/token"
DETAILS_PATH = "/products/v4/search/{}/productdetails"
KEYWORD_PATH = "/products/v4/search/keyword"
KEYWORD_LIMIT = 50  # maximum de l'API : une référence courante (1N4148) a beaucoup de variantes
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
    cid, secret = creds.get("DIGIKEY_CLIENT_ID"), creds.get("DIGIKEY_CLIENT_SECRET")
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
            data = json.loads(r.read().decode() or "{}")
        return data if isinstance(data, dict) else {}

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

    def check(self) -> None:
        """Vérifie les identifiants (obtention d'un jeton) ; lève DigiKeyError sinon."""
        self._token = self._auth()

    def _call(self, path: str, body: dict | None = None) -> dict | None:
        """Réponse de l'API (GET, ou POST JSON si `body`) ; None si Digi-Key ne trouve rien : 404, ou 400 d'une
        fiche produit (référence inconnue, ou ambiguë). Un 400 de la recherche est une requête mal formée :
        une erreur, jamais « introuvable » pour toute la liste."""
        data = None if body is None else json.dumps(body).encode()
        for attempt in (1, 2):
            if self._token is None:
                self._token = self._auth()
            headers = {"Authorization": f"Bearer {self._token}", "X-DIGIKEY-Client-Id": self._id,
                       "X-DIGIKEY-Locale-Language": "en", "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(self._base + path, data=data, headers=headers,
                                         method="GET" if data is None else "POST")
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
                if exc.code == 404 or (exc.code == 400 and body is None):
                    return None
                raise DigiKeyError(f"API Digi-Key : HTTP {exc.code}") from None
            except (urllib.error.URLError, OSError, ValueError) as exc:
                raise DigiKeyError(f"API Digi-Key injoignable ({type(exc).__name__})") from None
        raise DigiKeyError("API Digi-Key : jeton refusé deux fois")

    def _search(self, mpn: str, manufacturer: str) -> dict | None:
        """Fiche trouvée par la recherche par mot-clé : MPN identique d'abord (« MAX232CPE+ » avant
        « MAX232CPE »), puis à la ponctuation près, toujours du même fabricant."""
        res = self._call(KEYWORD_PATH, {"Keywords": mpn, "Limit": KEYWORD_LIMIT, "Offset": 0}) or {}
        ok = [p for k in ("ExactMatches", "Products") if isinstance(res.get(k), list) for p in res[k]
              if not _mismatch(p, mpn, manufacturer)]
        exact = components.norm_mpn(mpn)
        return next((p for p in ok if components.norm_mpn(_field(p, "ManufacturerProductNumber")) == exact),
                    ok[0] if ok else None)

    def status(self, mpn: str, manufacturer: str) -> dict:
        """{'status', 'product_url'} ou {'error'} (référence introuvable, fabricant différent)."""
        product = (self._call(DETAILS_PATH.format(urllib.parse.quote(mpn, safe=""))) or {}).get("Product")
        error = _mismatch(product, mpn, manufacturer)
        if error:
            # Référence de plusieurs fabricants (la fiche exige alors l'identifiant du fabricant), ou écrite sans
            # sa ponctuation (« 2N7002215 » pour « 2N7002,215 ») : la recherche par mot-clé la retrouve.
            product = self._search(mpn, manufacturer)
            if product is None:
                return {"error": error}
        status = _field(product, "ProductStatus", "Status").strip()
        if not status and product.get("EndOfLife"):
            status = "Obsolete"
        url = components.normalize_url(product.get("ProductUrl") or "")
        return {"status": status[:40] or "Inconnu", "product_url": url}


def _field(product: dict, *keys: str) -> str:
    """Texte d'un champ imbriqué de la fiche (« Manufacturer » / « Name »…), vide s'il manque ou est mal formé."""
    value = product
    for k in keys:
        value = value.get(k) if isinstance(value, dict) else None
    return str(value or "")


def _mismatch(product, mpn: str, manufacturer: str) -> str:
    """Motif du refus d'une fiche Digi-Key ; vide si c'est bien la référence suivie (ponctuation ignorée)."""
    if not isinstance(product, dict):
        return "introuvable chez Digi-Key"
    got_mpn, got_mfr = _field(product, "ManufacturerProductNumber"), _field(product, "Manufacturer", "Name")
    if components.loose_mpn(got_mpn) != components.loose_mpn(mpn):
        return f"référence différente chez Digi-Key ({got_mpn[:64]})"
    if not components.same_manufacturer(got_mfr, manufacturer):
        return f"fabricant différent chez Digi-Key ({got_mfr[:80]})"
    return ""
