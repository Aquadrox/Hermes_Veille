"""Carnet d'adresses : destinataires déjà saisis, proposés à la saisie d'une veille (autocomplétion).

Alimenté à la création d'une veille et par chaque adresse AJOUTÉE à ses destinataires (interface, CLI,
Telegram) ; amorcé avec les destinataires existants à la création de la base (db.py). Une adresse
« oubliée » (page Paramètres) n'est plus proposée ; elle reste destinataire des veilles qui l'utilisent.

Données personnelles : jamais écrites dans le journal d'audit, jamais exposées au MCP.
"""
from __future__ import annotations

import sqlite3

from . import db, store

MAX_SHOWN = 500  # adresses proposées (les plus utilisées d'abord)


def remember_in(con: sqlite3.Connection, addresses) -> None:
    """Enregistre (ou compte une utilisation de plus de) chaque adresse valide, dans la transaction de `con`.
    L'adresse factice d'une veille amorcée sans destinataire n'est jamais proposée."""
    now = store.now_iso()
    wanted = {a.strip().lower(): a.strip() for a in addresses if isinstance(a, str)}
    rows = [(a, now) for low, a in wanted.items()
            if store.EMAIL_RE.match(a) and low != store.PLACEHOLDER_RECIPIENT]
    con.executemany("INSERT INTO contacts (email, uses, last_used) VALUES (?, 1, ?) "
                    "ON CONFLICT (email) DO UPDATE SET uses = uses + 1, last_used = excluded.last_used", rows)


def remember(addresses) -> None:
    with db.connect() as con, con:
        remember_in(con, addresses)


def all_addresses(limit: int | None = MAX_SHOWN) -> list[str]:
    """Adresses du carnet, les plus utilisées puis les plus récentes d'abord (`limit` None : toutes)."""
    with db.connect() as con:
        return [r[0] for r in con.execute(
            "SELECT email FROM contacts ORDER BY uses DESC, last_used DESC, email LIMIT ?",
            (-1 if limit is None else limit,))]


def forget(address: str) -> bool:
    """Retire une adresse du carnet ; True si elle y était. Effacée aussi des pages libres du fichier."""
    with db.connect() as con, con:
        con.execute("PRAGMA secure_delete = ON")
        return con.execute("DELETE FROM contacts WHERE email = ?", ((address or "").strip(),)).rowcount > 0
