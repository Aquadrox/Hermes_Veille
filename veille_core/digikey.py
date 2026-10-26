"""Statut de cycle de vie des références chez Digi-Key (API Product Information v4).

OAuth « 2-legged » (identifiants client, sans intervention humaine) : DIGIKEY_CLIENT_ID et
DIGIKEY_CLIENT_SECRET, saisis dans l'interface (page Sources) ou définis dans le `.env`. Le jeton reste en mémoire le temps d'un passage.
Les avis PCN de Digi-Key exigent l'OAuth « 3-legged » (connexion interactive) : non utilisés.

Une référence = une recherche par mot-clé (`/products/v4/search/keyword`). La fiche exacte
(`/search/{mpn}/productdetails`) n'est pas utilisée : en OAuth 2-legged, elle exige l'en-tête
X-DIGIKEY-Account-Id (documentation v4) et ne reconnaît ni une référence de plusieurs fabricants ni une
référence sans sa ponctuation. Une référence introuvable part à la recherche web de l'agent, qui établit
lui-même son statut (`components.WEB_STATUSES`).

La fiche trouvée donne aussi la catégorie et les caractéristiques de la référence (`_specs`) : la fiche
composant de l'interface, et les critères de la recherche d'alternatives (`similar`) — même requête par
mot-clé, sans mot-clé, filtrée sur la sous-catégorie et les valeurs des caractéristiques critiques.

Bibliothèque standard uniquement ; débit limité (l'offre gratuite plafonne à 120 requêtes
par minute et environ 1000 par jour). Les secrets ne figurent jamais dans les erreurs.

Erreurs : `TransientError` pour une panne passagère (réseau coupé, délai dépassé, HTTP 5xx, HTTP 429), que le
passage reprend après une attente (`retry_delay`, au plus MAX_ATTEMPTS fois, voir `service.components_scan`) ;
`DigiKeyError` pour une erreur permanente (HTTP 400, 403, jeton refusé après renouvellement), jamais réessayée.
"""
from __future__ import annotations

import http.client
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

from . import components
from . import credentials as creds

API = "https://api.digikey.com"
TOKEN_PATH = "/v1/oauth2/token"
KEYWORD_PATH = "/products/v4/search/keyword"
KEYWORD_LIMIT = 50  # maximum de l'API : une référence courante (1N4148) a beaucoup de variantes
MIN_INTERVAL = 0.6  # secondes entre deux requêtes (< 120/min)
MAX_CALLS = 900     # par passage (quota journalier de l'offre gratuite)
MAX_ATTEMPTS = 10   # pannes passagères tolérées par passage, toutes références confondues : abandon à la 10e
BACKOFF = (5, 10, 20, 30, 45, 60)  # attentes (s) avant la 1re, 2e… reprise ; la dernière est le plafond
JITTER = 0.2        # ± 20 % : des reprises qui ne tombent pas toutes à la même seconde
RETRY_AFTER = 10    # HTTP 429 sans en-tête Retry-After exploitable (s)
HTTP_TIMEOUT = 20   # s par requête (connexion, puis lecture)
MAX_PARAMS = 40     # caractéristiques gardées par fiche (une résistance en a une quinzaine)
MAX_DEPTH = 4       # niveaux de catégorie (« Resistors » › « Chip Resistor - Surface Mount »)

# Pannes passagères : le réseau ou le serveur, pas la requête. HTTPError (sous-classe d'URLError) est traitée
# avant. HTTPException : réponse tronquée (IncompleteRead) ou ligne d'état illisible, après une coupure.
TRANSIENT = (ConnectionError, TimeoutError, urllib.error.URLError, http.client.HTTPException)

# Statut Digi-Key (minuscules) -> type d'avis : les statuts de cycle de vie qui sont aussi un type d'avis (NRND, LTB,
# OBSOLETE), plus « Discontinued at Digi-Key ». Active / Preliminary : pas d'avis.
STATUS_TYPES = {**{s: t for s, t in components.DK_LIFECYCLE.items() if t in components.TYPES},
                "discontinued at digi-key": "AUTRE"}


class DigiKeyError(RuntimeError):
    pass


