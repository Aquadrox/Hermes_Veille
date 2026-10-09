"""Fichiers d'une veille sur le volume.

    veilles/<slug>/
        veille.json      métadonnées (nom, sujet, fréquence, PDF, lien vers le job, type)
        config.md        brief de mission (éditable)
        recipients.txt   destinataires, un par ligne
        job-prompt.md    prompt RENDU depuis le gabarit (ne pas éditer à la main)
        rapports/        AAAA-MM-JJ-HHhMM-veille.{md,pdf}, -charts.json (avant la v0.3.2 : AAAA-MM-JJ-veille)
        runs/            journaux des lancements manuels
        components.json  veille composants : références suivies (voir components.py)
        notices.json     veille composants : avis PCN / EOL détectés

JSON plutôt que YAML : le chien de garde lit ces fichiers depuis l'hôte avec la
seule bibliothèque standard.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])$")
EMAIL_RE = re.compile(r"^[^@\s,;<>\"]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
# Horodatage AAAA-MM-JJ-HHhMM (sans « : », interdit sur Windows / FAT) ; l'ancien nom au jour reste lu.
REPORT_RE = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})(?:-([0-9]{2})h([0-9]{2}))?-veille\.(pdf|md)\Z")
RUN_LOG_RE = re.compile(r"^\d{8}-\d{6}\.log$")
KINDS = ("veille", "composants")


class StoreError(ValueError):
    pass


@dataclass
class PdfOptions:
    title: str = ""
    header: str = ""
    footer: str = "Document interne"
    audience: str = "direction & ingénierie"
    accent: str = "#0f766e"


@dataclass
class Veille:
    slug: str
    name: str
    subject: str = ""
    schedule: str = "0 8 * * 1"
    job_id: str | None = None
    max_age_hours: float = 192
    period: str = "week"
    model: str | None = None
    provider: str | None = None
    pdf: PdfOptions = field(default_factory=PdfOptions)
    kind: str = "veille"              # « composants » : surveillance PCN / fins de vie
    components: str | None = None     # veille : slug de la veille composants incluse en chapitre
    batch_size: int = 40              # composants : recherches web par exécution (lot tournant)
    created_at: str = ""
    updated_at: str = ""

    @property
    def dir(self) -> Path:
        return veille_dir(self.slug)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Veille:
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["pdf"] = PdfOptions(**{
            k: v for k, v in (data.get("pdf") or {}).items()
            if k in PdfOptions.__dataclass_fields__
        })
        return cls(**kwargs)


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def check_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise StoreError(
            "identifiant invalide : 2 à 40 caractères, minuscules, chiffres et tirets "
            "(ni au début ni à la fin)"
        )
    return slug


def slugify(text: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:40].strip("-")


def veille_dir(slug: str) -> Path:
    return paths.veilles_dir() / check_slug(slug)


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)  # mkstemp crée en 0600
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Veilles
# --------------------------------------------------------------------------
def exists(slug: str) -> bool:
    return (veille_dir(slug) / "veille.json").is_file()


def load(slug: str) -> Veille:
    path = veille_dir(slug) / "veille.json"
    try:
        with open(path, encoding="utf-8") as fh:
            return Veille.from_dict(json.load(fh))
    except FileNotFoundError:
        raise StoreError(f"veille inconnue : {slug}") from None


def save(v: Veille) -> None:
    check_slug(v.slug)
    v.updated_at = now_iso()
    if not v.created_at:
        v.created_at = v.updated_at
    d = v.dir
    (d / "rapports").mkdir(parents=True, exist_ok=True)
    atomic_write(d / "veille.json", json.dumps(v.to_dict(), ensure_ascii=False, indent=2) + "\n")


def list_all() -> list[Veille]:
    root = paths.veilles_dir()
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if child.name.startswith(".") or not (child / "veille.json").is_file():
            continue
        try:
            out.append(load(child.name))
        except (StoreError, ValueError, TypeError):
            continue
    return out


# --------------------------------------------------------------------------
# Destinataires
# --------------------------------------------------------------------------
def parse_recipients(text: str) -> list[str]:
    """Texte libre (lignes, virgules, points-virgules) -> adresses validées et dédoublonnées."""
    raw = [a.strip() for a in re.split(r"[\n,;]+", text or "") if a.strip()]
    raw = [a for a in raw if not a.startswith("#")]
    bad = [a for a in raw if not EMAIL_RE.match(a)]
    if bad:
        raise StoreError("adresse(s) invalide(s) : " + ", ".join(bad))
    seen, out = set(), []
    for a in raw:
        if a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


def read_recipients(slug: str) -> list[str]:
    try:
        text = (veille_dir(slug) / "recipients.txt").read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def write_recipients(slug: str, recipients: list[str]) -> None:
    if not recipients:
        raise StoreError("au moins un destinataire est requis")
    atomic_write(veille_dir(slug) / "recipients.txt", "\n".join(recipients) + "\n")


# --------------------------------------------------------------------------
# Brief
# --------------------------------------------------------------------------
def read_brief(slug: str) -> str:
    try:
        return (veille_dir(slug) / "config.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_brief(slug: str, text: str) -> None:
    if not (text or "").strip():
        raise StoreError("le brief de mission est vide")
    atomic_write(veille_dir(slug) / "config.md", text.replace("\r\n", "\n"))


def brief_templates() -> dict[str, str]:
    """Gabarits de brief proposés à la création : {nom: contenu}."""
    tdir = paths.defaults_dir() / "templates"
    if not tdir.is_dir():
        return {}
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(tdir.glob("*.md"))}


# --------------------------------------------------------------------------
# Rapports et journaux (noms en liste blanche : aucune traversée de chemin)
# --------------------------------------------------------------------------
def report_stamp(now: dt.datetime | None = None) -> str:
    """Préfixe horodaté d'un nouveau rapport (heure de Paris) : plusieurs exécutions le même jour
    ne s'écrasent plus."""
    from .schedule import ZONE

    return (now or dt.datetime.now(ZONE)).strftime("%Y-%m-%d-%Hh%M")


