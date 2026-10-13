"""Base des composants : ce que les veilles composants savent déjà de chaque référence (statuts Digi-Key et
web, dernière recherche web, avis PCN / fins de vie), partagé entre veilles.

Index des fichiers des veilles (voir db.py), avec sa PROVENANCE `source` : le slug d'une veille, ou le nom
du dossier d'une veille archivée.
- `sync` : chaque enregistrement de l'état d'une veille REMPLACE sa contribution. Une réinitialisation
  efface donc aussi ce qu'elle avait apporté ; ce que d'autres veilles en ont déjà repris leur appartient
  (à réinitialiser aussi, au besoin).
- `archive` : l'archivage conserve la contribution, sous le nom de l'archive.
- `rebuild` : tout est reconstruit depuis les fichiers (au démarrage, ou `veillectl catalog rebuild`).
- `knowledge` : à l'ajout de références dans une veille, ce qu'en savent les AUTRES sources.
- `browse` : tout ce qui a déjà été analysé, toutes veilles confondues (page « Composants » de l'interface).
"""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from . import components as comp
from . import db, paths, store

CHUNK = 500  # clés par requête (borne le nombre de paramètres SQLite)
ARCHIVE_RE = re.compile(r"(.+)-(\d{4})(\d{2})(\d{2})-\d{6}")  # dossier d'archive d'une veille (service.archive)


def _json(value) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value else None


def _rows(source: str, components: list[dict], notices: list[dict]) -> tuple[list[tuple], list[tuple]]:
    """Lignes (parts, notices) de la contribution d'une source. Une référence jamais vérifiée n'apprend rien
    aux autres veilles : pas indexée. Lève sur un fichier de forme inattendue (édité à la main)."""
    parts = [(source, comp.key_of(c["mpn"], c["manufacturer"]), c["mpn"], c["manufacturer"],
              _json(c.get("dk")), _json(c.get("web_status")), c.get("web_checked_at"))
             for c in components if c.get("dk") or c.get("web_status") or c.get("web_checked_at")]
    rows = [(source, n["id"], n["key"], _json({k: v for k, v in n.items() if k != "delivered"})) for n in notices]
    return parts, rows


def _replace(con: sqlite3.Connection, source: str, rows: tuple[list[tuple], list[tuple]]) -> None:
    con.execute("DELETE FROM parts WHERE source = ?", (source,))
    con.execute("DELETE FROM notices WHERE source = ?", (source,))
    con.executemany("INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?, ?, ?)", rows[0])
    con.executemany("INSERT OR REPLACE INTO notices VALUES (?, ?, ?, ?)", rows[1])


def sync(source: str, components: list[dict], notices: list[dict]) -> None:
    """La contribution de `source` devient cet état (une transaction)."""
    rows = _rows(source, components, notices)
    with db.connect() as con, con:
        _replace(con, source, rows)


def archive(source: str, archived_as: str) -> None:
    """Veille archivée : sa contribution est gardée sous le nom de son dossier d'archive (unique)."""
    with db.connect() as con, con:
        for table in ("parts", "notices"):
            con.execute(f"UPDATE {table} SET source = ? WHERE source = ?", (archived_as, source))


def _newest(known: dict, field: str, value) -> None:
    if isinstance(value, dict) and value and (value.get("checked_at") or "") >= (
            (known.get(field) or {}).get("checked_at") or ""):
        known[field] = value


def archived_label(source: str) -> str:
    """Source qui n'est plus une veille active (dossier d'archive) : « slug (archivée le AAAA-MM-JJ) »."""
    m = ARCHIVE_RE.fullmatch(source)
    return f"{m[1]} (archivée le {m[2]}-{m[3]}-{m[4]})" if m else f"{source} (archivée)"


def _usable(notice, key: str) -> bool:
    """Avis de la base exploitable tel quel (fusion, affichage, rapport), sinon ignoré : un fichier retouché à la
    main, même celui d'une veille archivée, ne bloque ni la reprise ni la page « Composants »."""
    sources = notice.get("sources") if isinstance(notice, dict) else None
    return (isinstance(sources, list) and bool(sources) and notice.get("key") == key
            and notice.get("type") in comp.TYPES
            and all(isinstance(notice.get(f), str) for f in ("mpn", "manufacturer", "title", "found_at"))
            and all(isinstance(s, dict) and isinstance(s.get("url"), str) and isinstance(s.get("label"), str)
                    for s in sources))


@contextlib.contextmanager
def _snapshot() -> Iterator[sqlite3.Connection]:
    """Connexion de lecture : `parts` et `notices` lues dans un même instantané (une autre veille peut écrire
    entre deux requêtes ; elle attend la fin de la lecture, quelques millisecondes)."""
    with db.connect() as con:
        con.execute("BEGIN")
        try:
            yield con
        finally:
            con.rollback()