class TransientError(DigiKeyError):
    """Panne passagère : la même requête peut aboutir un peu plus tard. `retry_after` : attente demandée par
    Digi-Key (HTTP 429), en secondes, sinon None."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def retry_delay(failures: int, exc: TransientError) -> float:
    """Attente avant la reprise qui suit le `failures`-ième échec (1 = le premier) : le Retry-After d'un 429,
    sinon progressive (BACKOFF) à ± JITTER près ; jamais au-delà du plafond (BACKOFF[-1])."""
    if exc.retry_after is not None:
        return min(exc.retry_after, BACKOFF[-1])
    base = BACKOFF[min(failures, len(BACKOFF)) - 1]
    return min(base * random.uniform(1 - JITTER, 1 + JITTER), BACKOFF[-1])


def _http_error(exc: urllib.error.HTTPError, what: str) -> DigiKeyError:
    """Erreur HTTP -> passagère (5xx ; 429, avec l'attente demandée) ou permanente (4xx)."""
    if exc.code == 429:
        retry = str((exc.headers or {}).get("Retry-After") or "").strip()
        return TransientError(f"{what} : HTTP 429", int(retry) if retry.isdigit() else RETRY_AFTER)
    return (TransientError if exc.code >= 500 else DigiKeyError)(f"{what} : HTTP {exc.code}")


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
        with self._urlopen(req, timeout=HTTP_TIMEOUT) as r:
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
            if exc.code == 429 or exc.code >= 500:
                raise _http_error(exc, "authentification Digi-Key indisponible") from None
            raise DigiKeyError(f"authentification Digi-Key refusée (HTTP {exc.code})") from None
        except TRANSIENT as exc:
            raise TransientError(f"API Digi-Key injoignable ({type(exc).__name__})") from None
        except (OSError, ValueError) as exc:
            raise DigiKeyError(f"API Digi-Key injoignable ({type(exc).__name__})") from None
        if not token:
            raise DigiKeyError("authentification Digi-Key : jeton absent de la réponse")
        return token

    def check(self) -> None:
        """Vérifie les identifiants (obtention d'un jeton) ; lève DigiKeyError sinon."""
        self._token = self._auth()

    def pause(self, seconds: float) -> None:
        """Attente avant la reprise d'un passage interrompu par une panne passagère (sleep injectable)."""
        self._sleep(seconds)

    def _call(self, path: str, body: dict) -> dict | None:
        """Réponse JSON d'une requête POST ; None si Digi-Key ne trouve rien (404).

        Seul un jeton expiré (401) est renouvelé ici, une fois. Une panne passagère (dont le 429) lève
        TransientError sans nouvel essai : c'est le passage qui reprend, avec un compteur unique.
        """
        data = json.dumps(body).encode()
        for attempt in (1, 2):
            if self._token is None:
                self._token = self._auth()
            req = urllib.request.Request(self._base + path, data=data, method="POST", headers={
                "Authorization": f"Bearer {self._token}", "X-DIGIKEY-Client-Id": self._id,
                "X-DIGIKEY-Locale-Language": "en", "Accept": "application/json", "Content-Type": "application/json"})
            try:
                return self._send(req)
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 1:
                    self._token = None  # jeton expiré : un seul renouvellement
                    continue
                if exc.code == 404:
                    return None
                # 400 compris : requête refusée, une erreur affichée et jamais « introuvable » pour toute la liste
                raise _http_error(exc, "API Digi-Key") from None
            except TRANSIENT as exc:
                raise TransientError(f"API Digi-Key injoignable ({type(exc).__name__})") from None
            except (OSError, ValueError) as exc:
                raise DigiKeyError(f"API Digi-Key injoignable ({type(exc).__name__})") from None
        raise DigiKeyError("API Digi-Key : jeton refusé deux fois")

    def _search(self, body: dict, what: str) -> dict:
        """Recherche par mot-clé ; `what` complète le message d'une erreur (référence, catégorie)."""
        try:
            return self._call(KEYWORD_PATH, body) or {}
        except TransientError as exc:
            raise TransientError(f"{exc} ({what})", exc.retry_after) from None
        except DigiKeyError as exc:
            raise DigiKeyError(f"{exc} ({what})") from None

    def status(self, mpn: str, manufacturer: str) -> dict:
        """{'status', 'product_url'[, 'category', 'params']} ou {'error'} (référence introuvable, fabricant différent).

        Recherche par mot-clé (fiches au MPN exact, puis résultats) : elle retrouve une référence de plusieurs
        fabricants et une référence saisie sans sa ponctuation (« 2N7002215 » pour « 2N7002,215 »). Fiche du
        fabricant suivi uniquement, au MPN identique de préférence.
        """
        res = self._search({"Keywords": mpn, "Limit": KEYWORD_LIMIT, "Offset": 0}, f"recherche de « {mpn} »")
        key = components.loose_mpn(mpn)
        same_ref = [p for k in ("ExactMatches", "Products") if isinstance(res.get(k), list) for p in res[k]
                    if components.loose_mpn(_field(p, "ManufacturerProductNumber")) == key]
        mine = [p for p in same_ref if components.same_manufacturer(_field(p, "Manufacturer", "Name"), manufacturer)]
        if not mine:
            others = ", ".join(dict.fromkeys(_field(p, "Manufacturer", "Name") for p in same_ref))
            return {"error": f"fabricant différent chez Digi-Key ({others[:80]})" if others
                    else "introuvable chez Digi-Key"}
        exact = components.norm_mpn(mpn)
        product = next((p for p in mine if components.norm_mpn(_field(p, "ManufacturerProductNumber")) == exact), mine[0])
        status = _field(product, "ProductStatus", "Status").strip()
        if not status and product.get("EndOfLife"):
            status = "Obsolete"
        url = components.normalize_url(product.get("ProductUrl") or "")
        return {"status": status[:40] or "Inconnu", "product_url": url, **_specs(product)}

    def similar(self, category_id: str, filters: list[tuple[int, str]]) -> list[dict]:
        """Fiches de la sous-catégorie `category_id` dont les caractéristiques `filters` ([(ParameterId, ValueId)])
        ont ces valeurs (recherche paramétrique, un appel) : {'mpn', 'manufacturer', 'status', 'product_url',
        'description'[, 'category', 'params']}. Le statut n'est pas filtré ici (identifiants de statut de l'API non
        documentés) : `components.rank_alternatives` ne garde que les fiches actives."""
        body = {"Keywords": "", "Limit": KEYWORD_LIMIT, "Offset": 0, "FilterOptionsRequest": {
            "CategoryFilter": [{"Id": category_id}],
            "ParameterFilterRequest": {
                "CategoryFilter": {"Id": category_id},
                "ParameterFilters": [{"ParameterId": pid, "FilterValues": [{"Id": vid}]} for pid, vid in filters]}}}
        res = self._search(body, f"alternatives, catégorie {category_id}")
        return [{"mpn": _field(p, "ManufacturerProductNumber")[:64], "manufacturer": _field(p, "Manufacturer", "Name")[:80],
                 "status": _field(p, "ProductStatus", "Status").strip()[:40],
                 "product_url": components.normalize_url(p.get("ProductUrl") or ""),
                 "description": " ".join(_field(p, "Description", "ProductDescription").split())[:200], **_specs(p)}
                for p in (res.get("Products") if isinstance(res.get("Products"), list) else []) if isinstance(p, dict)]


def _specs(product: dict) -> dict:
    """Catégorie (du plus général au plus précis : [{'id', 'name'}]) et caractéristiques ([{'id', 'name', 'value',
    'vid'}] : ParameterId, libellé, valeur affichée, ValueId) d'une fiche. Textes venus de l'API : bornés ici,
    échappés au rendu. Clés absentes si la fiche n'en a pas."""
    path, cat = [], product.get("Category")
    while isinstance(cat, dict) and len(path) < MAX_DEPTH:
        cid, name = _field(cat, "CategoryId"), " ".join(_field(cat, "Name").split())
        if cid and name:
            path.append({"id": cid[:20], "name": name[:80]})
        kids = cat.get("ChildCategories")
        cat = kids[0] if isinstance(kids, list) and kids else None
    params = []
    for p in product.get("Parameters") if isinstance(product.get("Parameters"), list) else []:
        pid = p.get("ParameterId") if isinstance(p, dict) else None
        if not isinstance(pid, int) or isinstance(pid, bool):
            continue
        name, value = (" ".join(_field(p, k).split()) for k in ("ParameterText", "ValueText"))
        if name and value:
            params.append({"id": pid, "name": name[:80], "value": value[:120], "vid": _field(p, "ValueId")[:40]})
        if len(params) >= MAX_PARAMS:
            break
    return {**({"category": path} if path else {}), **({"params": params} if params else {})}


def _field(product, *keys: str) -> str:
    """Texte d'un champ imbriqué de la fiche (« Manufacturer » / « Name »…), vide s'il manque ou est mal formé."""
    value = product
    for k in keys:
        value = value.get(k) if isinstance(value, dict) else None
    return str(value) if isinstance(value, str | int | float) else ""
