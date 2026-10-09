"""Veille composants : références suivies et avis PCN / fin de vie (EOL) détectés.

    veilles/<slug>/components.json   {"components": [...], "batch": [clés du lot web en cours]}
    veilles/<slug>/notices.json      {"notices": [...]}

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
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

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


def norm_mfr(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def key_of(mpn: str, manufacturer: str) -> str:
    return f"{norm_mfr(manufacturer)}|{norm_mpn(mpn)}"


def same_manufacturer(a: str, b: str) -> bool:
    """« Texas Instruments » ~ « Texas Instruments Inc. ». Un nom court (« TI », « ON ») doit être
    identique : « ON » n'est pas contenu dans « Infineon »."""
    na, nb = sorted((norm_mfr(a), norm_mfr(b)), key=len)
    return bool(na) and (na == nb or (len(na) >= 4 and na in nb))


def validate_entry(mpn, manufacturer, ref="") -> tuple[dict | None, str]:
    """Une référence -> (entrée normalisée, "") ou (None, motif du rejet). Règle unique pour la
    saisie manuelle et l'import de fichiers."""
    mpn, mfr, ref = (" ".join(str(x or "").split()) for x in (mpn, manufacturer, ref))
    if not MPN_RE.match(mpn):
        return None, "MPN vide ou invalide" if mpn else "MPN vide"
    if not norm_mfr(mfr) or len(mfr) > 80:
        return None, "fabricant manquant ou invalide"
    return {"mpn": mpn, "manufacturer": mfr, "ref": ref[:64]}, ""


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
    """Une référence par ligne : « MPN ; Fabricant ; Réf. interne » (tabulation, ; ou ,).

    Le collage depuis un tableur (tabulations) fonctionne tel quel ; `#` commente une ligne ;
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
        entry, _ = validate_entry(*(parts + ["", ""])[:3])
        if entry is None:
            bad.append(f"ligne {n}")
        else:
            out.append(entry)
    if bad:
        raise ComponentsError("format attendu « MPN ; Fabricant ; Réf. interne » : "
                              + ", ".join(bad[:10]) + (" …" if len(bad) > 10 else ""))
    return unique(out)


def format_components(components: list[dict]) -> str:
    """Inverse de parse_components (formulaire d'édition)."""
    return "\n".join(" ; ".join(filter(None, (c["mpn"], c["manufacturer"], c.get("ref")))) for c in components)


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


def replace_components(current: list[dict], wanted: list[dict]) -> list[dict]:
    """Nouvelle liste ; l'état (vérifications, statut Digi-Key) des références conservées est gardé."""
    by_key = {key_of(c["mpn"], c["manufacturer"]): c for c in current}
    out = []
    for w in wanted:
        old = by_key.get(key_of(w["mpn"], w["manufacturer"]))
        if old:
            out.append({**old, "mpn": w["mpn"], "manufacturer": w["manufacturer"], "ref": w.get("ref", "")})
        else:
            out.append({"mpn": w["mpn"], "manufacturer": w["manufacturer"], "ref": w.get("ref", ""),
                        "added_at": today(), "web_checked_at": None, "dk": None})
    return out


# --------------------------------------------------------------------------
# Fichiers (écriture atomique ; un fichier corrompu est une ERREUR, jamais une liste vide)
# --------------------------------------------------------------------------
def _load(slug: str, name: str) -> dict:
    path = store.veille_dir(slug) / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ComponentsError(f"{name} illisible ({exc}) : corriger ou restaurer le fichier") from None
    if not isinstance(data, dict):
        raise ComponentsError(f"{name} illisible (objet JSON attendu)")
    return data


def _save(slug: str, name: str, data: dict) -> None:
    store.atomic_write(store.veille_dir(slug) / name, json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def load_components(slug: str) -> dict:
    data = _load(slug, "components.json")
    return {"components": data.get("components") or [], "batch": data.get("batch") or []}


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


def validate_candidates(raw, components: list[dict]) -> tuple[list[dict], list[str]]:
    """{"notices": [...]} de l'agent -> (avis valides, un par composant suivi touché ; rejets)."""
    items = raw.get("notices") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ComponentsError("format attendu : {\"notices\": [...]}")
    by_mpn: dict[str, list[dict]] = {}
    for c in components:
        by_mpn.setdefault(norm_mpn(c["mpn"]), []).append(c)
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
        sources, seen_urls = [], set()
        for s in raw_sources:
            url = normalize_url(s.get("url") if isinstance(s, dict) else s)
            if url and url not in seen_urls:
                seen_urls.add(url)
                label = _text(s.get("label"), "label") if isinstance(s, dict) else ""
                sources.append({"url": url, "label": label or urlsplit(url).hostname})
        if not sources:
            rejected.append(f"#{i} : aucune source http(s) valide")
            continue
        manufacturer = _text(item.get("manufacturer"), "label")
        matched = []
        for mpn in mpns:
            cands = by_mpn.get(norm_mpn(_text(mpn, "label")), [])
            if len(cands) > 1 and manufacturer:
                cands = [c for c in cands if same_manufacturer(c["manufacturer"], manufacturer)]
            if len(cands) == 1 and cands[0] not in matched:
                matched.append(cands[0])
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
    if n.get("origin") == "digikey":
        ids.add((n["key"], "dk:" + n["title"].lower()))
    return ids


def notice_id(n: dict) -> str:
    ident = n.get("number") or n["sources"][0]["url"]
    return hashlib.sha1(f"{n['key']}|{ident}|{n['type']}".encode()).hexdigest()[:12]


def merge(notices: list[dict], candidates: list[dict], found_at: str | None = None) -> list[dict]:
    """Ajoute les candidats inconnus à `notices` (en place) et les renvoie.

    Un candidat déjà connu (même composant ET même numéro d'avis ou même URL) n'est pas
    signalé à nouveau : ses nouvelles sources et les champs vides sont complétés.
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
        n = {**c, "id": notice_id(c), "found_at": found_at or today(), "delivered": {}}
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
    """Jamais vérifiés, puis statut Digi-Key changé depuis la dernière recherche, puis les plus anciens."""
    def rank(c):
        web = c.get("web_checked_at")
        changed = (c.get("dk") or {}).get("changed_at")
        if not web:
            return (0, "")
        return (1 if changed and changed > web else 2, web)
    return sorted(components, key=rank)[: max(1, size)]


def search_since(c: dict) -> str | None:
    """Date à partir de laquelle chercher (None : historique complet, jamais vérifié)."""
    web = c.get("web_checked_at")
    if not web:
        return None
    return (dt.date.fromisoformat(web[:10]) - dt.timedelta(days=WEB_MARGIN_DAYS)).isoformat()


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


def summary(components: list[dict], notices: list[dict]) -> dict:
    worst = worst_by_key(notices)
    keys = {key_of(c["mpn"], c["manufacturer"]) for c in components}
    critical = [w for k, w in worst.items() if k in keys and w["type"] in CRITICAL]
    return {"components": len(components), "notices": len(notices), "critical": len(critical)}


def digest(components: list[dict], notices: list[dict], new: list[dict]) -> dict:
    """Vue commune au rapport, au chapitre et à l'e-mail."""
    keys = {key_of(c["mpn"], c["manufacturer"]) for c in components}
    refs = {key_of(c["mpn"], c["manufacturer"]): c.get("ref", "") for c in components}
    worst = worst_by_key(notices)
    reminder = _sort([w for k, w in worst.items() if k in keys and w["type"] in CRITICAL])
    new = _sort(new)
    return {
        "new_critical": [n for n in new if n["type"] in CRITICAL],
        "new_other": [n for n in new if n["type"] not in CRITICAL],
        "reminder": reminder,
        "refs": refs,
        "components": len(components),
        "web_checked": sum(1 for c in components if c.get("web_checked_at")),
        "dk_checked": sum(1 for c in components if (c.get("dk") or {}).get("status")),
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


def _md_table(rows: list[dict], refs: dict[str, str]) -> list[str]:
    out = ["| Composant | Fabricant | Réf. interne | Avis | Dernière cde | Sources |",
           "|---|---|---|---|---|---|"]
    for n in rows:
        title = f"**{md_escape(TYPES[n['type']][0])}** — {md_escape(n['title'])}"
        if n.get("number"):
            title += f" ({md_escape(n['number'])})"
        if n.get("scope") == "gamme":
            title += " · *gamme*"
        out.append(f"| **{md_escape(n['mpn'])}** | {md_escape(n['manufacturer'])} | {md_escape(refs.get(n['key']))} "
                   f"| {title} | {md_escape(n.get('ltb_date') or n.get('eol_date') or '—')} | {_md_sources(n)} |")
    return out


def to_markdown(view: dict, heading: str = "##", reminder_limit: int = 200) -> str:
    """Chapitre (heading « ## ») ou corps de rapport (« # ») au format du skill PDF."""
    h, sub = heading, heading + "#"
    crit, other, rem = view["new_critical"], view["new_other"], view["reminder"]
    lines = [f"{h} Composants — PCN & fins de vie", "",
             "::: {.keyfigures}",
             "::: {.fig}", f"**{len(crit)}**", "", "nouvelle(s) fin(s) de vie / dernière(s) commande(s)", ":::",
             "::: {.fig}", f"**{len(other)}**", "", "autre(s) nouvel(s) avis (PCN, NRND…)", ":::",
             "::: {.fig}", f"**{len(rem)}** / {view['components']}", "",
             "composant(s) suivi(s) en fin de vie ou en dernière commande", ":::",
             ":::", ""]
    if crit:
        lines += ["::: {.critical}", f"{sub} Nouvelles fins de vie et dernières commandes", "",
                  *_md_table(crit, view["refs"]), ":::", ""]
    if other:
        lines += [f"{sub} Autres nouveaux avis", "", *_md_table(other, view["refs"]), ""]
    if not crit and not other:
        lines += ["::: {.note}", "Aucun nouvel avis depuis le précédent envoi.", ":::", ""]
    if rem:
        lines += [f"{sub} Rappel : composants en fin de vie ou en dernière commande", "",
                  *_md_table(rem[:reminder_limit], view["refs"]), ""]
        if len(rem) > reminder_limit:
            lines += [f"*… et {len(rem) - reminder_limit} autres : voir l'interface.*", ""]
    lines += [f"*Couverture : {view['components']} composants suivis ; statut Digi-Key connu pour "
              f"{view['dk_checked']} ; recherche web effectuée au moins une fois pour {view['web_checked']}.*", ""]
    return "\n".join(lines)


def to_html_email(view: dict, title: str, ui_hint: str = "") -> str:
    """Corps d'e-mail HTML autonome (styles en ligne : les clients mail ignorent les feuilles)."""
    esc = html.escape

    def table(rows: list[dict], color: str) -> str:
        head = "".join(f'<th style="text-align:left;padding:6px;background:{color};color:#fff">{c}</th>'
                       for c in ("Composant", "Avis", "Dernière cde", "Sources"))
        body = []
        for n in rows:
            src = " · ".join(f'<a href="{esc(s["url"])}">{esc(s["label"])}</a>' for s in n["sources"])
            avis = f"<b>{esc(TYPES[n['type']][0])}</b> — {esc(n['title'])}"
            if n.get("number"):
                avis += f" ({esc(n['number'])})"
            ref = view["refs"].get(n["key"], "")
            who = f"<b>{esc(n['mpn'])}</b><br><small>{esc(n['manufacturer'])}{' · ' + esc(ref) if ref else ''}</small>"
            date = esc(n.get("ltb_date") or n.get("eol_date") or "—")
            cells = (who, avis, f'<span style="white-space:nowrap">{date}</span>', src)
            body.append("<tr>" + "".join(f'<td style="padding:6px;border-bottom:1px solid #e2e8f0;'
                                          f'vertical-align:top">{c}</td>' for c in cells) + "</tr>")
        return (f'<table style="border-collapse:collapse;width:100%;font-size:13px">'
                f"<tr>{head}</tr>{''.join(body)}</table>")

    crit, other, rem = view["new_critical"], view["new_other"], view["reminder"]
    parts = ['<div style="font-family:Arial,sans-serif;color:#1f2933;max-width:900px">',
             f"<h2 style=\"margin:0 0 12px\">{esc(title)}</h2>"]
    if crit:
        parts.append(f'<div style="border-left:6px solid #b91c1c;background:#fef2f2;padding:10px;margin:12px 0">'
                     f'<h3 style="color:#b91c1c;margin:0 0 8px">⚠ {len(crit)} nouvelle(s) fin(s) de vie / '
                     f"dernière(s) commande(s)</h3>{table(crit, '#b91c1c')}</div>")
    if other:
        parts.append(f"<h3>Autres nouveaux avis ({len(other)})</h3>{table(other, '#0f766e')}")
    if not crit and not other:
        parts.append("<p>Aucun nouvel avis depuis le précédent envoi.</p>")
    if rem:
        parts.append(f"<h3>Rappel : {len(rem)} composant(s) en fin de vie ou en dernière commande</h3>"
                     f"{table(rem[:200], '#7f1d1d')}")
    parts.append(f'<p style="color:#66727f;font-size:12px">{view["components"]} composants suivis. '
                 f"{esc(ui_hint)}</p></div>")
    return "".join(parts)


def to_text_email(view: dict) -> str:
    lines = []
    for label, rows in (("NOUVELLES FINS DE VIE / DERNIÈRES COMMANDES", view["new_critical"]),
                        ("AUTRES NOUVEAUX AVIS", view["new_other"])):
        if rows:
            lines.append(f"{label} ({len(rows)})")
            lines += [f"- {n['mpn']} ({n['manufacturer']}) : {TYPES[n['type']][0]} — {n['title']} — "
                      f"{n['sources'][0]['url']}" for n in rows]
            lines.append("")
    return "\n".join(lines) or "Aucun nouvel avis depuis le précédent envoi."
