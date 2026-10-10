"""Base SQLite de l'application (`veilles/.veille.db`) : connaissance partagée des composants et carnet
d'adresses.

Les fichiers de chaque veille (`components.json`, `notices.json`, `recipients.txt`) restent la source de
vérité : le chien de garde et les sauvegardes les lisent tels quels. Les tables `parts` et `notices` en
sont un INDEX, reconstructible à tout moment (`catalog.rebuild`, à chaque démarrage). Seul le carnet
d'adresses (`contacts`) n'existe qu'ici ; il est amorcé avec les destinataires des veilles à sa création.

Choix de robustesse :
- SQLite de la bibliothèque standard, fichier dans le volume : ni service ni dépendance de plus, et la
  base est sauvegardée avec le reste ;
- journal DELETE et non WAL : aucun fichier annexe permanent (`-wal`, `-shm`) qu'un `docker exec` en root
  créerait et rendrait inaccessible à hermes. Une base créée par root est rendue au propriétaire du
  dossier des veilles ;
- `TIMEOUT` : les écrivains (interface, CLI du job, MCP) s'attendent au lieu d'échouer ;
- schéma versionné (`PRAGMA user_version`), chaque migration dans UNE transaction ;
- base corrompue, ou d'un schéma plus récent que l'application : mise de côté par `ensure` (au démarrage)
  et recréée. La connaissance des composants se reconstruit ensuite depuis les fichiers.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
from collections.abc import Iterator

from . import paths, store

TIMEOUT = 10  # secondes d'attente d'un verrou tenu par un autre processus
# Échecs de la base (dont un JSON stocké illisible) : jamais bloquants pour les fichiers (voir service).
ERRORS = (sqlite3.Error, OSError, json.JSONDecodeError)
# Seules raisons de mettre la base de côté : un verrou tenu ou une E/S passagère ne le sont pas (le carnet
# d'adresses, qui n'existe qu'ici, serait perdu pour rien).
CORRUPT = frozenset({"SQLITE_CORRUPT", "SQLITE_NOTADB"})


class SchemaError(sqlite3.DatabaseError):
    """Base d'un schéma plus récent que l'application (retour à une image plus ancienne)."""


def _v1(con: sqlite3.Connection) -> None:
    """Schéma initial ; carnet d'adresses amorcé avec les destinataires des veilles existantes."""
    from . import contacts  # import local : contacts s'appuie sur ce module

    con.execute("""CREATE TABLE parts (
        source TEXT NOT NULL,              -- slug de la veille, ou dossier d'archive d'une veille archivée
        key TEXT NOT NULL,                 -- components.key_of
        mpn TEXT NOT NULL,
        manufacturer TEXT NOT NULL,
        dk TEXT,                           -- statut Digi-Key (JSON, comme dans components.json)
        web_status TEXT,                   -- statut établi par la recherche web (JSON)
        web_checked_at TEXT,
        PRIMARY KEY (source, key))""")
    con.execute("CREATE INDEX parts_key ON parts (key)")
    con.execute("""CREATE TABLE notices (
        source TEXT NOT NULL,
        id TEXT NOT NULL,                  -- components.notice_id
        key TEXT NOT NULL,
        data TEXT NOT NULL,                -- avis (JSON), sans son état de livraison propre à la veille
        PRIMARY KEY (source, id))""")
    con.execute("CREATE INDEX notices_key ON notices (key)")
    con.execute("""CREATE TABLE contacts (
        email TEXT PRIMARY KEY COLLATE NOCASE,
        uses INTEGER NOT NULL,
        last_used TEXT NOT NULL)""")
    contacts.remember_in(con, [a for v in store.list_all() for a in store.read_recipients(v.slug)])


# Une fonction par version de schéma, dans l'ordre. Ne jamais modifier une migration publiée : en ajouter une.
MIGRATIONS = (_v1,)


def _migrate(con: sqlite3.Connection) -> None:
    if con.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS):
        return
    con.execute("BEGIN IMMEDIATE")  # un seul processus migre ; les autres attendent, puis relisent la version
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise SchemaError(f"schéma {version} plus récent que l'application ({len(MIGRATIONS)})")
        for n in range(version, len(MIGRATIONS)):
            MIGRATIONS[n](con)
            con.execute(f"PRAGMA user_version = {n + 1}")
    except BaseException:
        con.rollback()
        raise
    con.commit()


def _own(path) -> None:
    """Base neuve : adresses e-mail (données personnelles) en 0600 ; créée par root (`docker exec`), rendue
    au propriétaire du dossier des veilles (hermes), qui sinon ne pourrait plus y écrire."""
    os.chmod(path, 0o600)
    if os.geteuid() == 0:
        st = path.parent.stat()
        os.chown(path, st.st_uid, st.st_gid)


@contextlib.contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Connexion, schéma à jour. Pour écrire : `with connect() as con, con:` (transaction validée en sortie,
    annulée sur exception)."""
    path = paths.database()
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    con = sqlite3.connect(path, timeout=TIMEOUT)
    try:
        if fresh:
            _own(path)
        _migrate(con)
        yield con
    finally:
        con.close()


def ensure() -> str:
    """Au démarrage : base lisible, intègre et d'un schéma connu ; sinon mise de côté (jamais effacée) et
    recréée. Toute autre erreur (verrou, E/S) est levée telle quelle. Renvoie le constat, pour le journal."""
    path = paths.database()
    problem = None
    if path.exists():
        try:
            with connect() as con:
                result = con.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                problem = f"contrôle d'intégrité : {result[:200]}"
        except sqlite3.DatabaseError as exc:
            if not isinstance(exc, SchemaError) and getattr(exc, "sqlite_errorname", None) not in CORRUPT:
                raise
            problem = f"{type(exc).__name__} : {exc}"
    if problem is None:
        with connect():  # création ou migration
            return "base ok"
    aside = path.with_name(f"{path.name}.corrompue-{dt.datetime.now():%Y%m%d-%H%M%S}")
    os.replace(path, aside)
    with contextlib.suppress(FileNotFoundError):  # journal d'une transaction interrompue : avec sa base
        os.replace(path.with_name(path.name + "-journal"), aside.with_name(aside.name + "-journal"))
    with connect():
        return f"base mise de côté ({problem}) dans {aside.name}, puis recréée"
