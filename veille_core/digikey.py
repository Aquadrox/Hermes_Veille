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
ACTIVE_STATUS = 0   # identifiant du statut « Active » (FilterOptions.Status d'une recherche)
MAX_VALUES = 100    # valeurs d'une tenue minimale envoyées au plus (une caractéristique en propose une soixantaine)

# Pannes passagères : le réseau ou le serveur, pas la requête. HTTPError (sous-classe d'URLError) est traitée
# avant. HTTPException : réponse tronquée (IncompleteRead) ou ligne d'état illisible, après une coupure.
TRANSIENT = (ConnectionError, TimeoutError, urllib.error.URLError, http.client.HTTPException)

# Statut Digi-Key (minuscules) -> type d'avis : les statuts de cycle de vie qui sont aussi un type d'avis (NRND, LTB,
# OBSOLETE), plus « Discontinued at Digi-Key ». Active / Preliminary : pas d'avis.
STATUS_TYPES = {**{s: t for s, t in components.DK_LIFECYCLE.items() if t in components.TYPES},
                "discontinued at digi-key": "AUTRE"}


class DigiKeyError(RuntimeError):
    pass


class RequestRefused(DigiKeyError):
    """Requête refusée telle quelle (HTTP 400, 422) : propre à CETTE requête (une recherche d'alternatives aux
    filtres refusés), pas à l'accès ; une autre requête peut aboutir."""


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
    kind = TransientError if exc.code >= 500 else RequestRefused if exc.code in (400, 422) else DigiKeyError
    return kind(f"{what} : HTTP {exc.code}")


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
            raise type(exc)(f"{exc} ({what})") from None  # même nature (RequestRefused) : l'appelant en décide

    def status(self, mpn: str, manufacturer: str) -> dict:
        """{'status', 'product_url'[, 'category', 'params']} ou {'error'[, 'near']} (référence introuvable, fabricant
        différent ; `near` : références proches, voir `_near`).

        Recherche par mot-clé (fiches au MPN exact, puis résultats) : elle retrouve une référence de plusieurs
        fabricants et une référence saisie sans sa ponctuation (« 2N7002215 » pour « 2N7002,215 »). Fiche du
        fabricant suivi uniquement, au MPN identique de préférence.
        """
        res = self._search({"Keywords": mpn, "Limit": KEYWORD_LIMIT, "Offset": 0}, f"recherche de « {mpn} »")
        key = components.loose_mpn(mpn)
        found = [p for k in ("ExactMatches", "Products") for p in _list(res.get(k)) if isinstance(p, dict)]
        same_ref = [p for p in found if components.loose_mpn(_field(p, "ManufacturerProductNumber")) == key]
        mine = [p for p in same_ref if components.same_manufacturer(_field(p, "Manufacturer", "Name"), manufacturer)]
        if not mine:
            others = ", ".join(dict.fromkeys(_field(p, "Manufacturer", "Name") for p in same_ref))
            near = _near(found, key, manufacturer)
            return {"error": f"fabricant différent chez Digi-Key ({others[:80]})" if others
                    else "référence exacte introuvable chez Digi-Key" if near else "introuvable chez Digi-Key",
                    **({"near": near} if near else {})}
        exact = components.norm_mpn(mpn)
        product = next((p for p in mine if components.norm_mpn(_field(p, "ManufacturerProductNumber")) == exact), mine[0])
        status = _field(product, "ProductStatus", "Status").strip()
        if not status and product.get("EndOfLife"):
            status = "Obsolete"
        url = components.normalize_url(product.get("ProductUrl") or "")
        return {"status": status[:40] or "Inconnu", "product_url": url, **_specs(product)}

    def similar(self, category_id: str, filters: list[tuple[int, str]],
                at_least: list[tuple[int, str]] = ()) -> list[dict]:
        """Fiches ACTIVES de la sous-catégorie `category_id` dont les caractéristiques `filters` ([(ParameterId,
        ValueId)]) ont ces valeurs (recherche paramétrique) : {'mpn', 'manufacturer', 'status', 'product_url',
        'description'[, 'category', 'params']}.

        `at_least` ([(ParameterId, valeur de la référence)]) : tenues supérieures ou égales (`components.at_least`),
        prises parmi les valeurs que Digi-Key propose pour les autres filtres (FilterOptions du premier appel), les
        plus proches de la référence d'abord, jusqu'à couvrir KEYWORD_LIMIT fiches (les fiches renvoyées ne sont
        pas classées : une 1N4004 ne doit pas évincer les 1N4001), puis demandées ensemble (second appel) ; moins
        de MAX_ALTERNATIVES fiches (comptes par caractéristique, pas croisés) : toutes les valeurs suffisantes
        (troisième appel). Aucune valeur suffisante : rien, sans second appel."""
        what = f"alternatives, catégorie {category_id}"
        exact = [(pid, [vid]) for pid, vid in filters]
        res = self._search(_parametric(category_id, exact), what)
        if at_least:
            options = res.get("FilterOptions")
            options = options.get("ParametricFilters") if isinstance(options, dict) else None
            offered = {pf["ParameterId"]: pf.get("FilterValues") for pf in options if isinstance(pf, dict)
                       and isinstance(pf.get("ParameterId"), int)} if isinstance(options, list) else {}
            enough = [[v for v in _list(offered.get(pid)) if isinstance(v, dict) and isinstance(v.get("ValueId"), str)
                       and v["ValueId"] and components.at_least(v.get("ValueName"), ref)] for pid, ref in at_least]
            if not all(enough):
                return []
            ranked = [(pid, *_ranked(vals)) for (pid, _), vals in zip(at_least, enough, strict=True)]
            res = self._search(_parametric(category_id, exact + [(pid, ids[:n]) for pid, ids, n in ranked]), what)
            if any(n < len(ids) for _, ids, n in ranked) and len(_products(res)) < components.MAX_ALTERNATIVES:
                res = self._search(_parametric(category_id, exact + [(pid, ids) for pid, ids, _ in ranked]), what)
        return [{"mpn": _field(p, "ManufacturerProductNumber")[:64], "manufacturer": _field(p, "Manufacturer", "Name")[:80],
                 "status": _field(p, "ProductStatus", "Status").strip()[:40],
                 "product_url": components.normalize_url(p.get("ProductUrl") or ""),
                 "description": " ".join(_field(p, "Description", "ProductDescription").split())[:200], **_specs(p)}
                for p in _products(res)]