def _gather(con: sqlite3.Connection, where: str, params: tuple, out: dict[str, dict]) -> None:
    """Ajoute à `out` ce que savent les lignes des tables `parts` et `notices` qui vérifient `where` (clause
    écrite ici, jamais venue de l'extérieur : les valeurs passent par `params`) : {clé: {"mpn", "manufacturer",
    "dk", "web_status", "web_checked_at" (les plus récents), "notices" (tous, à dédoublonner par
    components.merge), "sources"}}."""
    for source, key, mpn, mfr, dk, web, checked in con.execute(
            "SELECT source, key, mpn, manufacturer, dk, web_status, web_checked_at FROM parts "
            f"WHERE {where} ORDER BY rowid", params):
        known = out.setdefault(key, {"mpn": mpn, "manufacturer": mfr, "notices": [], "sources": set()})
        known["sources"].add(source)
        _newest(known, "dk", json.loads(dk) if dk else None)
        _newest(known, "web_status", json.loads(web) if web else None)
        if isinstance(checked, str) and checked > (known.get("web_checked_at") or ""):
            known["web_checked_at"] = checked
    for source, key, data in con.execute(f"SELECT source, key, data FROM notices WHERE {where} ORDER BY rowid",
                                         params):
        notice = json.loads(data)
        if _usable(notice, key):
            known = out.setdefault(key, {"mpn": notice["mpn"], "manufacturer": notice["manufacturer"],
                                         "notices": [], "sources": set()})
            known["sources"].add(source)
            known["notices"].append(notice)


def knowledge(keys, exclude: str) -> dict[str, dict]:
    """Ce que les sources autres que `exclude` savent des références `keys` (forme : voir `_gather`)."""
    keys = list(dict.fromkeys(keys))
    out: dict[str, dict] = {}
    with _snapshot() as con:
        for i in range(0, len(keys), CHUNK):
            chunk = keys[i:i + CHUNK]
            _gather(con, f"source != ? AND key IN ({', '.join('?' * len(chunk))})", (exclude, *chunk), out)
    return out


def browse(key: str | None = None) -> dict[str, dict]:
    """Tout ce que la base sait, toutes sources confondues (veilles actives et archivées), ou ce qu'elle sait
    de la seule référence `key` (forme : voir `_gather`)."""
    out: dict[str, dict] = {}
    with _snapshot() as con:
        _gather(con, "key = ?" if key is not None else "1", (key,) if key is not None else (), out)
    return out


def _sources() -> list[tuple[str, Path]]:
    """(source, dossier) de chaque veille composants : actives, puis archivées. Une copie de réinitialisation
    (`<slug>-reinit-…`) n'a pas de veille.json : ignorée, ce qu'on a effacé ne revient pas."""
    out = [(v.slug, v.dir) for v in store.list_all() if v.kind == "composants"]
    root = paths.archive_dir()
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        try:
            meta = store.read_json_dict(d / "veille.json", comp.ComponentsError, "veille.json") if d.is_dir() else {}
        except comp.ComponentsError:
            continue
        if meta.get("kind") == "composants":
            out.append((d.name, d))
    return out


def rebuild() -> dict:
    """Reconstruit l'index depuis les fichiers des veilles (le carnet d'adresses n'est pas touché). Une veille
    aux fichiers illisibles ou de forme inattendue est ignorée et signalée, sans empêcher les autres."""
    contributions, skipped = [], []
    for source, directory in _sources():
        try:
            contributions.append((source, _rows(source, *comp.read_state(directory))))
        except Exception as exc:  # noqa: BLE001 - une veille illisible ne prive pas les autres de l'index
            skipped.append(f"{source} : {exc if isinstance(exc, comp.ComponentsError) else type(exc).__name__}")
    with db.connect() as con, con:
        con.execute("DELETE FROM parts")
        con.execute("DELETE FROM notices")
        for source, rows in contributions:
            _replace(con, source, rows)
    return {**stats(), "skipped": skipped}


def stats() -> dict:
    """Références connues, avis distincts et sources (veilles actives et archivées) de l'index."""
    with db.connect() as con:
        parts, sources = con.execute(
            "SELECT COUNT(DISTINCT key), COUNT(DISTINCT source) FROM "
            "(SELECT key, source FROM parts UNION ALL SELECT key, source FROM notices)").fetchone()
        notices = con.execute("SELECT COUNT(DISTINCT id) FROM notices").fetchone()[0]
    return {"parts": parts, "notices": notices, "sources": sources}
