"""Veille composants : références suivies et avis PCN / fin de vie (EOL) détectés.

    veilles/<slug>/components.json   {"components": [...], "batch": [clés du lot web en cours]}
    veilles/<slug>/notices.json      {"notices": [...]}
    veilles/<slug>/specs.json        {"specs": {clé: fiche Digi-Key}}   (voir « Fiche composant et alternatives »)

Chaque référence garde ce qu'en sait Digi-Key (`dk` : statut, fiche produit, alternatives suggérées) et, si
l'utilisateur les a choisies, ses caractéristiques critiques (`critical_params`).

Les candidats viennent d'un agent LLM qui lit le web : ce sont des données NON FIABLES.
Tout passe par `validate_candidates` (schéma strict, URL http(s), tailles bornées) et chaque
rendu échappe chaque champ (`md_escape`, `html.escape`) : ni HTML brut dans pandoc (qui
récupérerait des ressources distantes), ni lien `javascript:`.

Fonctions pures (aucune E/S) sauf `load_*` / `save_*` : le verrouillage et l'orchestration
sont dans `service`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

from . import store

MPN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/+#()-]{0,63}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_COMPONENTS = 5000
MAX_CANDIDATES = 500
MAX_SOURCES = 5
MAX_CANDIDATES_BYTES = 2_000_000
WEB_MARGIN_DAYS = 30  # les distributeurs republient les avis avec retard

# Type d'avis -> (libellé, criticité : 0 = la plus haute). Ordre = ordre d'affichage.
TYPES = {
    "EOL": ("Fin de vie", 0),
    "PDN": ("Arrêt de production", 0),
    "OBSOLETE": ("Obsolète", 0),
    "LTB": ("Dernière commande", 1),
    "NRND": ("Déconseillé (NRND)", 2),
    "PCN": ("Modification (PCN)", 3),
    "AUTRE": ("Autre", 4),
}
CRITICAL = frozenset(t for t, (_, sev) in TYPES.items() if sev <= 1)
# Statut de cycle de vie qu'établit la recherche web pour une référence inconnue de Digi-Key -> libellé.
# Un statut qui est aussi un type d'avis (NRND, LTB, OBSOLETE) crée l'avis correspondant.
WEB_STATUSES = {"ACTIVE": "Actif", "NRND": TYPES["NRND"][0], "LTB": TYPES["LTB"][0],
                "OBSOLETE": TYPES["OBSOLETE"][0], "UNKNOWN": "Introuvable"}
# Statut Digi-Key (minuscules) -> même statut de cycle de vie (WEB_STATUSES) : « Obsolete » (Digi-Key) et « OBSOLETE »
# (recherche web) s'affichent et se filtrent sous un seul libellé. Les autres (« Preliminary »…) restent tels quels.
DK_LIFECYCLE = {"active": "ACTIVE", "not for new designs": "NRND", "last time buy": "LTB", "obsolete": "OBSOLETE"}
# Origines des avis tirés d'un statut de cycle de vie : identifiés par leur intitulé, pas par leur lien
# (le même statut cité avec une autre fiche n'est pas un nouvel avis).
STATUS_ORIGINS = ("digikey", "statut-web")
# Repli d'une référence introuvable (Digi-Key puis recherche web) : alldatasheet.com mène au fabricant (son
# « Direct Link »). Miroir de fiches techniques : il ne fait JAMAIS foi pour un statut ni pour un avis.
ALLDATASHEET_SEARCH = "https://www.alldatasheet.com/view.jsp?Searchword="
# Ses domaines : alldatasheet.com, .net, .es, .co.kr…, alldatasheetde.com, et leurs sous-domaines (html., pdf1.…).
_ALLDATASHEET_HOST = re.compile(r"(?:[a-z0-9-]+\.)*alldatasheet[a-z]{0,2}\.(?:co\.)?[a-z]{2,3}")
SCOPES = ("référence", "gamme")
HEADER_WORDS = {"mpn", "référence", "reference", "ref", "partnumber", "manufacturerpartnumber", "fabricant",
                "manufacturer", "mfr", "brand", "marque"}
_TEXT_LIMITS = {"title": 200, "number": 64, "summary": 600, "label": 80}


class ComponentsError(store.StoreError):
    pass


def today() -> str:
    from .schedule import ZONE

    return dt.datetime.now(ZONE).date().isoformat()


# --------------------------------------------------------------------------
# Références
# --------------------------------------------------------------------------
def norm_mpn(mpn: str) -> str:
    return re.sub(r"\s+", "", mpn or "").upper()


def loose_mpn(mpn: str) -> str:
    """MPN sans séparateurs, pour reconnaître une référence chez un distributeur : « 2N7002,215 » (Nexperia)
    ~ « 2N7002215 ». Restent significatifs le « + » (« MAX232CPE+ » sans plomb ≠ « MAX232CPE ») et le point
    décimal (« SMBJ6.0A », 6 V ≠ « SMBJ60A », 60 V)."""
    return re.sub(r"[^A-Z0-9+.]", "", (mpn or "").upper())


def norm_mfr(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def key_of(mpn: str, manufacturer: str) -> str:
    return f"{norm_mfr(manufacturer)}|{norm_mpn(mpn)}"


def same_manufacturer(a: str, b: str) -> bool:
    """« Texas Instruments » ~ « Texas Instruments Inc. ». Un nom court (« TI », « ON ») doit être
    identique : « ON » n'est pas contenu dans « Infineon »."""
    na, nb = sorted((norm_mfr(a), norm_mfr(b)), key=len)
    return bool(na) and (na == nb or (len(na) >= 4 and na in nb))


def validate_entry(mpn, manufacturer) -> tuple[dict | None, str]:
    """Une référence -> (entrée normalisée, "") ou (None, motif du rejet). Règle unique pour la
    saisie manuelle et l'import de fichiers."""
    mpn, mfr = (" ".join(str(x or "").split()) for x in (mpn, manufacturer))
    if not MPN_RE.match(mpn):
        return None, "MPN vide ou invalide" if mpn else "MPN vide"
    if not norm_mfr(mfr) or len(mfr) > 80:
        return None, "fabricant manquant ou invalide"
    return {"mpn": mpn, "manufacturer": mfr}, ""