def list_reports(slug: str) -> list[dict]:
    """Rapports du plus récent au plus ancien : {"stamp", "date" (jour), "time" ("HH:MM" ou ""), "pdf", "md", "size"}."""
    rdir = veille_dir(slug) / "rapports"
    if not rdir.is_dir():
        return []
    by_stamp: dict[str, dict] = {}
    for p in rdir.iterdir():
        m = REPORT_RE.match(p.name)
        if m:
            day, hh, mm, ext = m.groups()
            stamp = p.name[: -len("-veille.") - len(ext)]
            entry = by_stamp.setdefault(stamp, {"stamp": stamp, "date": day, "time": f"{hh}:{mm}" if hh else ""})
            entry[ext] = p.name
            if ext == "pdf":
                entry["size"] = p.stat().st_size
    return sorted(by_stamp.values(), key=lambda e: e["stamp"], reverse=True)


def report_path(slug: str, name: str) -> Path:
    if not REPORT_RE.match(name or ""):
        raise StoreError("nom de rapport invalide")
    path = veille_dir(slug) / "rapports" / name
    if not path.is_file():
        raise StoreError("rapport introuvable")
    return path


def runs_dir(slug: str) -> Path:
    return veille_dir(slug) / "runs"


def list_run_logs(slug: str, limit: int = 10) -> list[str]:
    rdir = runs_dir(slug)
    if not rdir.is_dir():
        return []
    names = sorted((p.name for p in rdir.iterdir() if RUN_LOG_RE.match(p.name)), reverse=True)
    return names[:limit]


def run_log_path(slug: str, name: str) -> Path:
    if not RUN_LOG_RE.match(name or ""):
        raise StoreError("nom de journal invalide")
    path = runs_dir(slug) / name
    if not path.is_file():
        raise StoreError("journal introuvable")
    return path