def _near(found: list[dict], key: str, manufacturer: str) -> list[dict]:
    """Références proches d'une référence absente (`key` : MPN sans séparateurs) parmi les fiches trouvées :
    variantes du même fabricant au MPN prolongé (« DS28E01P-100+T », « FT232RL-REEL ») ou qui la citent parmi
    leurs autres noms, puis alias : fiches d'AUTRES fabricants qui la citent (`OtherNames` : « APA102-2020 » d'un
    produit SparkFun).
    Indices pour l'utilisateur et l'agent, jamais un statut : une variante peut être une carte d'évaluation, un
    alias le kit d'un revendeur."""
    out, seen = [], set()
    for kind in components.NEAR_KINDS:
        for p in found:
            mpn, mfr = _field(p, "ManufacturerProductNumber")[:64], _field(p, "Manufacturer", "Name")[:80]
            loose = components.loose_mpn(mpn)
            if not mpn or loose == key or (mpn, mfr) in seen:
                continue
            named = any(components.loose_mpn(n) == key for n in _list(p.get("OtherNames")) if isinstance(n, str))
            mine = components.same_manufacturer(mfr, manufacturer)
            ok = mine and (loose.startswith(key) or named) if kind == "variante" else named and not mine
            if ok:
                seen.add((mpn, mfr))
                out.append({"mpn": mpn, "manufacturer": mfr, "status": _field(p, "ProductStatus", "Status").strip()[:40],
                            "url": components.normalize_url(p.get("ProductUrl") or ""), "kind": kind})
    return out[:components.MAX_NEAR]


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _products(res: dict) -> list[dict]:
    return [p for p in _list(res.get("Products") if isinstance(res, dict) else None) if isinstance(p, dict)]


def _ranked(values: list[dict]) -> tuple[list[str], int]:
    """Identifiants des valeurs admises (FilterValues) par valeur croissante, MAX_VALUES au plus, et combien des
    premières couvrent KEYWORD_LIMIT fiches (ProductCount ; absent : toutes)."""
    values = sorted(values, key=lambda v: min((x for x, _ in (components.rating(v.get("ValueName")) or {}).values()),
                                              default=0.0))[:MAX_VALUES]
    total = 0
    for n, v in enumerate(values, 1):
        total += v["ProductCount"] if isinstance(v.get("ProductCount"), int) else 0
        if total >= KEYWORD_LIMIT:
            return [v["ValueId"] for v in values], n
    return [v["ValueId"] for v in values], len(values)


def _parametric(category_id: str, filters: list[tuple[int, list[str]]]) -> dict:
    """Corps d'une recherche paramétrique : sous-catégorie, fiches actives, valeurs admises par caractéristique."""
    return {"Keywords": "", "Limit": KEYWORD_LIMIT, "Offset": 0, "FilterOptionsRequest": {
        "CategoryFilter": [{"Id": category_id}], "StatusFilter": [{"Id": ACTIVE_STATUS}],
        "ParameterFilterRequest": {"CategoryFilter": {"Id": category_id}, "ParameterFilters": [
            {"ParameterId": pid, "FilterValues": [{"Id": v} for v in vids]} for pid, vids in filters]}}}


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
