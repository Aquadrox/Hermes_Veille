"""Import d'une nomenclature (CSV ou Excel) : lecture bornée, reconnaissance des colonnes par
règles, proposition modifiable, puis liste de composants validée.

Aucun LLM : la reconnaissance repose sur un dictionnaire de libellés (FR / EN) et sur la forme
des valeurs ; rien ne sort du serveur. La proposition est toujours montrée à l'utilisateur,
qui la corrige au besoin (interface, Telegram, CLI : même moteur).

Fichiers non fiables : taille, lignes, colonnes et cellules bornées ; archive xlsx contrôlée
avant ouverture (bombe zip) ; XML lu via defusedxml (openpyxl l'utilise s'il est installé) ;
formules jamais évaluées (valeurs en cache) ; macros jamais exécutées.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import secrets
import time
import unicodedata
import zipfile
from pathlib import Path

from . import components as comp
from . import paths, store

MAX_BYTES = 5_000_000
MAX_ROWS = 10_000
MAX_COLS = 100
MAX_CELL = 200
MAX_UNZIPPED = 100_000_000
MAX_ZIP_ENTRIES = 2_000
HEADER_SCAN = 20
MAX_EMPTY_RUN = 1_000  # lignes vides consécutives : fin des données (feuilles « étirées »)
PREVIEW_ROWS = 10
FIELDS = ("mpn", "manufacturer", "ref")
LABELS = {"mpn": "MPN (référence fabricant)", "manufacturer": "Fabricant", "ref": "Réf. interne"}

# Libellés normalisés (minuscules, sans accents ni ponctuation) -> champ.
SYNONYMS = {
    "mpn": {"mpn", "manufacturerpartnumber", "manufacturerpn", "manufacturerpartno", "mfrpartnumber", "mfrpn",
            "mfrpartno", "mfrno", "mfgpartnumber", "mfgpn", "mpnfabricant", "referencefabricant", "reffabricant",
            "referenceconstructeur", "refconstructeur", "codefabricant", "numerofabricant", "partnumber",
            "partno", "pn", "ordercode", "referencecomposant", "refcomposant", "manufacturerreference"},
    "manufacturer": {"manufacturer", "manufacturername", "fabricant", "nomfabricant", "mfr", "mfg", "mfrname",
                     "marque", "brand", "constructeur", "make", "fondeur"},
    "ref": {"ref", "refinterne", "referenceinterne", "codeinterne", "codearticle", "article", "articleinterne",
            "internalpartnumber", "internalpn", "internalref", "internalreference", "item", "itemnumber",
            "itemcode", "partcode", "refarticle", "numeroarticle", "codeerp", "referenceerp"},
}
# Mots qui, dans un libellé, signalent un autre sens (jamais attribué au champ).
# Mots de 2 lettres : seulement en début ou en fin de libellé (« Mfr PN »).
EXCLUDE = {
    "mpn": ("digikey", "mouser", "farnell", "element14", "arrow", "rscomp", "rsstock", "rspro", "tme",
            "distrib", "supplier", "fournisseur", "vendor", "interne", "internal", "client", "customer"),
    "manufacturer": ("partnumber", "pn", "reference", "ref", "code"),
    "ref": ("fabricant", "manufacturer", "mfr", "mfg", "constructeur", "digikey", "mouser"),
}
# Fichier déposé dans le dossier d'import (Telegram / CLI) : un nom simple, jamais un chemin.
IMPORT_NAME_RE = re.compile(r"^[\w(][\w .()+,-]{0,150}\.(?:csv|txt|tsv|xlsx|xlsm)$", re.IGNORECASE)
STASH_RE = re.compile(r"^[0-9a-f]{32}$")
STASH_TTL = 3600
_MPN_VALUE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9][A-Za-z0-9._/+#()-]{2,63}$")


class BomImportError(comp.ComponentsError):
    pass


def norm(label: str) -> str:
    s = unicodedata.normalize("NFKD", str(label or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def column_name(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA (comme un tableur)."""
    name = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        name = chr(65 + r) + name
    return name


# --------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------
def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).split())[:MAX_CELL]


