"""Base des composants : ce que les veilles composants savent déjà de chaque référence (statuts Digi-Key et
web, dernière recherche web, avis PCN / fins de vie), partagé entre veilles.

Index des fichiers des veilles (voir db.py), avec sa PROVENANCE `source` : le slug d'une veille, ou le nom
du dossier d'une veille archivée.
- `sync` : chaque enregistrement de l'état d'une veille REMPLACE sa contribution. Une réinitialisation
  efface donc aussi ce qu'elle avait apporté.
- `archive` : l'archivage conserve la contribution, sous le nom de l'archive.
- `rebuild` : tout est reconstruit depuis les fichiers (au démarrage, ou `veillectl catalog rebuild`).
- `knowledge` : à l'ajout de références dans une veille, ce qu'en savent les AUTRES sources.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import components as comp
from . import db, paths, store

CHUNK = 500  # clés par requête (borne le nombre de paramètres SQLite)


def _json(value) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value else None


def _replace(con: sqlite3.Connection, source: str, components: list[dict], notices: list[dict]) -> None:
    con.execute("DELETE FROM parts WHERE source = ?", (source,))
    con.execute("DELETE FROM notices WHERE source = ?", (source,))
    # Une référence jamais vérifiée n'apprend rien aux autres veilles : pas indexée.
    con.executemany("INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?, ?, ?)", [
        (source, comp.key_of(c["mpn"], c["manufacturer"]), c["mpn"], c["manufacturer"],
         _json(c.get("dk")), _json(c.get("web_status")), c.get("web_checked_at"))
        for c in components if c.get("dk") or c.get("web_status") or c.get("web_checked_at")])
    con.executemany("INSERT OR REPLACE INTO notices VALUES (?, ?, ?, ?)", [
        (source, n["id"], n["key"], _json({k: v for k, v in n.items() if k != "delivered"})) for n in notices])


def sync(source: str, components: list[dict], notices: list[dict]) -> None:
    """La contribution de `source` devient cet état (une transaction)."""
    with db.connect() as con, con:
        _replace(con, source, components, notices)


def archive(source: str, archived_as: str) -> None:
    """Veille archivée : sa contribution est gardée sous le nom de son dossier d'archive (unique)."""
    with db.connect() as con, con:
        for table in ("parts", "notices"):
            con.execute(f"UPDATE {table} SET source = ? WHERE source = ?", (archived_as, source))


def _newest(known: dict, field: str, value: dict | None) -> None:
    if value and (value.get("checked_at") or "") >= ((known.get(field) or {}).get("checked_at") or ""):
        known[field] = value


def knowledge(keys, exclude: str) -> dict[str, dict]:
    """Ce que les sources autres que `exclude` savent des références `keys` : {clé: {"dk", "web_status",
    "web_checked_at" (les plus récents), "notices" (tous, à dédoublonner par components.merge)}}."""
    keys = list(dict.fromkeys(keys))
    out: dict[str, dict] = {}
    with db.connect() as con:
        for i in range(0, len(keys), CHUNK):
            chunk = keys[i:i + CHUNK]
            marks = ", ".join("?" * len(chunk))
            for key, dk, web, checked in con.execute(
                    f"SELECT key, dk, web_status, web_checked_at FROM parts WHERE source != ? AND key IN ({marks})",
                    (exclude, *chunk)):
                known = out.setdefault(key, {"notices": []})
                _newest(known, "dk", json.loads(dk) if dk else None)
                _newest(known, "web_status", json.loads(web) if web else None)
                if checked and checked > (known.get("web_checked_at") or ""):
                    known["web_checked_at"] = checked
            for key, data in con.execute(
                    f"SELECT key, data FROM notices WHERE source != ? AND key IN ({marks}) ORDER BY rowid",
                    (exclude, *chunk)):
                out.setdefault(key, {"notices": []})["notices"].append(json.loads(data))
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
    aux fichiers illisibles est ignorée et signalée, sans empêcher les autres."""
    states, skipped = [], []
    for source, directory in _sources():
        try:
            states.append((source, *comp.read_state(directory)))
        except comp.ComponentsError as exc:
            skipped.append(f"{source} : {exc}")
    with db.connect() as con, con:
        con.execute("DELETE FROM parts")
        con.execute("DELETE FROM notices")
        for source, components, notices in states:
            _replace(con, source, components, notices)
    return {**stats(), "skipped": skipped}


def stats() -> dict:
    """Références connues, avis distincts et sources (veilles actives et archivées) de l'index."""
    with db.connect() as con:
        parts, sources = con.execute(
            "SELECT COUNT(DISTINCT key), COUNT(DISTINCT source) FROM "
            "(SELECT key, source FROM parts UNION ALL SELECT key, source FROM notices)").fetchone()
        notices = con.execute("SELECT COUNT(DISTINCT id) FROM notices").fetchone()[0]
    return {"parts": parts, "notices": notices, "sources": sources}