def unique(entries: list[dict]) -> list[dict]:
    """Doublons (même fabricant et MPN) fusionnés, première occurrence gardée ; taille bornée."""
    seen, out = set(), []
    for e in entries:
        key = key_of(e["mpn"], e["manufacturer"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    if len(out) > MAX_COMPONENTS:
        raise ComponentsError(f"trop de composants ({len(out)}) : {MAX_COMPONENTS} au plus par veille")
    return out


def parse_components(text: str) -> list[dict]:
    """Une référence par ligne : « MPN ; Fabricant » (tabulation, ; ou ,).

    Le collage depuis un tableur (tabulations) fonctionne tel quel ; les colonnes suivantes sont
    ignorées (anciennes listes « MPN ; Fabricant ; Réf. interne » comprises) ; `#` commente une ligne ;
    une ligne d'en-tête « MPN… » est ignorée ; doublons fusionnés (même fabricant et MPN).
    """
    out, bad, first = [], [], True
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sep = "\t" if "\t" in line else (";" if ";" in line else ",")
        parts = [p.strip() for p in line.split(sep)]
        header = first and {re.sub(r"[^a-zé]", "", p.lower()) for p in parts[:2]} & HEADER_WORDS
        first = False
        if header:  # ligne d'en-tête d'un export de tableur
            continue
        entry, _ = validate_entry(*(parts + [""])[:2])
        if entry is None:
            bad.append(f"ligne {n}")
        else:
            out.append(entry)
    if bad:
        raise ComponentsError("format attendu « MPN ; Fabricant » : "
                              + ", ".join(bad[:10]) + (" …" if len(bad) > 10 else ""))
    return unique(out)


def format_components(components: list[dict]) -> str:
    """Inverse de parse_components (formulaire d'édition)."""
    return "\n".join(f"{c['mpn']} ; {c['manufacturer']}" for c in components)


def combine(current: list[dict], items: list[dict], mode: str = "add") -> tuple[list[dict], dict]:
    """Liste résultant d'un import : `add` (ajout, doublons ignorés) ou `replace` (la liste devient
    `items`). Renvoie (liste, {"added", "existing", "removed"})."""
    if mode not in ("add", "replace"):
        raise ComponentsError(f"mode d'import inconnu : {mode!r} (add ou replace)")
    items = unique(items)
    have = {key_of(c["mpn"], c["manufacturer"]) for c in current}
    new = {key_of(i["mpn"], i["manufacturer"]) for i in items}
    stats = {"added": len(new - have), "existing": len(new & have),
             "removed": len(have - new) if mode == "replace" else 0}
    if mode == "replace":
        return items, stats
    return unique(current + [i for i in items if key_of(i["mpn"], i["manufacturer"]) not in have]), stats


def _fresh(c: dict, added_at: str | None = None) -> dict:
    """Référence sans aucun état de veille (ni vérification, ni statut). Les caractéristiques critiques choisies par
    l'utilisateur sont gardées : un choix, pas un résultat de recherche."""
    return {"mpn": c["mpn"], "manufacturer": c["manufacturer"], "added_at": added_at or today(),
            "web_checked_at": None, "dk": None,
            **({"critical_params": c["critical_params"]} if c.get("critical_params") is not None else {})}


def replace_components(current: list[dict], wanted: list[dict]) -> list[dict]:
    """Nouvelle liste ; l'état (vérifications, statut Digi-Key) des références conservées est gardé. Une réf.
    interne d'avant la 0.5.0 n'est plus lue (aucune migration : elle reste dans l'état de sa référence)."""
    by_key = {key_of(c["mpn"], c["manufacturer"]): c for c in current}
    out = []
    for w in wanted:
        old = by_key.get(key_of(w["mpn"], w["manufacturer"]))
        if old:
            out.append({**old, "mpn": w["mpn"], "manufacturer": w["manufacturer"]})
        else:
            out.append(_fresh(w))
    return out


def reset_state(components: list[dict]) -> list[dict]:
    """Même liste (références, dates d'ajout, caractéristiques critiques), sans aucun état : tout sera revérifié."""
    return [_fresh(c, c.get("added_at")) for c in components]


# --------------------------------------------------------------------------
# Fichiers (écriture atomique ; un fichier corrompu est une ERREUR, jamais une liste vide)
# --------------------------------------------------------------------------
def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ComponentsError(f"{path.name} illisible ({exc}) : corriger ou restaurer le fichier") from None
    if not isinstance(data, dict):
        raise ComponentsError(f"{path.name} illisible (objet JSON attendu)")
    return data


def _load(slug: str, name: str) -> dict:
    return _read(store.veille_dir(slug) / name)


def read_state(directory: Path) -> tuple[list[dict], list[dict]]:
    """(références, avis) du dossier d'une veille, active ou archivée (base des composants)."""
    return (_read(directory / "components.json").get("components") or [],
            _read(directory / "notices.json").get("notices") or [])


def _save(slug: str, name: str, data: dict) -> None:
    store.atomic_write(store.veille_dir(slug) / name, json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def load_components(slug: str) -> dict:
    data = _load(slug, "components.json")
    out = {"components": data.get("components") or [], "batch": data.get("batch") or []}
    if isinstance(data.get("scan"), dict):  # passage Digi-Key en cours ou dernier passage (service.components_scan)
        out["scan"] = data["scan"]
    return out


def save_components(slug: str, data: dict) -> None:
    _save(slug, "components.json", data)


def load_notices(slug: str) -> list[dict]:
    return _load(slug, "notices.json").get("notices") or []


def save_notices(slug: str, notices: list[dict]) -> None:
    _save(slug, "notices.json", {"notices": notices})


# --------------------------------------------------------------------------
# Validation des candidats (sortie de l'agent)
# --------------------------------------------------------------------------
def normalize_url(url: str) -> str | None:
    """URL http(s) publique et canonique, ou None (schéma, identifiants, caractères interdits)."""
    if not isinstance(url, str) or len(url) > 500 or re.search(r"[\s\x00-\x1f\x7f<>\"]", url):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_")])
    path = parts.path.rstrip("/") or ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def is_alldatasheet(url: str | None) -> bool:
    return bool(url) and bool(_ALLDATASHEET_HOST.fullmatch(urlsplit(url).hostname or ""))


def alldatasheet_url(mpn: str) -> str:
    """Page de recherche alldatasheet d'une référence (MPN encodé : « # », « + », « / » sont permis)."""
    return ALLDATASHEET_SEARCH + quote_plus(mpn)


def _text(value, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str | int | float):
        return ""
    return " ".join(str(value).split())[: _TEXT_LIMITS.get(field, 200)]


def _date(value) -> str:
    value = _text(value, "date")
    if not DATE_RE.match(value):
        return ""
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return ""
    return value


def _matcher(components: list[dict]):
    """Fonction (MPN cité par l'agent, fabricant cité) -> composant suivi, ou None : inconnu, ou MPN suivi chez
    plusieurs fabricants sans fabricant pour trancher."""
    by_mpn: dict[str, list[dict]] = {}
    for c in components:
        by_mpn.setdefault(norm_mpn(c["mpn"]), []).append(c)

    def match(mpn, manufacturer: str) -> dict | None:
        cands = by_mpn.get(norm_mpn(_text(mpn, "label")), [])
        if len(cands) > 1 and manufacturer:
            cands = [c for c in cands if same_manufacturer(c["manufacturer"], manufacturer)]
        return cands[0] if len(cands) == 1 else None
    return match


def find(components: list[dict], mpn: str, manufacturer: str = "") -> dict:
    """Référence suivie de ce MPN (le fabricant départage un MPN suivi chez plusieurs) ; lève sinon."""
    c = _matcher(components)(mpn, manufacturer)
    if c is None:
        many = sum(1 for x in components if norm_mpn(x["mpn"]) == norm_mpn(_text(mpn, "label"))) > 1
        raise ComponentsError(f"« {_text(mpn, 'label')[:64]} » : " + (
            "suivie chez plusieurs fabricants, préciser le fabricant" if many else "référence non suivie par cette veille"))
    return c


def validate_candidates(raw, components: list[dict]) -> tuple[list[dict], list[str]]:
    """{"notices": [...]} de l'agent -> (avis valides, un par composant suivi touché ; rejets)."""
    items = raw.get("notices") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ComponentsError("format attendu : {\"notices\": [...]}")
    match = _matcher(components)
    valid, rejected = [], []
    for i, item in enumerate(items[:MAX_CANDIDATES], 1):
        if not isinstance(item, dict):
            rejected.append(f"#{i} : objet attendu")
            continue
        ntype = _text(item.get("type"), "type").upper()
        if ntype not in TYPES:
            rejected.append(f"#{i} : type inconnu « {ntype[:20]} »")
            continue
        raw_sources, mpns = item.get("sources"), item.get("mpns") or ([item["mpn"]] if item.get("mpn") else [])
        if not isinstance(raw_sources, list) or not isinstance(mpns, list):
            rejected.append(f"#{i} : « sources » et « mpns » doivent être des listes")
            continue
        sources, seen_urls, mirror = [], set(), False
        for s in raw_sources:
            url = normalize_url(s.get("url") if isinstance(s, dict) else s)
            if is_alldatasheet(url):  # miroir de fiches techniques : jamais la preuve d'un avis
                mirror = True
            elif url and url not in seen_urls:
                seen_urls.add(url)
                label = _text(s.get("label"), "label") if isinstance(s, dict) else ""
                sources.append({"url": url, "label": label or urlsplit(url).hostname})
        if not sources:
            rejected.append(f"#{i} : " + ("alldatasheet n'est jamais une source d'avis : lien du fabricant attendu"
                                          if mirror else "aucune source http(s) valide"))
            continue
        manufacturer = _text(item.get("manufacturer"), "label")
        matched = []
        for mpn in mpns:
            c = match(mpn, manufacturer)
            if c is not None and c not in matched:
                matched.append(c)
        if not matched:
            rejected.append(f"#{i} : aucune référence suivie ({', '.join(map(str, mpns))[:80] or 'mpns vide'})")
            continue
        scope = _text(item.get("scope"), "scope").lower()
        base = {
            "type": ntype,
            "title": _text(item.get("title"), "title") or TYPES[ntype][0],
            "number": _text(item.get("number"), "number"),
            "published": _date(item.get("published")),
            "ltb_date": _date(item.get("ltb_date")),
            "eol_date": _date(item.get("eol_date")),
            "scope": scope if scope in SCOPES else ("gamme" if len(matched) > 1 else "référence"),
            "summary": _text(item.get("summary"), "summary"),
            "sources": sources[:MAX_SOURCES],
            "origin": "web",
        }
        valid.extend({**base, "key": key_of(c["mpn"], c["manufacturer"]), "mpn": c["mpn"],
                      "manufacturer": c["manufacturer"]} for c in matched)
    if len(items) > MAX_CANDIDATES:
        rejected.append(f"{len(items) - MAX_CANDIDATES} avis au-delà de la limite de {MAX_CANDIDATES}")
    return valid, rejected


def validate_statuses(raw, components: list[dict]) -> tuple[dict[str, dict], list[str]]:
    """{"statuses": [...]} de l'agent (cycle de vie des références inconnues de Digi-Key) -> ({clé: {"status",
    "url"[, "datasheet", "maker"]}}, rejets). Même rigueur que les avis : référence suivie, statut connu, fiche
    http(s) obligatoire sauf pour UNKNOWN (aucune fiche trouvée). Champ facultatif : absent, rien à valider.

    Repli alldatasheet : `datasheet` (page alldatasheet consultée) et `maker` (site du fabricant atteint par
    son « Direct Link ») sont facultatifs. Une fiche alldatasheet ne fait jamais foi pour un statut : seule
    une référence UNKNOWN peut la citer (elle devient alors `datasheet`)."""
    items = raw.get("statuses") if isinstance(raw, dict) else None
    if items is None:
        return {}, []
    if not isinstance(items, list):  # champ facultatif : rejeté, les avis sont enregistrés quand même
        return {}, ["« statuses » : liste attendue"]
    match = _matcher(components)
    found, rejected = {}, []
    for i, item in enumerate(items[:MAX_CANDIDATES], 1):
        if not isinstance(item, dict):
            rejected.append(f"statut #{i} : objet attendu")
            continue
        status = _text(item.get("status"), "label").upper()
        url, datasheet, maker = (normalize_url(item.get(f)) if item.get(f) else None
                                 for f in ("url", "datasheet", "maker"))
        c = match(item.get("mpn"), _text(item.get("manufacturer"), "label"))
        if status == "UNKNOWN" and is_alldatasheet(url):
            url, datasheet = None, datasheet or url
        if status not in WEB_STATUSES:
            rejected.append(f"statut #{i} : statut inconnu « {status[:20]} »")
        elif is_alldatasheet(url):
            rejected.append(f"statut #{i} : alldatasheet ne fait pas foi pour un statut (fiche du fabricant attendue)")
        elif not url and status != "UNKNOWN":
            rejected.append(f"statut #{i} : aucune fiche http(s) valide")
        elif c is None:
            rejected.append(f"statut #{i} : aucune référence suivie ({_text(item.get('mpn'), 'label')[:64]})")
        else:
            found[key_of(c["mpn"], c["manufacturer"])] = {
                "status": status, "url": url,
                **({"datasheet": datasheet} if is_alldatasheet(datasheet) else {}),
                **({"maker": maker} if maker and not is_alldatasheet(maker) else {})}
    if len(items) > MAX_CANDIDATES:
        rejected.append(f"{len(items) - MAX_CANDIDATES} statut(s) au-delà de la limite de {MAX_CANDIDATES}")
    return found, rejected


def status_notice(c: dict, ntype: str, title: str, url: str, label: str, origin: str) -> dict:
    """Avis tiré d'un statut de cycle de vie (fiche Digi-Key, ou fiche trouvée par la recherche web)."""
    return {"key": key_of(c["mpn"], c["manufacturer"]), "mpn": c["mpn"], "manufacturer": c["manufacturer"],
            "type": ntype, "title": title, "number": "", "published": "", "ltb_date": "", "eol_date": "",
            "scope": "référence", "summary": "", "sources": [{"url": url, "label": label}], "origin": origin}


def apply_statuses(components: list[dict], statuses: dict[str, dict], day: str) -> list[dict]:
    """Enregistre (en place) le statut web de chaque composant ; renvoie les avis à fusionner pour ceux
    déconseillés, en dernière commande ou obsolètes (comme un statut Digi-Key)."""
    out = []
    for c in components:
        s = statuses.get(key_of(c["mpn"], c["manufacturer"]))
        if s is None:
            continue
        maker = s.get("maker") or (c.get("web_status") or {}).get("maker")  # site du fabricant : gardé
        c["web_status"] = {**s, **({"maker": maker} if maker else {}), "checked_at": day}
        if s["status"] in TYPES:
            out.append(status_notice(c, s["status"], f"Statut constaté sur le web : {WEB_STATUSES[s['status']]}",
                                     s["url"], urlsplit(s["url"]).hostname, "statut-web"))
    return out


# --------------------------------------------------------------------------
# Fusion incrémentale
# --------------------------------------------------------------------------
def _identities(n: dict) -> set[tuple[str, str]]:
    """Identités d'un avis. Le TYPE fait partie de l'identité par lien : une même fiche produit qui
    passe de NRND à « Obsolete » est une aggravation à signaler, pas un doublon. Un numéro
    d'avis désigne en revanche un seul document, quel que soit le type déclaré."""
    ids = {(n["key"], f"u:{n['type']}:{s['url']}") for s in n["sources"]}
    if n.get("number"):
        ids.add((n["key"], "n:" + norm_mpn(n["number"])))
    if n.get("origin") in STATUS_ORIGINS:
        ids.add((n["key"], "dk:" + n["title"].lower()))
    return ids


def notice_id(n: dict) -> str:
    ident = n.get("number") or n["sources"][0]["url"]
    return hashlib.sha1(f"{n['key']}|{ident}|{n['type']}".encode()).hexdigest()[:12]


def merge(notices: list[dict], candidates: list[dict], found_at: str | None = None) -> list[dict]:
    """Ajoute les candidats inconnus à `notices` (en place) et les renvoie.

    Un candidat déjà connu (même composant ET même numéro d'avis ou même URL) n'est pas
    signalé à nouveau : ses nouvelles sources et les champs vides sont complétés. Un candidat repris
    de la base des composants garde sa date de détection d'origine (`found_at`), sauf `found_at` imposé.
    """
    index: dict[tuple[str, str], dict] = {}
    for n in notices:
        for ident in _identities(n):
            index[ident] = n
    new = []
    for c in candidates:
        known = next((index[i] for i in _identities(c) if i in index), None)
        if known is not None:
            urls = {s["url"] for s in known["sources"]}
            known["sources"] += [s for s in c["sources"] if s["url"] not in urls][: MAX_SOURCES - len(known["sources"])]
            for field in ("number", "published", "ltb_date", "eol_date", "summary"):
                if not known.get(field) and c.get(field):
                    known[field] = c[field]
            for ident in _identities(known):
                index[ident] = known
            continue
        n = {**c, "id": notice_id(c), "found_at": found_at or c.get("found_at") or today(), "delivered": {}}
        notices.append(n)
        new.append(n)
        for ident in _identities(n):
            index[ident] = n
    return new


def chapter_channel(veille_slug: str) -> str:
    """Canal de livraison du chapitre d'une veille (préfixé : un slug « mail » ou « report » ne
    peut pas se confondre avec ces canaux)."""
    return "veille:" + veille_slug


def pending(notices: list[dict], channel: str, redo: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """Avis pas encore livrés sur ce canal, plus ceux livrés à l'une des dates `redo`.

    `redo` contient en général le jour même (rapport régénéré lors d'une relance) et, pour un
    chapitre, les jours où la veille n'a finalement produit aucun rapport (exécution en échec).
    """
    return [n for n in notices if n.get("delivered", {}).get(channel) in (None, *redo)]


def delivery_days(notices: list[dict], channel: str) -> set[str]:
    """Dates (AAAA-MM-JJ) de livraison sur ce canal (hors marqueurs « rattachement … »)."""
    return {d for n in notices if DATE_RE.match(d := n.get("delivered", {}).get(channel) or "")}


def mark_delivered(notices: list[dict], ids: set[str], channel: str, day: str | None = None) -> None:
    day = day or today()
    for n in notices:
        if n["id"] in ids:
            n.setdefault("delivered", {}).setdefault(channel, day)


# --------------------------------------------------------------------------
# Lot tournant de recherche web
# --------------------------------------------------------------------------
def batch(components: list[dict], size: int) -> list[dict]:
    """Jamais vérifiés, puis statut Digi-Key changé depuis la dernière recherche (y compris une référence que
    Digi-Key ne trouve plus : une recherche, pas toutes), puis les plus anciens."""
    def rank(c):
        web = c.get("web_checked_at")
        changed = (c.get("dk") or {}).get("changed_at")
        if not web:
            return (0, "")
        return (1 if changed and changed > web else 2, web)
    return sorted(components, key=rank)[: max(1, size)]


def checked_since(components: list[dict], day: str) -> int:
    """Références interrogées chez Digi-Key depuis ce jour (statut lu, ou référence qu'il ne connaît pas)."""
    return sum(1 for c in components if ((c.get("dk") or {}).get("checked_at") or "") >= day)


def scan_coverage(scan: dict | None) -> dict | None:
    """D4 — couverture du dernier passage Digi-Key, pour le rapport, le chapitre et l'e-mail :
    {"partial", "checked", "total", "text"}.

    Un passage partiel (budget atteint, Digi-Key en panne, exécution terminée entre deux tranches) est publié, et
    TOUJOURS dit comme tel, avec le nombre de références vérifiées : jamais présenté comme complet. Les autres
    gardent le statut de leur dernière vérification et passent en tête du passage suivant (les plus anciennes
    d'abord). Rejetés : refuser de publier (avis et lot web perdus, e-mail des fins de vie retardé d'une semaine
    pour 16 références sur 386), ou se taire. None sans passage avec Digi-Key : le rapport n'affirme alors rien.
    """
    if not scan or not scan.get("configured"):
        return None
    checked, total = scan.get("verified", 0), scan.get("total", 0)  # enregistrés par le passage
    note = scan.get("note") if scan.get("complete") else "passage inachevé : l'exécution s'est terminée avant sa fin"
    partial = not (scan.get("complete") and checked >= total)
    text = (f"Passage Digi-Key {'PARTIEL' if partial else 'complet'} du {scan['date']} : {checked} référence(s) sur "
            f"{total} interrogée(s)")
    if total > checked:
        text += (f" ; les {total - checked} autre(s) gardent le statut de leur dernière vérification et passent en "
                 "priorité au prochain passage")
    return {"partial": partial, "checked": checked, "total": total, "text": text + (f" ({note})." if note else ".")}


def search_since(c: dict) -> str | None:
    """Date à partir de laquelle chercher (None : historique complet, jamais vérifié)."""
    web = c.get("web_checked_at")
    if not web:
        return None
    return (dt.date.fromisoformat(web[:10]) - dt.timedelta(days=WEB_MARGIN_DAYS)).isoformat()


# --------------------------------------------------------------------------
# Fiche composant et alternatives
# --------------------------------------------------------------------------
# Fiche = catégorie et caractéristiques lues chez Digi-Key ({"category": [{"id", "name"}], "params": [{"id", "name",
# "value", "vid"}]}), dans `specs.json` ({"specs": {clé: fiche}}) et non dans components.json : une quinzaine de
# caractéristiques par référence multiplieraient par 8 le fichier que relisent la liste et le tableau de bord (mesuré :
# 1,3 -> 11 Mo et page ×2 pour 5000 références) ; la fiche n'est lue que pour le détail, la recherche et le MCP.
# Les alternatives trouvées (peu nombreuses, affichées dans la liste) restent dans `dk.alternatives`.
#
# Catégorie Digi-Key (d'un niveau quelconque) -> famille dont on cherche des alternatives : passifs et discrets,
# qui s'échangent sur leurs caractéristiques. Les circuits intégrés n'en sont pas : un équivalent paramétrique y
# serait trompeur (brochage, fonctions, logiciel).
FAMILIES = {
    "Resistors": "Résistance",
    "Capacitors": "Condensateur",
    "Inductors, Coils, Chokes": "Inductance",
    "Ferrite Beads and Chips": "Ferrite",
    "Discrete Semiconductor Products": "Semi-conducteur discret",
    "Crystals, Oscillators, Resonators": "Quartz / oscillateur",
}
# Caractéristiques critiques par défaut (libellés Digi-Key, anglais) : valeur nominale, boîtier, montage et tenue
# principale de chaque famille. Celles que la fiche n'a pas sont ignorées ; un choix de l'utilisateur les remplace.
DEFAULT_CRITICAL = frozenset({
    "Package / Case", "Mounting Type",
    "Resistance", "Capacitance", "Voltage - Rated", "Inductance", "Impedance @ Frequency",
    "Voltage - DC Reverse (Vr) (Max)", "Current - Average Rectified (Io)", "Voltage - Zener (Nom) (Vz)",
    "FET Type", "Drain to Source Voltage (Vdss)", "Current - Continuous Drain (Id) @ 25°C",
    "Transistor Type", "Voltage - Collector Emitter Breakdown (Max)", "Current - Collector (Ic) (Max)",
    "Frequency", "Load Capacitance",
})
MAX_ALTERNATIVES = 5
MAX_DIFFS = 8          # différences affichées par alternative
REPORT_DIFFS = 3       # dont dans le rapport PDF (toutes dans l'interface)
ALT_MAX_AGE_DAYS = 30  # une recherche plus ancienne est refaite (catalogue des distributeurs vivant)
_NO_VALUE = frozenset({"", "-", "—"})


_specs_cache: dict[Path, tuple[tuple, dict]] = {}  # fichier -> (identité lue, fiches)


def load_specs(slug: str) -> dict[str, dict]:
    """Fiches d'une veille. Copie de Digi-Key relue à chaque passage : illisible, elle est vide (et réécrite au
    passage suivant) plutôt qu'une erreur qui bloquerait le passage ou la page.

    Gardées en mémoire tant que le fichier n'a pas changé (inode, date, taille : `store.atomic_write` le remplace) :
    le détail d'un composant n'en relit pas plusieurs Mo à chaque ouverture. Copie de premier niveau renvoyée :
    l'appelant peut ajouter ou remplacer une fiche, jamais en modifier une en place."""
    path = store.veille_dir(slug) / "specs.json"
    try:
        st = path.stat()
    except FileNotFoundError:
        return {}
    ident = (st.st_ino, st.st_mtime_ns, st.st_size)
    cached = _specs_cache.get(path)
    if cached is None or cached[0] != ident:
        try:
            specs = _read(path).get("specs")
        except ComponentsError:
            specs = None
        cached = _specs_cache[path] = (ident, specs if isinstance(specs, dict) else {})
    return dict(cached[1])


def save_specs(slug: str, specs: dict[str, dict]) -> None:
    _save(slug, "specs.json", {"specs": specs})


def params(spec: dict | None) -> list[dict]:
    """Caractéristiques d'une fiche (entrées mal formées ignorées : fichier retouché à la main)."""
    items = (spec or {}).get("params") if isinstance(spec, dict) else None
    return [p for p in items if isinstance(p, dict) and isinstance(p.get("id"), int)
            and isinstance(p.get("name"), str) and isinstance(p.get("value"), str)] if isinstance(items, list) else []


def _categories(spec: dict | None) -> list[dict]:
    cats = (spec or {}).get("category") if isinstance(spec, dict) else None
    return [x for x in cats if isinstance(x, dict) and x.get("id") and isinstance(x.get("name"), str)] \
        if isinstance(cats, list) else []


def _valued(p: dict) -> bool:
    """Caractéristique renseignée (Digi-Key note « - » une valeur sans objet) : seule à pouvoir être critique."""
    return p["value"].strip() not in _NO_VALUE


def family(spec: dict | None) -> str | None:
    """Famille de la référence (FAMILIES) d'après sa catégorie Digi-Key, ou None (inconnue ou non concernée)."""
    return next((FAMILIES[x["name"]] for x in _categories(spec) if x["name"] in FAMILIES), None)


def leaf_category(spec: dict | None) -> str | None:
    """Sous-catégorie Digi-Key la plus précise (périmètre de la recherche d'alternatives)."""
    cats = _categories(spec)
    return str(cats[-1]["id"]) if cats else None


def criteria(c: dict, spec: dict | None) -> list[dict]:
    """Caractéristiques critiques effectives : celles choisies par l'utilisateur (`critical_params`, même une liste
    vide), sinon les défauts de la famille (DEFAULT_CRITICAL) ; jamais une caractéristique sans valeur."""
    chosen = c.get("critical_params")
    if chosen is None:
        return [p for p in params(spec) if _valued(p) and p["name"] in DEFAULT_CRITICAL]
    ids = {x for x in chosen if isinstance(x, int)} if isinstance(chosen, list) else set()
    return [p for p in params(spec) if _valued(p) and p["id"] in ids]


def critical_ids(c: dict, spec: dict | None, wanted) -> list[int]:
    """Choix de l'utilisateur -> identifiants triés, validés contre la fiche (identifiants ou libellés Digi-Key,
    casse ignorée) ; lève sur une caractéristique inconnue ou sans valeur."""
    valued = [p for p in params(spec) if _valued(p)]
    by_id, by_name = {p["id"]: p for p in valued}, {p["name"].lower(): p for p in valued}
    out, bad = set(), []
    for w in wanted:
        p = by_id.get(w) if isinstance(w, int) and not isinstance(w, bool) else by_name.get(str(w).strip().lower())
        if p is None and isinstance(w, str) and w.strip().isdigit():
            p = by_id.get(int(w))
        if p is None:
            bad.append(str(w)[:60])
        else:
            out.add(p["id"])
    if bad:
        raise ComponentsError(f"caractéristique(s) inconnue(s) ou sans valeur sur la fiche de {c['mpn']} : "
                              + ", ".join(bad[:10]))
    return sorted(out)


def _same(a: dict, b: dict | None) -> bool:
    """Même valeur : identifiant de valeur Digi-Key (ValueId) si les deux l'ont, sinon le texte affiché."""
    if b is None:
        return False
    if a.get("vid") and b.get("vid"):
        return a["vid"] == b["vid"]
    return " ".join(a["value"].split()).lower() == " ".join(b["value"].split()).lower()


def rank_alternatives(c: dict, spec: dict | None, products: list[dict], crit: list[dict]) -> list[dict]:
    """Fiches renvoyées par `digikey.Client.similar` -> MAX_ALTERNATIVES alternatives : fiches ACTIVES d'une autre
    référence, aux mêmes valeurs critiques (revérifiées : un filtre ignoré par l'API ne passe pas), classées par
    score (part des autres caractéristiques identiques) ; chacune avec ses différences avec la référence."""
    crit_ids = {p["id"] for p in crit}
    others = [p for p in params(spec) if p["id"] not in crit_ids and _valued(p)]
    own, seen, out = loose_mpn(c["mpn"]), set(), []
    for prod in products:
        mpn, mfr = _text(prod.get("mpn"), "label"), _text(prod.get("manufacturer"), "label")
        key = key_of(mpn, mfr)
        if (not mpn or loose_mpn(mpn) == own or key in seen
                or DK_LIFECYCLE.get(str(prod.get("status") or "").lower()) != "ACTIVE"):
            continue
        vals = {p["id"]: p for p in params(prod)}
        if not all(_same(p, vals.get(p["id"])) for p in crit):
            continue
        seen.add(key)
        diffs = [{"name": p["name"], "ref": p["value"], "alt": vals[p["id"]]["value"] if p["id"] in vals else "—"}
                 for p in others if not _same(p, vals.get(p["id"]))]
        out.append({"mpn": mpn, "manufacturer": mfr, "status": _text(prod.get("status"), "label"),
                    "url": normalize_url(prod.get("product_url") or "") if prod.get("product_url") else None,
                    "description": _text(prod.get("description"), "title"),
                    "score": round(100 * (len(others) - len(diffs)) / len(others)) if others else 100,
                    "diffs": diffs[:MAX_DIFFS]})
    out.sort(key=lambda a: (-a["score"], a["mpn"]))
    return out[:MAX_ALTERNATIVES]


def alternatives_record(c: dict, spec: dict | None, products: list[dict], day: str, error: str = "") -> dict:
    """Résultat d'une recherche, enregistré dans `dk.alternatives` : date, critères appliqués, alternatives ; ou
    le refus de Digi-Key (`error`), daté lui aussi : la référence repasse après les autres, et le refus s'affiche."""
    crit = criteria(c, spec)
    return {"searched_at": day, "criteria": [{"id": p["id"], "name": p["name"], "value": p["value"]} for p in crit],
            "items": [] if error else rank_alternatives(c, spec, products, crit), **({"error": error} if error else {})}


def alternatives(c: dict) -> dict | None:
    """Dernière recherche d'alternatives de la référence, ou None (jamais faite, ou fichier retouché)."""
    rec = (c.get("dk") or {}).get("alternatives")
    ok = (isinstance(rec, dict) and isinstance(rec.get("searched_at"), str)
          and isinstance(rec.get("criteria"), list) and isinstance(rec.get("items"), list))
    return rec if ok else None


def alternatives_stale(c: dict, spec: dict | None) -> bool:
    """Recherche faite avec d'autres critères que les critères actuels (choix de l'utilisateur modifié). Sans fiche
    (pas encore relue), rien à comparer : pas périmée."""
    rec = alternatives(c)
    return rec is not None and bool(params(spec)) and sorted(
        str(x.get("id")) for x in rec["criteria"] if isinstance(x, dict)) != sorted(
        str(p["id"]) for p in criteria(c, spec))  # ordre des caractéristiques de Digi-Key indifférent


def searchable(c: dict, spec: dict | None) -> str:
    """Motif pour lequel on ne peut pas chercher d'alternatives à cette référence, vide si on peut. Sans aucune
    critique, toute référence active de la catégorie passerait (une 1 MΩ pour une 10 kΩ) : refusé."""
    if not params(spec) and not _categories(spec):
        return "pas de fiche Digi-Key (statut Digi-Key à vérifier d'abord)"
    if family(spec) is None:
        return "famille non concernée (seuls les passifs et les discrets le sont)"
    if not criteria(c, spec):
        return "aucune caractéristique critique : en cocher au moins une (la valeur nominale)"
    return ""


def select(components: list[dict], mpns: list[str]) -> tuple[list[dict], list[str]]:
    """Références suivies de ces MPN (tous fabricants), et les MPN demandés qui ne le sont pas (bornés)."""
    wanted = {norm_mpn(m) for m in mpns}
    chosen = [c for c in components if norm_mpn(c["mpn"]) in wanted]
    tracked = {norm_mpn(c["mpn"]) for c in chosen}
    return chosen, [str(m)[:64] for m in dict.fromkeys(mpns) if norm_mpn(m) not in tracked]


def alternatives_due(components: list[dict], specs: dict[str, dict], notices: list[dict], day: str) -> list[dict]:
    """Références dont chercher les alternatives sans qu'on le demande : en fin de vie ou en dernière commande,
    d'une famille concernée, sans recherche récente (ALT_MAX_AGE_DAYS) ou recherchées avec d'autres critères.
    Jamais recherchées d'abord, puis les plus anciennes."""
    worst = worst_by_key(notices)
    limit = (dt.date.fromisoformat(day) - dt.timedelta(days=ALT_MAX_AGE_DAYS)).isoformat()
    due = []
    for c in components:
        key = key_of(c["mpn"], c["manufacturer"])
        w, spec = worst.get(key), specs.get(key)
        if not w or w["type"] not in CRITICAL or searchable(c, spec):
            continue
        rec = alternatives(c)
        if rec is None or rec["searched_at"] < limit or alternatives_stale(c, spec):
            due.append(c)
    return sorted(due, key=lambda c: (alternatives(c) or {}).get("searched_at") or "")


def _alt_items(rec: dict | None) -> list[dict]:
    """Alternatives affichables d'une recherche (une entrée sans MPN, fichier retouché, est ignorée)."""
    return [a for a in rec["items"] if isinstance(a, dict) and a.get("mpn") and isinstance(a["mpn"], str)] if rec else []


def alternatives_gap(c: dict, spec: dict | None) -> str:
    """Pourquoi aucune alternative n'est proposée pour cette référence (rapport, e-mail), vide s'il y en a."""
    rec = alternatives(c)
    if _alt_items(rec):
        return ""
    if rec:
        return (f"recherche refusée par Digi-Key le {rec['searched_at']}" if rec.get("error")
                else f"aucune référence active équivalente chez Digi-Key (recherche du {rec['searched_at']})")
    if (c.get("dk") or {}).get("error") and not spec:
        return "référence inconnue de Digi-Key"
    return searchable(c, spec) or "recherche au prochain passage Digi-Key"


def sheet(c: dict, spec: dict | None) -> dict:
    """Fiche composant (interface, MCP) : famille, catégorie, caractéristiques (critiques marquées), origine des
    critères, dernière recherche d'alternatives (`stale` : faite avec d'autres critères)."""
    crit = {p["id"] for p in criteria(c, spec)}
    return {"family": family(spec), "category": " › ".join(x["name"] for x in _categories(spec)),
            "unsearchable": searchable(c, spec),
            "params": [{**p, "critical": p["id"] in crit, "valued": _valued(p)} for p in params(spec)],
            "custom": c.get("critical_params") is not None, "alternatives": alternatives(c),
            "stale": alternatives_stale(c, spec)}


# --------------------------------------------------------------------------
# Synthèse
# --------------------------------------------------------------------------
def severity(ntype: str) -> int:
    return TYPES.get(ntype, TYPES["AUTRE"])[1]


def _sort(notices: list[dict]) -> list[dict]:
    return sorted(notices, key=lambda n: (severity(n["type"]), n.get("ltb_date") or "9999", n["mpn"]))


def for_display(notices: list[dict], ntype: str | None = None, since: str | None = None) -> list[dict]:
    """Filtre (type, date de détection) puis tri : fins de vie d'abord, plus récents ensuite."""
    rows = [n for n in notices if (not ntype or n["type"] == ntype) and (not since or n["found_at"] >= since)]
    rows.sort(key=lambda n: n["found_at"], reverse=True)
    return sorted(rows, key=lambda n: severity(n["type"]) > TYPES["LTB"][1])


def worst_by_key(notices: list[dict]) -> dict[str, dict]:
    """Avis le plus critique de chaque composant (le plus récent à criticité égale)."""
    out: dict[str, dict] = {}
    for n in notices:
        cur = out.get(n["key"])
        if cur is None or (severity(n["type"]), cur["found_at"]) < (severity(cur["type"]), n["found_at"]):
            out[n["key"]] = n
    return out


def badge_class(ntype: str) -> str:
    """Classe CSS du badge d'un type d'avis (interface et PDF) : les fins de vie partagent « eol »."""
    return "eol" if ntype in ("EOL", "PDN", "OBSOLETE") else ntype.lower()


def anchor(key: str) -> str:
    """Identifiant HTML / pandoc sûr d'un composant (la clé contient « | » et du texte libre)."""
    return "c-" + hashlib.sha1(key.encode()).hexdigest()[:10]


def web_status_label(web: dict) -> str:
    """Libellé du statut web d'un composant, « (alldatasheet) » s'il a fallu ce repli pour l'établir."""
    label = WEB_STATUSES.get(web.get("status"), "")
    return f"{label} (alldatasheet)" if label and web.get("datasheet") else label


def status_label(row: dict) -> str:
    """Statut de cycle de vie d'une ligne de `group` (colonne et filtre « Statut ») : celui de Digi-Key s'il connaît
    la référence (il fait foi) ; s'il ne la trouve plus, celui établi par la recherche web, à défaut son dernier
    statut connu. Libellé commun quand il existe (WEB_STATUSES) ; vide si aucun statut n'est connu."""
    dk, web = row["dk"], row["web"]
    if dk.get("status") and not (dk.get("error") and web.get("status")):
        return WEB_STATUSES.get(DK_LIFECYCLE.get(dk["status"].lower()), dk["status"])
    return web_status_label(web)


def _status_link(dk: dict, web: dict) -> dict | None:
    """Lien d'un composant sans avis : sa fiche Digi-Key, sinon la fiche trouvée par la recherche web."""
    if dk.get("product_url"):
        return {"url": dk["product_url"], "label": "Digi-Key"}
    if web.get("url"):
        return {"url": web["url"], "label": urlsplit(web["url"]).hostname}
    return None


def group(notices: list[dict], components: list[dict], include_quiet: bool = False) -> list[dict]:
    """Une ligne par composant : ses avis (le plus critique puis le plus récent d'abord), ses types,
    l'échéance et le lien de son avis le plus critique.

    `include_quiet` ajoute les composants suivis sans avis ; les avis d'un composant retiré de la
    liste restent affichés (`tracked` faux) mais ne sont plus une alerte (`critical` faux, comme dans
    `summary` et le rappel). Tri : critiques, puis criticité, échéance, MPN.
    """
    by_key = {key_of(c["mpn"], c["manufacturer"]): c for c in components}
    buckets: dict[str, list[dict]] = {k: [] for k in by_key} if include_quiet else {}
    for n in notices:
        buckets.setdefault(n["key"], []).append(n)
    rows = []
    for key, ns in buckets.items():
        ns = sorted(sorted(ns, key=lambda n: n["found_at"], reverse=True), key=lambda n: severity(n["type"]))
        c = by_key.get(key) or {}
        dk, web = c.get("dk") or {}, c.get("web_status") or {}
        worst = ns[0] if ns else None
        link = worst["sources"][0] if worst else _status_link(dk, web)
        alt = alternatives(c)
        rows.append({
            "key": key, "anchor": anchor(key), "tracked": bool(c), "dk": dk, "web": web,
            "mpn": c.get("mpn") or worst["mpn"], "manufacturer": c.get("manufacturer") or worst["manufacturer"],
            "web_checked_at": c.get("web_checked_at"), "notices": ns, "worst": worst, "link": link,
            "critical_params": c.get("critical_params"), "alt": alt, "alts": len(_alt_items(alt)),
            "types": list(dict.fromkeys(n["type"] for n in ns)),
            "critical": bool(c) and any(n["type"] in CRITICAL for n in ns),
            "deadline": next((d for n in ns if (d := n.get("ltb_date") or n.get("eol_date"))), ""),
            "found": max((n["found_at"] for n in ns), default=""),
        })
    rows.sort(key=lambda r: (not r["critical"], severity(r["types"][0]) if r["types"] else 99,
                             r["deadline"] or "9999", r["mpn"]))
    return rows


def summary(components: list[dict], notices: list[dict]) -> dict:
    worst = worst_by_key(notices)
    keys = {key_of(c["mpn"], c["manufacturer"]) for c in components}
    critical = [w for k, w in worst.items() if k in keys and w["type"] in CRITICAL]
    return {"components": len(components), "notices": len(notices), "critical": len(critical)}


def digest(components: list[dict], notices: list[dict], new: list[dict], scan: dict | None = None,
           specs: dict[str, dict] | None = None) -> dict:
    """Vue commune au rapport, au chapitre et à l'e-mail ; `scan` : dernier passage Digi-Key (`scan_coverage`) ;
    `specs` (fiches) : motif d'absence d'alternatives des composants en fin de vie (`alternatives_gap`) ; sans
    elles, aucun motif (qui serait faux : « pas de fiche »)."""
    by_key = {key_of(c["mpn"], c["manufacturer"]): c for c in components}
    worst = worst_by_key(notices)
    reminder = _sort([w for k, w in worst.items() if k in by_key and w["type"] in CRITICAL])
    new = _sort(new)
    return {
        "alt_gaps": {} if specs is None else {
            w["key"]: alternatives_gap(by_key[w["key"]], specs.get(w["key"])) for w in reminder},
        "new_critical": [n for n in new if n["type"] in CRITICAL],
        "new_other": [n for n in new if n["type"] not in CRITICAL],
        "reminder": reminder,
        "comps": components,
        "components": len(components),
        "web_checked": sum(1 for c in components if c.get("web_checked_at")),
        "dk_checked": sum(1 for c in components if (c.get("dk") or {}).get("status")),
        "coverage": scan_coverage(scan),
    }


def subject_prefix(view: dict) -> str:
    crit, other = len(view["new_critical"]), len(view["new_other"])
    if crit:
        return f"[EOL ×{crit}] "
    return f"[PCN ×{other}] " if other else ""


# --------------------------------------------------------------------------
# Rendu (chaque champ externe est échappé ICI et nulle part ailleurs)
# --------------------------------------------------------------------------
_MD_SPECIAL = re.compile(r"([!-/:-@\[-`{-~])")  # toute la ponctuation ASCII (pandoc l'échappe)


def md_escape(value) -> str:
    return _MD_SPECIAL.sub(r"\\\1", " ".join(str(value or "").split()))


def _md_url(url: str) -> str:
    # Destination de lien : ni espace ni parenthèse (URL déjà validée par normalize_url).
    return quote(url, safe=":/?#[]@!$&'*+,;=%-._~")


def _md_sources(n: dict) -> str:
    return " · ".join(f"[{md_escape(s['label'])}]({_md_url(s['url'])})" for s in n["sources"])


def _sections(view: dict, reminder_limit: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Lignes par composant communes au PDF et à l'e-mail : (nouveaux critiques, autres nouveaux, rappel)."""
    rows = group(view["new_critical"] + view["new_other"], view["comps"])
    return ([r for r in rows if r["critical"]], [r for r in rows if not r["critical"]],
            group(view["reminder"][:reminder_limit], view["comps"]))


def _md_badges(types: list[str]) -> str:
    return " ".join(f"[{md_escape(TYPES[t][0])}]{{.badge .{badge_class(t)}}}" for t in types)


def _md_link(link: dict | None) -> str:
    return f"[{md_escape(link['label'])}]({_md_url(link['url'])})" if link else "—"


def _md_table(rows: list[dict]) -> list[str]:
    """Tableau compact : une ligne par composant ; le MPN renvoie à son détail (lien interne).
    Bloc `.comp-table` : le tableau peut se couper entre deux pages (sinon une longue liste saute de page)."""
    out = ["::: {.comp-table}", "| Composant | Fabricant | Avis | Échéance | Lien |", "|---|---|---|---|---|"]
    out += [f"| [**{md_escape(r['mpn'])}**](#{r['anchor']}){' ⇄ ' + str(r['alts']) if r['alts'] else ''} "
            f"| {md_escape(r['manufacturer'])} "
            f"| {_md_badges(r['types'])} | {md_escape(r['deadline'] or '—')} | {_md_link(r['link'])} |" for r in rows]
    return [*out, ":::"]


def _md_alternatives(r: dict, gap: str) -> list[str]:
    """Alternatives d'un composant sous son détail (REPORT_DIFFS écarts chacune), ou le motif de leur absence."""
    rec, items = r["alt"], _alt_items(r["alt"])
    if not items:
        return ["", f"*Alternatives : {md_escape(gap)}.*"] if gap else []
    crit = " · ".join(f"{md_escape(p.get('name'))} = {md_escape(p.get('value'))}"
                      for p in rec["criteria"] if isinstance(p, dict))
    out = ["", f"**Alternatives actives Digi-Key** (recherche du {md_escape(rec['searched_at'])}"
               f"{f' ; à l’identique : {crit}' if crit else ''} ; à qualifier avant tout remplacement)", ""]
    for a in items:
        url, mpn = normalize_url(a.get("url") or ""), f"**{md_escape(a.get('mpn'))}**"
        line = f"- {f'[{mpn}]({_md_url(url)})' if url else mpn} — {md_escape(a.get('manufacturer'))}"
        if isinstance(a.get("score"), int):
            line += f" · {a['score']} % identique"
        if a.get("description"):
            line += f" · {md_escape(a['description'])}"
        diffs = [d for d in a.get("diffs") or [] if isinstance(d, dict)]
        gaps = " ; ".join(f"{md_escape(d.get('name'))} : {md_escape(d.get('ref'))} → {md_escape(d.get('alt'))}"
                          for d in diffs[:REPORT_DIFFS])
        more = " ; …" if len(diffs) > REPORT_DIFFS else ""
        out += [line + "\\", f"  {f'Écarts : {gaps}{more}' if gaps else 'Autres caractéristiques identiques'}"]
    return out


def _md_detail(r: dict, gap: str = "") -> list[str]:
    out = [f"::: {{#{r['anchor']} .comp}}", f"**{md_escape(r['mpn'])}** — {md_escape(r['manufacturer'])}", ""]
    for n in r["notices"]:
        line = f"- {_md_badges([n['type']])} **{md_escape(n['title'])}**"
        if n.get("number"):
            line += f" ({md_escape(n['number'])})"
        if n.get("scope") == "gamme":
            line += " · *gamme*"
        dates = [f"{label} {md_escape(n[f])}" for f, label in (("published", "publié"), ("ltb_date", "dernière cde"),
                                                              ("eol_date", "fin de vie"), ("found_at", "détecté"))
                 if n.get(f)]
        out.append(line + (" — " + " · ".join(dates) if dates else "") + "\\")
        if n.get("summary"):
            out.append(f"  {md_escape(n['summary'])}\\")
        out.append(f"  Sources : {_md_sources(n)}")
    return [*out, *_md_alternatives(r, gap), ":::", ""]


def to_markdown(view: dict, heading: str = "##", reminder_limit: int = 200) -> str:
    """Chapitre (heading « ## ») ou corps de rapport (« # ») au format du skill PDF : tableaux
    compacts par composant, puis le détail de chacun (cible des liens du tableau)."""
    h, sub = heading, heading + "#"
    crit, other, rem = view["new_critical"], view["new_other"], view["reminder"]
    crit_rows, other_rows, rem_rows = _sections(view, reminder_limit)
    # détail : chaque avis affiché (nouveautés et rappel), une seule fois
    shown = {id(n): n for n in crit + other + rem[:reminder_limit]}
    detail_rows = group(list(shown.values()), view["comps"])
    lines = [f"{h} Composants — PCN & fins de vie", "",
             "::: {.keyfigures}",
             "::: {.fig}", f"**{len(crit)}**", "", "nouvelle(s) fin(s) de vie / dernière(s) commande(s)", ":::",
             "::: {.fig}", f"**{len(other)}**", "", "autre(s) nouvel(s) avis (PCN, NRND…)", ":::",
             "::: {.fig}", f"**{len(rem)}** / {view['components']}", "",
             "composant(s) suivi(s) en fin de vie ou en dernière commande", ":::",
             ":::", ""]
    cov = view.get("coverage")
    if cov and cov["partial"]:
        lines += ["::: {.callout .vigilance}", md_escape(cov["text"]), ":::", ""]
    if crit_rows:
        lines += ["::: {.critical}", f"{sub} Nouvelles fins de vie et dernières commandes", "",
                  *_md_table(crit_rows), ":::", ""]
    if other_rows:
        lines += [f"{sub} Autres nouveaux avis", "", *_md_table(other_rows), ""]
    if not crit_rows and not other_rows:
        lines += ["::: {.note}", "Aucun nouvel avis depuis le précédent envoi.", ":::", ""]
    if rem_rows:
        lines += [f"{sub} Rappel : composants en fin de vie ou en dernière commande", "", *_md_table(rem_rows), ""]
        if len(rem) > reminder_limit:
            lines += [f"*… et {len(rem) - reminder_limit} autres : voir l'interface.*", ""]
    if detail_rows:
        lines += [f"{sub} Détail par composant", ""]
        for r in detail_rows:
            lines += _md_detail(r, view["alt_gaps"].get(r["key"], ""))
    if cov and not cov["partial"]:
        lines += [f"*{md_escape(cov['text'])}*", ""]
    lines += [f"*Couverture : {view['components']} composants suivis ; statut Digi-Key connu pour "
              f"{view['dk_checked']} ; recherche web effectuée au moins une fois pour {view['web_checked']}.*", ""]
    return "\n".join(lines)


# Couleurs des badges de l'e-mail (styles en ligne) : (texte, fond) ; fond plein pour les fins de vie.
_MAIL_BADGE = {"eol": ("#ffffff", "#b91c1c"), "ltb": ("#b91c1c", "#ffffff"), "nrnd": ("#a16207", "#ffffff"),
               "pcn": ("#1d4ed8", "#ffffff"), "autre": ("#1d4ed8", "#ffffff")}


def to_html_email(view: dict, title: str, ui_hint: str = "") -> str:
    """Corps d'e-mail HTML autonome (styles en ligne : les clients mail ignorent les feuilles) :
    tableau compact par composant, le détail est dans le PDF joint."""
    esc = html.escape

    def badges(types: list[str]) -> str:
        out = []
        for t in types:
            ink, bg = _MAIL_BADGE[badge_class(t)]
            out.append(f'<span style="display:inline-block;white-space:nowrap;font-size:11px;font-weight:bold;'
                       f'padding:1px 6px;margin:1px 2px 1px 0;border-radius:9px;border:1px solid {bg if bg != "#ffffff" else ink};'
                       f'color:{ink};background:{bg}">{esc(TYPES[t][0])}</span>')
        return "".join(out)

    def table(rows: list[dict], color: str) -> str:
        head = "".join(f'<th style="text-align:left;padding:6px;background:{color};color:#fff">{c}</th>'
                       for c in ("Composant", "Avis", "Échéance", "Lien"))
        body = []
        for r in rows:
            link = (f'<a href="{esc(r["link"]["url"])}">{esc(r["link"]["label"])}</a>' if r["link"] else "—")
            who = f"<b>{esc(r['mpn'])}</b><br><small>{esc(r['manufacturer'])}</small>"
            if r["alts"]:
                who += f'<br><small style="color:#0f766e">⇄ {r["alts"]} alternative(s) dans le PDF</small>'
            avis = f'{badges(r["types"])}<br><small>{esc(r["worst"]["title"])}</small>'
            cells = (who, avis, f'<span style="white-space:nowrap">{esc(r["deadline"] or "—")}</span>', link)
            tint = "background:#fef2f2;" if r["critical"] else ""
            body.append("<tr>" + "".join(f'<td style="{tint}padding:6px;border-bottom:1px solid #e2e8f0;'
                                          f'vertical-align:top">{c}</td>' for c in cells) + "</tr>")
        return (f'<table style="border-collapse:collapse;width:100%;font-size:13px">'
                f"<tr>{head}</tr>{''.join(body)}</table>")

    crit_rows, other_rows, rem_rows = _sections(view, 200)
    rem = view["reminder"]
    cov = view.get("coverage")
    parts = ['<div style="font-family:Arial,sans-serif;color:#1f2933;max-width:900px">',
             f"<h2 style=\"margin:0 0 12px\">{esc(title)}</h2>"]
    if cov and cov["partial"]:
        parts.append(f'<div style="border-left:6px solid #d97706;background:#fff7ed;padding:10px;margin:12px 0">'
                     f"<b>⚠ {esc(cov['text'])}</b></div>")
    if crit_rows:
        parts.append(f'<div style="border-left:6px solid #b91c1c;background:#fef2f2;padding:10px;margin:12px 0">'
                     f'<h3 style="color:#b91c1c;margin:0 0 8px">⚠ {len(crit_rows)} composant(s) avec une nouvelle '
                     f"fin de vie / dernière commande</h3>{table(crit_rows, '#b91c1c')}</div>")
    if other_rows:
        parts.append(f"<h3>Autres nouveaux avis ({len(other_rows)} composant(s))</h3>{table(other_rows, '#0f766e')}")
    if not crit_rows and not other_rows:
        parts.append("<p>Aucun nouvel avis depuis le précédent envoi.</p>")
    if rem_rows:
        parts.append(f"<h3>Rappel : {len(rem)} composant(s) en fin de vie ou en dernière commande</h3>"
                     f"{table(rem_rows, '#7f1d1d')}")
    done = f"{esc(cov['text'])} " if cov and not cov["partial"] else ""
    parts.append(f'<p style="color:#66727f;font-size:12px">{view["components"]} composants suivis. {done}'
                 f"Détail de chaque avis dans le PDF joint. {esc(ui_hint)}</p></div>")
    return "".join(parts)


def to_text_email(view: dict) -> str:
    crit_rows, other_rows, _ = _sections(view, 0)
    cov = view.get("coverage")
    lines = [f"⚠ {cov['text']}", ""] if cov and cov["partial"] else []
    for label, rows in (("NOUVELLES FINS DE VIE / DERNIÈRES COMMANDES", crit_rows),
                        ("AUTRES NOUVEAUX AVIS", other_rows)):
        if rows:
            lines.append(f"{label} ({len(rows)} composant(s))")
            lines += [f"- {r['mpn']} ({r['manufacturer']}) : {', '.join(TYPES[t][0] for t in r['types'])}"
                      f"{' — ' + str(r['alts']) + ' alternative(s) dans le PDF' if r['alts'] else ''}"
                      f"{' — ' + r['link']['url'] if r['link'] else ''}" for r in rows]
            lines.append("")
    if not crit_rows and not other_rows:
        lines.append("Aucun nouvel avis depuis le précédent envoi.")
    if cov and not cov["partial"]:
        lines.append(cov["text"])
    return "\n".join(lines)


# Export Excel : une ligne par composant, comme la liste de l'interface (`group(..., include_quiet=True)`).
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# Caractères interdits en XML 1.0 (openpyxl ne refuse que les contrôles : U+FFFE / U+FFFF corrompraient le fichier).
_XLSX_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_XLSX_COLUMNS = (("Composant", 24), ("Fabricant", 22), ("Critique", 9), ("Avis", 28),
                 ("Avis principal", 48), ("Échéance", 12), ("Détecté", 12), ("Statut Digi-Key", 20),
                 ("Vérifié Digi-Key", 15), ("Statut web", 18), ("Recherche web", 15), ("Alternatives", 40),
                 ("Lien", 48), ("Suivi", 9))
_XLSX_DATES = frozenset(i for i, (name, _) in enumerate(_XLSX_COLUMNS, 1)
                        if name in ("Échéance", "Détecté", "Vérifié Digi-Key", "Recherche web"))
_XLSX_LINK = next(i for i, (name, _) in enumerate(_XLSX_COLUMNS, 1) if name == "Lien")


def xlsx_name(slug: str, stamp: str) -> str:
    """Nom du fichier exporté (slug validé, horodatage AAAA-MM-JJ[-HHhMM])."""
    return f"{stamp}-{slug}.xlsx"


def xlsx_re(slug: str) -> re.Pattern:
    """Noms possibles des exports de cette veille composants (inverse de `xlsx_name`)."""
    return re.compile(rf"\d{{4}}-\d{{2}}-\d{{2}}(?:-\d{{2}}h\d{{2}})?-{re.escape(slug)}\.xlsx")


def _xlsx_cell(value, is_date: bool) -> str | dt.date:
    """Valeur sûre pour une cellule : date ISO valide -> vraie date (colonnes de dates seulement) ;
    sinon texte sans caractère interdit (un titre d'avis venu du web ne casse pas le fichier)."""
    value = "" if value is None else str(value)
    if is_date and value and _date(value) == value:
        return dt.date.fromisoformat(value)
    return _XLSX_ILLEGAL.sub("", value)


def to_xlsx(components: list[dict], notices: list[dict], sources: dict[str, list[str]] | None = None) -> bytes:
    """Classeur Excel d'une ligne par composant (critiques en tête, filtre automatique). `sources` (base des
    composants) : {clé: veilles qui ont analysé la référence}, en dernière colonne « Veilles ».

    Les textes viennent du web : chaque cellule texte est forcée en type CHAÎNE, une valeur
    « =… » n'est jamais une formule ; seuls les liens validés par `normalize_url` sont cliquables.
    """
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Composants"
    columns = _XLSX_COLUMNS + ((("Veilles", 40),) if sources is not None else ())
    ws.append([name for name, _ in columns])
    for i, (_, width) in enumerate(columns, 1):
        ws.cell(1, i).font = Font(bold=True, color="FFFFFF")
        ws.cell(1, i).fill = PatternFill("solid", fgColor="0F766E")
        ws.column_dimensions[ws.cell(1, i).column_letter].width = width
    crit_fill = PatternFill("solid", fgColor="FEF2F2")
    # numéro de ligne tenu ici : ws.max_row parcourt toute la feuille (export quadratique sinon)
    for n, r in enumerate(group(notices, components, include_quiet=True), 2):
        dk, link = r["dk"], r["link"]
        url = normalize_url(link["url"]) if link else None
        values = (r["mpn"], r["manufacturer"], "Oui" if r["critical"] else "",
                  ", ".join(TYPES[t][0] for t in r["types"]), r["worst"]["title"] if r["worst"] else "",
                  r["deadline"], r["found"], dk.get("status") or dk.get("error"), dk.get("checked_at"),
                  web_status_label(r["web"]), r["web_checked_at"],
                  " ; ".join(f"{a['mpn']} ({a.get('manufacturer') or '?'})" for a in _alt_items(r["alt"])), url,
                  "" if r["tracked"] else "retiré") + ((", ".join(sources.get(r["key"], ())),) if sources is not None else ())
        for col, value in enumerate(values, 1):
            cell = ws.cell(n, col, _xlsx_cell(value, col in _XLSX_DATES))
            if cell.data_type == "f":  # « =… » venu du web : texte, jamais formule
                cell.data_type = "s"
            elif cell.is_date:
                cell.number_format = "yyyy-mm-dd"
            if r["critical"]:
                cell.fill = crit_fill
        if url:
            ws.cell(n, _XLSX_LINK).hyperlink = url
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    # Impression : paysage, toutes les colonnes sur la largeur d'une page, en-tête répété.
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"
    out = BytesIO()
    wb.save(out)
    return out.getvalue()