def _bounded(rows) -> list[list[str]]:
    out, filled, empty_run = [], 0, 0
    for row in rows:
        cells = [_cell(v) for v in list(row)[:MAX_COLS]]
        while cells and not cells[-1]:
            cells.pop()
        if cells:
            filled, empty_run = filled + 1, 0
            if filled > MAX_ROWS:
                raise BomImportError(f"fichier trop long : {MAX_ROWS} lignes au plus")
        else:
            empty_run += 1
            if empty_run > MAX_EMPTY_RUN:
                break
        out.append(cells)
    while out and not out[-1]:
        out.pop()
    return out


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise BomImportError("encodage du fichier non reconnu (UTF-8 ou Windows-1252 attendu)")


def _read_csv(data: bytes) -> list[list[str]]:
    text = _decode(data)
    sample = "\n".join(text.splitlines()[:50])
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:  # une seule colonne, ou forme ambiguë
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    return _bounded(csv.reader(io.StringIO(text), delimiter=delimiter))


def _check_zip(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
    except zipfile.BadZipFile:
        raise BomImportError("fichier Excel illisible (archive .xlsx invalide)") from None
    if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_UNZIPPED:
        raise BomImportError("fichier Excel refusé : contenu décompressé trop volumineux")


def _read_xlsx(data: bytes, sheet: str | None) -> tuple[list[str], str, list[list[str]]]:
    _check_zip(data)
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - tout fichier invalide donne le même message
        raise BomImportError(f"fichier Excel illisible ({type(exc).__name__})") from None
    try:
        names = list(wb.sheetnames)
        if sheet is not None and sheet not in names:
            raise BomImportError(f"feuille inconnue : {sheet} ({', '.join(names)})")
        for name in [sheet] if sheet else names:
            rows = _bounded(wb[name].iter_rows(values_only=True))
            if rows or sheet:
                return names, name, rows
        return names, names[0] if names else "", []
    except BomImportError:
        raise
    except Exception as exc:  # noqa: BLE001 - XML malveillant (defusedxml) ou fichier corrompu
        raise BomImportError(f"fichier Excel refusé ({type(exc).__name__})") from None
    finally:
        wb.close()


def read_table(data: bytes, filename: str, sheet: str | None = None) -> dict:
    """Contenu brut d'un fichier : {"sheets", "sheet", "rows"} (cellules en texte)."""
    if len(data) > MAX_BYTES:
        raise BomImportError(f"fichier trop volumineux : {MAX_BYTES // 1_000_000} Mo au plus")
    ext = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    if ext in ("xlsx", "xlsm"):
        sheets, name, rows = _read_xlsx(data, sheet or None)
    elif ext in ("csv", "txt", "tsv", ""):
        sheets, name, rows = [], "", _read_csv(data)
    elif ext in ("xls", "ods", "numbers"):
        raise BomImportError(f"format .{ext} non pris en charge : enregistrer le fichier en .xlsx ou en .csv")
    else:
        raise BomImportError("format attendu : .csv, .txt, .xlsx ou .xlsm")
    if not any(rows):
        raise BomImportError("aucune donnée trouvée dans le fichier" + (f" (feuille « {name} »)" if name else ""))
    return {"sheets": sheets, "sheet": name, "rows": rows}


def read_path(path: Path, sheet: str | None = None) -> dict:
    """Fichier local (CLI : chemin choisi par l'administrateur dans le conteneur)."""
    try:
        if path.stat().st_size > MAX_BYTES:
            raise BomImportError(f"fichier trop volumineux : {MAX_BYTES // 1_000_000} Mo au plus")
        return read_table(path.read_bytes(), path.name, sheet)
    except OSError as exc:
        raise BomImportError(f"fichier illisible : {path.name} ({exc.strerror or type(exc).__name__})") from None


def import_path(name: str) -> Path:
    """Fichier du dossier d'import désigné par son NOM (MCP) : aucun autre chemin n'est accessible."""
    root = paths.imports_dir()
    if not isinstance(name, str) or not IMPORT_NAME_RE.match(name):
        raise BomImportError("nom de fichier invalide : un nom simple en .csv, .txt, .xlsx ou .xlsm, "
                             f"déposé dans {root}")
    path = (root / name).resolve()
    if path.parent != root.resolve() or not path.is_file():
        raise BomImportError(f"fichier introuvable dans le dossier d'import : {name}")
    return path


# --------------------------------------------------------------------------
# Analyses en cours de l'interface (le navigateur ne renvoie que la correspondance)
# --------------------------------------------------------------------------
def _owner(session_secret: str) -> str:
    return hashlib.sha256(f"veille-import:{session_secret}".encode()).hexdigest()


def _purge(root: Path) -> None:
    limit = time.time() - STASH_TTL
    for p in root.glob(".ui-*.json"):
        try:
            if p.stat().st_mtime < limit:
                p.unlink()
        except OSError:
            continue


def stash(data: bytes, filename: str, session_secret: str) -> str:
    """Conserve le fichier envoyé 1 h, rattaché à la session ; renvoie son jeton. Le fichier (et non
    une table) est gardé : changer de feuille ou de ligne d'en-tête le réanalyse."""
    root = paths.imports_dir()
    root.mkdir(parents=True, exist_ok=True)
    _purge(root)
    token = secrets.token_hex(16)
    store.atomic_write(root / f".ui-{token}.json", json.dumps({
        "owner": _owner(session_secret), "filename": str(filename)[:200],
        "data": base64.b64encode(data).decode()}), mode=0o600)
    return token


def unstash(token: str, session_secret: str) -> tuple[str, bytes]:
    """(nom du fichier, contenu) d'une analyse de cette session."""
    expired = BomImportError("analyse expirée ou introuvable : renvoyer le fichier")
    if not isinstance(token, str) or not STASH_RE.match(token):
        raise expired
    path = paths.imports_dir() / f".ui-{token}.json"
    try:
        if path.stat().st_mtime < time.time() - STASH_TTL:
            raise expired
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise expired from None
    if not secrets.compare_digest(str(data.get("owner", "")), _owner(session_secret)):
        raise expired  # autre session : même message, rien n'est révélé
    return data["filename"], base64.b64decode(data["data"])


# --------------------------------------------------------------------------
# Reconnaissance
# --------------------------------------------------------------------------
def _match(label: str, field: str) -> str | None:
    """'exacte', 'probable' (libellé contenant un synonyme) ou None."""
    n = norm(label)
    if not n or any((x in n) if len(x) > 2 else (n.startswith(x) or n.endswith(x)) for x in EXCLUDE[field]):
        return None
    if n in SYNONYMS[field]:
        return "exacte"
    if any(len(s) >= 4 and s in n for s in SYNONYMS[field]):
        return "probable"
    return None


def detect_header(rows: list[list[str]]) -> int | None:
    """Index de la ligne d'en-tête (celle qui contient le plus de libellés reconnus), ou None."""
    best, best_score = None, 0
    for i, row in enumerate(rows[:HEADER_SCAN]):
        score = sum(1 for f in FIELDS if any(_match(c, f) for c in row))
        if score > best_score:
            best, best_score = i, score
    return best


def headers(rows: list[list[str]], header_row: int | None) -> list[str]:
    width = max((len(r) for r in rows), default=0)
    head = rows[header_row] if header_row is not None else []
    return [(head[i] if i < len(head) and head[i] else "") or f"Colonne {column_name(i)}" for i in range(width)]


def _looks_like_mpn(values: list[str]) -> bool:
    vals = [v for v in values if v][:50]
    return len(vals) >= 3 and sum(bool(_MPN_VALUE.match(v)) for v in vals) / len(vals) >= 0.8 \
        and len(set(vals)) >= 0.8 * len(vals)


def guess_mapping(rows: list[list[str]], header_row: int | None) -> dict:
    """{champ: {"col": index | None, "confidence": "exacte" | "probable" | None}}."""
    cols = headers(rows, header_row)
    data = rows[header_row + 1:] if header_row is not None else rows
    taken, out = set(), {}
    for field in FIELDS:  # exact d'abord, puis partiel ; une colonne pour un seul champ
        for level in ("exacte", "probable"):
            col = next((i for i, h in enumerate(cols) if i not in taken and header_row is not None
                        and _match(h, field) == level), None)
            if col is not None:
                taken.add(col)
                out[field] = {"col": col, "confidence": level}
                break
        else:
            out[field] = {"col": None, "confidence": None}
    if header_row is None:  # fichier sans en-tête : MPN ; Fabricant ; Réf (format historique)
        for i, field in enumerate(FIELDS):
            if i < len(cols):
                out[field] = {"col": i, "confidence": "probable"}
    elif out["mpn"]["col"] is None:  # repli sur la forme des valeurs
        for i in range(len(cols)):
            if i not in taken and _looks_like_mpn([r[i] if i < len(r) else "" for r in data]):
                out["mpn"] = {"col": i, "confidence": "probable"}
                break
    return out


# --------------------------------------------------------------------------
# Application et proposition
# --------------------------------------------------------------------------
def resolve_column(value, cols: list[str]) -> int | None:
    """Colonne désignée par son libellé, sa lettre (A, B…) ou son numéro (1, 2…) ; vide ou 0 = aucune."""
    if value is None or str(value).strip().lower() in ("", "-", "0", "aucune", "none"):
        return None
    if isinstance(value, int):
        idx = value
    else:
        v = str(value).strip()
        named = [i for i, h in enumerate(cols) if norm(h) == norm(v)]
        if named:
            return named[0]
        if v.isdigit():
            idx = int(v) - 1
        elif re.fullmatch(r"[A-Za-z]{1,2}", v):
            idx = sum((ord(c) - 64) * 26 ** p for p, c in enumerate(reversed(v.upper()))) - 1
        else:
            raise BomImportError(f"colonne inconnue : {v}")
    if not 0 <= idx < len(cols):
        raise BomImportError(f"colonne hors du tableau : {value}")
    return idx


def apply(rows: list[list[str]], header_row: int | None, mapping: dict[str, int | None],
          default_manufacturer: str = "") -> tuple[list[dict], list[dict]]:
    """Lignes -> (composants valides et dédoublonnés, rejets [{"line", "reason"}])."""
    if mapping.get("mpn") is None:
        raise BomImportError("choisir la colonne du MPN (référence fabricant)")
    if mapping.get("manufacturer") is None and not (default_manufacturer or "").strip():
        raise BomImportError("choisir la colonne du fabricant, ou indiquer un fabricant par défaut")
    start = header_row + 1 if header_row is not None else 0
    out, rejected = [], []

    def cell(row, field):
        i = mapping.get(field)
        return row[i] if i is not None and i < len(row) else ""

    for n, row in enumerate(rows[start:], start + 1):
        if not any(row):
            continue
        entry, reason = comp.validate_entry(cell(row, "mpn"),
                                            cell(row, "manufacturer") or default_manufacturer,
                                            cell(row, "ref"))
        if entry:
            out.append(entry)
        else:
            rejected.append({"line": n, "reason": reason})
    return comp.unique(out), rejected


def propose_options(table: dict, header=None, columns: dict | None = None, default_manufacturer: str = "") -> dict:
    """Proposition à partir d'options « utilisateur » (interface, CLI, MCP, même sens partout) :
    `header` vide = détection automatique, 0 = pas d'en-tête, n = ligne n (1 = première) ;
    `columns` = {champ: libellé, lettre ou numéro de colonne} pour corriger la proposition."""
    if header in (None, ""):
        header_row = -1
    else:
        try:
            n = int(header)
        except (TypeError, ValueError):
            raise BomImportError(f"ligne d'en-tête invalide : {header!r}") from None
        header_row = n - 1 if n > 0 else None
    return propose(table, header_row, columns or None, (default_manufacturer or "").strip())


def propose(table: dict, header_row: int | None = -1, mapping: dict | None = None,
            default_manufacturer: str = "") -> dict:
    """Proposition complète, modifiable : en-tête, correspondance, aperçu et décomptes.

    `header_row` : -1 = détection automatique, None = pas d'en-tête, sinon index (0 = ligne 1).
    `mapping` : {champ: colonne} imposé par l'utilisateur (libellé, lettre, numéro ou index).
    """
    rows = table["rows"]
    if header_row == -1:
        header_row = detect_header(rows)
    if header_row is not None and not 0 <= header_row < len(rows):
        raise BomImportError(f"ligne d'en-tête hors du tableau : {header_row + 1}")
    cols = headers(rows, header_row)
    guessed = guess_mapping(rows, header_row)
    chosen = {}
    for field in FIELDS:
        if mapping is not None and field in mapping:
            col = resolve_column(mapping[field], cols)
            guessed[field] = {"col": col, "confidence": "choisie" if col is not None else None}
        chosen[field] = guessed[field]["col"]
    try:
        items, rejected = apply(rows, header_row, chosen, default_manufacturer)
        error = None
    except comp.ComponentsError as exc:
        items, rejected, error = [], [], str(exc)
    return {"sheets": table.get("sheets", []), "sheet": table.get("sheet", ""),
            "header_row": header_row, "columns": cols, "mapping": guessed,
            "default_manufacturer": default_manufacturer, "components": items,
            "preview": items[:PREVIEW_ROWS], "rejected": rejected, "error": error,
            "rows": len(rows) - (header_row + 1 if header_row is not None else 0)}
